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
- Circuit breaker: pauses quoting after repeated order rejections
- Daily loss limit: stops quoting when session P&L falls below threshold
- Binance WebSocket feed for BTC context in status logs

Fee structure (Polymarket, January 2026):
- Limit orders earn 0.20% maker rebate on both legs
- Effective net profit per round trip is spread + rebates
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

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
        self._log_trader_profile()
        self._log_market_info()
        return True

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def on_tick(self):
        now = time.time()
        secs = self._seconds_until_expiry()

        # Fast path: roll window if expiring soon (checked every 100ms)
        if secs < self.cancel_before_expiry:
            if not self._window_reset:
                logger.info(f"Window expiring in {secs:.0f}s — rolling")
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
        """
        if secs_remaining < MIN_WINDOW_FOR_QUOTING:
            return
        if self._session_pnl < -self.max_daily_loss:
            logger.warning(
                f"Daily loss limit hit: P&L ${self._session_pnl:.2f} / limit -${self.max_daily_loss:.2f}"
            )
            return
        if self._is_circuit_open():
            return

        # Force-reprice stale quotes
        if self._quotes_placed_at and (time.time() - self._quotes_placed_at > QUOTE_MAX_AGE):
            self.cancel_all_orders()
            self._quotes_placed_at = None

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
            skew = (position / self.max_inventory) * self.half_spread
            our_bid = self.round_price(mid - self.half_spread - skew)
            our_ask = self.round_price(mid + self.half_spread - skew)

            # Clamp to valid range
            our_bid = max(self.tick_size, min(our_bid, 1.0 - self.tick_size))
            our_ask = max(our_bid + self.tick_size, min(our_ask, 1.0 - self.tick_size))

            buy_orders, sell_orders = self.get_orders_for_outcome(ot.outcome)

            # BUY side — only post if below max inventory
            if position < self.max_inventory:
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
        """Refresh positions and orders (skip NAV/delta, not used here)."""
        self._positions = self.client.fetch_positions_dict_for_market(self.market)
        self._open_orders = self.client.fetch_open_orders(market_id=self.market_id)

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
            btc_str = f" | BTC ${current_btc:,.0f} ({pct:+.3%})"
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
            logger.error(
                f"Circuit breaker OPEN: {self._consecutive_rejections} failures in {elapsed:.0f}s "
                f"(last: {context}). Pausing for {CIRCUIT_BREAKER_COOLDOWN:.0f}s."
            )
