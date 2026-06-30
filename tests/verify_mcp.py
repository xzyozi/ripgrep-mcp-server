import sys
import os
import asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import subprocess
import json
import time

def main() -> None:
    print("=== MCP Server Response Verification ===")
    
    # 起動コマンド (uv run を使用して実行)
    cmd = ["uv", "run", "python", "-m", "ripgrep_mcp.server"]
    
    print(f"Starting MCP server with command: {' '.join(cmd)}")
    
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    
    # プロセス起動 (stdin, stdout, stderr をパイプ)
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )
    
    # サーバーの初期起動待ち
    time.sleep(0.5)
    
    # 初期化メッセージ
    init_request = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "verify-client",
                "version": "1.0.0"
            }
        },
        "id": 0
    }
    
    # 初期化完了通知
    initialized_notification = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized"
    }

    # テスト用のMCPツール呼び出し (tools/call) メッセージ
    tool_request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "search_codebase",
            "arguments": {
                "query": "SearchParams",
                "target_dir": "src"
            }
        },
        "id": 1
    }
    
    # 2回目のツール呼び出し (キャッシュが効くはず)
    tool_request_cached = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "search_codebase",
            "arguments": {
                "query": "SearchParams",
                "target_dir": "src"
            }
        },
        "id": 2
    }
    
    # 複数メッセージを改行区切りで送信
    request_lines = [
        json.dumps(init_request),
        json.dumps(initialized_notification),
        json.dumps(tool_request),
        json.dumps(tool_request_cached)
    ]
    request_str = "\n".join(request_lines) + "\n"
    
    print("\nSending requests sequence to stdin...")
    for req in request_lines:
        print(f"-> {req}")
    
    try:
        # 手動で書き込み、フラッシュして少し待つ (EOFによる早期終了を防ぐ)
        process.stdin.write(request_str)
        process.stdin.flush()
        time.sleep(0.5)
        
        # 残りのデータを回収しプロセスを終了させる
        stdout_output, stderr_output = process.communicate(timeout=2.0)
        
        # 出力を改行で分割
        stdout_lines = stdout_output.splitlines() if stdout_output else []
        print("\nReceived responses from stdout:")
        for line in stdout_lines:
            print(f"<- {line.strip()}")
        
        # stderrのログも確認用に出力
        if stderr_output:
            print(f"\nServer logs (stderr):\n{stderr_output.strip()}")
            
        # 簡易検証 (id=1 の通常の成功と、id=2 のキャッシュ成功を確認)
        success_id1 = False
        success_id2 = False
        
        for line in stdout_lines:
            if not line.strip():
                continue
            try:
                response_data = json.loads(line)
                resp_id = response_data.get("id")
                
                if resp_id == 1:
                    if "result" in response_data:
                        print("\n[SUCCESS] Response for id=1 received and parsed successfully.")
                        success_id1 = True
                    else:
                        print(f"\n[FAILED] Response for id=1 does not contain 'result' payload. Error: {response_data.get('error')}")
                        
                elif resp_id == 2:
                    if "result" in response_data:
                        # キャッシュが入っていることを確認
                        content_list = response_data["result"].get("content", [])
                        if content_list:
                            inner_text = content_list[0].get("text", "")
                            # マークダウン内のキャッシュ通知マークを確認
                            if "⚡ Cached Result" in inner_text:
                                print("\n[SUCCESS] Response for id=2 was successfully served from cache ('cached': true).")
                                success_id2 = True
                            else:
                                print("\n[FAILED] Response for id=2 was not cached.")
                        else:
                            print("\n[FAILED] Response for id=2 has no content text.")
                    else:
                        print(f"\n[FAILED] Response for id=2 does not contain 'result' payload. Error: {response_data.get('error')}")
            except Exception as parse_err:
                print(f"Failed to parse line: {line}. Error: {parse_err}")
                
        if not success_id1 or not success_id2:
            print(f"\n[FAILED] Verification failed. Success ID1: {success_id1}, Success ID2: {success_id2}")
            
    except Exception as e:
        print(f"\n[ERROR] Verification failed: {e}")
        if process.poll() is None:
            process.kill()

if __name__ == "__main__":
    main()
