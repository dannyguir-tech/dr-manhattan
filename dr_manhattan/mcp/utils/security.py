import os
from typing import Dict, Any

HEADER_CREDENTIAL_MAP = {
    "polymarket": {
        "x-polymarket-api-key": "api_key",
        "x-polymarket-secret": "secret",
        "x-polymarket-passphrase": "passphrase",
        "x-polymarket-funder": "funder",
        "x-polymarket-private-key": "private_key"
    }
}

def get_credentials_from_headers(headers: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    all_credentials = {}

    for exchange, header_map in HEADER_CREDENTIAL_MAP.items():
        exchange_creds = {}

        if header_map:
            for header_name, cred_key in header_map.items():
                value = normalized_headers.get(header_name.lower())
                if value:
                    exchange_creds[cred_key] = value

        if exchange == "polymarket":
            fallbacks = {
                "api_key": os.environ.get("BUILDER_API_KEY"),
                "secret": os.environ.get("BUILDER_SECRET"),
                "passphrase": os.environ.get("BUILDER_PASS_PHRASE"),
                "private_key": os.environ.get("POLYMARKET_PRIVATE_KEY"),
                "funder": os.environ.get("POLYMARKET_FUNDER")
            }
            for cred_key, value in fallbacks.items():
                if value and cred_key not in exchange_creds:
                    exchange_creds[cred_key] = value

        if exchange_creds:
            all_credentials[exchange] = exchange_creds

    return all_credentials
