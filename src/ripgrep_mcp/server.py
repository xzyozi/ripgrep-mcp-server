import os
import sys
import json
import logging
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from .sanitizer import SearchParams, is_safe_path
from .command import run_search

# ロガー設定 - stdioがMCPプロトコルに使われるため、確実にstderrへ出力する
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("ripgrep-mcp-server")

# 検索対象の起点となるベースディレクトリを設定（環境変数から取得、デフォルトは現在のカレントディレクトリ）
BASE_DIR = Path(os.environ.get("RIPGREP_BASE_DIR", os.getcwd())).resolve()
logger.info(f"Ripgrep MCP Server initialized with base directory: {BASE_DIR}")

# FastMCPインスタンスを作成
mcp = FastMCP("ripgrep-mcp-server")

@mcp.tool(
    name="search_codebase",
    description="リポジトリ内のソースコードを正規表現で検索し、マッチした行と周辺のコンテキストを取得します。関数定義・クラス・特定の変数の使われ方を調査するのに使用してください。大量ヒットが予想される場合は file_extensions や target_dir で対象を絞り込んでください。"
)
def search_codebase(
    query: str,
    target_dir: str,
    context_lines: int = 3,
    file_extensions: list[str] = None,
    max_matches: int = 20
) -> str:
    """
    ソースコードを指定の正規表現クエリで検索します。
    
    :param query: 検索キーワードまたはRust互換の正規表現。（例: 'def my_function', 'class [A-Z]\\w+'）
    :param target_dir: 検索対象のディレクトリパス（相対パス）。リポジトリ全体を検索する場合は '.'
    :param context_lines: マッチ行の前後何行を取得するか。最大20行。
    :param file_extensions: 検索対象ファイルの拡張子リスト（例: ['py', 'ts']）。
    :param max_matches: 取得するマッチの最大件数。最大50。
    """
    if file_extensions is None:
        file_extensions = []
        
    try:
        # パラメータバリデーション
        params = SearchParams(
            query=query,
            target_dir=target_dir,
            context_lines=context_lines,
            file_extensions=file_extensions,
            max_matches=max_matches
        )
    except ValidationError as e:
        logger.error(f"Parameter validation failed: {e}")
        return json.dumps({
            "status": "error",
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "引数のバリデーションに失敗しました。",
                "suggestion": "指定されたパラメータを確認してください。",
                "ripgrep_error": str(e)
            }
        }, ensure_ascii=False)

    # パストラバーサル防止チェック
    if not is_safe_path(BASE_DIR, params.target_dir):
        logger.warning(f"Path traversal attempt blocked: {params.target_dir}")
        return json.dumps({
            "status": "error",
            "error": {
                "code": "INVALID_PATH",
                "message": "無効なディレクトリパスが指定されました（検索対象ルート外）。",
                "suggestion": "対象ルート配下の有効な相対パスを指定してください。",
                "ripgrep_error": None
            }
        }, ensure_ascii=False)

    # 検索実行
    result = run_search(params, BASE_DIR)
    return json.dumps(result, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    mcp.run()
