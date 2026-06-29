import subprocess
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Tuple
from .sanitizer import SearchParams

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 8000

class FileMatch:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.lines: Dict[int, str] = {}

    def add_line(self, line_num: int, text: str) -> None:
        self.lines[line_num] = text

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
                
                if rel_path not in file_matches:
                    file_matches[rel_path] = FileMatch(rel_path)
                
                file_matches[rel_path].add_line(line_num, line_text)
        except json.JSONDecodeError:
            continue
            
    results = []
    total_chars = 0
    truncated = False
    
    for rel_path, match in file_matches.items():
        snippet = match.get_snippet()
        snippet_len = len(snippet) + len(rel_path) + 100
        if total_chars + snippet_len > MAX_RESPONSE_CHARS:
            truncated = True
            break
        
        results.append({
            "file": rel_path.replace("\\", "/"),  # Windowsパスの区切り文字を一貫してスラッシュに統一
            "code_snippet": snippet
        })
        total_chars += len(snippet)
        
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
