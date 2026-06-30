import subprocess
import json
import logging
import ast
from pathlib import Path
from typing import Dict, List, Any, Tuple
from .sanitizer import SearchParams

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 8000

class ScopeFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scopes: Dict[int, str] = {}  # line_number -> scope path string
        self.current_path: List[str] = []

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

def find_python_scopes(file_path: Path, line_numbers: List[int]) -> str:
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
    except Exception as e:
        logger.debug(f"Failed to parse AST for {file_path}: {e}")
        
    return ""

class FileMatch:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.lines: Dict[int, str] = {}
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

def build_rg_command(params: SearchParams, target_path: str) -> List[str]:
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

def execute_ripgrep(cmd: List[str], timeout: float = 3.0) -> Tuple[int, str, str]:
    """
    ripgrepプロセスを安全に実行する。シェルを経由しないことでコマンドインジェクションを防御。
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("ripgrep command execution timed out.")
        return -1, "", "TIMEOUT"
    except FileNotFoundError:
        logger.error("ripgrep (rg) command was not found on the system.")
        return -2, "", "RIPGREP_NOT_FOUND"
    except Exception as e:
        logger.error(f"Unexpected error running ripgrep: {e}", exc_info=True)
        return -99, "", str(e)

def parse_ripgrep_output(stdout: str, base_dir: Path) -> Tuple[List[Dict[str, Any]], bool]:
    """
    ripgrepのJSONL出力をパースし、ファイルごとに行番号付きでコードスニペットをまとめる。
    """
    file_matches: Dict[str, FileMatch] = {}
    
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

def run_search(params: SearchParams, base_dir: Path) -> Dict[str, Any]:
    """
    サニタイズされたパラメータを受け取り、検索を実行してパースした結果を返す。
    """
    target_path = str((base_dir / params.target_dir).resolve())
    cmd = build_rg_command(params, target_path)
    
    returncode, stdout, stderr = execute_ripgrep(cmd)
    
    if returncode == 0:
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
    elif returncode == 1:
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
    elif returncode == -1:
        return {
            "status": "error",
            "error": {
                "code": "TIMEOUT",
                "message": "検索クエリが複雑すぎるか、検索範囲が広すぎます。",
                "suggestion": "正規表現をシンプルにするか、ディレクトリを絞り込んでください。",
                "ripgrep_error": None
            }
        }
    elif returncode == -2:
        return {
            "status": "error",
            "error": {
                "code": "RIPGREP_NOT_FOUND",
                "message": "システムにripgrep (rg) がインストールされていません。",
                "suggestion": "ホスト環境にripgrepをインストールしてください。",
                "ripgrep_error": None
            }
        }
    else:
        # その他のエラー（正規表現構文エラーなど）
        # ripgrepのエラー内容はstderrに出力されるため、それをLLMへ返す
        ripgrep_err = stderr.strip() if stderr else "Unknown error"
        code = "REGEX_ERROR" if "regex" in ripgrep_err.lower() or returncode == 2 else "INTERNAL_ERROR"
        return {
            "status": "error",
            "error": {
                "code": code,
                "message": "検索中にエラーが発生しました。" if code == "INTERNAL_ERROR" else "正規表現の構文エラーです。",
                "suggestion": "正規表現を修正するか、設定を確認してください。",
                "ripgrep_error": ripgrep_err
            }
        }
