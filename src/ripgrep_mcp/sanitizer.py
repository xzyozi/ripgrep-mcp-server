import logging
from pathlib import Path
from typing import List, Tuple

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

def is_safe_path(base_dir: str | Path, target_path: str | Path) -> bool:
    """
    検証対象のパス (target_path) がベースディレクトリ (base_dir) の配下に存在するか検証する。
    """
    try:
        base = Path(base_dir).resolve()
        target = Path(target_path)
        if not target.is_absolute():
            target = base / target

        target = target.resolve()

        # targetがbaseと同じか、またはbaseの配下にあれば安全
        return base in target.parents or base == target
    except Exception as e:
        logger.error(f"Path validation error: {e}", exc_info=True)
        return False

def is_safe_regex(pattern: str) -> Tuple[bool, str]:
    """
    正規表現パターンが ReDoS などの脆弱性や高負荷リスクを内包しているか静的に解析する。
    戻り値: (is_safe, error_message)
    """
    # 1. クエリ長のチェック (最大150文字)
    if len(pattern) > 150:
        return False, "正規表現クエリが長すぎます（最大150文字）。"

    chars = list(pattern)
    length = len(chars)

    # グループのネスト状態を保持するスタック
    # 各要素は dict: {"has_quantifier": bool}
    stack = []
    max_depth = 0

    # 隣接するワイルドカード（.* や .+）の連続チェック用
    last_was_dot_star = False

    i = 0
    while i < length:
        c = chars[i]

        # エスケープ文字のスキップ
        if c == '\\':
            i += 2
            last_was_dot_star = False
            continue

        # 文字クラス [...] 内の走査
        if c == '[':
            i += 1
            # 閉じるまでスキップ
            while i < length and chars[i] != ']':
                if chars[i] == '\\':
                    i += 2
                else:
                    i += 1
            i += 1
            last_was_dot_star = False
            continue

        # グループ開始 '('
        if c == '(':
            stack.append({"has_quantifier": False})
            max_depth = max(max_depth, len(stack))
            if max_depth > 4:
                return False, "グループのネストが深すぎます（最大4階層）。"
            i += 1
            continue

        # グループ終了 ')'
        if c == ')':
            if not stack:
                i += 1
                continue

            group_info = stack.pop()

            # 閉じ括弧の直後に量指定子があるか確認
            has_outer_quantifier = False
            next_idx = i + 1
            while next_idx < length and chars[next_idx].isspace():
                next_idx += 1

            if next_idx < length and chars[next_idx] in ('*', '+', '?'):
                has_outer_quantifier = True
                i = next_idx
            elif next_idx < length and chars[next_idx] == '{':
                has_outer_quantifier = True
                i = next_idx
                while i < length and chars[i] != '}':
                    i += 1

            # グループ内部に量指定子があり、かつグループ外部にも量指定子がある場合 (例: (a+)+) -> ReDoSリスク
            if group_info["has_quantifier"] and has_outer_quantifier:
                return False, "ネストされた量指定子 (例: (a+)+) が検出されました。ReDoSの危険性があります。"

            if stack:
                if group_info["has_quantifier"] or has_outer_quantifier:
                    stack[-1]["has_quantifier"] = True

            i += 1
            continue

        # 量指定子そのものの検知
        if c in ('*', '+', '?', '{'):
            if stack:
                stack[-1]["has_quantifier"] = True

        # 曖昧なワイルドカードの連続 (.*.* や .+.* など) の検知
        if c == '.':
            next_idx = i + 1
            if next_idx < length and chars[next_idx] in ('*', '+'):
                if last_was_dot_star:
                    return False, "曖昧なワイルドカードの連続 (例: .*.*) が検出されました。ReDoSの危険性があります。"
                last_was_dot_star = True
                i += 2
                continue

        # スペースや選言(|)以外の文字があれば連続フラグをリセット
        if not c.isspace() and c not in ('|', '(', ')'):
            last_was_dot_star = False

        i += 1

    return True, ""

class SearchParams(BaseModel):
    query: str = Field(..., min_length=1)
    target_dir: str = Field(...)
    context_lines: int = Field(3, ge=0, le=20)
    file_extensions: List[str] = Field(default_factory=list)
    max_matches: int = Field(20, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query cannot be empty or whitespace only")

        # ReDoS・複雑度の静的チェック
        is_safe, error_msg = is_safe_regex(v)
        if not is_safe:
            raise ValueError(error_msg)

        return v
