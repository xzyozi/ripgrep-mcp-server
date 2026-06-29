# LLMコード解析向け ネイティブGrepエンジン — モジュール設計書

**バージョン:** 2.0.0
**ステータス:** ドラフト完成
**対象読者:** バックエンドエンジニア、MLエンジニア、DevOpsエンジニア

---

## 目次

1. [概要](#1-概要)
2. [モジュール構成](#2-モジュール構成)
3. [システムアーキテクチャ](#3-システムアーキテクチャ)
4. [LLMコード解析特有の要件](#4-llmコード解析特有の要件)
5. [MCP Tool定義](#5-mcp-tool定義)
6. [セキュリティ設計（ネイティブ実行）](#6-セキュリティ設計ネイティブ実行)
7. [MCPサーバー設計](#7-mcpサーバー設計)
8. [インターフェース仕様](#8-インターフェース仕様)
9. [エラーハンドリング & エージェントリカバリ](#9-エラーハンドリング--エージェントリカバリ)
10. [セキュリティチェックリスト](#10-セキュリティチェックリスト)
11. [依存関係・前提条件](#11-依存関係前提条件)
12. [今後の拡張方針](#12-今後の拡張方針)

---

## 1. 概要

### 1.1 モジュールの目的

本モジュール（`ripgrep-mcp-server`）は、LLMエージェント（RAGパイプラインおよび自律コーディングエージェント）が外部ソースコードやユーザーが提供したリポジトリを解析する際に用いる、**高速かつ安全なコード検索MCPサーバー**です。

WSL（Windows Subsystem for Linux）などのハードウェア制限環境におけるコンテナ起動オーバーヘッドを排除し、パフォーマンスと応答速度を極大化するため、Dockerコンテナによる隔離を行わず、ホスト環境にインストールされた [ripgrep](https://github.com/BurntSushi/ripgrep) をネイティブプロセスとして安全に呼び出す設計を採用します。

### 1.2 設計原則

| 原則 | 内容 |
|------|------|
| **セキュリティ徹底** | プロセスレベルでパストラバーサルとコマンドインジェクションを完全に防御する |
| **トークン効率** | LLMのコンテキストウィンドウ消費を最小限に抑えるため、出力制限とメタデータを最適化する |
| **自律リカバリ** | エラーや切り詰め（Truncation）が発生した際、LLM自身が理解して修正行動を取れる形式で返却する |
| **ネイティブ高速性** | stdio通信とネイティブプロセスの組み合わせにより、ミリ秒単位の超高速な検索応答を実現する |
| **可観測性** | プロセスのログは標準エラー出力（stderr）を介して出力し、MCPプロトコルの通信（stdout）と完全に分離する |

---

## 2. モジュール構成

移行および配置を容易にするため、すべてのコードを `src/ripgrep_mcp/` ディレクトリ配下に集約します。

```
ripgrep-mcp-server/
│
├── README.md                      # プロジェクト概要・クイックスタート
├── docs/
│   └── design/
│       └── base_design.md         # 本設計書（このファイル）
│
├── src/
│   └── ripgrep_mcp/
│       ├── __init__.py            # パッケージ初期化
│       ├── server.py              # MCPサーバー（FastMCP）
│       ├── sanitizer.py           # パストラバーサル・入力値サニタイズ
│       └── command.py             # ripgrepプロセス実行・結果パース
│
├── tests/                         # テストスイート
│   ├── __init__.py
│   └── test_ripgrep_mcp.py        # 各種バリデーター・パーサーのテスト
│
├── mypy.ini                       # Mypy設定
├── pyproject.toml                 # パッケージ定義・依存関係
└── ruff.toml                      # Ruff設定
```

---

## 3. システムアーキテクチャ

### 3.1 全体フロー

```
┌─────────────────────────────────────────────────────────────────┐
│  LLM Agent (Claude, Desktop App, etc.)                          │
│                                                                 │
│  1. 検索意図の決定                                                │
│     例: "get_user関数の定義を探したい"                             │
│         → query: "def get_user", target_dir: "src", ...        │
└────────────────────────┬────────────────────────────────────────┘
                         │ MCP Tool Call (stdio via JSON-RPC)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  MCP Server  (src/ripgrep_mcp/server.py)                        │
│                                                                 │
│  2a. 引数バリデーション (SearchParams)                           │
│  2b. パストラバーサルチェック (sanitizer.py)                       │
│  2c. コマンドライン引数のリスト組み立て (command.py)              │
└────────────────────────┬────────────────────────────────────────┘
                         │ subprocess.run(["rg", ...], shell=False)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  ネイティブプロセス実行                                          │
│                                                                 │
│  - 実行ディレクトリをベースパス配下に厳格制限                      │
│  - タイムアウト監視 (3.0秒)                                      │
│  - シェルを経由しない配列引数実行                                  │
└────────────────────────┬────────────────────────────────────────┘
                         │ stdout (JSONL) / stderr / exit code
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  MCP Server  後処理                                                │
│                                                                 │
│  4a. ripgrep JSONL出力をパース (command.py)                       │
│  4b. マッチ行およびコンテキストの行番号整理                        │
│  4c. レスポンス文字数上限（8,000文字）に収まるよう切り詰め         │
└────────────────────────┬────────────────────────────────────────┘
                         │ MCP Response (Text)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  LLM Agent                                                      │
│  5. 結果をコンテキストに組み込み → 次の推論ステップへ                  │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 コンポーネント責務

| コンポーネント | 場所 | 責務 |
|---|---|---|
| **MCPサーバー** | `server.py` | MCPプロトコル制御、`search_codebase` ツールの公開、例外捕捉とレスポンス返却 |
| **サニタイザー** | `sanitizer.py` | パストラバーサル（`..`）防止、クエリ正規表現のバリデーション |
| **コマンドビルダー** | `command.py` | 安全な引数リスト構築、`subprocess.run` を用いたタイムアウト付き実行 |
| **アウトプットパーサー** | `command.py` | ripgrep JSONLのパース、行番号付与、レスポンスサイズ最適化 |

---

## 4. LLMコード解析特有の要件

### 4.1 コンテキスト行の取得

マッチした行単体だけでなく、関数の本体やクラスの定義をLLMが理解できるよう、前後のコンテキスト行を併せて返却します。

- `context_lines` パラメータで指定可能（デフォルト: 3行、最大: 20行）
- ripgrepの `-C <N>` オプションを使用
- 離れた行の間には区切り文字（` --\n`）を挿入し、構造を認識させやすくする

### 4.2 トークン数の制御（コンテキストウィンドウ爆発の防止）

`.*` などの緩い正規表現により大量の行がヒットした場合、LLMのコンテキストウィンドウを急激に消費するのを防ぎます。

| 制限種別 | 実装方法 | 設定値 | 目的 |
|---|---|---|---|
| 最大マッチ件数 | `rg -m <N>` | デフォルト20件 / 最大50件 | 検索出力自体のサイズを制限 |
| レスポンス最大文字数 | パース後の結果切り詰め | 最大 8,000 文字 | トークン消費量を安全な範囲に抑える |

切り詰めが発生した場合は、レスポンスのメタデータに `truncated: true` を明示し、LLMに「クエリを絞り込むべき」というインサイトを提供します。

### 4.3 ReDoS（正規表現DoS）対策

LLMが生成した非効率な正規表現（例: `(a+)+`）が最悪時間計算量になり、ホストのCPUを占有し続けるリスクがあります。

**対策:**
1. **実行タイムアウト:** `subprocess.run` の `timeout=3.0` により、3秒経過した時点でプロセスをシグナルで強制終了（Kill）する。
2. **巨大ファイル回避:** `--max-filesize 10M` オプションを指定し、ビルド成果物や巨大ログファイルを検索対象外にする。

---

## 5. MCP Tool定義

### 5.1 スキーマ定義

本サーバーが公開するツール `search_codebase` の仕様です。

```json
{
  "name": "search_codebase",
  "description": "リポジトリ内のソースコードを正規表現で検索し、マッチした行と周辺のコンテキストを取得します。関数定義・クラス・特定の変数の使われ方を調査するのに使用してください。大量ヒットが予想される場合は file_extensions や target_dir で対象を絞り込んでください。",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "検索キーワードまたはRust互換の正規表現。（例: 'def my_function', 'class [A-Z]\\w+'）シンプルな正規表現を推奨。"
      },
      "target_dir": {
        "type": "string",
        "description": "検索対象のディレクトリパス（相対パス）。リポジトリ全体を検索する場合は '.'"
      },
      "context_lines": {
        "type": "integer",
        "description": "マッチ行の前後何行を取得するか。関数全体を読みたい場合は 10、単純な変数参照なら 2 を推奨。最大20。",
        "default": 3,
        "minimum": 0,
        "maximum": 20
      },
      "file_extensions": {
        "type": "array",
        "items": { "type": "string" },
        "description": "検索対象ファイルの拡張子リスト（例: ['py', 'ts']）。絞り込み不要な場合は空配列。",
        "default": []
      },
      "max_matches": {
        "type": "integer",
        "description": "取得するマッチの最大件数。デフォルト20、最大50。",
        "default": 20,
        "minimum": 1,
        "maximum": 50
      }
    },
    "required": ["query", "target_dir"]
  }
}
```

---

## 6. セキュリティ設計（ネイティブ実行）

Dockerによるカーネルレベルの隔離を行わないため、Pythonプロセス内での厳格なバリデーションが必要不可欠です。

### 6.1 パストラバーサル（Directory Traversal）防御

LLMや不正な入力が `target_dir` に `../../../../etc/passwd` や `..\..\..\Windows` などを指定し、ワークスペース外の機密ファイルを窃取する攻撃を防御します。

- **解決プロセス:**
  1. システムの検索基盤となる `BASE_DIR`（ワークスペースの絶対パス）を環境変数（`RIPGREP_BASE_DIR`、未指定時はカレントディレクトリ）から取得し、`Path.resolve()` で正規化。
  2. `target_dir` から絶対パスを合成し、`Path.resolve()` でシンボリックリンクや相対表現（`..`）を完全に展開。
  3. 展開後の絶対パスの親（`Path.parents`）に `BASE_DIR` が含まれていること、あるいは `BASE_DIR == target` であることを検証。一致しない場合は `INVALID_PATH` エラーを返却し、コマンド実行を拒否。

### 6.2 コマンドインジェクション防御

`; rm -rf /` や `& calc.exe` などのOS特有のコマンド結合文字を入力に含め、意図しないコマンドを実行させる攻撃を防ぎます。

- **解決プロセス:**
  - Pythonの `subprocess.run` を呼び出す際、`shell=False`（デフォルト）を設定。
  - 引数をすべて配列形式（`list[str]`）で渡し、文字列結合によるシェルパーサーの呼び出しをバイパス。
  - ripgrepにオプションと誤認識されるのを防ぐため、クエリとパスの前に `--`（引数終端マーカー）を配置。

---

## 7. MCPサーバー設計

### 7.1 バリデーションクラス (`sanitizer.py`)

Pydanticを用いて、パラメータの型や範囲制限を宣言的に定義します。

```python
from pydantic import BaseModel, Field, field_validator
from typing import List

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
        return v
```

### 7.2 コマンド実行 & パース (`command.py`)

シェルを介さない安全なプロセス実行と、JSONL形式の出力をLLM用に行番号付きフォーマットへ変換するロジックを構成します。

```python
# build_rg_command の概略
def build_rg_command(params: SearchParams, target_path: str) -> list[str]:
    cmd = ["rg", "--json", "-C", str(params.context_lines), "-m", str(params.max_matches), "--max-filesize", "10M"]
    for ext in params.file_extensions:
        cmd.extend(["-g", f"*.{ext.lstrip('.')}"])
    cmd.extend(["--", params.query, target_path])
    return cmd
```

---

## 8. インターフェース仕様

### 8.1 終了コードの扱い

| コード | 意味 | サーバーの処理 |
|---|---|---|
| `0` | マッチあり | 正常にパースして結果を返却 |
| `1` | マッチなし | エラーではなく、空配列 `results: []` を返却 |
| `2` / その他 | コマンドエラー / 正規表現エラー | `stderr` の中身を取得し、`REGEX_ERROR` または `INTERNAL_ERROR` として返却 |
| `-1` | タイムアウト | `TIMEOUT` エラーを返却 |
| `-2` | `rg` コマンドが存在しない | `RIPGREP_NOT_FOUND` エラーを返却 |

### 8.2 レスポンス形式

#### 成功時
```json
{
  "status": "success",
  "metadata": {
    "query": "def get_user",
    "target_dir": "src",
    "truncated": false
  },
  "results": [
    {
      "file": "src/ripgrep_mcp/server.py",
      "code_snippet": "  40: \n  41: def get_user(token: str):\n  42:     return db.query(User).filter(...)\n  43: "
    }
  ]
}
```

#### 切り詰め（Truncated）時
```json
{
  "status": "success",
  "metadata": {
    "query": "def ",
    "target_dir": ".",
    "truncated": true
  },
  "results": [
    {
      "file": "src/ripgrep_mcp/server.py",
      "code_snippet": "  41: def get_user(token: str):\n..."
    }
  ]
}
```

#### エラー時 (正規表現エラー)
```json
{
  "status": "error",
  "error": {
    "code": "REGEX_ERROR",
    "message": "正規表現の構文エラーです。",
    "suggestion": "正規表現を修正してください。",
    "ripgrep_error": "regex parse error: unclosed group near index 8\n  (get_user\n        ^"
  }
}
```

---

## 9. エラーハンドリング & エージェントリカバリ

LLMが誤った正規表現や広すぎる検索範囲を指定した際、自律的にクエリを修正してリトライできるようにエラー構造を設計しています。

### 9.1 タイムアウト時の自律リカバリフロー

```
LLM: query=".*.*.*.*" (ReDoSの可能性のあるクエリ)
  │
  ├─► MCPサーバー: 3.0秒のタイムアウト監視下で rg 起動
  │
  ├─► プロセス側: ReDoSによるCPU占有発生
  │
  ├─► 3.0秒経過: subprocess.TimeoutExpired 発生、プロセスをKill
  │
  └─► LLMへエラー返却:
        {
          "status": "error",
          "error": {
            "code": "TIMEOUT",
            "message": "検索クエリが複雑すぎるか、検索範囲が広すぎます。",
            "suggestion": "正規表現をシンプルにするか、ディレクトリを絞り込んでください。"
          }
        }
          │
          └─► LLM: 提案に従い、より具体的なクエリに修正して再実行 ✅
```

---

## 10. セキュリティチェックリスト

リリース前に以下の設計が満たされているか検証すること。

- [ ] `Path.resolve()` による親ディレクトリ参照（`..`）の完全な正規化と検証。
- [ ] シンボリックリンクを介した脱出（ベースディレクトリ外への参照）が遮断されていること。
- [ ] `subprocess` 呼び出しにおける `shell=True` の完全な排除。
- [ ] タイムアウト（3.0秒）による実行プロセスの中断。
- [ ] ファイルサイズ上限（10MB）による過大検索の抑止。
- [ ] ログ出力（特に `print`）が標準出力（`sys.stdout`）へ書き込まれず、標準エラー出力（`sys.stderr`）に向いていること。

---

## 11. 依存関係・前提条件

### ホスト環境
- **Python**: 3.11 以上
- **ripgrep**: `rg` コマンドがシステムパス（PATH環境変数）に含まれていること。

### パッケージ依存関係
- `mcp>=0.1.0` (FastMCP機能を含む Python MCP SDK)
- `pydantic>=2.0.0` (パラメータ検証用)

---

## 12. 今後の拡張方針

1. **正規表現静的バリデーター**: 実行前に Python 側でクエリの複雑度をチェックし、ReDoS を事前検知する。
2. **キャッシュ機構**: 頻繁に呼び出されるクエリ結果の短期キャッシュ。
3. **ASTパーサーとの統合**: 単なる行ベースの検索に加え、シンボルの定義位置をツリーベースで返す機能の追加。

---

## 付録A: ripgrep JSONL出力フォーマット（参考）

```jsonl
{"type":"begin","data":{"path":{"text":"src/ripgrep_mcp/server.py"}}}
{"type":"match","data":{"path":{"text":"src/ripgrep_mcp/server.py"},"lines":{"text":"def search_codebase(\n"},"line_number":28,"absolute_offset":512,"submatches":[{"match":{"text":"def search_codebase"},"start":0,"end":19}]}}
{"type":"context","data":{"path":{"text":"src/ripgrep_mcp/server.py"},"lines":{"text":"    query: str,\n"},"line_number":29,"absolute_offset":532}}
{"type":"end","data":{"path":{"text":"src/ripgrep_mcp/server.py"},"binary_offset":null,"stats":{"elapsed":{"secs":0,"nanos":12345},"searches":1,"searches_with_match":1,"bytes_searched":1024,"matched_lines":1,"matches":1}}}
```
