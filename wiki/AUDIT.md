# Dr. Manhattan Trading Bot - Audit Report

**Date:** 2026-03-29
**Scope:** Functionality, profitability, risk management, improvement potential

---

## Executive Summary

Dr. Manhattan is a BBO market-making bot trading 5-minute BTC Up/Down binary
outcomes on Polymarket. The codebase is well-structured with 5 exchange
integrations, comprehensive tests, and a backtesting pipeline. However, the
core BTC scalp strategy has **several critical bugs** in its risk management
that could cause significant losses in production.

**Overall assessment:** Solid architecture, dangerous risk management bugs.

---

## 1. Critical Bugs (Fixed / To Fix)

### 1.1 Cash Validation Inverted (FIXED)

**File:** `btc_scalp.py:393`

```python
# BEFORE (broken): allows trading with zero or negative balance
cash_ok = self._cached_cash <= 0 or self._cached_cash >= order_cost

# AFTER (fixed): blocks orders when balance is unknown or insufficient
cash_ok = self._cached_cash >= order_cost if self._cached_cash > 0 else False
```

**Impact:** Bot could place orders with zero or negative USDC balance. The
`_cached_cash` field initializes to `0.0`, meaning before the first balance
fetch, every order passes the cash check.

### 1.2 Arb Position Single-Leg Risk (NOT FIXED)

**File:** `btc_scalp.py:299, 365, 475`

When `YES_ask + NO_ask < 0.97`, the bot buys both sides. But the two buys are
independent API calls. If the second leg fails (rate limit, network error), the
bot holds an unhedged position that it then **excludes from liquidation** at
expiry (line 475).

**Fix needed:** Verify both legs filled before adding to `_arb_positions`. If
one leg fails, treat the filled leg as a normal directional position.

### 1.3 BTC Trend Filter Disables Silently (NOT FIXED)

**File:** `btc_scalp.py:194-197`

If the Binance WebSocket disconnects AND the REST fallback fails, the trend
filter returns `False` (filter disabled). The bot then posts bids on losing
outcomes without directional protection.

**Fix needed:** Return `True` (block bids) when price data is unavailable,
rather than disabling the filter.

### 1.4 Position Cache Staleness (NOT FIXED)

**File:** `exchange_client.py:136` (2s TTL) vs `btc_scalp.py` (100ms tick)

The bot ticks 20x per cache cycle. During that window, fills go undetected and
the bot can accumulate inventory beyond `max_inventory`. Reduce cache TTL to
0.5s or fetch positions every tick during Phase 1 liquidation.

---

## 2. Profitability Analysis

### Revenue Model

The strategy earns from:
1. **Bid-ask spread:** Default 6 ticks (3 half-spread per side)
2. **Maker rebate:** 0.20% per leg (Polymarket Jan 2026 fee schedule)
3. **Arb capture:** When YES+NO ask < 0.97, buys both for guaranteed profit at
   settlement

### Profitability Levers

| Parameter | Default | Effect |
|-----------|---------|--------|
| `half_spread` | 0.03 | Wider = safer but fewer fills; narrower = more fills but adverse selection risk |
| `order_size` | 5 | Contracts per order; larger = more capital at risk per fill |
| `max_inventory` | 50 | Cap on one-sided exposure; lower = safer, higher = more throughput |
| `max_daily_loss` | 50.0 | Session kill switch |
| `ARB_THRESHOLD` | 0.97 | Combined ask below this triggers both-sides buy |
| `OFI threshold` | 0.40 | Order flow imbalance gate for entry |

### Profitability Risks

1. **Adverse selection in trending markets.** The BTC trend filter (0.3%
   threshold) is the primary defense. When disabled (see bug 1.3), the bot
   posts on losing outcomes and gets picked off by informed traders.

2. **Expiry inventory risk.** With 60s remaining and up to 22 contracts held
   (from inventory decay formula), the 30-second Phase 1 window may not fully
   liquidate in thin markets. Unsold contracts resolve at 0 or 1.

3. **Volatility scaling baseline too low.** `VOL_SCALE_BASELINE = 0.50` is
   well below BTC's typical 70-100% annualized vol. Spreads are almost always
   scaled up, potentially leaving edge on the table during calm periods.

4. **Circuit breaker kills entire windows.** 5 consecutive rejections trigger
   a 300s (5 min) cooldown -- the exact length of a trading window. One flaky
   API period means a full window missed.

### Estimated P&L Per Window (Ideal Conditions)

