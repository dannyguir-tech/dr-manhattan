"""FastAPI bridge for Polymarket scan forwarding."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="Dr. Manhattan Polymarket Bridge", version="0.2.0")

SENSITIVE_SCAN_KEYS = (
    "wallet",
    "balance",
    "key",
    "secret",
    "private",
    "passphrase",
    "seed",
    "mnemonic",
)
PRIVATE_KEY_RE = re.compile(r"0x[a-fA-F0-9]{64}")
WALLET_ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
POKE_SCAN_LOG_PREFIX = "POKE_POLYMARKET_SCAN"


def _redact_string(value: str) -> str:
    value = PRIVATE_KEY_RE.sub("[redacted-private-key]", value)
    value = WALLET_ADDRESS_RE.sub("[redacted-wallet-address]", value)
    return value


def sanitize_scan_payload(value: Any) -> Any:
    """Recursively remove sensitive wallet, balance, and key material."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_name = str(key)
            key_lower = key_name.lower()
            if any(term in key_lower for term in SENSITIVE_SCAN_KEYS):
                continue
            sanitized[key_name] = sanitize_scan_payload(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_scan_payload(item) for item in value]

    if isinstance(value, tuple):
        return [sanitize_scan_payload(item) for item in value]

    if isinstance(value, str):
        return _redact_string(value)

    return value


def _publish_scan_to_poke(sanitized_scan: dict[str, Any]) -> dict[str, Any]:
    """Emit the sanitized scan in a structured log line retrievable by agents."""
    payload = {
        "event": "polymarket_scan",
        "source": "dr-manhattan",
        "sanitized_scan": sanitized_scan,
    }
    logger.info("%s %s", POKE_SCAN_LOG_PREFIX, json.dumps(payload, sort_keys=True, default=str))
    return {
        "delivery": "logged",
        "event": "polymarket_scan",
        "log_prefix": POKE_SCAN_LOG_PREFIX,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "dr-manhattan-bridge"}


@app.post("/api/polymarket-scan")
async def polymarket_scan(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

    sanitized_scan = sanitize_scan_payload(payload)
    delivery = _publish_scan_to_poke(sanitized_scan)

    return {
        "status": "forwarded",
        "sanitized_scan": sanitized_scan,
        "delivery": delivery,
    }
