"""
MiniMax Coding Agent
ReAct-style agent loop: prompt → MiniMax M2.7 → parse tool calls → execute → feed back.

Usage:
    python3 agent.py "fix the bug in main.py"
    python3 agent.py  # interactive mode
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from minimax_client import load_config, send_message

# --- Constants ---
MAX_TURNS = 30
EXPLORE_BUDGET = 12
CWD = os.environ.get("MMX_CWD", os.getcwd())

SYSTEM_PROMPT = """You are a coding agent. You work in: {cwd}

# TOOL FORMAT (CRITICAL)
To use a tool, you MUST output EXACTLY this format. No other format is accepted:

<tool_call>
{"name": "TOOL_NAME", "args": {"key": "value"}}
</tool_call>

WRONG (will be ignored):
- ```json {"name": ...} ```
- [tool_call] ...
- Bare JSON without <tool_call> tags
- Tool calls inside code blocks

# TOOLS

read_file {"path": "file.py"}
write_file {"path": "file.py", "content": "..."}
edit_file {"path": "file.py", "old_string": "before", "new_string": "after"}
execute_command {"command": "npm install"}
list_directory {"path": "."}
find_files {"pattern": "**/*.py"}
search_text {"pattern": "TODO", "path": "src/"}
task_complete {"summary": "what was done"}