- Spread earned per round-trip: ~$0.30 (6 ticks * $0.05/tick)
- Maker rebate per round-trip: ~$0.004 (0.20% on ~$1 notional * 2 legs)
- Fills per 5-min window: 5-15 (depends on market activity)
- Gross per window: $1.50 - $4.50
- Minus: adverse selection losses, failed liquidations, stale positions
- **Realistic net: $0.50 - $2.00 per window in calm markets**

At 288 windows/day: **$144 - $576/day theoretical maximum** (before losses).
Actual performance depends heavily on market conditions and bug exposure.

---

## 3. Architecture Assessment

### Strengths

- **Clean exchange abstraction.** CCXT-style unified API across 5 exchanges
  (Polymarket, Kalshi, Opinion, Limitless, PredictFun). All fully functional.
- **Proper error hierarchy.** 8 exception types with clear semantics.
- **Rate limiting and retry logic.** Exponential backoff with jitter on all
  HTTP requests.
- **WebSocket infrastructure.** Auto-reconnection, heartbeat monitoring, proper
  async lifecycle.
- **MCP integration.** 19+ tools for Claude-based market analysis.
- **Backtesting pipeline.** Binance data feed, Polymarket orderbook snapshots,
  realistic fee modeling.
- **Test coverage.** 66+ test cases across unit, integration, and MCP layers.

### Weaknesses

- **Cross-exchange matcher is stubbed.** `matcher.py` strategies return `0.0` --
  cross-exchange arb not functional.
- **Mixed async/threading in WebSockets.** Fragile but works. Potential for
  orphaned threads on long runs.
- **No slippage protection on liquidation.** Phase 1 sells at best bid with
  full position size; no max-slippage guard.
- **SQLite without WAL mode.** Concurrent bot instances corrupt the session DB.
- **Unused dependencies.** `matplotlib` and `scikit-learn` in pyproject.toml
  bloat the container image.

### Exchange Integration Status

| Exchange | Status | WebSocket | Trading |
|----------|--------|-----------|---------|
| Polymarket | Production | Yes | Yes |
| Kalshi | Production | No | Yes |
| Opinion | Production | No (API limit) | Yes |
| Limitless | Production | Yes | Yes |
| PredictFun | Production | Yes | Yes |

---

## 4. Improvement Opportunities

### High Impact

1. **Fix the 3 remaining critical bugs** (arb single-leg, trend filter
   fallback, cache staleness). These are the biggest risk to profitability.

2. **Adaptive spread based on realized fill rate.** Track fill rates per
   window; widen spread when fills are too fast (adverse selection signal),
   narrow when too slow (missing edge).

3. **Multi-window inventory management.** Currently each window is independent.
   Allow carrying hedged positions across windows when profitable.

4. **Cross-exchange arbitrage.** Complete the matcher stubs to find price
   discrepancies between Polymarket/Kalshi/Limitless on the same events.

### Medium Impact

5. **Reduce circuit breaker cooldown** from 300s to 30-60s, or scale with
   remaining window time.

6. **Add slippage limits to liquidation orders.** Set max acceptable slippage
   (e.g., 2 ticks) and retry if market moves too far.

7. **Improve OFI signal.** Current implementation uses top-of-book only.
   Weight by price level depth for more reliable signal.

8. **Increase VOL_SCALE_BASELINE to 0.70-0.80** to match BTC's typical
   volatility regime and avoid permanent spread widening.

### Low Impact

9. **Add Prometheus/StatsD metrics.** Track fill rate, spread capture,
   inventory levels, P&L per window for real-time monitoring.

10. **Implement dry-run mode.** Log orders without executing for strategy
    validation.

11. **Remove unused deps** (matplotlib, scikit-learn) from production image.

12. **Add WAL mode to SQLite** for safe concurrent access.

---

## 5. Summary

| Category | Rating | Notes |
|----------|--------|-------|
| Code quality | Good | Clean architecture, proper abstractions |
| Exchange integrations | Excellent | 5 exchanges, all functional |
| Risk management | Poor | Critical bugs in cash check, arb, trend filter |
| Profitability potential | Moderate | Positive edge exists but bugs erode it |
| Test coverage | Good | 66+ tests, 100% pass rate |
| Documentation | Good | Wiki, examples, strategy docs |
| Improvement potential | High | Cross-exchange arb, adaptive spreads, monitoring |

**Bottom line:** Fix the critical risk bugs before running with real capital.
The architecture supports much more sophisticated strategies -- the current
BTC scalp is a solid starting point but needs hardening.
