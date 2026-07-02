import pytest
from pathlib import Path
from pydantic import ValidationError

from ripgrep_mcp.sanitizer import is_safe_path, SearchParams, is_safe_regex
from ripgrep_mcp.command import build_rg_command, parse_ripgrep_output, FileMatch, find_python_scopes, search_cache, run_search

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

def test_ast_scope_resolution(tmp_path: Path) -> None:
    # テスト用の一時的なPythonファイルを作成
    test_code = (
        "class MyClass:\n"
        "    def method_one(self):\n"
        "        pass\n"
        "\n"
        "    async def async_method(self):\n"
        "        x = 10\n"
        "        return x\n"
        "\n"
        "def global_func():\n"
        "    pass\n"
    )
    test_file = tmp_path / "dummy.py"
    test_file.write_text(test_code, encoding="utf-8")
    
    # 2行目 (method_one)
    scope_2 = find_python_scopes(test_file, [2])
    assert scope_2 == "class MyClass -> def method_one"
    
    # 6行目 (async_method の内部ボディ)
    scope_6 = find_python_scopes(test_file, [6])
    assert scope_6 == "class MyClass -> async def async_method"
    
    # 9行目 (global_func)
    scope_9 = find_python_scopes(test_file, [9])
    assert scope_9 == "def global_func"
    
    # 複数行の複合スコープ
    scope_multi = find_python_scopes(test_file, [2, 6])
    assert scope_multi == "class MyClass -> def method_one, class MyClass -> async def async_method"
    
    # クラス直下の行
    scope_1 = find_python_scopes(test_file, [1])
    assert scope_1 == "class MyClass"

def test_parse_ripgrep_output_with_scope(tmp_path: Path) -> None:
    # `parse_ripgrep_output` にて Pythonファイルのスコープが追加されることを検証
    test_code = (
        "class Controller:\n"
        "    def index(self):\n"
        "        return 'hello'\n"
    )
    # 実際のファイルが必要なので、tmp_path に作成
    (tmp_path / "src").mkdir()
    py_file = tmp_path / "src" / "controller.py"
    py_file.write_text(test_code, encoding="utf-8")
    
    # ripgrep の jsonl 擬似出力
    mock_stdout = (
        f'{{"type":"match","data":{{"path":{{"text":"{py_file.as_posix()}"}},"lines":{{"text":"        return \'hello\'\\\\n"}},"line_number":3}}}}\n'
    )
    
    results, truncated = parse_ripgrep_output(mock_stdout, tmp_path)
    
    assert len(results) == 1
    assert results[0]["file"] == "src/controller.py"
    assert "scope" in results[0]
    assert results[0]["scope"] == "class Controller -> def index"

@pytest.mark.anyio
async def test_search_cache_mechanism(tmp_path: Path) -> None:
    # キャッシュを一度クリア
    search_cache.clear()
    
    # 正常系データ検索用パラメータ
    params = SearchParams(query="hello", target_dir="src")
    
    # テスト対象ファイル作成
    (tmp_path / "src").mkdir(exist_ok=True)
    py_file = tmp_path / "src" / "hello.py"
    py_file.write_text("print('hello')\n", encoding="utf-8")
    
    # 1回目の検索（キャッシュなし、実検索実行）
    res1 = await run_search(params, tmp_path)
    assert res1["status"] == "success"
    assert "cached" not in res1["metadata"]
    
    # 2回目の検索（キャッシュヒットするはず）
    res2 = await run_search(params, tmp_path)
    assert res2["status"] == "success"
    assert res2["metadata"].get("cached") is True
    assert len(res2["results"]) == len(res1["results"])
    
    # キャッシュクリアのテスト
    search_cache.clear()
    res3 = await run_search(params, tmp_path)
    assert res3["status"] == "success"
    assert "cached" not in res3["metadata"]
    
    # TTLを極めて短くしてTTL切れを再現するテスト
    search_cache.clear()
    search_cache.ttl = 0.01  # TTLを非常に短くする
    
    await run_search(params, tmp_path)
    import time
    time.sleep(0.02)  # TTLを超えるのを待つ
    
    res4 = await run_search(params, tmp_path)
    assert res4["status"] == "success"
    assert "cached" not in res4["metadata"]  # キャッシュ切れのため、実スキャンされるはず
    
    # 元のTTL設定に戻す
    search_cache.ttl = 30.0
