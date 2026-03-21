#!/bin/sh
printenv > .env
exec uv run drmanhattan/mcp/server_sse.py