# RULES
- Read before edit. Always.
- One tool per <tool_call> block. Multiple blocks OK.
- Tool results appear as [RESULT tool_name] ... [/RESULT]. Never generate these yourself.
- Keep explanations short. Act, don't talk.
- Never hallucinate tool results. Wait for real ones.
- When done, call task_complete.
""".strip()

# --- Tool Implementations ---

def tool_read_file(args: dict) -> dict:
    path = resolve_path(args["path"])
    try:
        content = Path(path).read_text()
        if len(content) > 50000:
            content = content[:50000] + "\n... (truncated)"
        return {"success": True, "content": content, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_write_file(args: dict) -> dict:
    path = resolve_path(args["path"])
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(args["content"])
        return {"success": True, "path": path, "bytes": len(args["content"])}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_edit_file(args: dict) -> dict:
    path = resolve_path(args["path"])
    try:
        content = Path(path).read_text()
        old = args["old_string"]
        new = args["new_string"]
        if old not in content:
            return {"success": False, "error": f"old_string not found in {path}"}
        count = content.count(old)
        content = content.replace(old, new, 1)
        Path(path).write_text(content)
        return {"success": True, "path": path, "occurrences": count}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_execute_command(args: dict) -> dict:
    cmd = args["command"]
    # Safety: block destructive commands
    dangerous = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd"]
    if any(d in cmd for d in dangerous):
        return {"success": False, "error": f"Blocked dangerous command: {cmd}"}
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=120, cwd=CWD
        )
        output = result.stdout[-10000:] if len(result.stdout) > 10000 else result.stdout
        stderr = result.stderr[-5000:] if len(result.stderr) > 5000 else result.stderr
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": output,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out (120s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_list_directory(args: dict) -> dict:
    path = resolve_path(args.get("path", "."))
    try:
        entries = sorted(os.listdir(path))
        result = []
        for e in entries[:200]:
            full = os.path.join(path, e)
            t = "dir" if os.path.isdir(full) else "file"
            result.append(f"{t}\t{e}")
        return {"success": True, "entries": "\n".join(result), "count": len(entries)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_find_files(args: dict) -> dict:
    base = resolve_path(args.get("path", "."))
    pattern = args["pattern"]
    try:
        matches = sorted(str(p) for p in Path(base).glob(pattern))[:100]
        return {"success": True, "files": "\n".join(matches), "count": len(matches)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_search_text(args: dict) -> dict:
    base = resolve_path(args.get("path", "."))
    pattern = args["pattern"]
    glob_pat = args.get("glob", "")
    try:
        cmd = ["grep", "-rn", "--include", glob_pat, pattern, base] if glob_pat else ["grep", "-rn", pattern, base]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = result.stdout.strip().split("\n")[:50]
        return {"success": True, "matches": "\n".join(lines), "count": len(lines)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_task_complete(args: dict) -> dict:
    return {"success": True, "done": True, "summary": args.get("summary", "")}


TOOL_MAP = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "execute_command": tool_execute_command,
    "list_directory": tool_list_directory,
    "find_files": tool_find_files,
    "search_text": tool_search_text,
    "task_complete": tool_task_complete,
}

EXPLORE_TOOLS = {"list_directory", "find_files", "search_text", "read_file"}


def resolve_path(p: str) -> str:
    if os.path.isabs(p):
        return p
    return os.path.join(CWD, p)


# --- Tool Call Parser ---

TOOL_NAMES = {"read_file", "write_file", "edit_file", "execute_command",
              "list_directory", "find_files", "search_text", "task_complete"}

# Map from XML-style parameter names to our arg names
XML_PARAM_MAP = {
    "file_path": "path", "path": "path", "content": "content",
    "old_string": "old_string", "new_string": "new_string",
    "command": "command", "pattern": "pattern", "glob": "glob",
    "summary": "summary",
}
# Map from XML invoke names to our tool names
XML_NAME_MAP = {
    "read": "read_file", "read_file": "read_file",
    "write": "write_file", "write_file": "write_file",
    "edit": "edit_file", "edit_file": "edit_file",
    "execute": "execute_command", "execute_command": "execute_command",
    "list": "list_directory", "list_directory": "list_directory",
    "find": "find_files", "find_files": "find_files",
    "search": "search_text", "search_text": "search_text",
    "task_complete": "task_complete",
}


def parse_tool_calls(text: str) -> list:
    """Extract tool calls from LLM response. Handles multiple formats."""
    calls = []

    # Format 1: <tool_call>{"name": ..., "args": ...}</tool_call>
    for match in re.finditer(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, re.DOTALL):
        try:
            call = json.loads(match.group(1))
            if "name" in call:
                calls.append(call)
        except json.JSONDecodeError:
            pass

    if calls:
        return calls

    # Format 2: XML invoke style (Anthropic-like)
    # <invoke name="write_file"><parameter name="path">...</parameter>...</invoke>
    # Also handles <invoke name="write"> variant
    for match in re.finditer(
        r'<invoke\s+name="(\w+)">(.*?)</invoke>', text, re.DOTALL
    ):
        invoke_name = match.group(1)
        tool_name = XML_NAME_MAP.get(invoke_name)
        if not tool_name:
            continue
        body = match.group(2)
        args = {}
        for pmatch in re.finditer(
            r'<parameter\s+name="(\w+)">(.*?)</parameter>', body, re.DOTALL
        ):
            param_name = pmatch.group(1)
            param_val = pmatch.group(2).strip()
            mapped = XML_PARAM_MAP.get(param_name, param_name)
            args[mapped] = param_val
        calls.append({"name": tool_name, "args": args})

    if calls:
        return calls

    # Format 3: ```tool_call or ```json blocks
    for match in re.finditer(r'```(?:tool_call|json)?\s*\n(\{.*?\})\s*\n```', text, re.DOTALL):
        try:
            call = json.loads(match.group(1))
            if "name" in call and "args" in call:
                calls.append(call)
        except json.JSONDecodeError:
            pass

    if calls:
        return calls

    # Format 4: Inline JSON
    for match in re.finditer(
        r'(\{"name"\s*:\s*"(?:' + "|".join(TOOL_NAMES) + r')".*?\})',
        text, re.DOTALL
    ):
        try:
            call = json.loads(match.group(1))
            if "name" in call:
                calls.append(call)
        except json.JSONDecodeError:
            pass

    return calls


# --- Agent Loop ---

def execute_tools(tool_calls: list, explore_count: int, repeat_tracker: dict) -> tuple:
    """Execute parsed tool calls. Returns (results_list, is_done, explore_count)."""
    results = []
    done = False

    for call in tool_calls:
        name = call["name"]
        args = call.get("args", {})

        # Explore budget
        if name in EXPLORE_TOOLS:
            explore_count += 1
            if explore_count > EXPLORE_BUDGET:
                results.append(f"[Tool {name}] Budget exceeded ({EXPLORE_BUDGET} explore calls). Make changes or finish.")
                continue

        # Loop detection
        call_key = f"{name}:{json.dumps(args, sort_keys=True)}"
        repeat_tracker[call_key] = repeat_tracker.get(call_key, 0) + 1
        if repeat_tracker[call_key] > 2:
            results.append(f"[Tool {name}] Blocked: identical call repeated 3 times.")
            continue

        fn = TOOL_MAP.get(name)
        if not fn:
            results.append(f"[Tool {name}] Unknown tool")
            continue

        print(f"  > {name}({json.dumps(args, ensure_ascii=False)[:100]})")
        result = fn(args)

        if name == "task_complete" and result.get("done"):
            print(f"\n[Done] {result.get('summary', '')}")
            done = True
            break

        result_str = json.dumps(result, ensure_ascii=False)
        if len(result_str) > 15000:
            result_str = result_str[:15000] + "...(truncated)"
        results.append(f"[RESULT {name}]\n{result_str}\n[/RESULT]")

    return results, done, explore_count


def try_auto_repair():
    """Run auto_update to refresh signature secret on auth failure."""
    try:
        from auto_update import fetch_page, extract_bundle_urls, find_signature_secret, update_config
        print("  [Auto-repairing: fetching latest config...]")
        html = fetch_page()
        urls = extract_bundle_urls(html)
        info = find_signature_secret(urls)
        if info["secret"]:
            update_config(info["secret"], info["bundle_version"])
            # Reload the secret in minimax_client
            import minimax_client
            minimax_client.SIGNATURE_SECRET = info["secret"]
            print(f"  [Repaired: secret={info['secret'][:20]}...]")
            return True
    except Exception as e:
        print(f"  [Auto-repair failed: {e}]")
    return False


def call_llm(history: list) -> str:
    """Build prompt from history, compact if needed, call MiniMax."""
    prompt = "\n\n".join(history)

    if len(prompt) > 80000:
        history[:] = history[:1] + history[-8:]
        prompt = "\n\n".join(history)
        print("  [history compacted]")

    try:
        return send_message(prompt)
    except Exception as e:
        err = str(e)
        if "401" in err or "403" in err or "signature" in err.lower():
            if try_auto_repair():
                return send_message(prompt)  # Retry after repair
        raise


# --- Slash Commands ---

def cmd_account(args: str):
    """Manage accounts. /account add | list | switch N | remove N"""
    from minimax_client import _accounts, _account_index, _activate_account, CONFIG_PATH

    parts = args.strip().split()
    sub = parts[0] if parts else "list"

    if sub == "list":
        for i, acct in enumerate(_accounts):
            marker = " *" if i == _account_index else "  "
            uid = acct.get("real_user_id", "?")
            # Decode name from JWT
            name = "?"
            try:
                payload = json.loads(base64.b64decode(acct["token"].split('.')[1] + '=='))
                name = payload.get("user", {}).get("name", "?")
            except Exception:
                pass
            print(f"{marker}[{i + 1}] {name} (uid: {uid})")

    elif sub == "add":
        print("Paste JWT token (_token from localStorage):")
        try:
            token = input("  token> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not token:
            return
        print("Paste realUserID (from user_detail_agent.realUserID):")
        try:
            real_user_id = input("  real_user_id> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not real_user_id:
            return

        device_id = "0"
        try:
            payload = json.loads(base64.b64decode(token.split('.')[1] + '=='))
            device_id = str(payload.get("user", {}).get("deviceID", "0"))
            name = payload.get("user", {}).get("name", "unknown")
        except Exception:
            name = "unknown"

        new_acct = {"token": token, "real_user_id": real_user_id, "device_id": device_id}
        _accounts.append(new_acct)
        _save_accounts()
        print(f"  Added account: {name} (total: {len(_accounts)})")

    elif sub == "switch" and len(parts) > 1:
        try:
            idx = int(parts[1]) - 1
            if 0 <= idx < len(_accounts):
                _activate_account(idx)
                print(f"  Switched to account {idx + 1}")
            else:
                print(f"  Invalid index. Use 1-{len(_accounts)}")
        except ValueError:
            print("  Usage: /account switch N")

    elif sub == "remove" and len(parts) > 1:
        try:
            idx = int(parts[1]) - 1
            if 0 <= idx < len(_accounts):
                removed = _accounts.pop(idx)
                _save_accounts()
                if _account_index >= len(_accounts):
                    _activate_account(0)
                print(f"  Removed account {idx + 1}")
            else:
                print(f"  Invalid index. Use 1-{len(_accounts)}")
        except ValueError:
            print("  Usage: /account remove N")

    else:
        print("Usage: /account [list|add|switch N|remove N]")


def _save_accounts():
    """Write current accounts list back to config.json."""
    from minimax_client import _accounts, CONFIG_PATH, SIGNATURE_SECRET

    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}

    cfg["accounts"] = [
        {"token": a["token"], "real_user_id": a["real_user_id"], "device_id": a.get("device_id", "0")}
        for a in _accounts
    ]
    cfg["signature_secret"] = SIGNATURE_SECRET

    # Remove old single-account fields if accounts array exists
    for key in ("token", "real_user_id", "device_id"):
        cfg.pop(key, None)

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)


def cmd_update(_args: str):
    """Run auto_update to refresh signature secret."""
    try_auto_repair()


def cmd_status(_args: str):
    """Show current status."""
    from minimax_client import _accounts, _account_index, SIGNATURE_SECRET
    acct = _accounts[_account_index]
    name = "?"
    try:
        payload = json.loads(base64.b64decode(acct["token"].split('.')[1] + '=='))
        name = payload.get("user", {}).get("name", "?")
        exp = payload.get("exp", 0)
        import datetime
        exp_str = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
    except Exception:
        exp_str = "?"
    print(f"  Account: {name} ({_account_index + 1}/{len(_accounts)})")
    print(f"  Token expires: {exp_str}")
    print(f"  Secret: {SIGNATURE_SECRET[:20]}...")
    print(f"  CWD: {CWD}")


def cmd_help(_args: str):
    """Show available slash commands."""
    print("  /account [list|add|switch N|remove N]  Manage accounts")
    print("  /status                                Show current status")
    print("  /update                                Refresh signature secret")
    print("  /clear                                 Clear conversation history")
    print("  /help                                  Show this help")
    print("  exit                                   Quit")


def cmd_clear(_args: str):
    """Clear conversation history."""
    # Signal handled in main loop via return value
    print("  [History cleared]")


SLASH_COMMANDS = {
    "account": cmd_account,
    "status": cmd_status,
    "update": cmd_update,
    "clear": cmd_clear,
    "help": cmd_help,
}


def handle_input(prompt_str: str = "\n> ") -> str:
    """Get user input. Returns None on exit, handles slash commands."""
    try:
        user_input = input(prompt_str)
    except (EOFError, KeyboardInterrupt):
        print("\nBye!")
        return None

    stripped = user_input.strip()
    if stripped.lower() in ('exit', 'quit', 'q'):
        return None

    # Slash commands
    if stripped.startswith("/"):
        parts = stripped[1:].split(None, 1)
        cmd_name = parts[0].lower() if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else ""
        handler = SLASH_COMMANDS.get(cmd_name)
        if handler:
            handler(cmd_args)
            if cmd_name == "clear":
                return "__clear__"
            return ""  # Empty = don't send to LLM, prompt again
        else:
            print(f"  Unknown command: /{cmd_name}. Type /help")
            return ""

    return user_input


def main():
    load_config()

    history = []
    system = SYSTEM_PROMPT.replace("{cwd}", CWD)
    history.append(f"<<SYSTEM>>\n{system}\n<</SYSTEM>>")

    explore_count = 0
    repeat_tracker = {}

    print(f"MiniMax M2.7 Coding Agent")
    print(f"cwd: {CWD}")
    print(f"Type /help for commands, 'exit' to quit\n")

    # Get first user input or from argv
    if len(sys.argv) > 1:
        user_input = " ".join(sys.argv[1:])
        print(f"> {user_input}\n")
    else:
        user_input = handle_input("> ")
        while user_input is not None and not user_input.strip():
            user_input = handle_input("> ")
        if user_input is None:
            return

    history.append(f"<<USER>>\n{user_input}\n<</USER>>")

    while True:
        # Call LLM
        try:
            response = call_llm(history)
        except KeyboardInterrupt:
            print("\n[Interrupted]")
            user_input = handle_input()
            if user_input is None:
                break
            if not user_input.strip():
                continue
            history.append(f"<<USER>>\n{user_input}\n<</USER>>")
            continue
        except Exception as e:
            print(f"\n[API Error: {e}]")
            time.sleep(3)
            continue

        history.append(f"<<ASSISTANT>>\n{response}\n<</ASSISTANT>>")

        # Parse tool calls
        tool_calls = parse_tool_calls(response)

        # Print the text part (strip tool_call blocks, XML invokes, and role tags)
        clean_text = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL)
        clean_text = re.sub(r'<invoke\s+name="[^"]*">.*?</invoke>', '', clean_text, flags=re.DOTALL)
        clean_text = re.sub(r'</(?:minimax:)?tool_call>', '', clean_text)
        clean_text = re.sub(r'<</?(?:SYSTEM|USER|ASSISTANT|RESULT)>>', '', clean_text).strip()
        if clean_text:
            print(f"\n{clean_text}")

        if tool_calls:
            results, done, explore_count = execute_tools(tool_calls, explore_count, repeat_tracker)
            explore_count = 0

            if done:
                user_input = handle_input()
                if user_input is None:
                    break
                if not user_input.strip():
                    continue
                history.append(f"<<USER>>\n{user_input}\n<</USER>>")
                continue

            if results:
                history.append("\n".join(results))
            continue

        else:
            # Conversational response. Wait for user.
            user_input = handle_input()
            if user_input is None:
                break
            if not user_input.strip():
                continue
            # /clear command
            if user_input.strip() == "__clear__":
                history = [history[0]]
                print("  [History cleared]")
                continue
            history.append(f"<<USER>>\n{user_input}\n<</USER>>")


if __name__ == "__main__":
    main()
