"""
Entrypoint for running BTCScalpStrategy directly.

Usage:
    uv run python -m dr_manhattan.strategies.btc_scalp

Required environment variables:
    POLYMARKET_PRIVATE_KEY  — 64-hex EVM private key (with or without 0x)
    POLYMARKET_FUNDER       — Wallet address that holds USDC collateral

Optional:
    POLYMARKET_API_KEY      — L2 CLOB API key (auto-derived if absent)
    ORDER_SIZE_USD          — USD per side per window (default: 10.0)
    MAX_DAILY_LOSS          — Stop trading when session P&L < -MAX (default: 50.0)
    ENTRY_PRICE             — Limit buy price (default: 0.32)
    PROFIT_TARGET           — Initial sell target (default: 0.35)
"""

import os

from dr_manhattan import create_exchange
from dr_manhattan.strategies.btc_scalp import BTCScalpStrategy


def main():
    exchange = create_exchange("polymarket", use_env=True, verbose=True)

    strategy = BTCScalpStrategy(
        exchange=exchange,
        entry_price=float(os.environ.get("ENTRY_PRICE", "0.32")),
        profit_target=float(os.environ.get("PROFIT_TARGET", "0.35")),
        order_size_usd=float(os.environ.get("ORDER_SIZE_USD", "10.0")),
        max_daily_loss=float(os.environ.get("MAX_DAILY_LOSS", "50.0")),
    )
    strategy.run()


if __name__ == "__main__":
    main()
