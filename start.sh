#!/bin/sh
printenv > .env
exec uv run dr_manhattan/mcp/server_sse.py
