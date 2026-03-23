"""
BTC 5-Minute Scalp Strategy

Places passive limit buy orders at entry_price on both YES and NO outcomes
of the rolling Polymarket BTC 5-minute Up/Down market. When one side fills,
places a sell at profit_target and cancels the other pending buy.

Phase 1 features:
- Auto-discovers the active BTC 5-min market window
- Rolls to next window automatically before expiry
- Both-sides arbitrage detection: buy both sides when combined cost < 0.97

Phase 2 features:
- Kelly Criterion position sizing based on running win rate
- EWMA momentum filter: skips entries during sustained price declines
- Order lifetime enforcement: cancels unfilled buys after order_lifetime seconds

Phase 3 features (dynamic profit rules):
- High-water mark trailing stop: tracks highest mid-price seen since fill
- Three-tier exit logic based on gain magnitude and time remaining:
    Tier 1 (<30% gain): hold at profit_target; near expiry lower to entry+0.01
    Tier 2 (30-100% gain): trail at 85% of high-water (tightens to 92% near expiry)
    Tier 3 (>100% gain): trail at 88% of high-water (tightens to 94% near expiry)
- Emergency exit: if price gaps below trailing floor, sell immediately at bid

Fee structure (Polymarket, January 2026):
- Limit orders earn 0.20% maker rebate on both entry and exit legs
- Effective net profit per round trip is slightly above raw spread
"""

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from ..base.strategy import Strategy
from ..models.market import Market, OutcomeToken
from ..models.order import Order, OrderSide
from ..utils import setup_logger

logger = setup_logger(__name__)

# Minimum seconds remaining in a window to place new entry orders
MIN_WINDOW_FOR_ENTRY = 162  # cancel_before_expiry(90) + order_lifetime(72)

# Threshold for both-sides arbitrage detection
ARB_THRESHOLD = 0.97  # buy both when YES_ask + NO_ask < this

# Adaptive exit: Tier 1 fallback when <120s left and gain is small
ADAPTIVE_EXIT_SECS = 120

# --- Trailing stop thresholds ---
# Tier 2: price gained 30-100% above entry
MOMENTUM_THRESHOLD = 0.30
# Tier 3: price gained >100% above entry (near resolution territory)
LARGE_GAIN_THRESHOLD = 1.00

# Tier 2 trailing percentages (interpolated from early → late window)
TRAILING_STOP_EARLY = 0.85   # trail at 85% of high-water when >180s remain
TRAILING_STOP_LATE = 0.92    # tighten to 92% near expiry

# Tier 3 trailing percentages
TRAILING_LARGE_EARLY = 0.88
TRAILING_LARGE_LATE = 0.94


