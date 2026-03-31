#!/usr/bin/env python3
"""
Bounty PR monitor — detects new maintainer activity on ALL open PRs by ormealex,
across every repo (claude-builders-bounty, Algora targets, IssueHunt, etc.).

Outputs pending_fixes.json when actionable feedback is found, so the calling
agent can implement the requested changes on the correct PR branch.

State keys use "owner/repo#number" format to avoid collisions across repos.

Usage:
  GITHUB_TOKEN=ghp_... python3 pr_monitor.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORK = "ormealex/claude-builders-bounty"
OWNER = "ormealex"
STATE_FILE = "pr_state.json"
FIXES_FILE = "pending_fixes.json"
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
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  GitHub API {e.code} {path}: {e.read().decode()[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  GitHub API error {path}: {e}", file=sys.stderr)
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


def repo_from_url(api_url):
    """Extract owner/repo from a GitHub API repository URL."""
    return api_url.replace("https://api.github.com/repos/", "").rstrip("/")


def get_open_prs():
    """
    Find ALL open PRs by ormealex across every public repo using GitHub search.
    Returns list of dicts with: number, title, html_url, repo, branch, pr_detail
    """
    query = urllib.parse.quote(f"is:pr is:open author:{OWNER}")
    results = []
    page = 1
    while True:
        data = gh(f"/search/issues?q={query}&per_page=100&page={page}")
        items = (data or {}).get("items", [])
        if not items:
            break
        for item in items:
            repo = repo_from_url(item.get("repository_url", ""))
            number = item["number"]
            # Fetch full PR details for branch name and merged status
            pr_detail = gh(f"/repos/{repo}/pulls/{number}") or {}
            results.append({
                "number": number,
                "title": item["title"],
                "html_url": item["html_url"],
                "repo": repo,
                "branch": pr_detail.get("head", {}).get("ref", ""),
                "pr_detail": pr_detail,
            })
        if len(items) < 100:
            break
        page += 1
    return results


def max_id(items):
    return max((x["id"] for x in items), default=0) if items else 0


def new_items(items, since_id):
    return [
        x for x in (items or [])
        if x["id"] > since_id and not is_ignored(x.get("user", {}).get("login", ""))
    ]


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

    print(f"Fetching all open PRs by {OWNER} across all repos...")
    open_prs = get_open_prs()

    if not open_prs:
        print("No open PRs found.")
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, state)
        return

    print(f"Found {len(open_prs)} open PR(s) across {len(set(p['repo'] for p in open_prs))} repo(s)")
    for pr in open_prs:
        print(f"  {pr['repo']}#{pr['number']} — {pr['title'][:60]}")

    pending_fixes = []
    notification_sections = []
    first_seen = []

    for pr in open_prs:
        repo = pr["repo"]
        num = pr["number"]
        state_key = f"{repo}#{num}"
        title = pr["title"]
        url = pr["html_url"]
        branch = pr["branch"]
        pr_detail = pr["pr_detail"]

        comments = gh(f"/repos/{repo}/issues/{num}/comments?per_page=100") or []
        reviews = gh(f"/repos/{repo}/pulls/{num}/reviews") or []

        current_state = "open"
        if pr_detail.get("merged"):
            current_state = "merged"
        elif pr_detail.get("state") == "closed":
            current_state = "closed"

        if state_key not in state["prs"]:
            # First time seeing this PR — establish baseline, no alert
            state["prs"][state_key] = {
                "last_comment_id": max_id(comments),
                "last_review_id": max_id(reviews),
                "state": current_state,
                "title": title,
                "branch": branch,
                "repo": repo,
            }
            first_seen.append(state_key)
            print(f"  {state_key}: baseline established")
            continue

        pr_state = state["prs"][state_key]
        pr_state["branch"] = branch
        pr_state["title"] = title
        pr_state["repo"] = repo

        new_comments = new_items(comments, pr_state["last_comment_id"])
        actionable_reviews = [
            r for r in reviews
            if r["id"] > pr_state["last_review_id"]
            and not is_ignored(r.get("user", {}).get("login", ""))
            and r["state"] in ("CHANGES_REQUESTED", "COMMENTED")
        ]
        state_changed = current_state != pr_state.get("state", "open")

        if not new_comments and not actionable_reviews and not state_changed:
            print(f"  {state_key}: no new activity")
            continue

        print(f"  {state_key}: {len(new_comments)} comment(s), {len(actionable_reviews)} review(s), state={current_state}")

        section_lines = [f"### [{repo}#{num}]({url}): {title} (branch: `{branch}`)"]

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
                fix_feedback.append({
                    "type": "review",
                    "state": r["state"],
                    "author": r["user"]["login"],
                    "body": r["body"],
                })

        notification_sections.append("\n".join(section_lines))

        # Queue a fix if there is actionable feedback and PR is still open
        if fix_feedback and current_state == "open":
            pending_fixes.append({
                "pr_number": num,
                "pr_title": title,
                "pr_url": url,
                "repo": repo,
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
        print(f"Baseline set for: {first_seen}")

    if pending_fixes:
        save_json(FIXES_FILE, pending_fixes)
        print(f"pending_fixes.json written — {len(pending_fixes)} fix(es) to implement.")
    elif os.path.exists(FIXES_FILE):
        os.remove(FIXES_FILE)

    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_FILE, state)
    print("State saved.")


if __name__ == "__main__":
    main()
