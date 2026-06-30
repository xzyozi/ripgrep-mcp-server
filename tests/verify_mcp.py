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
    
    # 複数メッセージを改行区切りで送信
    request_lines = [
        json.dumps(init_request),
        json.dumps(initialized_notification),
        json.dumps(tool_request)
    ]
    request_str = "\n".join(request_lines) + "\n"
    
    print("\nSending requests sequence to stdin...")
    for req in request_lines:
        print(f"-> {req}")
    
    try:
        # communicateを使用して安全にデータを送信し、出力を取得
        stdout_output, stderr_output = process.communicate(input=request_str, timeout=3.0)
        
        # 出力を改行で分割
        stdout_lines = stdout_output.splitlines() if stdout_output else []
        print("\nReceived responses from stdout:")
        for line in stdout_lines:
            print(f"<- {line.strip()}")
        
        # stderrのログも確認用に出力
        if stderr_output:
            print(f"\nServer logs (stderr):\n{stderr_output.strip()}")
            
        # 簡易検証 (tools/call の id である 1 のレスポンスを探す)
        success = False
        for line in stdout_lines:
            if not line.strip():
                continue
            try:
                response_data = json.loads(line)
                if response_data.get("id") == 1:
                    if "result" in response_data:
                        print("\n[SUCCESS] Response received and parsed successfully.")
                        success = True
                    else:
                        print(f"\n[FAILED] Response for id=1 does not contain 'result' payload. Error: {response_data.get('error')}")
                    break
            except Exception as parse_err:
                print(f"Failed to parse line: {line}. Error: {parse_err}")
                
        if not success:
            # id=1 のレスポンスが見つからなかった場合
            has_id_1 = any(json.loads(l).get("id") == 1 for l in stdout_lines if l.strip())
            if not has_id_1:
                print("\n[FAILED] No response for tools/call (id=1) received from server.")
            
    except Exception as e:
        print(f"\n[ERROR] Verification failed: {e}")
        if process.poll() is None:
            process.kill()

if __name__ == "__main__":
    main()
