#!/usr/bin/env python3
"""
Bounty PR monitor — checks all open PRs by ormealex on
claude-builders-bounty/claude-builders-bounty for new maintainer activity.

Usage:
  GITHUB_TOKEN=ghp_... python3 pr_monitor.py

State is persisted in pr_state.json in the current working directory.
Notifications are posted as GitHub issues on ormealex/claude-builders-bounty.
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
BOT_SUFFIXES = ("[bot]",)
OWNER = "ormealex"


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


def is_bot(login):
    return login == OWNER or any(login.endswith(s) for s in BOT_SUFFIXES)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_updated": "", "prs": {}}


def save_state(state):
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_open_prs():
    """Fetch all open PRs by OWNER in TARGET — dynamic, picks up future PRs automatically."""
    prs = gh(f"/repos/{TARGET}/pulls?state=open&per_page=100")
    if not prs:
        return []
    return [p for p in prs if p["user"]["login"] == OWNER]


def get_latest_comment_id(pr_number):
    comments = gh(f"/repos/{TARGET}/issues/{pr_number}/comments?per_page=100")
    if not comments:
        return 0
    return max((c["id"] for c in comments), default=0)


def get_latest_review_id(pr_number):
    reviews = gh(f"/repos/{TARGET}/pulls/{pr_number}/reviews")
    if not reviews:
        return 0
    return max((r["id"] for r in reviews), default=0)


def get_new_comments(pr_number, since_id):
    comments = gh(f"/repos/{TARGET}/issues/{pr_number}/comments?per_page=100")
    if not comments:
        return []
    return [c for c in comments if c["id"] > since_id and not is_bot(c["user"]["login"])]


def get_new_reviews(pr_number, since_id):
    reviews = gh(f"/repos/{TARGET}/pulls/{pr_number}/reviews")
    if not reviews:
        return []
    return [r for r in reviews if r["id"] > since_id and not is_bot(r["user"]["login"])
            and r["state"] != "PENDING"]


def post_issue(title, body):
    # Ensure the pr-monitor label exists first
    gh(f"/repos/{FORK}/labels", method="POST",
       data={"name": "pr-monitor", "color": "0075ca"})
    result = gh(f"/repos/{FORK}/issues", method="POST",
                data={"title": title, "body": body, "labels": ["pr-monitor"]})
    if result:
        print(f"  Issue created: {result['html_url']}")
    return result


def main():
    if not TOKEN:
        print("Error: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    open_prs = get_open_prs()

    if not open_prs:
        print("No open PRs found for ormealex in target repo.")
        save_state(state)
        return

    print(f"Found {len(open_prs)} open PR(s): {[p['number'] for p in open_prs]}")

    activity_lines = []
    first_run_prs = []

    for pr in open_prs:
        num = str(pr["number"])
        title = pr["title"]
        url = pr["html_url"]

        if num not in state["prs"]:
            # First time seeing this PR — establish baseline, no alert
            latest_comment = get_latest_comment_id(pr["number"])
            latest_review = get_latest_review_id(pr["number"])
            state["prs"][num] = {
                "last_comment_id": latest_comment,
                "last_review_id": latest_review,
                "state": "open",
                "title": title,
            }
            first_run_prs.append(num)
            print(f"  PR #{num}: baseline set (comment_id={latest_comment}, review_id={latest_review})")
            continue

        pr_state = state["prs"][num]
        new_comments = get_new_comments(pr["number"], pr_state["last_comment_id"])
        new_reviews = get_new_reviews(pr["number"], pr_state["last_review_id"])

        # Check if PR was merged or closed
        pr_detail = gh(f"/repos/{TARGET}/pulls/{pr['number']}")
        current_state = "open"
        if pr_detail:
            if pr_detail.get("merged"):
                current_state = "merged"
            elif pr_detail.get("state") == "closed":
                current_state = "closed"

        state_changed = current_state != pr_state.get("state", "open")
        has_activity = new_comments or new_reviews or state_changed

        if has_activity:
            section = [f"### PR #{num}: [{title}]({url})"]

            if current_state == "merged":
                section.append("**BOUNTY WON** — PR was merged!")
            elif current_state == "closed":
                section.append("**REJECTED** — PR was closed without merge.")

            for c in new_comments:
                section.append(f"\n**Comment by @{c['user']['login']}** at {c['created_at']}:")
                section.append(f"> {c['body'][:1000]}")

            for r in new_reviews:
                section.append(f"\n**Review by @{r['user']['login']}**: {r['state']}")
                if r.get("body"):
                    section.append(f"> {r['body'][:500]}")

            activity_lines.append("\n".join(section))
            print(f"  PR #{num}: {len(new_comments)} new comment(s), {len(new_reviews)} new review(s), state={current_state}")

            # Update state
            if new_comments:
                pr_state["last_comment_id"] = max(c["id"] for c in new_comments)
            if new_reviews:
                pr_state["last_review_id"] = max(r["id"] for r in new_reviews)
            pr_state["state"] = current_state
            pr_state["title"] = title
        else:
            print(f"  PR #{num}: no new activity")

    if activity_lines:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        body = "## New activity on bounty PRs\n\n" + "\n\n---\n\n".join(activity_lines)
        if first_run_prs:
            body += f"\n\n---\n_New PRs added to monitoring: {', '.join('#' + n for n in first_run_prs)}_"
        post_issue(f"PR Monitor: New bounty activity — {today}", body)
    elif first_run_prs:
        print(f"First run — baseline established for PR(s): {', '.join(first_run_prs)}")
    else:
        print("No new activity on any PR.")

    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
