"""FastAPI bridge for Polymarket scan grading."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="Dr. Manhattan Polymarket Bridge", version="0.1.0")

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


def _grade_scan_with_anthropic(sanitized_scan: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured")

    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-20250514")
    anthropic_version = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "1024"))

    system_prompt = (
        "You grade sanitized Polymarket scan signals for trade quality. "
        "Use only the cleaned payload. Return concise JSON with verdict, score, "
        "confidence, action, thesis, risks, and key_signals."
    )
    user_prompt = (
        "Grade this sanitized Polymarket scan for trade opportunities.\n\n"
        f"Sanitized scan:\n{json.dumps(sanitized_scan, indent=2, sort_keys=True, default=str)}"
    )

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=60,
    )
    response.raise_for_status()

    anthropic_json = response.json()
    text = "".join(
        block.get("text", "")
        for block in anthropic_json.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    return {
        "model": model,
        "text": text,
        "parsed": parsed,
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

    try:
        grade = await asyncio.to_thread(_grade_scan_with_anthropic, sanitized_scan)
    except Exception as exc:
        logger.exception("Polymarket scan grading failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    return {
        "status": "graded",
        "sanitized_scan": sanitized_scan,
        "trade_grade": grade,
    }
