#!/usr/bin/env python3
"""
Bounty PR monitor — detects new maintainer activity on all open PRs by ormealex.

Outputs pending_fixes.json when actionable feedback is found, so the calling
agent can implement the requested changes on the PR branch.

Usage:
  GITHUB_TOKEN=ghp_... python3 pr_monitor.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

TOKEN = os.environ.get("GITHUB_TOKEN", "")
TARGET = "claude-builders-bounty/claude-builders-bounty"
FORK = "ormealex/claude-builders-bounty"
STATE_FILE = "pr_state.json"
FIXES_FILE = "pending_fixes.json"
OWNER = "ormealex"
BOT_SUFFIXES = ("[bot]",)


def gh(path, method="GET", data=None):
    url = f"https://api.github.com{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "pr-monitor/1.0",
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"GitHub API error {e.code} for {path}: {e.read().decode()}", file=sys.stderr)
        return None


def is_ignored(login):
    return login == OWNER or any(login.endswith(s) for s in BOT_SUFFIXES)


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_open_prs():
    prs = gh(f"/repos/{TARGET}/pulls?state=open&per_page=100")
    return [p for p in (prs or []) if p["user"]["login"] == OWNER]


def max_id(items):
    return max((x["id"] for x in items), default=0) if items else 0


def new_items(items, since_id):
    return [x for x in (items or []) if x["id"] > since_id and not is_ignored(x.get("user", {}).get("login", ""))]


def post_issue(title, body):
    gh(f"/repos/{FORK}/labels", method="POST", data={"name": "pr-monitor", "color": "0075ca"})
    result = gh(f"/repos/{FORK}/issues", method="POST",
                data={"title": title, "body": body, "labels": ["pr-monitor"]})
    if result:
        print(f"  Notification issue: {result['html_url']}")


def main():
    if not TOKEN:
        print("Error: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    state = load_json(STATE_FILE, {"last_updated": "", "prs": {}})
    open_prs = get_open_prs()

    if not open_prs:
        print("No open PRs found.")
        save_json(STATE_FILE, {**state, "last_updated": datetime.now(timezone.utc).isoformat()})
        return

    print(f"Monitoring {len(open_prs)} open PR(s): {[p['number'] for p in open_prs]}")

    pending_fixes = []
    notification_sections = []
    first_seen = []

    for pr in open_prs:
        num = str(pr["number"])
        title = pr["title"]
        url = pr["html_url"]
        branch = pr["head"]["ref"]

        comments = gh(f"/repos/{TARGET}/issues/{pr['number']}/comments?per_page=100") or []
        reviews = gh(f"/repos/{TARGET}/pulls/{pr['number']}/reviews") or []
        pr_detail = gh(f"/repos/{TARGET}/pulls/{pr['number']}") or {}

        current_state = "open"
        if pr_detail.get("merged"):
            current_state = "merged"
        elif pr_detail.get("state") == "closed":
            current_state = "closed"

        if num not in state["prs"]:
            # First time seeing — establish baseline only
            state["prs"][num] = {
                "last_comment_id": max_id(comments),
                "last_review_id": max_id(reviews),
                "state": current_state,
                "title": title,
                "branch": branch,
            }
            first_seen.append(num)
            print(f"  PR #{num}: baseline established")
            continue

        pr_state = state["prs"][num]
        pr_state["branch"] = branch  # keep branch up to date
        pr_state["title"] = title

        new_comments = new_items(comments, pr_state["last_comment_id"])
        actionable_reviews = [
            r for r in reviews
            if r["id"] > pr_state["last_review_id"]
            and not is_ignored(r.get("user", {}).get("login", ""))
            and r["state"] in ("CHANGES_REQUESTED", "COMMENTED")
        ]
        state_changed = current_state != pr_state.get("state", "open")

        if not new_comments and not actionable_reviews and not state_changed:
            print(f"  PR #{num}: no new activity")
            continue

        print(f"  PR #{num}: {len(new_comments)} comment(s), {len(actionable_reviews)} review(s), state={current_state}")

        section_lines = [f"### PR #{num}: [{title}]({url}) (branch: `{branch}`)"]

        if current_state == "merged":
            section_lines.append("**BOUNTY WON** — PR was merged!")
        elif current_state == "closed":
            section_lines.append("**REJECTED** — PR was closed without merge.")

        fix_feedback = []

        for c in new_comments:
            section_lines.append(f"\n**@{c['user']['login']}** ({c['created_at']}):")
            section_lines.append(f"> {c['body'][:1000]}")
            fix_feedback.append({"type": "comment", "author": c["user"]["login"], "body": c["body"]})

        for r in actionable_reviews:
            section_lines.append(f"\n**@{r['user']['login']}** review: `{r['state']}`")
            if r.get("body"):
                section_lines.append(f"> {r['body'][:500]}")
                fix_feedback.append({"type": "review", "state": r["state"], "author": r["user"]["login"], "body": r["body"]})

        notification_sections.append("\n".join(section_lines))

        # Queue a fix if there is actionable feedback and PR is still open
        if fix_feedback and current_state == "open":
            pending_fixes.append({
                "pr_number": pr["number"],
                "pr_title": title,
                "pr_url": url,
                "branch": branch,
                "feedback": fix_feedback,
            })

        # Update state
        if new_comments:
            pr_state["last_comment_id"] = max(c["id"] for c in new_comments)
        if actionable_reviews:
            pr_state["last_review_id"] = max(r["id"] for r in actionable_reviews)
        pr_state["state"] = current_state

    # Post notification issue if there is any activity
    if notification_sections:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        body = "## Bounty PR activity detected\n\n" + "\n\n---\n\n".join(notification_sections)
        if pending_fixes:
            body += f"\n\n---\n_Auto-fix queued for {len(pending_fixes)} PR(s)._"
        post_issue(f"PR Monitor: activity detected — {today}", body)

    if first_seen:
        print(f"Baseline set for new PR(s): {first_seen}")

    # Write pending fixes for the agent to act on
    if pending_fixes:
        save_json(FIXES_FILE, pending_fixes)
        print(f"pending_fixes.json written with {len(pending_fixes)} fix(es) to implement.")
    elif os.path.exists(FIXES_FILE):
        os.remove(FIXES_FILE)

    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_FILE, state)
    print("State saved.")


if __name__ == "__main__":
    main()
