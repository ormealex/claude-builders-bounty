#!/usr/bin/env python3
"""
Bounty hunter scanner — finds open bounties across all reachable platforms.

Sources:
  - claude-builders-bounty (direct GitHub issues)
  - Algora        (GitHub label: algora-bounty / 💎 Bounty)
  - IssueHunt     (GitHub label: IssueHunt)
  - Opire         (GitHub label: opire)
  - GitHub broad  (label variants: bounty, has-bounty, Help Wanted+bounty)

Outputs shortlist.json for the agent to act on.
State persisted in attempted_tasks.json.

Usage:
  GITHUB_TOKEN=ghp_... python3 bounty_hunter.py
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORK = "ormealex/claude-builders-bounty"
CLAUDE_BOUNTY_REPO = "claude-builders-bounty/claude-builders-bounty"
STATE_FILE = "attempted_tasks.json"
SHORTLIST_FILE = "shortlist.json"
MIN_BUDGET = 50
MAX_DAILY_ATTEMPTS = 3

# Skills we can deliver — used for scoring
SKILL_KEYWORDS = [
    "kubernetes", "k8s", "helm", "k3s",
    "ansible", "playbook",
    "terraform", "opentofu", "tofu",
    "github actions", "gitlab", "ci/cd", "pipeline",
    "docker", "compose", "dockerfile",
    "prometheus", "grafana", "alertmanager", "loki", "tempo",
    "nginx", "traefik", "haproxy",
    "python", "bash", "shell script",
    "aws", "digitalocean", "hetzner",
    "monitoring", "observability", "alerting",
    "infrastructure", "devops", "sre", "platform engineering",
    "runbook", "sop", "post-mortem",
    "k6", "load test",
    "claude code", "claude", "mcp", "llm", "ai agent",
    "n8n", "workflow", "automation",
    "hook", "skill", "changelog",
]

# Patterns that indicate low-quality or non-deliverable bounties
JUNK_PATTERNS = [
    r"\bRTC\b",                               # RustChain token, not real USD
    r"\bstar[s]?\s+(?:repos?|drive|campaign)\b",  # star campaigns
    r"\brefer\s+a\s+friend\b",               # referral bounties
    r"\bsocial\s+media\b",                   # social media promotion
    r"\bvideo\b.*\bbounty\b",               # video creation bounties
    r"\blogo\b.*\bbounty\b",                # logo design bounties
    r"\btweet\b",                            # Twitter/social tasks
    r"₹",                                    # Indian rupees (very low USD value)
    r"¥",                                    # Yen
]


def _is_mostly_ascii(text):
    """Require at least 50% ASCII — filters fully non-English titles."""
    if not text:
        return True
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) > 0.5


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def gh(path, method="GET", data=None):
    url = f"https://api.github.com{path}"
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
        print(f"  GitHub API error {path}: {e}", file=sys.stderr)
        return None


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "bounty-hunter/1.0",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  Fetch error {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

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
    text = text or ""
    m = re.search(r"\$\s*(\d[\d,]*)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"(\d[\d,]*)\s*USD", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"(\d[\d,]*)\s*(USDC|DAI)", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


def make_task(platform, title, url, budget, body, **extra):
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "budget": budget,
        "body": (body or "")[:3000],
        **extra,
    }


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def scan_claude_builders():
    print("Scanning claude-builders-bounty...")
    issues = gh(f"/repos/{CLAUDE_BOUNTY_REPO}/issues?state=open&labels=bounty&per_page=50")
    results = []
    for issue in (issues or []):
        budget = parse_budget(issue.get("title", "") + " " + (issue.get("body") or ""))
        results.append(make_task(
            "claude-builders-bounty",
            issue["title"], issue["html_url"], budget,
            issue.get("body"),
            number=issue["number"],
            repo=CLAUDE_BOUNTY_REPO,
        ))
    print(f"  {len(results)} bounty issue(s)")
    return results


def scan_algora_github():
    """Algora bounties on GitHub issues (label: algora-bounty or 💎 Bounty)."""
    print("Scanning Algora (GitHub labels)...")
    results = []
    seen = set()
    queries = [
        "label:algora-bounty+state:open",
        'label:"💎+Bounty"+state:open',
    ]
    for q in queries:
        data = gh(f"/search/issues?q={urllib.parse.quote(q)}&sort=created&order=desc&per_page=30")
        for item in (data or {}).get("items", []):
            if item["html_url"] in seen:
                continue
            seen.add(item["html_url"])
            budget = parse_budget(item.get("title", "") + " " + (item.get("body") or ""))
            repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
            results.append(make_task(
                "algora",
                item["title"], item["html_url"], budget,
                item.get("body"),
                number=item["number"],
                repo=repo,
            ))
    print(f"  {len(results)} Algora bounty/bounties found")
    return results


def scan_issuehunt():
    """IssueHunt bounties via GitHub label search."""
    print("Scanning IssueHunt (GitHub labels)...")
    results = []
    seen = set()
    for q in ["label:IssueHunt+state:open", "label:issuehunt+state:open"]:
        data = gh(f"/search/issues?q={urllib.parse.quote(q)}&sort=created&order=desc&per_page=30")
        for item in (data or {}).get("items", []):
            if item["html_url"] in seen:
                continue
            seen.add(item["html_url"])
            budget = parse_budget(item.get("title", "") + " " + (item.get("body") or ""))
            repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
            results.append(make_task(
                "issuehunt",
                item["title"], item["html_url"], budget,
                item.get("body"),
                number=item["number"],
                repo=repo,
            ))
    print(f"  {len(results)} IssueHunt bounty/bounties found")
    return results


def scan_opire():
    """Opire bounties via GitHub label search."""
    print("Scanning Opire (GitHub labels)...")
    results = []
    seen = set()
    for q in ["label:opire+state:open", "label:opire-bounty+state:open"]:
        data = gh(f"/search/issues?q={urllib.parse.quote(q)}&sort=created&order=desc&per_page=30")
        for item in (data or {}).get("items", []):
            if item["html_url"] in seen:
                continue
            seen.add(item["html_url"])
            budget = parse_budget(item.get("title", "") + " " + (item.get("body") or ""))
            repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
            results.append(make_task(
                "opire",
                item["title"], item["html_url"], budget,
                item.get("body"),
                number=item["number"],
                repo=repo,
            ))
    print(f"  {len(results)} Opire bounty/bounties found")
    return results


def scan_github_broad():
    """Broad GitHub label search covering common bounty label variants."""
    print("Scanning GitHub (broad labels)...")
    results = []
    seen = set()

    label_queries = [
        "label:bounty+state:open+language:python",
        "label:bounty+state:open+language:shell",
        "label:bounty+state:open+language:dockerfile",
        "label:bounty+state:open+language:typescript",
        "label:bounty+state:open+language:go",
        'label:"has+bounty"+state:open',
        'label:"help+wanted"+label:bounty+state:open',
    ]

    for q in label_queries:
        data = gh(f"/search/issues?q={q}&sort=created&order=desc&per_page=20")
        for item in (data or {}).get("items", []):
            if item["html_url"] in seen:
                continue
            seen.add(item["html_url"])
            budget = parse_budget(item.get("title", "") + " " + (item.get("body") or ""))
            repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
            results.append(make_task(
                "github",
                item["title"], item["html_url"], budget,
                item.get("body"),
                number=item["number"],
                repo=repo,
            ))

    print(f"  {len(results)} GitHub broad bounty/bounties found")
    return results


# ---------------------------------------------------------------------------
# Scoring and filtering
# ---------------------------------------------------------------------------

def is_junk(task):
    """Return True if the task is a low-quality or non-deliverable bounty."""
    combined = ((task.get("title") or "") + " " + (task.get("body") or ""))
    title = task.get("title") or ""

    if not _is_mostly_ascii(title):
        return True

    for pattern in JUNK_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return True

    return False


def score_task(task):
    if is_junk(task):
        return 0

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

    combined = ((task.get("title") or "") + " " + (task.get("body") or "")).lower()

    matches = sum(1 for kw in SKILL_KEYWORDS if kw in combined)
    score += min(matches, 3)

    vague = ["tbd", "to be defined", "discuss", "scope unclear", "requirements tbd"]
    if any(v in combined for v in vague):
        score -= 2

    if task.get("platform") == "claude-builders-bounty":
        score += 1
    if task.get("platform") in ("algora", "issuehunt", "opire"):
        score += 1

    return min(max(score, 0), 10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        print("Error: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    log = load_json(STATE_FILE, [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    attempts_today = sum(1 for t in log if t.get("date") == today and t.get("status") == "attempted")

    if attempts_today >= MAX_DAILY_ATTEMPTS:
        print(f"Daily limit reached ({MAX_DAILY_ATTEMPTS} attempts today). Skipping.")
        save_json(SHORTLIST_FILE, [])
        return

    remaining = MAX_DAILY_ATTEMPTS - attempts_today

    all_tasks = []
    all_tasks.extend(scan_claude_builders())
    all_tasks.extend(scan_algora_github())
    all_tasks.extend(scan_issuehunt())
    all_tasks.extend(scan_opire())
    all_tasks.extend(scan_github_broad())

    # Deduplicate by URL
    seen_urls = set()
    deduped = []
    for t in all_tasks:
        if t["url"] and t["url"] not in seen_urls:
            seen_urls.add(t["url"])
            deduped.append(t)
    all_tasks = deduped

    print(f"\nTotal unique tasks found: {len(all_tasks)} — filtering...")

    shortlist = []
    skipped_log = []

    for task in all_tasks:
        if already_attempted(task["url"], log):
            print(f"  SKIP (attempted): {task['title'][:60]}")
            continue

        score = score_task(task)
        task["score"] = score

        if score >= 7:
            shortlist.append(task)
            print(f"  SHORTLIST score={score} ${task.get('budget','?'):>4} [{task['platform']}] {task['title'][:55]}")
        else:
            budget = task.get("budget", 0)
            if budget < MIN_BUDGET:
                reason = "low-budget"
            elif is_junk(task):
                reason = "junk"
            else:
                reason = f"score={score}"
            print(f"  SKIP ({reason}): {task['title'][:60]}")
            skipped_log.append({
                "date": today,
                "platform": task["platform"],
                "title": task["title"],
                "url": task["url"],
                "budget": f"${budget}",
                "status": "skipped",
                "deliverable_path": None,
                "notes": reason,
            })

    shortlist.sort(key=lambda t: (-t["score"], -t.get("budget", 0)))
    shortlist = shortlist[:remaining]

    print(f"\nFinal shortlist: {len(shortlist)} task(s) to attempt today")

    log.extend(skipped_log)
    save_json(STATE_FILE, log)
    save_json(SHORTLIST_FILE, shortlist)
    print("State and shortlist saved.")


if __name__ == "__main__":
    main()
