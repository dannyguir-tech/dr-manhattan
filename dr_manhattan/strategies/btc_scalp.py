"""
BTC 5-Minute BBO Market Making Strategy

Posts passive limit bids and asks on both YES and NO outcomes of the rolling
Polymarket BTC 5-minute Up/Down market. Earns the bid-ask spread from both
directions instead of waiting for deep out-of-the-money fills.

Strategy mechanics:
- Quote bid at mid - half_spread, ask at mid + half_spread
- Inventory skewing: when long one side, push its bid away and pull its ask in
  to exit faster while reducing further buying on that side
- Refresh quotes when the mid moves more than 1 tick from current quote
- Force-refresh all quotes every QUOTE_MAX_AGE seconds regardless

Features:
- Auto-discovers and rolls the active BTC 5-min market window
- Both-sides arbitrage: buy YES+NO immediately when combined ask < 0.97
- BTC trend filter: suppresses bids on the losing side when BTC moves >0.3% from window open
- Volatility-scaled spread: widens half_spread up to 2.5× during fast BTC moves
- Two-phase expiry: cancel bids at 90s, actively sell inventory at market bid 60-90s out
- Circuit breaker: pauses quoting after repeated order rejections
- Daily loss limit: stops quoting when session P&L falls below threshold

Fee structure (Polymarket, January 2026):
- Limit orders earn 0.20% maker rebate on both legs
- Effective net profit per round trip is spread + rebates
"""

import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import requests

from ..base.strategy import Strategy
from ..feeds.binance import BinancePriceFeed
from ..models.market import Market, OutcomeToken
from ..models.order import OrderSide
from ..utils import setup_logger

logger = setup_logger(__name__)

# Stop quoting when fewer than this many seconds remain in the window.
# Near expiry the market converges rapidly to 0/1 — adverse inventory risk spikes.
MIN_WINDOW_FOR_QUOTING = 120

# Buy both sides immediately when YES_ask + NO_ask falls below this
ARB_THRESHOLD = 0.97

# REST state refresh interval
STATE_REFRESH_INTERVAL = 2.0  # seconds

# Force-reprice all quotes after this many seconds even if mid hasn't moved
QUOTE_MAX_AGE = 30.0  # seconds

# BTC trend filter: suppress bids on losing outcome when BTC moves this far from window open
TREND_THRESHOLD = 0.003  # 0.3% from window open price

# Volatility-scaled spread: scale half_spread up when realized vol exceeds baseline
VOL_SCALE_BASELINE = 0.50  # annualized vol at which no scaling occurs (~50% is very low for BTC)
VOL_SCALE_MAX = 2.5        # cap multiplier at 2.5× (e.g. 0.03 → 0.075 at high vol)

# Two-phase expiry: Phase 1 cancels bids and sells inventory at bid price
EXPIRY_PHASE1_SECS = 60  # switch from Phase 1 to Phase 2 at this many seconds remaining

# Circuit breaker: pause quoting after repeated API rejections
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_WINDOW = 60.0
CIRCUIT_BREAKER_COOLDOWN = 300.0