class BTCScalpStrategy(Strategy):
    """
    Passive limit-order scalp on the Polymarket BTC 5-minute Up/Down market.

    Strategy parameters:
        entry_price: Limit buy price for both YES and NO (default 0.30)
        profit_target: Limit sell price after fill (default 0.33)
        order_size_usd: USD to risk per side (default 10.0)
        order_lifetime: Seconds before cancelling unfilled buys (default 72)
        cancel_before_expiry: Cancel all orders this many seconds before window close (default 90)

    Usage:
        strategy = BTCScalpStrategy(exchange, market_id="btc-5min-auto")
        strategy.run()
    """

    def __init__(
        self,
        exchange,
        market_id: str = "btc-5min-auto",
        entry_price: float = 0.30,
        profit_target: float = 0.33,
        order_size_usd: float = 10.0,
        order_lifetime: float = 72.0,
        cancel_before_expiry: float = 90.0,
        **kwargs,
    ):
        # Strip kwargs that aren't accepted by base Strategy
        base_keys = {"max_position", "order_size", "max_delta", "check_interval", "track_fills"}
        base_kwargs = {k: v for k, v in kwargs.items() if k in base_keys}
        # Default check_interval to 5s if not specified
        base_kwargs.setdefault("check_interval", 5.0)
        super().__init__(exchange, market_id, **base_kwargs)

        if profit_target <= entry_price + 0.005:
            raise ValueError(
                f"profit_target ({profit_target}) must be > entry_price ({entry_price}) + 0.005"
            )

        self.entry_price = entry_price
        self.profit_target = profit_target
        self.order_size_usd = order_size_usd
        self.order_lifetime = order_lifetime
        self.cancel_before_expiry = cancel_before_expiry

        # Per-window state
        self._buy_order_ids: Dict[str, str] = {}   # outcome -> order_id
        self._sell_order_ids: Dict[str, str] = {}  # outcome -> order_id
        self._orders_placed_at: Optional[float] = None

        # Kelly Criterion state (Phase 2)
        self._wins: int = 0
        self._losses: int = 0
        self._seed_win_rate: float = 0.60  # Conservative prior until we have data

        # EWMA momentum state (Phase 2)
        self._ewma_alpha: float = 0.3
        self._price_ewma: Dict[str, float] = {}       # outcome -> ewma value
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}  # outcome -> [(t, mid)]

        # Trailing stop state (Phase 3)
        self._high_water: Dict[str, float] = {}  # outcome -> highest mid seen since fill

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

        # No WebSocket for this strategy — REST fallback in get_best_bid_ask()
        self._positions = self.client.fetch_positions_dict_for_market(self.market)

        self._log_trader_profile()
        self._log_market_info()
        return True

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def on_tick(self):
        self.refresh_state()
        secs = self._seconds_until_expiry()

        # Roll to next window if expiring soon
        if secs < self.cancel_before_expiry:
            logger.info(f"Window expiring in {secs:.0f}s — rolling")
            self._reset_window()
            new_market = self._find_btc_5min_market()
            if new_market and new_market.id != self.market_id:
                self._switch_market(new_market)
            return

        # Both-sides arbitrage check
        if self._check_arb():
            return

        # Update EWMA for all outcomes
        for ot in self.outcome_tokens:
            bid, ask = self.get_best_bid_ask(ot.token_id)
            if bid and ask:
                self._update_price_ewma(ot.outcome, (bid + ask) / 2.0)

        # Handle fills: detect filled buys and manage sell orders
        self._handle_fills(secs)

        # Cancel buys that exceeded order_lifetime
        if self._orders_placed_at and (time.time() - self._orders_placed_at > self.order_lifetime):
            self._cancel_pending_buys()
            self._orders_placed_at = None

        # Place new entry orders if no active buys/sells and enough time remains
        no_open_positions = not any(self._positions.get(o.outcome, 0) > 0.5 for o in self.outcome_tokens)
        if (
            not self._buy_order_ids
            and not self._sell_order_ids
            and no_open_positions
            and secs > MIN_WINDOW_FOR_ENTRY
        ):
            self._place_entry_orders()

        self._log_scalp_status(secs)

    # -------------------------------------------------------------------------
    # Market discovery
    # -------------------------------------------------------------------------

    def _find_btc_5min_market(self) -> Optional[Market]:
        """Find the currently active BTC 5-minute Up/Down market."""
        now = datetime.now()
        try:
            markets = self.exchange.search_markets(
                keywords=["BTC", "Up or Down"],
                closed=False,
                end_date_min=now,
                end_date_max=now + timedelta(minutes=6),
                min_liquidity=1.0,
            )
            if not markets:
                # Broader fallback
                markets = self.exchange.search_markets(
                    query="Bitcoin Up or Down",
                    closed=False,
                    end_date_min=now,
                    end_date_max=now + timedelta(minutes=8),
                )
            if not markets:
                logger.warning("No BTC 5-min market found")
                return None

            # Pick the market with the soonest close_time (current window)
            with_close = [m for m in markets if m.close_time]
            return min(with_close, key=lambda m: m.close_time) if with_close else markets[0]

        except Exception as e:
            logger.warning(f"Market discovery error: {e}")
            return None

    def _switch_market(self, market: Market):
        """Switch to a new market window."""
        self.market = market
        self.market_id = market.id
        self.tick_size = market.tick_size

        token_ids = market.metadata.get("clobTokenIds", [])
        self.outcome_tokens = [
            OutcomeToken(market_id=self.market_id, outcome=outcome, token_id=token_id)
            for outcome, token_id in zip(market.outcomes, token_ids)
        ]
        self._positions = self.client.fetch_positions_dict_for_market(self.market)
        self._price_history.clear()
        self._price_ewma.clear()
        logger.info(f"New window: {market.question[:70]}")

    # -------------------------------------------------------------------------
    # Phase 2: Kelly Criterion sizing
    # -------------------------------------------------------------------------

    def _kelly_size(self) -> float:
        """
        Kelly Criterion position size in USD.

        f* = p - (1-p)/b
        where p = win rate, b = gain/loss ratio (profit_target - entry) / entry

        Returns a fraction of order_size_usd, clamped to 5%-100%.
        """
        total = self._wins + self._losses
        p = self._wins / total if total >= 10 else self._seed_win_rate
        b = (self.profit_target - self.entry_price) / self.entry_price
        f = p - (1.0 - p) / b
        f = max(0.05, min(f, 1.0))
        return round(self.order_size_usd * f, 2)

    # -------------------------------------------------------------------------
    # Phase 3: Dynamic sell target (trailing stop)
    # -------------------------------------------------------------------------

    def _dynamic_sell_target(self, outcome: str, current_mid: float, secs_remaining: float) -> float:
        """
        Compute the optimal sell price based on gain size and time remaining.

        Tier 1 — gain < 30%: keep sell at profit_target; near expiry lower to entry+0.01.
        Tier 2 — gain 30-100%: trailing stop at 85-92% of high-water (time-interpolated).
        Tier 3 — gain > 100%: tighter trail at 88-94% of high-water.

        Emergency gap-down is handled separately in _handle_fills().
        """
        gain = (current_mid - self.entry_price) / self.entry_price
        high_water = self._high_water.get(outcome, current_mid)

        # Urgency: 0.0 = plenty of time, 1.0 = window almost expired
        urgency = max(0.0, min(1.0, 1.0 - secs_remaining / 300.0))

        if gain < MOMENTUM_THRESHOLD:
            # Tier 1: small gain — use flat target, drop to entry+0.01 near expiry
            if secs_remaining < ADAPTIVE_EXIT_SECS:
                return self.round_price(self.entry_price + 0.01)
            return self.profit_target

        elif gain < LARGE_GAIN_THRESHOLD:
            # Tier 2: significant run — trail with moderate slack
            pct = TRAILING_STOP_EARLY + (TRAILING_STOP_LATE - TRAILING_STOP_EARLY) * urgency
            return max(self.profit_target, self.round_price(high_water * pct))

        else:
            # Tier 3: large run (near resolution territory) — tighter trail
            pct = TRAILING_LARGE_EARLY + (TRAILING_LARGE_LATE - TRAILING_LARGE_EARLY) * urgency
            return max(self.profit_target, self.round_price(high_water * pct))

    # -------------------------------------------------------------------------
    # Phase 2: EWMA momentum filter
    # -------------------------------------------------------------------------

    def _update_price_ewma(self, outcome: str, mid: float):
        if outcome not in self._price_ewma:
            self._price_ewma[outcome] = mid
        else:
            self._price_ewma[outcome] = (
                self._ewma_alpha * mid + (1.0 - self._ewma_alpha) * self._price_ewma[outcome]
            )

        history = self._price_history.setdefault(outcome, [])
        history.append((time.time(), mid))
        if len(history) > 60:
            history.pop(0)

    def _is_momentum_favorable(self, outcome: str) -> bool:
        """
        Return False if price has declined for 3+ consecutive ticks.
        A sustained decline suggests trend continuation, not mean reversion.
        """
        history = self._price_history.get(outcome, [])
        if len(history) < 4:
            return True
        recent = [p for _, p in history[-4:]]
        consecutive_falls = sum(1 for i in range(len(recent) - 1) if recent[i + 1] < recent[i])
        if consecutive_falls >= 3:
            logger.info(f"Skipping {outcome}: {consecutive_falls} consecutive declining ticks")
            return False
        return True

    # -------------------------------------------------------------------------
    # Arbitrage check
    # -------------------------------------------------------------------------

    def _check_arb(self) -> bool:
        """
        Buy both YES and NO immediately (taker) when combined ask < ARB_THRESHOLD.
        Returns True if arb executed (skip normal logic this tick).
        """
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
        contracts = max(1, round(self.order_size_usd / total))
        for ot in self.outcome_tokens:
            try:
                self.create_order(ot.outcome, OrderSide.BUY, asks[ot.outcome], contracts, ot.token_id)
                logger.info(f"Arb buy: {ot.outcome} @ {asks[ot.outcome]:.4f} x{contracts}")
            except Exception as e:
                logger.warning(f"Arb buy failed ({ot.outcome}): {e}")
        return True

    # -------------------------------------------------------------------------
    # Entry orders
    # -------------------------------------------------------------------------

    def _place_entry_orders(self):
        """Place limit buy orders on both outcomes at entry_price."""
        kelly_usd = self._kelly_size()
        contracts = max(1, round(kelly_usd / self.entry_price))
        self._orders_placed_at = time.time()

        for ot in self.outcome_tokens:
            bid, ask = self.get_best_bid_ask(ot.token_id)

            # Skip if current ask is well above entry (no fill expected)
            if ask is not None and ask > self.entry_price + 0.05:
                continue

            # Phase 2: momentum filter
            if not self._is_momentum_favorable(ot.outcome):
                continue

            try:
                order = self.create_order(ot.outcome, OrderSide.BUY, self.entry_price, contracts, ot.token_id)
                self._buy_order_ids[ot.outcome] = order.id
                self.log_order(OrderSide.BUY, contracts, ot.outcome, self.entry_price)
            except Exception as e:
                logger.warning(f"Buy order failed ({ot.outcome}): {e}")

    # -------------------------------------------------------------------------
    # Fill management
    # -------------------------------------------------------------------------

    def _handle_fills(self, secs_remaining: float):
        """
        Detect filled buys, place and update sell orders using dynamic trailing stop.
        Also cleans up completed sell orders.
        """
        open_order_ids = {o.id for o in self._open_orders}

        # Clean up completed sell orders (order gone + position gone = filled)
        for outcome in list(self._sell_order_ids.keys()):
            if self._sell_order_ids[outcome] not in open_order_ids:
                if self._positions.get(outcome, 0.0) < 0.5:
                    logger.info(f"Sell filled: {outcome} — position closed")
                    del self._sell_order_ids[outcome]
                    self._high_water.pop(outcome, None)

        for ot in self.outcome_tokens:
            pos = self._positions.get(ot.outcome, 0.0)
            if pos < 0.5:
                continue

            bid, ask = self.get_best_bid_ask(ot.token_id)
            current_mid = (bid + ask) / 2.0 if bid and ask else None

            if ot.outcome in self._sell_order_ids:
                # Position held — update trailing stop each tick
                if current_mid is None:
                    continue

                # Raise high-water mark if price has moved up
                self._high_water[ot.outcome] = max(
                    self._high_water.get(ot.outcome, current_mid), current_mid
                )

                dynamic_target = self._dynamic_sell_target(ot.outcome, current_mid, secs_remaining)

                _, sell_orders = self.get_orders_for_outcome(ot.outcome)
                active_sells = [o for o in sell_orders if o.id in open_order_ids]

                for sell_order in active_sells:
                    if abs(sell_order.price - dynamic_target) <= self.tick_size:
                        continue  # Already at the right price

                    try:
                        self.client.cancel_order(sell_order.id)

                        # Emergency exit: price gapped below trailing floor — sell at bid
                        if bid is not None and bid < dynamic_target - self.tick_size:
                            exit_price = bid
                            logger.info(
                                f"Emergency exit: {ot.outcome} gapped to {bid:.4f} "
                                f"(floor was {dynamic_target:.4f})"
                            )
                        else:
                            exit_price = dynamic_target
                            logger.info(
                                f"Trailing: {ot.outcome} "
                                f"{sell_order.price:.4f} → {exit_price:.4f} "
                                f"(HWM={self._high_water[ot.outcome]:.4f})"
                            )

                        new_order = self.create_order(
                            ot.outcome, OrderSide.SELL, exit_price, pos, ot.token_id
                        )
                        self._sell_order_ids[ot.outcome] = new_order.id
                    except Exception as e:
                        logger.warning(f"Sell update failed ({ot.outcome}): {e}")
                continue

            # New fill detected — seed high-water and place initial sell
            self._high_water[ot.outcome] = current_mid if current_mid else self.entry_price
            initial_target = self._dynamic_sell_target(
                ot.outcome, self._high_water[ot.outcome], secs_remaining
            )

            logger.info(
                f"Fill: {ot.outcome} {pos:.0f}c — sell @ {initial_target:.4f} "
                f"(mid={self._high_water[ot.outcome]:.4f})"
            )
            self._wins += 1

            # Cancel the other side's pending buy
            for other in self.outcome_tokens:
                if other.outcome != ot.outcome and other.outcome in self._buy_order_ids:
                    try:
                        self.client.cancel_order(self._buy_order_ids[other.outcome])
                        del self._buy_order_ids[other.outcome]
                        logger.info(f"Cancelled opposite buy: {other.outcome}")
                    except Exception as e:
                        logger.warning(f"Cancel opposite buy failed ({other.outcome}): {e}")

            self._buy_order_ids.pop(ot.outcome, None)

            try:
                sell_order = self.create_order(
                    ot.outcome, OrderSide.SELL, initial_target, pos, ot.token_id
                )
                self._sell_order_ids[ot.outcome] = sell_order.id
                self.log_order(OrderSide.SELL, pos, ot.outcome, initial_target)
            except Exception as e:
                logger.warning(f"Sell order failed ({ot.outcome}): {e}")

    # -------------------------------------------------------------------------
    # Window management
    # -------------------------------------------------------------------------

    def _cancel_pending_buys(self):
        """Cancel all pending buy orders (order_lifetime exceeded)."""
        for outcome, order_id in list(self._buy_order_ids.items()):
            try:
                self.client.cancel_order(order_id)
                logger.info(f"Lifetime expired: cancelled buy {outcome}")
            except Exception as e:
                logger.warning(f"Cancel failed ({outcome}): {e}")
        self._buy_order_ids.clear()

    def _reset_window(self):
        """Cancel all orders and reset per-window state. Track losses if sells were open."""
        if self._sell_order_ids:
            self._losses += len(self._sell_order_ids)
        self.cancel_all_orders()
        self._buy_order_ids.clear()
        self._sell_order_ids.clear()
        self._orders_placed_at = None
        self._high_water.clear()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _seconds_until_expiry(self) -> float:
        if not self.market or not self.market.close_time:
            return float("inf")
        delta = (self.market.close_time - datetime.now()).total_seconds()
        return max(0.0, delta)

    def _log_scalp_status(self, secs_remaining: float):
        total = self._wins + self._losses
        win_rate = f"{self._wins / total:.0%}" if total > 0 else "N/A"
        kelly_usd = self._kelly_size()
        logger.info(
            f"  [Scalp] W/L: {self._wins}/{self._losses} ({win_rate}) | "
            f"Kelly: ${kelly_usd:.2f} | "
            f"Window: {secs_remaining:.0f}s | "
            f"Buys: {len(self._buy_order_ids)} | "
            f"Sells: {len(self._sell_order_ids)}"
        )
