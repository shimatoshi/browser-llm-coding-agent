"""
MiniMax Agent API Client
CLI from Termux to MiniMax M2.7 via reverse-engineered browser API.
No API fees. Uses curl_cffi to bypass Cloudflare.
Supports multi-account rotation for unlimited usage.

Usage:
    python3 minimax_client.py "your prompt here"
    python3 minimax_client.py  # interactive mode
"""

import json
import hashlib
import os
import time
import sys
import base64
from urllib.parse import urlencode, quote

from curl_cffi import requests

# --- State ---
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PKG_DIR, "config.json")

BASE_URL = "https://agent.minimax.io"
SIGNATURE_SECRET = "I*7Cf%WZ#S&%1RlZJ&C2"
MAX_POLL = 60
POLL_INTERVAL = 2

# Current active account
_current = {
    "token": "",
    "real_user_id": "",
    "device_id": "",
}

# All accounts for rotation
_accounts = []
_account_index = 0


def load_config():
    """Load config including accounts list and signature secret."""
    global SIGNATURE_SECRET, _accounts, _account_index

    config_paths = [CONFIG_PATH, "config.json"]
    config_path = next((p for p in config_paths if os.path.exists(p)), None)
    if not config_path:
        print("Error: config.json not found")
        print("  Create it from config.json.example")
        sys.exit(1)

    with open(config_path) as f:
        cfg = json.load(f)

    # Load signature secret if auto_update has written one
    if cfg.get("signature_secret"):
        SIGNATURE_SECRET = cfg["signature_secret"]

    # Load accounts list (new format) or single account (old format)
    if "accounts" in cfg and cfg["accounts"]:
        _accounts = cfg["accounts"]
    else:
        # Single account (backwards compatible)
        acct = {}
        if cfg.get("token"):
            acct["token"] = cfg["token"]
        if cfg.get("real_user_id"):
            acct["real_user_id"] = cfg["real_user_id"]
        if cfg.get("device_id"):
            acct["device_id"] = cfg["device_id"]
        if acct.get("token"):
            _accounts = [acct]

    if not _accounts:
        print("Error: No accounts configured in config.json")
        print("  Set 'token' and 'real_user_id', or use 'accounts' array")
        sys.exit(1)

    # Fill in device_id from JWT if missing
    for acct in _accounts:
        if not acct.get("device_id") and acct.get("token"):
            try:
                payload = json.loads(base64.b64decode(acct["token"].split('.')[1] + '=='))
                acct["device_id"] = str(payload.get('user', {}).get('deviceID', '0'))
            except Exception:
                acct["device_id"] = "0"

    _account_index = 0
    _activate_account(0)
    print(f"  {len(_accounts)} account(s) loaded", file=sys.stderr)


def _activate_account(index: int):
    """Switch to account at given index."""
    global _account_index
    _account_index = index % len(_accounts)
    acct = _accounts[_account_index]
    _current["token"] = acct["token"]
    _current["real_user_id"] = acct["real_user_id"]
    _current["device_id"] = acct.get("device_id", "0")


def rotate_account():
    """Switch to next account. Returns True if rotated, False if only 1 account."""
    if len(_accounts) <= 1:
        return False
    old = _account_index
    _activate_account(_account_index + 1)
    print(f"  [Rotated to account {_account_index + 1}/{len(_accounts)}]", file=sys.stderr)
    return True


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
        "uuid": _current["real_user_id"],
        "device_id": _current["device_id"],
        "user_id": _current["real_user_id"],
        "unix": str(ts_ms),
        "token": _current["token"],
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
            "token": _current["token"],
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

    data = resp.json()

    # Check for quota/credit errors
    base_resp = data.get("base_resp", {})
    status_code = base_resp.get("status_code", 0)
    if status_code != 0:
        status_msg = base_resp.get("status_msg", "")
        # Credit exhausted or rate limited
        if status_code in (30100, 30101, 30102, 429) or "credit" in status_msg.lower() or "limit" in status_msg.lower():
            if rotate_account():
                # Retry with new account
                return make_request(path, body, timeout)
            else:
                raise Exception(f"Credits exhausted and no more accounts: {status_msg}")
        raise Exception(f"API error {status_code}: {status_msg}")

    return data


def send_message(text: str) -> str:
    """Send a message and wait for AI response."""
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

    # Poll for AI response
    for i in range(MAX_POLL):
        time.sleep(POLL_INTERVAL)

        detail = make_request("/matrix/api/v1/chat/get_chat_detail", {
            "chat_id": chat_id,
        })

        messages = detail.get("messages", [])
        ai_msg = next((m for m in messages if m.get("msg_type") == 2), None)

        if ai_msg and ai_msg.get("msg_content"):
            return ai_msg["msg_content"]

        print(f"  waiting... ({i + 1}/{MAX_POLL})", end='\r', file=sys.stderr)

    raise Exception(f"No AI response after {MAX_POLL} polls")


def main():
    load_config()

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        response = send_message(prompt)
        print(response)
    else:
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
