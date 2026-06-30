import sys
import asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
import logging
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from typing import Optional

from .sanitizer import SearchParams, is_safe_path
from .command import run_search

# ロガー設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("ripgrep-mcp-server")

BASE_DIR = Path(os.environ.get("RIPGREP_BASE_DIR", os.getcwd())).resolve()
logger.info(f"Ripgrep MCP Server initialized with base directory: {BASE_DIR}")

mcp = FastMCP("ripgrep-mcp-server")

def format_to_markdown(result_dict: dict) -> str:
    """
    内部のJSON辞書を、ローカルLLMが読みやすいマークダウン（プレーンテキスト）に変換するラッパー関数
    """
    if result_dict.get("status") == "error":
        err = result_dict.get("error", {})
        return f"【検索エラー】\n理由: {err.get('message')}\n推奨: {err.get('suggestion')}\n詳細: {err.get('ripgrep_error')}"

    results = result_dict.get("results", [])
    if not results:
        return "マッチする検索結果が見つかりませんでした。ディレクトリやクエリを変えて再試行してください。"

    md_lines = []
    meta = result_dict.get("metadata", {})
    if meta.get("cached"):
        md_lines.append("> ⚡ Cached Result")

    for item in results:
        scope_info = f" [Scope: {item['scope']}]" if "scope" in item else ""
        md_lines.append(f"--- {item['file']}{scope_info} ---")
        md_lines.append(item["code_snippet"])

    if meta.get("truncated"):
        md_lines.append("\n[システム通知: トークン保護のため検索結果が途中で切り捨てられました。必要に応じて target_dir や拡張子を絞ってください。]")

    return "\n".join(md_lines)

@mcp.tool(
    name="search_codebase",
    description="リポジトリ内のソースコードを正規表現で検索し、マッチした行と周辺のコンテキストを取得します。関数定義やクラスを調査するのに使用してください。"
)
def search_codebase(
    query: str,
    target_dir: str,
    token_budget: Optional[int] = None
) -> str:
    """
    ソースコードを指定の正規表現クエリで検索します。
    
    :param query: 検索キーワードまたはRust互換の正規表現。（例: 'def my_function', 'class [A-Z]\\w+'）
    :param target_dir: 検索対象のディレクトリパス（相対パス）。リポジトリ全体を検索する場合は '.'
    :param token_budget: (任意) 今回の検索結果に割り当てる最大トークン数。デフォルトは環境設定に従います。
    """
    # 1. 内部パラメータの自動補完 (LLMに推論させず、システムでよしなに決定する)
    file_extensions = []
    context_lines = 3
    max_matches = 20

    # token_budget が指定された場合、それをベースに文字数上限を算出 (3文字 = 1トークン)
    dynamic_char_limit = (token_budget * 3) if token_budget else 8000

    try:
        params = SearchParams(
            query=query,
            target_dir=target_dir,
            context_lines=context_lines,
            file_extensions=file_extensions,
            max_matches=max_matches
        )
    except ValidationError as e:
        logger.error(f"Parameter validation failed: {e}")
        return f"【バリデーションエラー】\n引数が不正です。正規表現が複雑すぎるか、空のクエリです。\n詳細: {e}"

    if not is_safe_path(BASE_DIR, params.target_dir):
        return "【アクセス拒否】\n無効なディレクトリパスが指定されました。対象ルート配下の有効な相対パスを指定してください。"

    # 2. コアロジックの実行
    raw_result = run_search(params, BASE_DIR)
    
    # 3. マークダウンへのパースと返却
    md_result = format_to_markdown(raw_result)
    
    # 動的なトークン予算による切り詰め
    if len(md_result) > dynamic_char_limit:
        md_result = md_result[:dynamic_char_limit] + "\n\n[システム通知: トークン予算上限に達したため検索結果が途中で切り捨てられました]"
        
    return md_result

if __name__ == "__main__":
    mcp.run()