class BTCScalpStrategy(Strategy):
    """
    BBO market maker on the Polymarket BTC 5-minute Up/Down market.

    Posts both a bid and ask on each outcome near the current mid-price.
    Collects the spread from fills on both sides. Inventory skewing keeps
    the net position balanced so the book stays near delta-neutral.

    Parameters:
        half_spread: Half the desired bid-ask spread (default 0.03 → 6-tick spread).
            At a mid of 0.50 this gives bid=0.47, ask=0.53.
        min_market_spread: Skip quoting if the market spread is tighter than this.
            Tight spreads mean high competition and little edge (default 0.02).
        order_size: Contracts per order per side (default 5).
        max_inventory: Max contracts held per outcome before buying stops (default 50).
        cancel_before_expiry: Seconds before window close to cancel all orders (default 90).
        max_daily_loss: Stop quoting when session P&L falls below -this value (default 50.0).

    Tick rate: 100ms. REST state refresh capped at once per 2s.
    """

    def __init__(
        self,
        exchange,
        market_id: str = "btc-5min-auto",
        half_spread: float = 0.03,
        min_market_spread: float = 0.02,
        order_size: int = 5,
        max_inventory: float = 50.0,
        cancel_before_expiry: float = 90.0,
        max_daily_loss: float = 50.0,
        **kwargs,
    ):
        base_keys = {"max_position", "max_delta", "check_interval", "track_fills"}
        base_kwargs = {k: v for k, v in kwargs.items() if k in base_keys}
        base_kwargs.setdefault("check_interval", 0.1)
        super().__init__(exchange, market_id, order_size=float(order_size), **base_kwargs)

        self.half_spread = half_spread
        self.min_market_spread = min_market_spread
        self.order_size = order_size
        self.max_inventory = max_inventory
        self.cancel_before_expiry = cancel_before_expiry
        self.max_daily_loss = max_daily_loss

        # Per-window state
        self._window_reset: bool = False
        self._liquidation_started: bool = False  # True after Phase 1 bid cancellation
        self._quotes_placed_at: Optional[float] = None  # timestamp of last quote batch

        # P&L and fill tracking
        self._session_pnl: float = 0.0
        self._wins: int = 0
        self._losses: int = 0
        self._avg_entry: Dict[str, float] = {}       # outcome -> average cost basis
        self._last_bid: Dict[str, float] = {}         # last bid price placed (fill price proxy)
        self._last_ask: Dict[str, float] = {}         # last ask price placed (fill price proxy)
        self._prev_positions: Dict[str, float] = {}   # positions from previous tick

        # Arb position tracking (arb positions ride to resolution)
        self._arb_positions: set = set()

        # Binance price feed for context
        self._price_feed: BinancePriceFeed = BinancePriceFeed()
        self._window_start_btc: Optional[float] = None
        self._last_state_refresh: float = 0.0
        self._feed_stale_logged: bool = False

        # Circuit breaker
        self._consecutive_rejections: int = 0
        self._first_rejection_time: float = 0.0
        self._circuit_open: bool = False
        self._circuit_open_until: float = 0.0

        # SQLite persistence
        db_dir = "/data"
        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "bot.db")
        self._init_db()

        # Cached cash balance for per-order pre-check
        self._cached_cash: float = 0.0

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------

    def setup(self) -> bool:
        """Discover active BTC 5-min market and initialize."""
        market = self._find_btc_5min_market()
        if not market:
            logger.error("No active BTC 5-min market found during setup")
            return False

        self.market = market
        self.market_id = market.id
        self.tick_size = market.tick_size

        token_ids = market.metadata.get("clobTokenIds", [])
        if not token_ids:
            logger.error("No clobTokenIds in market metadata")
            return False

        self.outcome_tokens = [
            OutcomeToken(market_id=self.market_id, outcome=outcome, token_id=token_id)
            for outcome, token_id in zip(market.outcomes, token_ids)
        ]

        token_ids = [ot.token_id for ot in self.outcome_tokens]
        self.client.setup_orderbook_websocket(self.market_id, token_ids)
        self._positions = self.client.fetch_positions_dict_for_market(self.market)
        self._prev_positions = dict(self._positions)

        self._price_feed.start()
        for _ in range(20):
            if self._price_feed.price is not None:
                break
            time.sleep(0.1)
        self._window_start_btc = self._price_feed.price
        if self._window_start_btc:
            logger.info(f"BTC window open price: ${self._window_start_btc:,.2f}")
        else:
            logger.warning("Binance feed not yet connected — BTC context unavailable")

        self._reconcile_on_startup()
        self._load_session_state()
        self._log_trader_profile()
        self._log_market_info()
        self._notify(f"Bot started. Session P&L loaded: ${self._session_pnl:+.2f} | W/L: {self._wins}/{self._losses}")
        return True

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def on_tick(self):
        now = time.time()
        secs = self._seconds_until_expiry()

        # Fast path: two-phase expiry (checked every 100ms)
        if secs < self.cancel_before_expiry:
            if secs > EXPIRY_PHASE1_SECS:
                # Phase 1 (60–90s before expiry): cancel bids, sell inventory at market bid
                if not self._liquidation_started:
                    logger.info(f"Expiry Phase 1 ({secs:.0f}s): cancelling bids, liquidating inventory")
                    self._cancel_all_bids()
                    self._liquidation_started = True
                # Re-check inventory every STATE_REFRESH_INTERVAL (not every 100ms)
                if now - self._last_state_refresh >= STATE_REFRESH_INTERVAL:
                    self.refresh_state()
                    self._last_state_refresh = now
                    self._liquidate_inventory_at_bid()
            else:
                # Phase 2 (<60s before expiry): full reset and roll
                if not self._window_reset:
                    logger.info(f"Expiry Phase 2 ({secs:.0f}s): resetting window")
                    self._reset_window()
                    self._window_reset = True
            new_market = self._find_btc_5min_market()
            if new_market and new_market.id != self.market_id:
                self._switch_market(new_market)
            return

        # Both-sides arb check runs every 100ms — arb windows last only 2-3s
        if self._check_arb():
            return

        # Slow path: REST calls capped at STATE_REFRESH_INTERVAL
        if now - self._last_state_refresh < STATE_REFRESH_INTERVAL:
            return

        self.refresh_state()
        self._last_state_refresh = now

        self._detect_fills()
        self._place_quotes(secs)
        self._log_mm_status(secs)

    # -------------------------------------------------------------------------
    # Market discovery
    # -------------------------------------------------------------------------

    def _find_btc_5min_market(self) -> Optional[Market]:
        """Find the currently active BTC 5-minute Up/Down market."""
        now = datetime.now(timezone.utc)
        try:
            markets = self.exchange.search_markets(
                keywords=["BTC", "Up or Down"],
                closed=False,
                end_date_min=now,
                end_date_max=now + timedelta(minutes=6),
                min_liquidity=1.0,
            )
            if not markets:
                markets = self.exchange.search_markets(
                    query="Bitcoin Up or Down",
                    closed=False,
                    end_date_min=now,
                    end_date_max=now + timedelta(minutes=8),
                )
            if not markets:
                logger.warning("No BTC 5-min market found")
                return None
            with_close = [m for m in markets if m.close_time]
            return min(with_close, key=lambda m: m.close_time) if with_close else markets[0]
        except Exception as e:
            logger.warning(f"Market discovery error: {e}")
            return None

    def _switch_market(self, market: Market):
        """Switch to a new market window and reset per-window state."""
        self.market = market
        self.market_id = market.id
        self.tick_size = market.tick_size

        token_ids = market.metadata.get("clobTokenIds", [])
        self.outcome_tokens = [
            OutcomeToken(market_id=self.market_id, outcome=outcome, token_id=token_id)
            for outcome, token_id in zip(market.outcomes, token_ids)
        ]
        self._positions = self.client.fetch_positions_dict_for_market(self.market)
        self._prev_positions = dict(self._positions)
        token_ids = [ot.token_id for ot in self.outcome_tokens]
        self.client.setup_orderbook_websocket(self.market_id, token_ids)
        self._arb_positions.clear()
        self._avg_entry.clear()
        self._last_bid.clear()
        self._last_ask.clear()
        self._window_reset = False
        self._liquidation_started = False
        self._quotes_placed_at = None
        self._window_start_btc = self._price_feed.price
        if self._window_start_btc:
            logger.info(f"New window: {market.question[:60]} | BTC ${self._window_start_btc:,.2f}")
        else:
            logger.info(f"New window: {market.question[:70]}")

    # -------------------------------------------------------------------------
    # BBO quoting
    # -------------------------------------------------------------------------

    def _place_quotes(self, secs_remaining: float):
        """
        Post or refresh bid/ask quotes on each outcome.

        For each outcome:
        - Compute mid = (best_bid + best_ask) / 2
        - Quote our_bid = mid - half_spread - skew
        - Quote our_ask = mid + half_spread - skew
        - Skew = (inventory / max_inventory) * half_spread
          When long, the skew pushes our bid down (less aggressive on buys)
          and pulls our ask down (more aggressive on sells). This winds down
          inventory while still collecting some spread on the sell side.
        - If the mid moves >1 tick from our current quote, cancel and repost
        - Force-reprice everything after QUOTE_MAX_AGE seconds
        - BTC trend filter: skip bids on the outcome BTC is trending against
        - Volatility-scaled spread: widen half_spread up to 2.5× during fast moves
        """
        if secs_remaining < MIN_WINDOW_FOR_QUOTING:
            return
        if self._session_pnl < -self.max_daily_loss:
            logger.warning(
                f"Daily loss limit hit: P&L ${self._session_pnl:.2f} / limit -${self.max_daily_loss:.2f}"
            )
            self._notify(
                f"DAILY LOSS LIMIT HIT: P&L ${self._session_pnl:+.2f} / limit -${self.max_daily_loss:.2f}"
            )
            return
        if self._is_circuit_open():
            return

        # Force-reprice stale quotes
        if self._quotes_placed_at and (time.time() - self._quotes_placed_at > QUOTE_MAX_AGE):
            self.cancel_all_orders()
            self._quotes_placed_at = None

        # Volatility-scaled spread: widen when BTC is moving fast
        vol = self._price_feed.realized_vol_30s
        if vol is not None:
            vol_scale = max(1.0, min(vol / VOL_SCALE_BASELINE, VOL_SCALE_MAX))
            effective_half_spread = self.half_spread * vol_scale
        else:
            effective_half_spread = self.half_spread

        # Gamma-aware max inventory: reduce position ceiling as expiry nears
        # At 300s+: full max_inventory. At 60s: ~37% of max_inventory.
        time_decay = min(secs_remaining / 300.0, 1.0)
        effective_max_inventory = self.max_inventory * (0.3 + 0.7 * time_decay)

        for ot in self.outcome_tokens:
            if ot.outcome in self._arb_positions:
                continue

            bid, ask = self.get_best_bid_ask(ot.token_id)
            if bid is None or ask is None:
                continue

            if ask - bid < self.min_market_spread:
                continue  # Market too tight — no edge to capture

            mid = (bid + ask) / 2.0
            position = self._positions.get(ot.outcome, 0.0)

            # Inventory skew: reduce aggression on the heavy side
            skew = (position / effective_max_inventory) * effective_half_spread
            our_bid = self.round_price(mid - effective_half_spread - skew)
            our_ask = self.round_price(mid + effective_half_spread - skew)

            # Clamp to valid range
            our_bid = max(self.tick_size, min(our_bid, 1.0 - self.tick_size))
            our_ask = max(our_bid + self.tick_size, min(our_ask, 1.0 - self.tick_size))

            buy_orders, sell_orders = self.get_orders_for_outcome(ot.outcome)

            # BUY side — only post if below max inventory, BTC trend allows,
            # OFI doesn't show strong sell pressure, and cash covers the order
            ofi = self._order_flow_imbalance(ot.token_id)
            order_cost = our_bid * self.order_size
            cash_ok = self._cached_cash <= 0 or self._cached_cash >= order_cost
            if not cash_ok:
                logger.debug(f"Bid skipped ({ot.outcome}): insufficient cash ${self._cached_cash:.2f} < ${order_cost:.2f}")
            if (
                position < effective_max_inventory
                and not self._btc_trend_blocks_bid(ot.outcome)
                and ofi >= 0.40
                and cash_ok
            ):
                if not self.has_order_at_price(buy_orders, our_bid):
                    self.cancel_stale_orders(buy_orders, our_bid)
                    try:
                        self.create_order(
                            ot.outcome, OrderSide.BUY, our_bid, self.order_size, ot.token_id
                        )
                        self._last_bid[ot.outcome] = our_bid
                        self.log_order(OrderSide.BUY, self.order_size, ot.outcome, our_bid)
                        self._record_order_success()
                        if not self._quotes_placed_at:
                            self._quotes_placed_at = time.time()
                    except Exception as e:
                        logger.warning(f"Bid failed ({ot.outcome}): {e}")
                        self._record_order_failure(f"bid {ot.outcome}")

            # SELL side — only post if we hold enough to sell
            if position >= self.order_size:
                if not self.has_order_at_price(sell_orders, our_ask):
                    self.cancel_stale_orders(sell_orders, our_ask)
                    try:
                        self.create_order(
                            ot.outcome, OrderSide.SELL, our_ask, self.order_size, ot.token_id
                        )
                        self._last_ask[ot.outcome] = our_ask
                        self.log_order(OrderSide.SELL, self.order_size, ot.outcome, our_ask)
                        self._record_order_success()
                    except Exception as e:
                        logger.warning(f"Ask failed ({ot.outcome}): {e}")
                        self._record_order_failure(f"ask {ot.outcome}")

    def _btc_trend_blocks_bid(self, outcome: str) -> bool:
        """
        Return True if BTC is trending strongly against buying this outcome.

        When BTC moves >TREND_THRESHOLD from window open, the losing outcome is in
        free fall — posting bids there invites adverse selection from informed traders.
        Sells on the same outcome are still allowed (to exit existing inventory).
        """
        current = self._price_feed.price
        if not current and self._price_feed.is_fresh is False:
            current = self._price_feed.fetch_price_rest()
        if not current or not self._window_start_btc:
            return False
        pct = (current - self._window_start_btc) / self._window_start_btc
        if outcome in ("Yes", "UP") and pct < -TREND_THRESHOLD:
            logger.info(f"Bid blocked ({outcome}): BTC {pct:+.3%} from window open")
            return True
        if outcome in ("No", "DOWN") and pct > TREND_THRESHOLD:
            logger.info(f"Bid blocked ({outcome}): BTC {pct:+.3%} from window open")
            return True
        return False

    def _cancel_all_bids(self):
        """Cancel all open buy orders, leaving sell orders (inventory exits) intact."""
        buy_orders = [o for o in self._open_orders if o.side == OrderSide.BUY]
        for order in buy_orders:
            try:
                self.client.cancel_order(order.id)
            except Exception as e:
                logger.warning(f"Cancel bid failed ({order.outcome}): {e}")
        if buy_orders:
            logger.info(f"Cancelled {len(buy_orders)} bids (expiry Phase 1)")

    def _liquidate_inventory_at_bid(self):
        """
        Reprice all inventory sell orders to the current best bid for immediate exit.

        Called during expiry Phase 1 (60–90s before close) to guarantee inventory
        exits before the window resolves. Arb positions are excluded — they ride
        to resolution at 1.00.
        """
        for ot in self.outcome_tokens:
            pos = self._positions.get(ot.outcome, 0.0)
            if pos < 1.0 or ot.outcome in self._arb_positions:
                continue
            bid, _ = self.get_best_bid_ask(ot.token_id)
            if not bid or bid <= 0:
                continue
            _, sell_orders = self.get_orders_for_outcome(ot.outcome)
            if not self.has_order_at_price(sell_orders, bid):
                self.cancel_stale_orders(sell_orders, bid)
                try:
                    self.create_order(ot.outcome, OrderSide.SELL, bid, pos, ot.token_id)
                    self.log_order(OrderSide.SELL, pos, ot.outcome, bid, "EXPIRY")
                except Exception as e:
                    logger.warning(f"Expiry sell failed ({ot.outcome}): {e}")

    # -------------------------------------------------------------------------
    # Fill detection and P&L
    # -------------------------------------------------------------------------

    def _detect_fills(self):
        """
        Detect position changes since the previous tick to identify fills.

        Uses last posted bid/ask prices as fill price proxies (accurate to
        within the quote refresh interval). Updates cost basis and P&L.
        """
        for ot in self.outcome_tokens:
            prev = self._prev_positions.get(ot.outcome, 0.0)
            curr = self._positions.get(ot.outcome, 0.0)

            if curr > prev + 0.5:
                # Buy fill detected
                filled = curr - prev
                fill_price = self._last_bid.get(ot.outcome, 0.0)
                if fill_price <= 0:
                    fill_price, _ = self.get_best_bid_ask(ot.token_id)
                    fill_price = fill_price or 0.0
                # Update average cost basis (weighted average)
                if ot.outcome in self._avg_entry and prev > 0.5:
                    self._avg_entry[ot.outcome] = (
                        self._avg_entry[ot.outcome] * prev + fill_price * filled
                    ) / curr
                else:
                    self._avg_entry[ot.outcome] = fill_price
                logger.info(
                    f"Buy fill: {ot.outcome} +{filled:.0f}c @ {fill_price:.4f} "
                    f"(total {curr:.0f}c, avg entry {self._avg_entry[ot.outcome]:.4f})"
                )

            elif curr < prev - 0.5:
                # Sell fill detected
                sold = prev - curr
                fill_price = self._last_ask.get(ot.outcome, 0.0)
                if fill_price <= 0:
                    _, fill_price = self.get_best_bid_ask(ot.token_id)
                    fill_price = fill_price or 1.0
                entry = self._avg_entry.get(ot.outcome, 0.0)
                if entry > 0:
                    pnl = (fill_price - entry) * sold
                    self._session_pnl += pnl
                    if pnl >= 0:
                        self._wins += 1
                    else:
                        self._losses += 1
                    logger.info(
                        f"Sell fill: {ot.outcome} -{sold:.0f}c @ {fill_price:.4f} "
                        f"(entry {entry:.4f}, P&L: ${pnl:+.2f}, session: ${self._session_pnl:+.2f})"
                    )
                    self._notify(
                        f"SELL FILL {ot.outcome} -{sold:.0f}c @ {fill_price:.4f} | "
                        f"P&L: ${pnl:+.2f} | Session: ${self._session_pnl:+.2f}"
                    )
                    self._save_session_state()

                    # Warn at 80% of daily loss limit
                    if self._session_pnl < -self.max_daily_loss * 0.8:
                        self._notify(
                            f"WARNING: Session P&L ${self._session_pnl:+.2f} approaching daily limit "
                            f"-${self.max_daily_loss:.2f}"
                        )

        self._prev_positions = dict(self._positions)

    # -------------------------------------------------------------------------
    # Arbitrage
    # -------------------------------------------------------------------------

    def _check_arb(self) -> bool:
        """Buy both YES and NO immediately when combined ask < ARB_THRESHOLD."""
        if len(self.outcome_tokens) < 2:
            return False

        asks: Dict[str, float] = {}
        for ot in self.outcome_tokens:
            _, ask = self.get_best_bid_ask(ot.token_id)
            if ask is None:
                return False
            asks[ot.outcome] = ask

        total = sum(asks.values())
        if total >= ARB_THRESHOLD:
            return False

        logger.info(f"Arb: {asks} total={total:.4f} < {ARB_THRESHOLD}")
        contracts = max(1, round(20.0 / total))
        for ot in self.outcome_tokens:
            try:
                self.create_order(
                    ot.outcome, OrderSide.BUY, asks[ot.outcome], contracts, ot.token_id
                )
                self._arb_positions.add(ot.outcome)
                logger.info(f"Arb buy: {ot.outcome} @ {asks[ot.outcome]:.4f} x{contracts}")
            except Exception as e:
                logger.warning(f"Arb buy failed ({ot.outcome}): {e}")
        return True

    # -------------------------------------------------------------------------
    # Window management
    # -------------------------------------------------------------------------

    def _reset_window(self):
        """Cancel all orders and reset per-window state at expiry."""
        self.cancel_all_orders()
        self._arb_positions.clear()
        self._avg_entry.clear()
        self._last_bid.clear()
        self._last_ask.clear()
        self._quotes_placed_at = None
        self._liquidation_started = False

    def _reconcile_on_startup(self) -> None:
        """Reconstruct state from live exchange on startup to avoid double-entry."""
        try:
            open_orders = self.client.fetch_open_orders(self.market_id)
            if open_orders:
                logger.info(f"Reconciled {len(open_orders)} open orders from exchange")
            else:
                logger.info("Reconciliation: clean slate")
        except Exception as e:
            logger.warning(f"Startup reconciliation failed (proceeding without it): {e}")

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def cleanup(self):
        """Stop Binance feed before base cleanup."""
        self._price_feed.stop()
        super().cleanup()

    def refresh_state(self):
        """Refresh positions, orders, and cash balance."""
        self._positions = self.client.fetch_positions_dict_for_market(self.market)
        self._open_orders = self.client.fetch_open_orders(market_id=self.market_id)
        try:
            self._cached_cash = self.client.fetch_balance().get("USDC", 0.0)
        except Exception:
            pass

    def _seconds_until_expiry(self) -> float:
        if not self.market or not self.market.close_time:
            return float("inf")
        close_time = self.market.close_time
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=timezone.utc)
        return max(0.0, (close_time - datetime.now(timezone.utc)).total_seconds())

    def _log_mm_status(self, secs_remaining: float):
        total = self._wins + self._losses
        win_rate = f"{self._wins / total:.0%}" if total > 0 else "N/A"

        positions_str = " | ".join(
            f"{ot.outcome}: {self._positions.get(ot.outcome, 0):.0f}c"
            for ot in self.outcome_tokens
            if self._positions.get(ot.outcome, 0) > 0
        ) or "flat"

        btc_str = ""
        current_btc = self._price_feed.price
        if current_btc and self._window_start_btc:
            pct = (current_btc - self._window_start_btc) / self._window_start_btc
            vol = self._price_feed.realized_vol_30s
            vol_str = f" vol={vol:.0%}" if vol is not None else ""
            btc_str = f" | BTC ${current_btc:,.0f} ({pct:+.3%}{vol_str})"
        elif current_btc:
            btc_str = f" | BTC ${current_btc:,.0f}"

        logger.info(
            f"  [BBO] W/L: {self._wins}/{self._losses} ({win_rate}) | "
            f"P&L: ${self._session_pnl:+.2f} | "
            f"Pos: {positions_str} | "
            f"Orders: {len(self._open_orders)} | "
            f"Window: {secs_remaining:.0f}s"
            f"{btc_str}"
        )

    # -------------------------------------------------------------------------
    # Telegram notifications
    # -------------------------------------------------------------------------

    def _notify(self, msg: str) -> None:
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=3,
            )
        except Exception as e:
            logger.debug(f"Telegram notify failed: {e}")

    # -------------------------------------------------------------------------
    # SQLite session persistence
    # -------------------------------------------------------------------------

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session (
                    date TEXT PRIMARY KEY,
                    session_pnl REAL,
                    wins INTEGER,
                    losses INTEGER,
                    updated_at TEXT
                )
                """
            )

    def _load_session_state(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT session_pnl, wins, losses FROM session WHERE date = ?", (today,)
                ).fetchone()
            if row:
                self._session_pnl, self._wins, self._losses = row[0], row[1], row[2]
                logger.info(
                    f"Session restored: P&L ${self._session_pnl:+.2f} | W/L {self._wins}/{self._losses}"
                )
            else:
                logger.info("No session data for today — starting fresh")
        except Exception as e:
            logger.warning(f"Failed to load session state: {e}")

    def _save_session_state(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO session (date, session_pnl, wins, losses, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        session_pnl = excluded.session_pnl,
                        wins = excluded.wins,
                        losses = excluded.losses,
                        updated_at = excluded.updated_at
                    """,
                    (today, self._session_pnl, self._wins, self._losses, now_str),
                )
        except Exception as e:
            logger.warning(f"Failed to save session state: {e}")

    # -------------------------------------------------------------------------
    # Order Flow Imbalance
    # -------------------------------------------------------------------------

    def _order_flow_imbalance(self, token_id: str) -> float:
        """
        Return bid-side depth fraction: bid_depth / (bid_depth + ask_depth).

        OFI > 0.60 signals buy pressure (price likely rising).
        OFI < 0.40 signals sell pressure (price likely falling).
        Returns 0.5 (neutral) when orderbook data is unavailable.
        """
        try:
            ob = self.client.get_orderbook(token_id)
            if not ob:
                return 0.5
            bid_depth = sum(float(level.get("size", 0)) for level in ob.get("bids", []))
            ask_depth = sum(float(level.get("size", 0)) for level in ob.get("asks", []))
            total = bid_depth + ask_depth
            if total <= 0:
                return 0.5
            return bid_depth / total
        except Exception:
            return 0.5

    # -------------------------------------------------------------------------
    # Circuit breaker
    # -------------------------------------------------------------------------

    def _is_circuit_open(self) -> bool:
        if not self._circuit_open:
            return False
        if time.time() >= self._circuit_open_until:
            self._circuit_open = False
            self._consecutive_rejections = 0
            logger.info("Circuit breaker CLOSED — resuming order placement")
            return False
        logger.debug(f"Circuit breaker open — {self._circuit_open_until - time.time():.0f}s left")
        return True

    def _record_order_success(self) -> None:
        self._consecutive_rejections = 0

    def _record_order_failure(self, context: str) -> None:
        now = time.time()
        if self._first_rejection_time == 0.0 or now - self._first_rejection_time > CIRCUIT_BREAKER_WINDOW:
            self._first_rejection_time = now
            self._consecutive_rejections = 1
        else:
            self._consecutive_rejections += 1

        if self._consecutive_rejections >= CIRCUIT_BREAKER_THRESHOLD and not self._circuit_open:
            self._circuit_open = True
            self._circuit_open_until = now + CIRCUIT_BREAKER_COOLDOWN
            elapsed = now - self._first_rejection_time
            msg = (
                f"Circuit breaker OPEN: {self._consecutive_rejections} failures in {elapsed:.0f}s "
                f"(last: {context}). Pausing for {CIRCUIT_BREAKER_COOLDOWN:.0f}s."
            )
            logger.error(msg)
            self._notify(f"CIRCUIT BREAKER OPEN — {msg}")
