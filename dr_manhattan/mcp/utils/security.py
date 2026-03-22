import os
import re
from typing import Dict, Any, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Sensitive header names – values for these are NEVER logged or echoed back
# ---------------------------------------------------------------------------
SENSITIVE_HEADERS: List[str] = [
    "authorization",
    "x-api-key",
    "x-api-secret",
    "x-api-passphrase",
    "builder-api-key",
    "builder-secret",
    "builder-pass-phrase",
    "polymarket-private-key",
    "polymarket-funder",
]

# ---------------------------------------------------------------------------
# Write-operation access control
# ---------------------------------------------------------------------------

# Only these exchanges are allowed to perform write operations via the SSE server.
SSE_WRITE_ENABLED_EXCHANGES: List[str] = ["polymarket"]

# Tool names that are considered write (state-changing) operations.
WRITE_OPERATIONS: List[str] = [
    "create_order",
    "cancel_order",
    "cancel_all_orders",
]

# ---------------------------------------------------------------------------
# Credential extraction
# ---------------------------------------------------------------------------

# Maps HTTP header names to the credential dict keys that exchange_manager
# reads via exchange_creds.get(k).  The Builder credential keys MUST be
# 'api_key', 'api_secret', 'api_passphrase' to match the has_builder_creds
# check in exchange_manager.get_exchange():
#   has_builder_creds = all(
#       exchange_creds.get(k) for k in ("api_key", "api_secret", "api_passphrase")
#   )
HEADER_CREDENTIAL_MAP = {
    "polymarket": {
        "x-polymarket-api-key": "api_key",
        # NOTE: key name is 'api_secret' (not 'secret') to match exchange_manager
        "x-polymarket-secret": "api_secret",
        # NOTE: key name is 'api_passphrase' (not 'passphrase') to match exchange_manager
        "x-polymarket-passphrase": "api_passphrase",
        "x-polymarket-funder": "funder",
        "x-polymarket-private-key": "private_key",
        # Operator mode
        "x-polymarket-user-address": "user_address",
    }
}


def get_credentials_from_headers(headers: Optional[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Extract exchange credentials from request headers.

    Always returns a dict – never None – so callers can safely iterate over
    the result (e.g. ``if exchange in credentials``) without a prior None-check.
    """
    # Guard: treat a None headers argument as an empty dict so the rest of
    # the function never tries to iterate over None.
    if not headers:
        return {}

    normalized_headers = {k.lower(): v for k, v in headers.items()}
    all_credentials: Dict[str, Dict[str, Any]] = {}

    for exchange, header_map in HEADER_CREDENTIAL_MAP.items():
        # Guard: exchange_creds is always initialised as a dict – never None.
        exchange_creds: Dict[str, Any] = {}

        if header_map:
            for header_name, cred_key in header_map.items():
                value = normalized_headers.get(header_name.lower())
                if value:
                    exchange_creds[cred_key] = value

        if exchange == "polymarket":
            fallbacks = {
                "api_key": os.environ.get("BUILDER_API_KEY"),
                "api_secret": os.environ.get("BUILDER_SECRET"),
                "api_passphrase": os.environ.get("BUILDER_PASS_PHRASE"),
                "private_key": os.environ.get("POLYMARKET_PRIVATE_KEY"),
                "funder": os.environ.get("POLYMARKET_FUNDER"),
            }
            # Defensive guard: ensure exchange_creds is always a dict before
            # iterating – fixes 'NoneType is not iterable' if a prior code path
            # ever sets it to None.
            exchange_creds = exchange_creds or {}
            for cred_key, value in fallbacks.items():
                if value and cred_key not in exchange_creds:
                    exchange_creds[cred_key] = value

        if exchange_creds:
            all_credentials[exchange] = exchange_creds

    return all_credentials


# ---------------------------------------------------------------------------
# Credential validation helpers
# ---------------------------------------------------------------------------


def has_any_credentials(credentials: Optional[Dict[str, Dict[str, Any]]]) -> bool:
    """Return True if the credentials dict contains at least one exchange entry."""
    if not credentials:
        return False
    return any(bool(v) for v in credentials.values())


def validate_credentials_present(
    credentials: Optional[Dict[str, Dict[str, Any]]],
    exchange: str,
) -> None:
    """Raise ValueError if credentials for *exchange* are absent or empty."""
    if not credentials or not credentials.get(exchange):
        raise ValueError(
            f"No credentials found for exchange '{exchange}'. "
            "Pass the required headers (e.g. X-Polymarket-Api-Key) with your request."
        )


def validate_operator_credentials(
    credentials: Dict[str, Any],
) -> Tuple[bool, str]:
    """Validate that operator-mode credentials contain a user_address.

    Called by exchange_manager.get_exchange() when operator mode is detected
    (user_address present, no private_key, no builder creds).

    Args:
        credentials: Per-exchange credential dict extracted from request headers.

    Returns:
        (is_valid, error_message) – error_message is empty string when valid.
    """
    # Guard: credentials must be a dict – never None – before any key checks
    credentials = credentials or {}
    if not credentials:
        return False, "No credentials provided for operator mode."

    user_address = credentials.get("user_address")
    if not user_address:
        return False, (
            "Operator mode requires 'user_address'. "
            "Pass X-Polymarket-User-Address header with the wallet address to trade for."
        )

    # Basic sanity check: must look like an Ethereum address
    if not isinstance(user_address, str) or not user_address.startswith("0x"):
        return False, (
            f"Invalid user_address '{user_address}': must be a 0x-prefixed Ethereum address."
        )

    return True, ""


# ---------------------------------------------------------------------------
# Write-operation validation
# ---------------------------------------------------------------------------


def is_write_operation(tool_name: str) -> bool:
    """Return True if *tool_name* is a state-changing (write) operation."""
    return tool_name in WRITE_OPERATIONS


def validate_write_operation(
    tool_name: str,
    exchange: Optional[str],
) -> Tuple[bool, str]:
    """
    Check whether a write operation is permitted for the given exchange.

    Returns:
        (is_allowed, error_message) – error_message is empty when allowed.
    """
    if not is_write_operation(tool_name):
        return True, ""

    if exchange is None:
        return False, (
            f"Tool '{tool_name}' is a write operation but no exchange was specified."
        )

    if exchange.lower() not in SSE_WRITE_ENABLED_EXCHANGES:
        return False, (
            f"Write operations are not permitted for exchange '{exchange}' via the SSE server. "
            f"Only {SSE_WRITE_ENABLED_EXCHANGES} support write operations."
        )

    return True, ""


# ---------------------------------------------------------------------------
# Logging / sanitization helpers
# ---------------------------------------------------------------------------

_SENSITIVE_HEADER_SET = {h.lower() for h in SENSITIVE_HEADERS}

# Patterns that look like secrets even when not in a known header
_SECRET_PATTERNS = [
    re.compile(r"(0x[a-fA-F0-9]{40,})", re.IGNORECASE),   # private keys / addresses
    re.compile(r"([A-Za-z0-9+/]{40,}={0,2})", re.IGNORECASE),  # base64 blobs
]


def sanitize_headers_for_logging(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of *headers* with sensitive values replaced by '[REDACTED]'."""
    sanitized: Dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADER_SET:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


def sanitize_error_message(message: str) -> str:
    """Strip potential secret material from an error message string."""
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    return message
