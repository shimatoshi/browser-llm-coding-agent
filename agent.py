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
    """Extract tool calls from LLM response."""
    calls = []
    for match in re.finditer(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, re.DOTALL):
        try:
            call = json.loads(match.group(1))
            if "name" in call:
                calls.append(call)
        except json.JSONDecodeError:
            pass
    return calls


# --- Agent Loop ---

def run_agent(task: str):
    """Main agent loop."""
    history = []
    explore_count = 0
    repeat_tracker = {}

    system = SYSTEM_PROMPT.replace("{cwd}", CWD)
    history.append(f"[System]\n{system}")
    history.append(f"[User]\n{task}")

    print(f"\n{'='*60}")
    print(f"Task: {task}")
    print(f"Working directory: {CWD}")
    print(f"{'='*60}\n")

    for turn in range(MAX_TURNS):
        # Build prompt from history
        prompt = "\n\n".join(history)

        # Compact if too long
        if len(prompt) > 80000:
            # Keep system + user task + last 6 entries
            history = history[:2] + history[-6:]
            prompt = "\n\n".join(history)
            print("  [history compacted]", file=sys.stderr)

        # Call MiniMax
        print(f"--- Turn {turn + 1}/{MAX_TURNS} ---", file=sys.stderr)
        try:
            response = send_message(prompt)
        except Exception as e:
            print(f"API Error: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        history.append(f"[Assistant]\n{response}")

        # Parse tool calls
        tool_calls = parse_tool_calls(response)

        if not tool_calls:
            # No tool calls - print response and wait for user
            print(f"\n{response}\n")
            # Check if the model thinks it's done
            if any(phrase in response.lower() for phrase in ["task is complete", "task_complete", "all done", "finished"]):
                print("\n[Agent finished]")
                break
            # If no tool calls and not done, ask the model to continue
            history.append("[User]\nContinue with the task. Use tools to make progress.")
            continue

        # Execute tool calls
        results = []
        done = False

        # Print non-tool-call text
        clean_text = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL).strip()
        if clean_text:
            print(f"\n{clean_text}")

        for call in tool_calls:
            name = call["name"]
            args = call.get("args", {})

            # Explore budget
            if name in EXPLORE_TOOLS:
                explore_count += 1
                if explore_count > EXPLORE_BUDGET:
                    results.append(f"[Tool {name}] Budget exceeded ({EXPLORE_BUDGET} explore calls per turn). Make changes or finish.")
                    continue

            # Loop detection
            call_key = f"{name}:{json.dumps(args, sort_keys=True)}"
            repeat_tracker[call_key] = repeat_tracker.get(call_key, 0) + 1
            if repeat_tracker[call_key] > 2:
                results.append(f"[Tool {name}] Blocked: identical call repeated 3 times. Try a different approach.")
                continue

            # Execute
            fn = TOOL_MAP.get(name)
            if not fn:
                results.append(f"[Tool {name}] Unknown tool")
                continue

            print(f"  > {name}({json.dumps(args)[:100]})", file=sys.stderr)
            result = fn(args)

            if name == "task_complete" and result.get("done"):
                print(f"\n[Task Complete] {result.get('summary', '')}")
                done = True
                break

            # Format result
            result_str = json.dumps(result, ensure_ascii=False)
            if len(result_str) > 15000:
                result_str = result_str[:15000] + "...(truncated)"
            results.append(f"[Tool Result: {name}]\n{result_str}")

        if done:
            break

        # Reset explore budget each turn
        explore_count = 0

        # Feed results back
        if results:
            history.append("[Tool Results]\n" + "\n\n".join(results))
    else:
        print(f"\n[Agent stopped: max turns ({MAX_TURNS}) reached]")


def main():
    load_config()

    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        run_agent(task)
    else:
        print("MiniMax M2.7 Coding Agent (type 'exit' to quit)")
        print("=" * 50)
        while True:
            try:
                task = input("\nTask> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if task.strip().lower() in ('exit', 'quit', 'q'):
                break
            if not task.strip():
                continue
            run_agent(task)


if __name__ == "__main__":
    main()
