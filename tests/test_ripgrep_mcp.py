import pytest
from pathlib import Path
from pydantic import ValidationError

from ripgrep_mcp.sanitizer import is_safe_path, SearchParams, is_safe_regex
from ripgrep_mcp.command import build_rg_command, parse_ripgrep_output, FileMatch

def test_is_safe_path() -> None:
    base = Path("/workspace/project").resolve()
    
    # 正常系 (同値、配下、相対パス)
    assert is_safe_path(base, "src") is True
    assert is_safe_path(base, "src/api/user.py") is True
    assert is_safe_path(base, ".") is True
    
    # 異常系 (親ディレクトリへのトラバーサル)
    assert is_safe_path(base, "../") is False
    assert is_safe_path(base, "../../etc/passwd") is False
    assert is_safe_path(base, "/etc/passwd") is False

def test_search_params_validation() -> None:
    # 正常系
    params = SearchParams(query="def test", target_dir="src")
    assert params.query == "def test"
    assert params.context_lines == 3
    assert params.max_matches == 20
    
    # 異常系: 空のクエリ
    with pytest.raises(ValidationError):
        SearchParams(query="  ", target_dir="src")
        
    # 異常系: コンテキスト行数が上限超過
    with pytest.raises(ValidationError):
        SearchParams(query="def", target_dir="src", context_lines=25)

def test_build_rg_command() -> None:
    params = SearchParams(
        query="test_query",
        target_dir="src",
        context_lines=5,
        file_extensions=["py", ".ts"],
        max_matches=10
    )
    cmd = build_rg_command(params, "/workspace/project/src")
    
    # 必須引数が含まれているか
    assert "rg" in cmd
    assert "--json" in cmd
    assert "-C" in cmd
    assert "5" in cmd
    assert "-m" in cmd
    assert "10" in cmd
    
    # 拡張子指定
    assert "-g" in cmd
    assert "*.py" in cmd
    assert "*.ts" in cmd
    
    # クエリとターゲット
    assert "test_query" in cmd
    assert "/workspace/project/src" in cmd

def test_file_match_class() -> None:
    match = FileMatch("src/api.py")
    match.add_line(10, "def api_call():\n")
    match.add_line(11, "    pass\n")
    match.add_line(15, "# end of file\n")
    
    snippet = match.get_snippet()
    
    # 行番号と内容が含まれているか
    assert "  10: def api_call():\n" in snippet
    assert "  11:     pass\n" in snippet
    # 離れた行の間に区切り文字が入っているか
    assert " --\n" in snippet
    assert "  15: # end of file\n" in snippet

def test_parse_ripgrep_output() -> None:
    # ripgrepの擬似出力
    mock_stdout = (
        '{"type":"match","data":{"path":{"text":"/workspace/src/main.py"},"lines":{"text":"def main():\\n"},"line_number":1}}\n'
        '{"type":"context","data":{"path":{"text":"/workspace/src/main.py"},"lines":{"text":"    print(\\"hello\\")\\n"},"line_number":2}}\n'
    )
    
    results, truncated = parse_ripgrep_output(mock_stdout, Path("/workspace"))
    
    assert len(results) == 1
    assert results[0]["file"] == "src/main.py"
    assert "   1: def main():\n" in results[0]["code_snippet"]
    assert "   2:     print(\"hello\")\n" in results[0]["code_snippet"]
    assert truncated is False

def test_is_safe_regex() -> None:
    # 正常系 (安全なクエリ)
    assert is_safe_regex("def test")[0] is True
    assert is_safe_regex("class [A-Z]\\w+")[0] is True
    assert is_safe_regex(".*")[0] is True
    assert is_safe_regex("^[0-9]+$")[0] is True
    assert is_safe_regex(r"\(escaped_parens\)*")[0] is True
    assert is_safe_regex(r"\.\*")[0] is True
    
    # 異常系 (ネストされた量指定子)
    assert is_safe_regex("(a+)+")[0] is False
    assert is_safe_regex("(a*)*")[0] is False
    assert is_safe_regex("(a?)+")[0] is False
    assert is_safe_regex("(a{1,2})*")[0] is False
    assert is_safe_regex("((a+)+)")[0] is False
    
    # 異常系 (曖昧なワイルドカードの連続)
    assert is_safe_regex(".*.*")[0] is False
    assert is_safe_regex(".+.*")[0] is False
    assert is_safe_regex(".*.+")[0] is False
    assert is_safe_regex(".*  .*")[0] is False
    assert is_safe_regex(".*|.*")[0] is False
    
    # 異常系 (グループのネストが深すぎる)
    assert is_safe_regex("(((((a)))))")[0] is False
    
    # 異常系 (クエリ長制限)
    long_query = "a" * 151
    assert is_safe_regex(long_query)[0] is False

def test_search_params_regex_validation() -> None:
    # 正常な正規表現は ValidationError を起こさない
    params = SearchParams(query="[a-z]+", target_dir="src")
    assert params.query == "[a-z]+"
    
    # 危険な正規表現は ValidationError を起こす
    with pytest.raises(ValidationError) as exc_info:
        SearchParams(query="(a+)+", target_dir="src")
    assert "ReDoS" in str(exc_info.value)
    
    with pytest.raises(ValidationError) as exc_info:
        SearchParams(query=".*.*", target_dir="src")
    assert "ReDoS" in str(exc_info.value)
