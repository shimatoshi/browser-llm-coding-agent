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

SYSTEM_PROMPT = """You are a coding agent running in a terminal on Android (Termux).
You have access to the following tools. To use a tool, output a tool call block in this exact format:

<tool_call>
{"name": "tool_name", "args": {"arg1": "value1"}}
</tool_call>

Available tools:

1. read_file: Read a file's contents
   args: {"path": "relative/or/absolute/path"}

2. write_file: Write content to a file (creates dirs if needed)
   args: {"path": "path", "content": "file content"}

3. edit_file: Replace a string in a file
   args: {"path": "path", "old_string": "text to find", "new_string": "replacement text"}

4. execute_command: Run a shell command
   args: {"command": "shell command here"}

5. list_directory: List files in a directory
   args: {"path": "directory path"}

6. find_files: Find files matching a glob pattern
   args: {"pattern": "**/*.py", "path": "optional/base/dir"}

7. search_text: Search for text/regex in files
   args: {"pattern": "search regex", "path": "optional/base/dir", "glob": "optional file glob"}

8. task_complete: Signal that the task is done
   args: {"summary": "what was accomplished"}

Rules:
- Always read a file before editing it.
- Use execute_command for builds, tests, git, etc.
- Work in the current directory: {cwd}
- You can call multiple tools in sequence in a single response.
- After executing tools, you'll receive the results. Use them to decide next steps.
- When the task is fully complete, call task_complete.
- Be concise in explanations. Focus on doing, not talking.
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

    # Format 2: ```tool_call or ```json blocks with tool call structure
    for match in re.finditer(r'```(?:tool_call|json)?\s*\n(\{.*?\})\s*\n```', text, re.DOTALL):
        try:
            call = json.loads(match.group(1))
            if "name" in call and "args" in call:
                calls.append(call)
        except json.JSONDecodeError:
            pass

    if calls:
        return calls

    # Format 3: Inline JSON with "name" and "args" keys
    for match in re.finditer(r'(\{"name"\s*:\s*"(?:read_file|write_file|edit_file|execute_command|list_directory|find_files|search_text|task_complete)".*?\})', text, re.DOTALL):
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
        results.append(f"[Tool Result: {name}]\n{result_str}")

    return results, done, explore_count


def call_llm(history: list) -> str:
    """Build prompt from history, compact if needed, call MiniMax."""
    prompt = "\n\n".join(history)

    if len(prompt) > 80000:
        history[:] = history[:1] + history[-8:]
        prompt = "\n\n".join(history)
        print("  [history compacted]")

    return send_message(prompt)


def main():
    load_config()

    history = []
    system = SYSTEM_PROMPT.replace("{cwd}", CWD)
    history.append(f"[System]\n{system}")

    explore_count = 0
    repeat_tracker = {}
    auto_continue = False  # True when agent is mid-task (tools were called)

    print(f"MiniMax M2.7 Coding Agent")
    print(f"cwd: {CWD}")
    print(f"Type 'exit' to quit, Ctrl+C to interrupt\n")

    # Get first user input or from argv
    if len(sys.argv) > 1:
        user_input = " ".join(sys.argv[1:])
        print(f"> {user_input}\n")
    else:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return
        if user_input.strip().lower() in ('exit', 'quit', 'q'):
            return

    history.append(f"[User]\n{user_input}")

    while True:
        # Call LLM
        try:
            response = call_llm(history)
        except KeyboardInterrupt:
            print("\n[Interrupted]")
            auto_continue = False
            try:
                user_input = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if user_input.strip().lower() in ('exit', 'quit', 'q'):
                break
            history.append(f"[User]\n{user_input}")
            continue
        except Exception as e:
            print(f"\n[API Error: {e}]")
            time.sleep(3)
            continue

        history.append(f"[Assistant]\n{response}")

        # Parse tool calls
        tool_calls = parse_tool_calls(response)

        # Print the text part (strip tool_call blocks)
        clean_text = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL).strip()
        if clean_text:
            print(f"\n{clean_text}")

        if tool_calls:
            # Execute tools automatically
            results, done, explore_count = execute_tools(tool_calls, explore_count, repeat_tracker)
            explore_count = 0  # Reset per turn

            if done:
                auto_continue = False
                # After task_complete, wait for new input
                try:
                    user_input = input("\n> ")
                except (EOFError, KeyboardInterrupt):
                    print("\nBye!")
                    break
                if user_input.strip().lower() in ('exit', 'quit', 'q'):
                    break
                history.append(f"[User]\n{user_input}")
                continue

            # Feed results back and auto-continue
            if results:
                history.append("[Tool Results]\n" + "\n\n".join(results))
            auto_continue = True
            continue

        else:
            # No tool calls = conversational response. Wait for user.
            auto_continue = False
            try:
                user_input = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if user_input.strip().lower() in ('exit', 'quit', 'q'):
                break
            if not user_input.strip():
                continue
            history.append(f"[User]\n{user_input}")


if __name__ == "__main__":
    main()
