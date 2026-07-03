import os
import json
import logging
import asyncio
import ast
import time
from pathlib import Path
from typing import Any
from .sanitizer import SearchParams

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 8000

class CacheEntry:
    def __init__(self, timestamp: float, response: dict[str, Any]) -> None:
        self.timestamp = timestamp
        self.response = response

class SearchCache:
    def __init__(self, ttl: float = 30.0, max_size: int = 100) -> None:
        self.ttl = ttl
        self.max_size = max_size
        self._cache: dict[tuple[Any, ...], CacheEntry] = {}

    def get(self, params: SearchParams) -> dict[str, Any] | None:
        key = (
            params.query,
            params.target_dir,
            params.context_lines,
            tuple(params.file_extensions),
            params.max_matches
        )
        entry = self._cache.get(key)
        if entry:
            if time.time() - entry.timestamp < self.ttl:
                return entry.response
            del self._cache[key]  # TTL切れ
        return None

    def set(self, params: SearchParams, response: dict[str, Any]) -> None:
        # キャッシュが上限に達したら最古のキー（辞書の先頭）を削除
        if len(self._cache) >= self.max_size:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]

        key = (
            params.query,
            params.target_dir,
            params.context_lines,
            tuple(params.file_extensions),
            params.max_matches
        )
        self._cache[key] = CacheEntry(time.time(), response)

    def clear(self) -> None:
        self._cache.clear()

search_cache = SearchCache(ttl=30.0)

class ScopeFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scopes: dict[int, str] = {}  # line_number -> scope path string
        self.current_path: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.current_path.append(f"class {node.name}")
        self._record_scope(node)
        self.generic_visit(node)
        self.current_path.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.current_path.append(f"def {node.name}")
        self._record_scope(node)
        self.generic_visit(node)
        self.current_path.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.current_path.append(f"async def {node.name}")
        self._record_scope(node)
        self.generic_visit(node)
        self.current_path.pop()

    def _record_scope(self, node: ast.AST) -> None:
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        if end is None:
            end = start
        
        path = " -> ".join(self.current_path)
        for line in range(start, end + 1):
            # より深いネスト構造のスコープを優先して上書き
            self.scopes[line] = path

def find_python_scopes(file_path: Path, line_numbers: list[int]) -> str:
    """
    Pythonファイルから指定された行番号のスコープ情報を取得する。
    """
    if not file_path.exists() or file_path.suffix != ".py":
        return ""
        
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=str(file_path))
        finder = ScopeFinder()
        finder.visit(tree)
        
        matched_scopes = []
        for line in line_numbers:
            scope = finder.scopes.get(line)
            if scope and scope not in matched_scopes:
                matched_scopes.append(scope)
                
        if matched_scopes:
            return ", ".join(matched_scopes)
    except (SyntaxError, ValueError) as e:
        logger.debug(f"Failed to parse AST for {file_path}: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error reading/parsing {file_path}: {e}")
        
    return ""

class FileMatch:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.lines: dict[int, str] = {}
        self.match_lines: set[int] = set()

    def add_line(self, line_num: int, text: str, is_match: bool = False) -> None:
        self.lines[line_num] = text
        if is_match:
            self.match_lines.add(line_num)

    def get_snippet(self) -> str:
        sorted_lines = sorted(self.lines.items())
        result = []
        last_num = None
        for num, text in sorted_lines:
            # 連続しない行の間に区切りを挿入
            if last_num is not None and num > last_num + 1:
                result.append(" --\n")
            # 末尾に改行がない場合は補完
            clean_text = text if text.endswith('\n') else text + '\n'
            result.append(f"{num:4d}: {clean_text}")
            last_num = num
        return "".join(result)

def build_rg_command(params: SearchParams, target_path: str) -> list[str]:
    """
    ripgrepのコマンドライン引数リストを安全に組み立てる。
    """
    cmd = [
        "rg",
        "--json",
        "-C", str(params.context_lines),
        "-m", str(params.max_matches),
        "--max-filesize", "10M"
    ]
    for ext in params.file_extensions:
        clean_ext = ext.lstrip('.')
        cmd.extend(["-g", f"*.{clean_ext}"])
        
    cmd.extend(["--", params.query, target_path])
    return cmd

