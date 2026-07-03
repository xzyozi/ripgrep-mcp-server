#!/bin/bash
# Ripgrep MCP Server 起動スクリプト
echo "Starting Ripgrep MCP Server..."
uv run python -m ripgrep_mcp.server
