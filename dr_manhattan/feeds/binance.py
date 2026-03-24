"""
Binance real-time price feed via WebSocket.

Streams BTC/USDT aggregate trades from Binance public API.
Runs in a daemon thread — no credentials required.

Usage:
    feed = BinancePriceFeed()
    feed.start()
    price = feed.price   # None until first message arrives
    feed.stop()
"""

import json
import threading
import time
from typing import Optional

import urllib.request

from ..utils import setup_logger

logger = setup_logger(__name__)

# Binance public aggregate trade stream — no auth needed
_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# Reconnect backoff: doubles each failure, caps at 30s
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0
_STALE_THRESHOLD = 10.0  # seconds before feed is considered stale
_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


class BinancePriceFeed:
    """
    Streams BTC/USDT price from Binance via aggregate trade WebSocket.

    Thread-safe: `price` property can be read from any thread.
    Auto-reconnects on disconnect or error.
    """

    def __init__(self):
        self._price: Optional[float] = None
        self._last_message_time: float = 0.0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def price(self) -> Optional[float]:
        """Latest BTC/USDT price. None until first message arrives."""
        with self._lock:
            return self._price

    @property
    def is_connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_fresh(self) -> bool:
        """True if a WebSocket message arrived within the last 10 seconds."""
        return self._last_message_time > 0 and time.time() - self._last_message_time < _STALE_THRESHOLD

    def fetch_price_rest(self) -> Optional[float]:
        """Fetch BTC/USDT price via REST as fallback when WebSocket is stale."""
        try:
            with urllib.request.urlopen(_REST_URL, timeout=3) as resp:
                data = json.loads(resp.read())
                return float(data["price"])
        except Exception as e:
            logger.warning(f"Binance REST fallback failed: {e}")
            return None

    def start(self):
        """Start streaming in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="BinanceFeed")
        self._thread.start()
        logger.info("BinancePriceFeed started")

    def stop(self):
        """Signal the feed to stop. The thread will exit on its next reconnect attempt."""
        self._running = False
        logger.info("BinancePriceFeed stopped")

    def _run(self):
        """Main loop: connect, stream, reconnect on failure."""
        from websockets.sync.client import connect

        delay = _RECONNECT_BASE
        while self._running:
            try:
                with connect(_WS_URL, open_timeout=10) as ws:
                    delay = _RECONNECT_BASE  # reset on success
                    logger.info("Binance WS connected")
                    for raw in ws:
                        if not self._running:
                            return
                        data = json.loads(raw)
                        with self._lock:
                            self._price = float(data["p"])
                            self._last_message_time = time.time()
            except Exception as e:
                if not self._running:
                    return
                logger.warning(f"Binance WS error ({e}), reconnecting in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)
