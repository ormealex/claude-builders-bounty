#!/usr/bin/env python3
"""
Bounty hunter scanner — finds open bounties and filters against already-attempted tasks.

Outputs shortlist.json for the agent to act on (build deliverable + submit PR).
State is persisted in attempted_tasks.json in the current working directory.

Usage:
  GITHUB_TOKEN=ghp_... python3 bounty_hunter.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORK = "ormealex/claude-builders-bounty"
CLAUDE_BOUNTY_REPO = "claude-builders-bounty/claude-builders-bounty"
STATE_FILE = "attempted_tasks.json"
SHORTLIST_FILE = "shortlist.json"
MIN_BUDGET = 50
MAX_DAILY_ATTEMPTS = 3


def gh(path, method="GET", data=None, base="https://api.github.com"):
    url = f"{base}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "bounty-hunter/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  API error for {path}: {e}", file=sys.stderr)
        return None


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Fetch error for {url}: {e}", file=sys.stderr)
        return ""


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def already_attempted(url, log):
    return any(t.get("url") == url for t in log)


def parse_budget(text):
    """Extract dollar amount from text like '$150' or '150 USD'."""
    import re
    m = re.search(r"\$\s*(\d+)", text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*USD", text or "", re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0


def scan_claude_builders():
    """Highest priority — issues on the claude-builders-bounty repo."""
    print("Scanning claude-builders-bounty...")
    issues = gh(f"/repos/{CLAUDE_BOUNTY_REPO}/issues?state=open&labels=bounty&per_page=50")
    results = []
    for issue in (issues or []):
        budget = parse_budget(issue.get("title", "") + issue.get("body", ""))
        results.append({
            "platform": "claude-builders-bounty",
            "title": issue["title"],
            "url": issue["html_url"],
            "api_url": issue["url"],
            "number": issue["number"],
            "budget": budget,
            "body": (issue.get("body") or "")[:3000],
        })
    print(f"  Found {len(results)} bounty issue(s)")
    return results


def scan_algora():
    """Scan Algora for open bounties."""
    print("Scanning Algora...")
    results = []
    # Algora exposes bounties as GitHub issues with a bounty label
    # Search GitHub for issues with algora-bounty label
    search_url = "/search/issues?q=label:algora-bounty+state:open+language:python+OR+language:shell+OR+kubernetes+OR+ansible+OR+terraform&sort=created&per_page=20"
    data = gh(search_url)
    for item in (data or {}).get("items", []):
        budget = parse_budget(item.get("title", "") + item.get("body", ""))
        if budget < MIN_BUDGET:
            continue
        results.append({
            "platform": "algora",
            "title": item["title"],
            "url": item["html_url"],
            "api_url": item["url"],
            "number": item["number"],
            "budget": budget,
            "body": (item.get("body") or "")[:3000],
            "repo": item.get("repository_url", "").replace("https://api.github.com/repos/", ""),
        })
    print(f"  Found {len(results)} qualifying Algora bounty/bounties")
    return results


def scan_gitcoin():
    """Scan GitHub for issues labelled as gitcoin bounties."""
    print("Scanning Gitcoin/GitHub bounties...")
    results = []
    queries = [
        "label:bounty+state:open+kubernetes+OR+ansible+OR+terraform+OR+prometheus",
        "label:bounty+state:open+devops+OR+infrastructure+OR+monitoring",
    ]
    seen = set()
    for q in queries:
        data = gh(f"/search/issues?q={q}&sort=created&per_page=10")
        for item in (data or {}).get("items", []):
            if item["html_url"] in seen:
                continue
            seen.add(item["html_url"])
            budget = parse_budget(item.get("title", "") + item.get("body", ""))
            if budget < MIN_BUDGET:
                continue
            results.append({
                "platform": "gitcoin",
                "title": item["title"],
                "url": item["html_url"],
                "api_url": item["url"],
                "number": item["number"],
                "budget": budget,
                "body": (item.get("body") or "")[:3000],
                "repo": item.get("repository_url", "").replace("https://api.github.com/repos/", ""),
            })
    print(f"  Found {len(results)} qualifying Gitcoin/GitHub bounty/bounties")
    return results


def score_task(task):
    """Score 1-10. Higher budget + clearer spec + infra/devops focus = higher score."""
    import re
    score = 5
    budget = task.get("budget", 0)
    if budget >= 300:
        score += 3
    elif budget >= 150:
        score += 2
    elif budget >= 100:
        score += 1
    elif budget < MIN_BUDGET:
        return 0

    body = (task.get("body") or "").lower()
    title = (task.get("title") or "").lower()
    combined = body + " " + title

    # Boost for strong capability match
    strong_matches = ["kubernetes", "ansible", "terraform", "prometheus", "grafana",
                      "ci/cd", "github actions", "gitlab", "docker", "helm",
                      "monitoring", "alerting", "runbook", "k8s", "devops", "sre",
                      "nginx", "traefik", "haproxy", "bash", "python script"]
    matches = sum(1 for kw in strong_matches if kw in combined)
    score += min(matches, 3)

    # Penalise vague or overly complex specs
    vague_signals = ["tbd", "to be defined", "discuss", "scope unclear"]
    if any(v in combined for v in vague_signals):
        score -= 2

    # claude-builders-bounty gets a +1 (known platform, direct PR submission)
    if task.get("platform") == "claude-builders-bounty":
        score += 1

    return min(score, 10)


def main():
    if not TOKEN:
        print("Error: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    log = load_json(STATE_FILE, [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    attempts_today = sum(1 for t in log if t.get("date") == today and t.get("status") == "attempted")

    if attempts_today >= MAX_DAILY_ATTEMPTS:
        print(f"Daily limit reached ({MAX_DAILY_ATTEMPTS} attempts today). Skipping scan.")
        save_json(SHORTLIST_FILE, [])
        return

    remaining = MAX_DAILY_ATTEMPTS - attempts_today

    # Scan all platforms
    all_tasks = []
    all_tasks.extend(scan_claude_builders())
    all_tasks.extend(scan_algora())
    all_tasks.extend(scan_gitcoin())

    print(f"\nTotal found: {len(all_tasks)} — filtering...")

    # Filter and score
    shortlist = []
    for task in all_tasks:
        if already_attempted(task["url"], log):
            print(f"  SKIP (already attempted): {task['title'][:60]}")
            continue
        score = score_task(task)
        task["score"] = score
        if score >= 7:
            shortlist.append(task)
            print(f"  SHORTLIST (score={score}): {task['title'][:60]} — ${task.get('budget', '?')}")
        else:
            print(f"  SKIP (score={score}): {task['title'][:60]}")

    # Sort by score desc, cap at daily remaining
    shortlist.sort(key=lambda t: (-t["score"], -t.get("budget", 0)))
    shortlist = shortlist[:remaining]

    print(f"\nShortlist: {len(shortlist)} task(s) to attempt")
    save_json(SHORTLIST_FILE, shortlist)

    # Save skipped tasks to log too
    for task in all_tasks:
        if not already_attempted(task["url"], log) and task not in shortlist:
            log.append({
                "date": today,
                "platform": task["platform"],
                "title": task["title"],
                "url": task["url"],
                "budget": f"${task.get('budget', 0)}",
                "status": "skipped",
                "deliverable_path": None,
                "notes": f"score={task.get('score', 0)}",
            })

    save_json(STATE_FILE, log)
    print("State saved.")


if __name__ == "__main__":
    main()