async def execute_ripgrep(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    """
    ripgrepプロセスを安全に非同期実行する。シェルを経由しないことでコマンドインジェクションを防御。
    """
    env = os.environ.copy()
    try:
        # プロセス非同期生成 (Windows環境等のWinsockエラーを防ぐため環境変数を渡す)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        try:
            # wait_for を使用してタイムアウト制御しながら実行
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
            returncode = process.returncode if process.returncode is not None else -1
            return (
                returncode,
                stdout_bytes.decode('utf-8', errors='replace'),
                stderr_bytes.decode('utf-8', errors='replace')
            )
        except asyncio.TimeoutError:
            logger.error("ripgrep command execution timed out.")
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            return -1, "", "TIMEOUT"
            
    except OSError as e:
        logger.error(f"ripgrep (rg) command execution failed: {e}")
        return -2, "", "RIPGREP_EXEC_ERROR"
    except Exception as e:
        logger.error(f"Unexpected error executing ripgrep: {e}", exc_info=True)
        return -99, "", str(e)

def parse_ripgrep_output(stdout: str, base_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    """
    ripgrepのJSONL出力をパースし、ファイルごとに行番号付きでコードスニペットをまとめる。
    """
    file_matches: dict[str, FileMatch] = {}
    
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            dtype = data.get("type")
            if dtype in ("match", "context"):
                payload = data.get("data", {})
                raw_path = payload.get("path", {}).get("text", "")
                if not raw_path:
                    continue
                
                try:
                    rel_path = str(Path(raw_path).relative_to(base_dir))
                except ValueError:
                    rel_path = raw_path
                
                line_num = payload.get("line_number")
                line_text = payload.get("lines", {}).get("text", "")
                is_match = (dtype == "match")
                
                if rel_path not in file_matches:
                    file_matches[rel_path] = FileMatch(rel_path)
                
                file_matches[rel_path].add_line(line_num, line_text, is_match)
        except json.JSONDecodeError:
            continue
            
    results = []
    total_chars = 0
    truncated = False
    
    for rel_path, match in file_matches.items():
        snippet = match.get_snippet()
        
        # ASTからスコープ情報を解決 (Pythonのみ)
        scope = ""
        if rel_path.endswith(".py"):
            abs_path = base_dir / rel_path
            scope = find_python_scopes(abs_path, sorted(list(match.match_lines)))
            
        snippet_len = len(snippet) + len(rel_path) + len(scope) + 100
        if total_chars + snippet_len > MAX_RESPONSE_CHARS:
            truncated = True
            break
        
        item = {
            "file": rel_path.replace("\\", "/"),  # Windowsパスの区切り文字を一貫してスラッシュに統一
            "code_snippet": snippet
        }
        if scope:
            item["scope"] = scope
            
        results.append(item)
        total_chars += len(snippet) + len(scope)
        
    return results, truncated

def build_search_response(
    returncode: int,
    stdout: str,
    stderr: str,
    params: SearchParams,
    base_dir: Path
) -> dict[str, Any]:
    """
    ripgrepの実行結果（リターンコード）から、MCPサーバー用の標準的なレスポンス構造を組み立てる。
    """
    match returncode:
        case 0:
            results, truncated = parse_ripgrep_output(stdout, base_dir)
            return {
                "status": "success",
                "metadata": {
                    "query": params.query,
                    "target_dir": params.target_dir,
                    "truncated": truncated
                },
                "results": results
            }
        case 1:
            # マッチなし
            return {
                "status": "success",
                "metadata": {
                    "query": params.query,
                    "target_dir": params.target_dir,
                    "truncated": False
                },
                "results": []
            }
        case -1:
            return {
                "status": "error",
                "error": {
                    "code": "TIMEOUT",
                    "message": "検索クエリが複雑すぎるか、検索範囲が広すぎます。",
                    "suggestion": "正規表現をシンプルにするか、ディレクトリを絞り込んでください。",
                    "ripgrep_error": None
                }
            }
        case -2:
            return {
                "status": "error",
                "error": {
                    "code": "RIPGREP_EXEC_ERROR",
                    "message": "ripgrepの実行ファイルが見つからないか、実行権限がありません。",
                    "suggestion": "システム環境にripgrepが正しくインストールされ、実行可能パスが通っていることを確認してください。",
                    "ripgrep_error": stderr.strip() if stderr else None
                }
            }
        case _:
            # その他のエラー（正規表現構文エラー、INTERNAL_ERROR など）
            ripgrep_err = stderr.strip() if stderr else "Unknown error"
            code = "REGEX_ERROR" if "regex" in ripgrep_err.lower() or returncode == 2 else "INTERNAL_ERROR"
            message = "検索中にエラーが発生しました。" if code == "INTERNAL_ERROR" else "正規表現の構文エラーです。"
            return {
                "status": "error",
                "error": {
                    "code": code,
                    "message": message,
                    "suggestion": "正規表現を修正するか、設定を確認してください。",
                    "ripgrep_error": ripgrep_err
                }
            }

async def run_search(params: SearchParams, base_dir: Path) -> dict[str, Any]:
    """
    サニタイズされたパラメータを受け取り、検索を実行してパースした結果を返す。
    """
    # キャッシュの確認
    cached_result = search_cache.get(params)
    if cached_result is not None:
        # キャッシュヒット時はメタデータに "cached": True を追加
        response = cached_result.copy()
        if "metadata" in response:
            response["metadata"] = {**response["metadata"], "cached": True}
        return response

    resolved_base = base_dir.resolve()
    target_path_obj = (resolved_base / params.target_dir).resolve()
    
    # セキュリティチェック: ターゲットがbase_dir配下にあることを確認 (多層防御)
    if not target_path_obj.is_relative_to(resolved_base):
        return {
            "status": "error",
            "error": {
                "code": "INVALID_PATH",
                "message": "指定された検索パスが許可されたディレクトリの外部を指しています。",
                "suggestion": "対象ルート配下の有効な相対パスを指定してください。",
                "ripgrep_error": None
            }
        }

    cmd = build_rg_command(params, str(target_path_obj))
    
    returncode, stdout, stderr = await execute_ripgrep(cmd)
    
    response = build_search_response(returncode, stdout, stderr, params, base_dir)

    # 正常系レスポンスのみキャッシュに保存する
    if response.get("status") == "success":
        search_cache.set(params, response)

    return response
