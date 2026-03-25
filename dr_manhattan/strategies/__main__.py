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
    TELEGRAM_CHAT_ID        — Telegram chat/user ID to send alerts to (optional)
    PORT                    — Port for Railway health check endpoint (default: 8080)
"""

import http.server
import logging
import os
import sys
import threading

from dr_manhattan import create_exchange
from dr_manhattan.strategies.btc_scalp import BTCScalpStrategy

logger = logging.getLogger(__name__)


def _start_health_server(port: int) -> None:
    """Bind a minimal HTTP server so Railway health checks pass."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass  # silence per-request access logs

    server = http.server.HTTPServer(("", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health check server listening on port %d", port)


def main():
    port = int(os.environ.get("PORT", "8080").strip())
    _start_health_server(port)

    try:
        exchange = create_exchange("polymarket", use_env=True, verbose=True)

        strategy = BTCScalpStrategy(
            exchange=exchange,
            half_spread=float(os.environ.get("HALF_SPREAD", "0.03").strip()),
            order_size=int(os.environ.get("ORDER_SIZE", "5").strip()),
            max_inventory=float(os.environ.get("MAX_INVENTORY", "50.0").strip()),
            max_daily_loss=float(os.environ.get("MAX_DAILY_LOSS", "50.0").strip()),
        )
        strategy.run()
    except Exception as e:
        logger.error("Fatal startup error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
