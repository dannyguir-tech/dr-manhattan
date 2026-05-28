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
    TELEGRAM_TOKEN          — Telegram bot token for trade alerts (optional)
    TELEGRAM_CHAT_ID        — Telegram bot token for trade alerts (optional)
    PORT                    — Port for Railway health check endpoint (default: 8080)
"""

import logging
import os
import sys
import threading
from typing import Optional

import uvicorn

from dr_manhattan import create_exchange
from dr_manhattan.bridge_api import app as bridge_api_app
from dr_manhattan.strategies.btc_scalp import BTCScalpStrategy

logger = logging.getLogger(__name__)


class _UvicornServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        """Disable signal registration when serving from a background thread."""
        return


def _read_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default if default is not None else "")
    if value is None:
        return ""
    return value.strip()


def _require_env(name: str) -> str:
    value = _read_env(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _parse_int_env(name: str, default: int) -> int:
    raw_value = _read_env(name, str(default))
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {raw_value!r}") from exc


def _parse_float_env(name: str, default: float) -> float:
    raw_value = _read_env(name, str(default))
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid number for {name}: {raw_value!r}") from exc


def _start_bridge_api(port: int) -> None:
    """Start the FastAPI bridge API in a background thread."""
    config = uvicorn.Config(
        bridge_api_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = _UvicornServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    logger.info("Bridge API listening on port %d", port)


def main():
    try:
        port = _parse_int_env("PORT", 8080)
        half_spread = _parse_float_env("HALF_SPREAD", 0.03)
        order_size = _parse_int_env("ORDER_SIZE", 5)
        max_inventory = _parse_float_env("MAX_INVENTORY", 50.0)
        max_daily_loss = _parse_float_env("MAX_DAILY_LOSS", 50.0)

        # Fail fast with a clear error if the Railway deployment is missing the
        # credentials required to create a Polymarket exchange session.
        _require_env("POLYMARKET_PRIVATE_KEY")
        _require_env("POLYMARKET_FUNDER")
    except ValueError as exc:
        logger.error("Startup configuration error: %s", exc)
        sys.exit(1)

    _start_bridge_api(port)

    try:
        exchange = create_exchange("polymarket", use_env=True, verbose=True)

        strategy = BTCScalpStrategy(
            exchange=exchange,
            half_spread=half_spread,
            order_size=order_size,
            max_inventory=max_inventory,
            max_daily_loss=max_daily_loss,
        )
        strategy.run()
    except Exception as e:
        logger.error("Fatal startup error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
