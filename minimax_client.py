"""
MiniMax Agent API Client - PoC
CLI from Termux to MiniMax M2.7 via reverse-engineered browser API.
No API fees. Uses curl_cffi to bypass Cloudflare.

Usage:
    python3 minimax_client.py "your prompt here"
    python3 minimax_client.py  # interactive mode
"""

import json
import hashlib
import time
import sys
import base64
from urllib.parse import urlencode, quote

from curl_cffi import requests

# --- Configuration ---
# Set these from your browser's localStorage after logging in to agent.minimax.io
# _token: JWT token
# user_detail_agent.realUserID: your real user ID (NOT the JWT user.id)

TOKEN = ""
REAL_USER_ID = ""
DEVICE_ID = ""

BASE_URL = "https://agent.minimax.io"
SIGNATURE_SECRET = "I*7Cf%WZ#S&%1RlZJ&C2"
MAX_POLL = 60
POLL_INTERVAL = 2


def load_config():
    """Load token from config file if exists."""
    global TOKEN, REAL_USER_ID, DEVICE_ID
    try:
        with open("config.json") as f:
            cfg = json.load(f)
            TOKEN = cfg.get("token", TOKEN)
            REAL_USER_ID = cfg.get("real_user_id", REAL_USER_ID)
            DEVICE_ID = cfg.get("device_id", DEVICE_ID)
    except FileNotFoundError:
        pass

    if not TOKEN or not REAL_USER_ID:
        # Try to extract from JWT
        if TOKEN:
            try:
                payload = json.loads(base64.b64decode(TOKEN.split('.')[1] + '=='))
                if not DEVICE_ID:
                    DEVICE_ID = str(payload.get('user', {}).get('deviceID', '0'))
            except Exception:
                pass

    if not TOKEN or not REAL_USER_ID:
        print("Error: Set TOKEN and REAL_USER_ID in config.json or environment")
        print("  TOKEN: JWT from localStorage._token")
        print("  REAL_USER_ID: from localStorage.user_detail_agent.realUserID")
        sys.exit(1)


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def make_request(path: str, body: dict, timeout: int = 30) -> dict:
    """Make authenticated request to MiniMax Agent API."""
    ts = int(time.time())
    ts_ms = ts * 1000
    body_str = json.dumps(body, separators=(',', ':'))

    params = {
        "device_platform": "web",
        "app_id": "3001",
        "version_code": "22200",
        "uuid": REAL_USER_ID,
        "device_id": DEVICE_ID,
        "user_id": REAL_USER_ID,
        "unix": str(ts_ms),
        "token": TOKEN,
        "client": "web",
    }

    sig = md5(f"{ts}{SIGNATURE_SECRET}{body_str}")

    qs = urlencode(params)
    full_path = f"{path}?{qs}"
    time_hash = md5(str(ts_ms))
    yy = md5(quote(full_path, safe='') + "_" + body_str + time_hash + "ooui")

    resp = requests.post(
        f"{BASE_URL}{path}",
        data=body_str,
        headers={
            "token": TOKEN,
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "x-timestamp": str(ts),
            "x-signature": sig,
            "yy": yy,
        },
        params=params,
        impersonate="chrome",
        timeout=timeout,
    )

    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

    return resp.json()


def send_message(text: str) -> str:
    """Send a message and wait for AI response."""
    # Step 1: Send message
    result = make_request("/matrix/api/v1/chat/send_msg", {
        "msg_type": 1,
        "text": text,
        "chat_type": 1,
        "attachments": [],
        "selected_mcp_tools": [],
        "backend_config": {},
        "sub_agent_ids": [],
    })

    chat_id = result.get("chat_id")
    if not chat_id:
        raise Exception(f"No chat_id in response: {result}")

    base_resp = result.get("base_resp", {})
    if base_resp.get("status_code") != 0:
        raise Exception(f"Send failed: {base_resp.get('status_msg')}")

    # Step 2: Poll for AI response
    for i in range(MAX_POLL):
        time.sleep(POLL_INTERVAL)

        detail = make_request("/matrix/api/v1/chat/get_chat_detail", {
            "chat_id": chat_id,
        })

        messages = detail.get("messages", [])
        ai_msg = next((m for m in messages if m.get("msg_type") == 2), None)

        if ai_msg and ai_msg.get("msg_content"):
            return ai_msg["msg_content"]

        # Show progress
        print(f"  waiting... ({i + 1}/{MAX_POLL})", end='\r', file=sys.stderr)

    raise Exception(f"No AI response after {MAX_POLL} polls")


def main():
    load_config()

    if len(sys.argv) > 1:
        # One-shot mode
        prompt = " ".join(sys.argv[1:])
        response = send_message(prompt)
        print(response)
    else:
        # Interactive mode
        print("MiniMax M2.7 Agent CLI (type 'exit' to quit)")
        print("=" * 50)
        while True:
            try:
                prompt = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if prompt.strip().lower() in ('exit', 'quit', 'q'):
                break

            if not prompt.strip():
                continue

            try:
                response = send_message(prompt)
                print(f"\n{response}")
            except Exception as e:
                print(f"\nError: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
