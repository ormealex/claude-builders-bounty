#!/usr/bin/env python3
"""
Claude Code Pre-Tool-Use Security Guard Hook
Blocks destructive bash commands before they execute.

Location: ~/.claude/hooks/pre_tool_use_guard.py
Logs all blocked attempts to: ~/.claude/hooks/blocked.log
"""

import json
import sys
import os
import re
from datetime import datetime, timezone


# ── Log file location ──────────────────────────────────────────────────────────
HOOK_DIR = os.path.expanduser("~/.claude/hooks")
LOG_FILE = os.path.join(HOOK_DIR, "blocked.log")


# ── Destructive patterns and their human-readable explanations ─────────────────
DESTRUCTIVE_PATTERNS = [
    {
        "name": "rm -rf (recursive force delete)",
        "pattern": re.compile(
            r"\brm\s+(?:[^\|;&\n]*\s)?-[^\s]*f[^\s]*r[^\s]*\s|"
            r"\brm\s+(?:[^\|;&\n]*\s)?-[^\s]*r[^\s]*f[^\s]*\s|"
            r"\brm\s+-rf\b|\brm\s+-fr\b",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `rm -rf` is a recursive force-delete command that permanently "
            "removes files and directories with no confirmation. This is irreversible. "
            "If you need to delete something, use a safer alternative:\n"
            "  • `rm -i` (interactive, asks before each deletion)\n"
            "  • `trash` / `gio trash` (moves to trash, recoverable)\n"
            "  • `rm -r` without `-f` (prompts on protected files)"
        ),
    },
    {
        "name": "git push --force",
        "pattern": re.compile(
            r"\bgit\s+push\b(?:[^\|;&\n]*\s)--force\b|"
            r"\bgit\s+push\b(?:[^\|;&\n]*\s)-f\b",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `git push --force` rewrites remote history and can permanently "
            "destroy teammates' commits. Use safer alternatives:\n"
            "  • `git push --force-with-lease` (fails if remote has unseen commits)\n"
            "  • `git push --force-if-includes` (Git 2.30+, even safer)\n"
            "  • If you truly need a force-push, do it manually after verifying the diff."
        ),
    },
    {
        "name": "SQL DROP TABLE",
        "pattern": re.compile(
            r"\bDROP\s+TABLE\b",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `DROP TABLE` permanently deletes an entire database table and "
            "all its data. This cannot be undone without a backup. "
            "If this is intentional:\n"
            "  • Take a backup first: `pg_dump`, `mysqldump`, etc.\n"
            "  • Use `DROP TABLE IF EXISTS` + a transaction so you can ROLLBACK.\n"
            "  • Run the command manually after reviewing the impact."
        ),
    },
    {
        "name": "SQL TRUNCATE",
        "pattern": re.compile(
            r"\bTRUNCATE\b(?:\s+TABLE)?\s+\w",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `TRUNCATE` deletes ALL rows from a table instantly and cannot "
            "be rolled back in most databases (unlike DELETE). "
            "If this is intentional:\n"
            "  • Back up the data first.\n"
            "  • Run the command manually in a controlled environment.\n"
            "  • Consider `DELETE FROM <table> WHERE <condition>` for surgical removals."
        ),
    },
    {
        "name": "SQL DELETE without WHERE",
        "pattern": re.compile(
            r"\bDELETE\s+FROM\s+\w+\s*(?:;|$|\bLIMIT\b|\bRETURNING\b|\bUSING\b)",
            re.IGNORECASE | re.MULTILINE,
        ),
        "message": (
            "⛔  Blocked: `DELETE FROM <table>` without a WHERE clause will erase every "
            "row in the table. This is almost always a mistake. "
            "To proceed safely:\n"
            "  • Add a WHERE clause to target only the intended rows.\n"
            "  • Wrap in a transaction: `BEGIN; DELETE ...; SELECT COUNT(*); ROLLBACK;` "
            "to preview before committing.\n"
            "  • Run the command manually after double-checking the scope."
        ),
    },
    {
        "name": "git reset --hard",
        "pattern": re.compile(
            r"\bgit\s+reset\b(?:[^\|;&\n]*\s)?--hard\b",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `git reset --hard` permanently discards all uncommitted changes "
            "and moves HEAD to a previous commit. Any unstaged or staged work that hasn't "
            "been committed will be gone forever. Safer alternatives:\n"
            "  • `git stash` — saves your current changes so you can restore them later\n"
            "  • `git reset --soft HEAD~1` — undoes the last commit but keeps your changes staged\n"
            "  • `git restore .` — discards working tree changes file-by-file with more control"
        ),
    },
    {
        "name": "git clean -fd (force-delete untracked files)",
        "pattern": re.compile(
            r"\bgit\s+clean\b(?:[^\|;&\n]*\s)-[^\s]*f[^\s]*d[^\s]*\b|"
            r"\bgit\s+clean\b(?:[^\|;&\n]*\s)-[^\s]*d[^\s]*f[^\s]*\b",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `git clean -fd` permanently deletes all untracked files and "
            "directories — this includes new files you haven't committed yet. "
            "Safer alternatives:\n"
            "  • `git clean -n` (dry-run — shows what WOULD be deleted without deleting)\n"
            "  • `git stash -u` (stashes untracked files so you can restore them)\n"
            "  • Review `git status` first and delete specific files manually"
        ),
    },
    {
        "name": "SQL DROP DATABASE",
        "pattern": re.compile(
            r"\bDROP\s+DATABASE\b",
            re.IGNORECASE,
        ),
        "message": (
            "⛔  Blocked: `DROP DATABASE` permanently destroys an entire database — every "
            "table, index, function, and row it contains. This is irreversible without a "
            "backup. If this is truly intentional:\n"
            "  • Take a full backup first: `pg_dump`, `mysqldump`, etc.\n"
            "  • Verify you're connected to the correct server and database name.\n"
            "  • Run the command manually in a controlled session."
        ),
    },
]


def _ensure_log_dir() -> None:
    os.makedirs(HOOK_DIR, exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w") as f:
            f.write("# Claude Code Blocked Commands Log\n")
            f.write("# Format: ISO timestamp | pattern matched | project path | command\n\n")


def _log_blocked(pattern_name: str, command: str) -> None:
    _ensure_log_dir()
    timestamp = datetime.now(timezone.utc).isoformat()
    project_path = os.getcwd()
    safe_command = command.replace("\n", " ↵ ").replace("\r", "")
    entry = f"{timestamp} | {pattern_name} | {project_path} | {safe_command}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(entry)
    except OSError:
        pass  # never block on log failure


def _check_command(command: str):
    for rule in DESTRUCTIVE_PATTERNS:
        if rule["pattern"].search(command):
            return True, rule["message"], rule["name"]
    return False, "", ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[security-guard] WARNING: could not parse hook payload: {e}\n")
        sys.exit(0)

    tool_name: str = payload.get("tool_name", "")
    tool_input: dict = payload.get("tool_input", {})

    if tool_name not in ("Bash", "bash", "shell", "run_bash_command"):
        sys.exit(0)

    command: str = tool_input.get("command", "") or tool_input.get("cmd", "")
    if not command:
        sys.exit(0)

    blocked, message, pattern_name = _check_command(command)

    if blocked:
        _log_blocked(pattern_name, command)
        print(message, flush=True)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
