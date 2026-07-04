@echo off
rem Ripgrep MCP Server 起動スクリプト
echo Starting Ripgrep MCP Server... 1>&2
set PATH=%USERPROFILE%\.local\bin;%PATH%
set UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.cache\uv-env-ripgrep
uv run python -m ripgrep_mcp.server

