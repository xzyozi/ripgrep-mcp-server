# LLMコード解析向け Rust製Grepエンジン — モジュール設計書

**バージョン:** 1.0.0
**ステータス:** ドラフト完成
**対象読者:** バックエンドエンジニア、MLエンジニア、DevOpsエンジニア

---

## 目次

1. [概要](#1-概要)
2. [モジュール構成](#2-モジュール構成)
3. [システムアーキテクチャ](#3-システムアーキテクチャ)
4. [LLMコード解析特有の要件](#4-llmコード解析特有の要件)
5. [LLM向け Function Calling（Tool）定義](#5-llm向け-function-callingtool定義)
6. [Dockerサンドボックス環境設計](#6-dockerサンドボックス環境設計)
7. [ホストAPI設計](#7-ホストapi設計)
8. [インターフェース仕様](#8-インターフェース仕様)
9. [エラーハンドリング & エージェントリカバリ](#9-エラーハンドリング--エージェントリカバリ)
10. [セキュリティチェックリスト](#10-セキュリティチェックリスト)
11. [依存関係・前提条件](#11-依存関係前提条件)
12. [今後の拡張方針](#12-今後の拡張方針)

---

## 1. 概要

### 1.1 モジュールの目的

本モジュール（`llm-grep-engine`）は、LLMエージェント（RAGパイプラインおよびコーディングエージェント）が外部ソースコードリポジトリやユーザー提供コードを解析する際に用いる、**高速かつ安全なコード検索ツール**です。

LLMの Function Calling（Tool Use）として呼び出され、安全に隔離されたDockerサンドボックス内でRust製の高速検索エンジン [ripgrep](https://github.com/BurntSushi/ripgrep) を実行します。

### 1.2 設計原則

| 原則 | 内容 |
|------|------|
| **安全性優先** | 未検証コードとLLM生成クエリは常に敵対的入力として扱う |
| **トークン効率** | LLMのコンテキストウィンドウ消費を最小限に抑える |
| **自律リカバリ** | エラー情報をLLMが理解・修正できる形式で返す |
| **疎結合** | 本モジュールは単一ディレクトリに完結し、親プロジェクトへの依存を最小化する |
| **可観測性** | 全実行ログを構造化JSONで記録し、デバッグ・監査を容易にする |

---

## 2. モジュール構成

本モジュールは親プロジェクトへの移行を容易にするため、すべての主要コードを **`llm-grep-engine/`** ディレクトリ配下に集約します。

```
llm-grep-engine/
│
├── README.md                      # モジュール概要・クイックスタート
├── DESIGN.md                      # 本設計書（このファイル）
│
├── api/                           # ホストAPI層
│   ├── __init__.py
│   ├── server.py                  # FastAPIエントリポイント
│   ├── routes.py                  # /search エンドポイント定義
│   ├── schema.py                  # リクエスト/レスポンス Pydanticモデル
│   └── middleware.py              # 認証・レートリミット
│
├── core/                          # コアロジック
│   ├── __init__.py
│   ├── sanitizer.py               # 入力パラメータのサニタイズ
│   ├── command_builder.py         # ripgrepコマンド組み立て
│   ├── container_runner.py        # Dockerコンテナ実行管理
│   └── output_parser.py           # ripgrep JSON出力のパース・整形
│
├── sandbox/                       # Dockerサンドボックス定義
│   ├── Dockerfile                 # ripgrepコンテナイメージ
│   └── entrypoint.sh              # （オプション）ラッパースクリプト
│
├── config/                        # 設定ファイル
│   ├── settings.py                # 環境変数・デフォルト値定義
│   └── limits.py                  # リソース制限定数
│
├── tests/                         # テストスイート
│   ├── unit/
│   │   ├── test_sanitizer.py
│   │   ├── test_command_builder.py
│   │   └── test_output_parser.py
│   └── integration/
│       └── test_container_runner.py
│
├── tool_definition/               # LLM向けTool定義（JSON Schema）
│   └── search_codebase.json       # OpenAI / Anthropic形式のTool定義
│
├── docker-compose.yml             # 開発・テスト用Compose設定
├── pyproject.toml                 # パッケージ定義・依存関係
└── .env.example                   # 環境変数テンプレート
```

> **移行時の注意:** 親プロジェクトへ組み込む際は `llm-grep-engine/` ディレクトリごとコピーし、`api/server.py` のルーターを親のAPIサーバーにマウントしてください。外部依存は `pyproject.toml` にすべて記載されています。

---

## 3. システムアーキテクチャ

### 3.1 全体フロー

```
┌─────────────────────────────────────────────────────────────────┐
│  LLM Agent / RAG Pipeline                                       │
│                                                                 │
│  1. 検索意図の決定                                                │
│     例: "get_user関数の定義を探したい"                             │
│         → query: "def get_user", target_dir: "src/", ...       │
└────────────────────────┬────────────────────────────────────────┘
                         │ Function Calling / Tool Use
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  ホストAPI  (llm-grep-engine/api/)                               │
│                                                                 │
│  2a. 入力バリデーション & サニタイズ (sanitizer.py)                 │
│  2b. ripgrepコマンド組み立て (command_builder.py)                  │
│  2c. 実行タイムアウト監視開始                                      │
└────────────────────────┬────────────────────────────────────────┘
                         │ docker run ...
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Docker サンドボックス  (llm-grep-engine/sandbox/)               │
│                                                                 │
│  ┌─────────────────┐       ┌──────────────────────────────┐    │
│  │   ripgrep (rg)  │◄──────│  対象ソースコード               │    │
│  │  (Alpine Linux) │       │  (Read-Only マウント :ro)     │    │
│  └────────┬────────┘       └──────────────────────────────┘    │
│           │ 3. 検索実行                                          │
│           ▼                                                     │
│     JSON出力 (JSONL形式) → stdout                               │
│     [制限] --read-only / --network none / -m 256m / --cpus=1.0 │
└────────────────────────┬────────────────────────────────────────┘
                         │ stdout / stderr / exit code
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  ホストAPI  後処理                                                │
│                                                                 │
│  4a. ripgrep JSONLのパース (output_parser.py)                    │
│  4b. トークン削減フォーマットへの変換                               │
│  4c. Truncation判定 & メタデータ付与                              │
└────────────────────────┬────────────────────────────────────────┘
                         │ JSON Response
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  LLM Agent                                                      │
│  5. 結果をコンテキストに組み込み → 次の推論ステップへ                  │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 コンポーネント責務

| コンポーネント | 場所 | 責務 |
|---|---|---|
| **ホストAPI** | `api/` | リクエスト受付、サニタイズ、レスポンス整形 |
| **サニタイザー** | `core/sanitizer.py` | 正規表現・パスのバリデーション、インジェクション防止 |
| **コマンドビルダー** | `core/command_builder.py` | 安全なripgrepコマンドのリスト形式構築 |
| **コンテナランナー** | `core/container_runner.py` | Dockerコンテナのライフサイクル管理・タイムアウト監視 |
| **アウトプットパーサー** | `core/output_parser.py` | ripgrep JSONLのパースとLLM向けフォーマット変換 |
| **Dockerサンドボックス** | `sandbox/` | ripgrep実行環境の隔離・制限 |

---

## 4. LLMコード解析特有の要件

通常のgrep検索ツールとは異なり、LLMエージェントのツールとして動作するため、以下の要件を満たす設計とします。

### 4.1 コンテキスト行の取得

マッチした1行だけでなく、関数の本体・クラス定義を理解するために**前後N行のコード**を併せて返却します。

- `context_lines` パラメータで調整可能（デフォルト: 3行）
- ripgrepの `-C <N>` オプションを使用
- コンテキスト区切り（`--`）を用いて複数マッチの視認性を確保

### 4.2 トークン数の制御（コンテキストウィンドウ爆発の防止）

`.` などの汎用正規表現で大量ヒットした場合、LLMのコンテキストウィンドウ（通常8K〜200Kトークン）を超過するリスクがあります。

**対策:**

| 制限種別 | 実装 | デフォルト値 | 設定箇所 |
|---|---|---|---|
| 最大マッチ件数 | `rg -m <N>` | 20件 | `config/limits.py` |
| 最大ファイル数 | ホスト側で件数カウント後に打ち切り | 10ファイル | `config/limits.py` |
| 最大コンテキスト行数 | パラメータ上限バリデーション | 20行 | `core/sanitizer.py` |
| レスポンス最大文字数 | 出力パース後にトリミング | 8,000文字 | `core/output_parser.py` |

切り捨てが発生した場合、`metadata.truncated: true` をレスポンスに含め、LLMが自律的にクエリを絞り込めるよう誘導します。

### 4.3 ReDoS（正規表現DoS）対策

LLMが生成した複雑な正規表現（例: `(.+)+`, `.*.*.*`）が壊滅的バックトラッキングに陥り、ホストのCPUを占有するリスクがあります。

**多層防御:**

1. **タイムアウト:** ホスト側で実行を3秒で強制終了（`subprocess.run(timeout=3)`）
2. **CPUリソース制限:** Dockerの `--cpus="1.0"` でホストへの影響を局所化
3. **静的バリデーション（将来拡張）:** `regex` ライブラリの複雑度チェックを `sanitizer.py` で実施予定

---

## 5. LLM向け Function Calling（Tool）定義

### 5.1 JSON Schema定義

`tool_definition/search_codebase.json` として配置し、OpenAI API・Anthropic API・その他LLMフレームワークから読み込めます。

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
        "description": "検索対象のディレクトリパス（相対パス）。リポジトリ全体を検索する場合は '.'、特定サービスに絞る場合は 'src/services/' のように指定。"
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
        "description": "検索対象ファイルの拡張子リスト（例: ['py', 'ts', 'go']）。絞り込み不要な場合は空配列。",
        "default": []
      },
      "max_matches": {
        "type": "integer",
        "description": "取得するマッチの最大件数。デフォルト20、最大50。大きくするとトークンを多く消費します。",
        "default": 20,
        "minimum": 1,
        "maximum": 50
      }
    },
    "required": ["query", "target_dir"]
  }
}
```

### 5.2 LLMへの利用ガイドライン（System Promptへの埋め込み推奨）

以下を親プロジェクトのSystem Promptに追記することで、LLMが本ツールを適切に使用できます。

```
## search_codebase ツールの使い方

- 関数定義を探す場合: query="def function_name" または "function functionName"
- クラスを探す場合: query="class [A-Z]\w+"
- 結果が truncated=true の場合: file_extensions や target_dir を絞り込んでリトライ
- Timeout エラーの場合: 正規表現をシンプルにしてリトライ（例: ".+" → "get_user"）
- 変数の使用箇所を探す場合: context_lines=2 で十分、関数全体を読む場合は context_lines=10
```

---

## 6. Dockerサンドボックス環境設計

### 6.1 Dockerfile

```dockerfile
# sandbox/Dockerfile
# ベース: 軽量・セキュアなAlpine Linux
FROM alpine:3.19

# メタデータ
LABEL maintainer="your-team"
LABEL description="Sandboxed ripgrep engine for LLM code analysis"
LABEL version="1.0.0"

# ripgrepのインストール（バージョン固定推奨）
RUN apk add --no-cache ripgrep=14.1.0-r0

# セキュリティ: root権限を完全剥奪
USER nobody

# 作業ディレクトリ
WORKDIR /workspace

# エントリポイントを固定 — シェルを介さないEXEC形式で任意コマンド実行を防止
ENTRYPOINT ["rg"]
```

> **重要:** `CMD` は設定しません。すべての引数はホストAPI側から明示的に渡します。シェルを経由しないことで、引数インジェクションによる任意コマンド実行を防ぎます。

### 6.2 実行時セキュリティ＆リソース制限

```bash
docker run \
  --rm \                          # 実行完了後にコンテナを即時破棄
  --read-only \                   # コンテナ内ファイルシステムを読み取り専用に
  --network none \                # ネットワークを完全遮断
  -m 256m \                       # メモリ上限 256MB
  --memory-swap 256m \            # スワップも同値（スワップ無効化）
  --cpus="1.0" \                  # CPU使用を1コアに制限
  --pids-limit 64 \               # プロセス数上限（フォーク爆弾対策）
  --cap-drop ALL \                # 全Linuxケーパビリティを剥奪
  --security-opt no-new-privileges \  # 権限昇格を禁止
  -v "/host/repo:/workspace/src:ro" \ # ソースコードを読み取り専用でマウント
  llm-grep-engine:1.0.0 \
  [ripgrepの引数...]
```

### 6.3 リソース制限の設計根拠

| 制限項目 | 設定値 | 設計根拠 |
|---|---|---|
| `--read-only` | — | 解析対象コードの改ざん・ファイル生成を防止 |
| `--network none` | — | 悪意あるコードへの外部通信・データ漏洩を遮断 |
| `-m 256m` | 256MB | 巨大バイナリを誤って検索した際のメモリ枯渇防止 |
| `--cpus="1.0"` | 1コア | ReDoSによるホストCPU全占有を防止 |
| `--pids-limit 64` | 64プロセス | フォーク爆弾によるホストプロセス枯渇を防止 |
| `--cap-drop ALL` | 全剥奪 | コンテナエスケープの攻撃面を最小化 |
| `--rm` | — | コンテナが状態を保持しないことを保証 |

### 6.4 Dockerイメージのビルド

```bash
# llm-grep-engine/ ディレクトリから実行
docker build -t llm-grep-engine:1.0.0 ./sandbox/
```

---

## 7. ホストAPI設計

### 7.1 エンドポイント

```
POST /v1/grep/search
```

### 7.2 リクエストスキーマ（`api/schema.py`）

```python
from pydantic import BaseModel, Field, validator
from typing import List, Optional

class SearchRequest(BaseModel):
    query: str = Field(..., description="検索クエリ or 正規表現")
    target_dir: str = Field(..., description="検索対象ディレクトリ（相対パス）")
    context_lines: int = Field(default=3, ge=0, le=20)
    file_extensions: List[str] = Field(default_factory=list)
    max_matches: int = Field(default=20, ge=1, le=50)

    @validator("query")
    def query_not_empty(cls, v):
        if not v.strip():
            raise ValueError("queryは空にできません")
        return v

    @validator("target_dir")
    def target_dir_no_traversal(cls, v):
        # パストラバーサル防止
        if ".." in v or v.startswith("/"):
            raise ValueError("無効なtarget_dirです")
        return v
```

### 7.3 コマンド組み立て（`core/command_builder.py`）

コマンドは **リスト形式** で構築します。文字列結合によるシェルインジェクションを排除します。

```python
def build_rg_command(request: SearchRequest) -> list[str]:
    """
    ripgrepコマンドをリスト形式で組み立てる。
    シェルを経由せず subprocess に直接渡すことでインジェクションを防止。
    """
    cmd = [
        "rg",
        "--json",                           # JSONL形式で出力
        "-C", str(request.context_lines),   # 前後コンテキスト行
        "-m", str(request.max_matches),     # 最大マッチ数（ホスト側で強制）
        "--max-filesize", "10M",            # 巨大ファイルをスキップ
    ]

    # ファイル拡張子フィルタ
    for ext in request.file_extensions:
        cmd.extend(["-t", ext])

    # クエリと対象ディレクトリは "--" の後に配置（オプション誤認識防止）
    cmd.extend(["--", request.query, f"/workspace/src/{request.target_dir}"])

    return cmd
```

### 7.4 コンテナ実行（`core/container_runner.py`）

```python
import subprocess
import shlex
from config.limits import EXECUTION_TIMEOUT_SEC

def run_in_sandbox(
    rg_command: list[str],
    host_repo_path: str,
    image: str = "llm-grep-engine:1.0.0"
) -> tuple[str, str, int]:
    """
    Dockerサンドボックス内でripgrepを実行し (stdout, stderr, exit_code) を返す。
    """
    docker_cmd = [
        "docker", "run",
        "--rm",
        "--read-only",
        "--network", "none",
        "-m", "256m",
        "--memory-swap", "256m",
        "--cpus", "1.0",
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{host_repo_path}:/workspace/src:ro",
        image,
        *rg_command,   # "rg", "--json", ... などripgrepコマンド
    ]

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT_SEC,  # デフォルト3秒
        )
        return result.stdout, result.stderr, result.returncode

    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
```

---

## 8. インターフェース仕様

### 8.1 ripgrepコマンド例

```bash
# 例: get_user関数を src/api/ 配下の .py ファイルから検索、前後5行取得、最大20件
rg --json -C 5 -m 20 -t py -- "def get_user" /workspace/src/api/
```

### 8.2 ripgrep終了コード

| コード | 意味 | システムの扱い |
|---|---|---|
| `0` | マッチあり | 正常レスポンス |
| `1` | マッチなし | 空結果を返す（エラーではない） |
| `2` | 正規表現・引数エラー | エラーレスポンス（stderrをLLMへ転送） |
| `-1` | タイムアウト（ホスト側kill） | タイムアウトエラーレスポンス |

### 8.3 成功レスポンス（`status: success`）

```json
{
  "status": "success",
  "metadata": {
    "query": "def get_user",
    "target_dir": "src/",
    "total_matches_found": 2,
    "files_searched": 34,
    "truncated": false,
    "execution_time_ms": 142
  },
  "results": [
    {
      "file": "src/api/auth.py",
      "code_snippet": " 40:     # Validate token\n 41:     if not token:\n 42:         def get_user(token: str):\n 43:             return db.query(User).filter(...)\n 44: "
    },
    {
      "file": "src/services/user.py",
      "code_snippet": " 13: class UserService:\n 14:     @staticmethod\n 15:     def get_user(user_id: int):\n 16:         pass\n 17: "
    }
  ]
}
```

> `code_snippet` の各行先頭に行番号（` NN:` 形式）を付与します。これにより、LLMが「`src/services/user.py` の15行目を書き換えるパッチを生成して」のような後続指示を正確に実行できます。

### 8.4 Truncatedレスポンス（上限到達時）

```json
{
  "status": "success",
  "metadata": {
    "query": "def ",
    "total_matches_found": 20,
    "truncated": true,
    "truncation_reason": "max_matches_reached",
    "suggestion": "file_extensions や target_dir を絞り込んで再検索してください。"
  },
  "results": [ "..." ]
}
```

### 8.5 エラーレスポンス

```json
{
  "status": "error",
  "error": {
    "code": "TIMEOUT",
    "message": "検索クエリが複雑すぎるか、検索範囲が広すぎます。",
    "suggestion": "正規表現をシンプルにするか、ディレクトリを絞り込んでください。",
    "ripgrep_error": null
  }
}
```

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

### 8.6 エラーコード一覧

| `error.code` | 原因 | LLMへの推奨アクション |
|---|---|---|
| `TIMEOUT` | 実行が3秒を超過 | 正規表現を単純化・対象ディレクトリを絞る |
| `REGEX_ERROR` | 正規表現構文エラー | `ripgrep_error` を参照し正規表現を修正 |
| `NO_RESULTS` | マッチなし（Exit 1） | クエリを緩める・対象ディレクトリを変更 |
| `INVALID_PATH` | パストラバーサル試行など | 有効な相対パスを指定 |
| `INTERNAL_ERROR` | ホスト側の予期せぬエラー | システム管理者に報告 |

---

## 9. エラーハンドリング & エージェントリカバリ

LLMは時に不正・非効率なクエリを生成するため、システム側でのフェイルセーフとLLMへの**自律的リカバリ誘導**が重要です。

### 9.1 タイムアウト時の自律リカバリフロー

```
LLM: query=".+\n.+"（複雑な正規表現）
  │
  ├─► ホストAPI: 3秒タイマー開始
  │
  ├─► コンテナ実行: ReDoS発生 → 応答なし
  │
  ├─► 3秒経過: subprocess.TimeoutExpired → コンテナをkill
  │
  └─► LLMへ返却:
        {
          "status": "error",
          "error": {
            "code": "TIMEOUT",
            "message": "...",
            "suggestion": "正規表現をシンプルにするか、ディレクトリを絞り込んでください。"
          }
        }
          │
          └─► LLM: エラー内容を理解し、query="get_user" に修正してリトライ ✅
```

### 9.2 Truncation時の自律リカバリフロー

```
LLM: query="def "（汎用すぎる検索）
  │
  ├─► 20件上限到達: metadata.truncated=true
  │
  └─► LLMへ返却:
        { "truncated": true, "suggestion": "file_extensions を絞り込んでください。" }
          │
          └─► LLM: file_extensions=["py"], target_dir="src/api/" で再検索 ✅
```

### 9.3 正規表現エラー時の自律リカバリフロー

```
LLM: query="class (Controller" （括弧閉じ忘れ）
  │
  ├─► Exit Code 2: stderrに "unclosed group" エラー
  │
  └─► LLMへ返却: ripgrep_error をそのまま転送
        │
        └─► LLM: "unclosed group" を読み、query="class \\w+Controller" に修正 ✅
```

---

## 10. セキュリティチェックリスト

本モジュールをデプロイ前に必ず確認してください。

### ホストAPI層

- [ ] 全入力パラメータを `sanitizer.py` で検証済み
- [ ] `target_dir` のパストラバーサル（`..`）を検出してリジェクト
- [ ] `file_extensions` のホワイトリスト検証（英数字のみ許可）
- [ ] 正規表現の最大長制限（例: 500文字）
- [ ] API認証（Bearer Token または API Key）の実装
- [ ] レートリミットの実装（例: 60リクエスト/分/ユーザー）
- [ ] 全実行を構造化ログ（JSON）で記録

### Dockerサンドボックス層

- [ ] `--read-only` フラグが有効
- [ ] `--network none` フラグが有効
- [ ] メモリ・CPU・PID制限が設定済み
- [ ] `--cap-drop ALL` が設定済み
- [ ] `--security-opt no-new-privileges` が設定済み
- [ ] ボリュームマウントが `:ro`（読み取り専用）
- [ ] コンテナイメージのバージョンが固定済み（`latest` タグ不使用）

### 運用層

- [ ] 本番環境でのDockerイメージ署名・検証の実施
- [ ] コンテナイメージの定期的な脆弱性スキャン
- [ ] ホストAPI側での実行タイムアウト監視
- [ ] 異常なリクエスト頻度のアラート設定

---

## 11. 依存関係・前提条件

### ホスト環境

| 要件 | バージョン | 備考 |
|---|---|---|
| Docker Engine | 24.0以上 | `--pids-limit` サポートのため |
| Python | 3.11以上 | ホストAPI実行環境 |
| FastAPI | 0.110以上 | APIフレームワーク |
| Pydantic | 2.x | バリデーション |

### コンテナ内

| 要件 | バージョン | 備考 |
|---|---|---|
| Alpine Linux | 3.19 | ベースイメージ |
| ripgrep | 14.1.0 | バージョン固定推奨 |

### 環境変数（`.env.example`）

```dotenv
# サンドボックス設定
DOCKER_IMAGE=llm-grep-engine:1.0.0
HOST_REPO_BASE_PATH=/data/repos
EXECUTION_TIMEOUT_SEC=3

# リソース制限
MAX_MEMORY=256m
MAX_CPUS=1.0
MAX_PIDS=64

# 検索制限
DEFAULT_MAX_MATCHES=20
MAX_CONTEXT_LINES=20
MAX_QUERY_LENGTH=500

# API設定
API_HOST=0.0.0.0
API_PORT=8080
API_WORKERS=4
```

---

## 12. 今後の拡張方針

| 優先度 | 拡張項目 | 概要 |
|---|---|---|
| 高 | **正規表現複雑度チェック** | `sanitizer.py` に静的解析を追加し、ReDoSリスクのあるパターンを事前に拒否 |
| 高 | **結果のキャッシュ** | 同一クエリのRedisキャッシュ（TTL: 5分）でレスポンスを高速化 |
| 中 | **マルチコンテナ並列実行** | 大規模リポジトリを対象にサブディレクトリ単位で並列検索 |
| 中 | **構造的コード解析の統合** | Tree-sitterと組み合わせ、ASTベースの関数境界検出を追加 |
| 低 | **検索履歴のLLMへの提供** | 重複クエリをLLMに通知し、検索効率を改善 |
| 低 | **WebSocket対応** | 長時間検索のプログレス通知をストリーミングで提供 |

---

## 付録A: ripgrep JSONL出力フォーマット（参考）

ホスト側でのパース対象となるripgrepの生出力フォーマットです。

```jsonl
{"type":"begin","data":{"path":{"text":"src/api/auth.py"}}}
{"type":"match","data":{"path":{"text":"src/api/auth.py"},"lines":{"text":"def get_user(token: str):\n"},"line_number":42,"absolute_offset":1024,"submatches":[{"match":{"text":"def get_user"},"start":0,"end":12}]}}
{"type":"context","data":{"path":{"text":"src/api/auth.py"},"lines":{"text":"    return db.query(User)\n"},"line_number":43,"absolute_offset":1056}}
{"type":"end","data":{"path":{"text":"src/api/auth.py"},"binary_offset":null,"stats":{"elapsed":{"secs":0,"nanos":123456,"human":"0.000123s"},"searches":1,"searches_with_match":1,"bytes_searched":2048,"bytes_printed":256,"matched_lines":1,"matches":1}}}
{"type":"summary","data":{"elapsed_total":{"secs":0,"nanos":234567,"human":"0.000234s"},"stats":{"searches":5,"searches_with_match":2,"bytes_searched":10240,"bytes_printed":512,"matched_lines":2,"matches":2}}}
```
