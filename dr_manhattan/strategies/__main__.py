"""
Entrypoint for running BTCScalpStrategy (BBO market maker) directly.

Usage:
    uv run python -m dr_manhattan.strategies.btc_scalp

Required environment variables:
    POLYMARKET_PRIVATE_KEY  — 64-hex EVM private key (with or without 0x)
    POLYMARKET_FUNDER       — Wallet address that holds USDC collateral

Optional:
    POLYMARKET_API_KEY      — L2 CLOB API key (auto-derived if absent)
    HALF_SPREAD             — Half the quoted bid-ask spread (default: 0.03)
    ORDER_SIZE              — Contracts per order per side (default: 5)
    MAX_INVENTORY           — Max contracts per outcome before buying stops (default: 50)
    MAX_DAILY_LOSS          — Stop quoting when session P&L < -MAX (default: 50.0)
"""

import os

from dr_manhattan import create_exchange
from dr_manhattan.strategies.btc_scalp import BTCScalpStrategy


def main():
    exchange = create_exchange("polymarket", use_env=True, verbose=True)

    strategy = BTCScalpStrategy(
        exchange=exchange,
        half_spread=float(os.environ.get("HALF_SPREAD", "0.03")),
        order_size=int(os.environ.get("ORDER_SIZE", "5")),
        max_inventory=float(os.environ.get("MAX_INVENTORY", "50.0")),
        max_daily_loss=float(os.environ.get("MAX_DAILY_LOSS", "50.0")),
    )
    strategy.run()


if __name__ == "__main__":
    main()
