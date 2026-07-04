#!/bin/bash
# Ripgrep MCP Server 起動スクリプト
echo "Starting Ripgrep MCP Server..." >&2 >&2 >&2
export PATH="$HOME/.local/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/uv-env-ripgrep"
uv run python -m ripgrep_mcp.server
