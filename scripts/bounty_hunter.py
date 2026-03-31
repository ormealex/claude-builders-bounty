#!/usr/bin/env python3
"""
Bounty hunter scanner — finds open bounties across all reachable platforms.

Sources:
  - claude-builders-bounty (direct GitHub issues)
  - Algora        (GitHub label: algora-bounty + console.algora.io API for known orgs)
  - IssueHunt     (GitHub label: IssueHunt)
  - Gitcoin       (REST API: gitcoin.co/api/v0.1/bounties)
  - GitHub broad  (label variants: bounty, $bounty, has-bounty, Help Wanted+paid)
  - Contra        (web scrape — Replit's successor platform)

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
]


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


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  HTML fetch error {url}: {e}", file=sys.stderr)
        return ""


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
    m = re.search(r"(\d[\d,]*)\s*(USDC|DAI|ETH)", text, re.IGNORECASE)
    if m:
        # rough ETH conversion guard — skip if looks like ETH amount
        val = int(m.group(1).replace(",", ""))
        return val if m.group(2).upper() in ("USDC", "DAI") else 0
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
    """Algora bounties announced on GitHub issues (label: algora-bounty or 💎 Bounty)."""
    print("Scanning Algora (GitHub labels)...")
    results = []
    seen = set()
    queries = [
        "label:algora-bounty+state:open",
        'label:"💎 Bounty"+state:open',
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
    print(f"  {len(results)} Algora bounty/bounties found on GitHub")
    return results


def scan_algora_api():
    """Algora platform API — known active orgs."""
    print("Scanning Algora API (known orgs)...")
    results = []
    # Sample of active orgs on Algora — expand as needed
    orgs = ["calcom", "formbricks", "documenso", "twenty", "dub", "Cap", "openbb-finance"]
    seen = set()
    for org in orgs:
        data = fetch_json(f"https://console.algora.io/api/orgs/{org}/bounties?limit=20")
        for item in (data or {}).get("items", []):
            if item.get("status") != "active":
                continue
            task = item.get("task", {})
            repo = f"{task.get('repo_owner', '')}/{task.get('repo_name', '')}"
            issue_url = f"https://github.com/{repo}/issues/{task.get('number', '')}"
            if issue_url in seen:
                continue
            seen.add(issue_url)
            budget = (item.get("reward") or {}).get("amount", 0)
            # fetch issue title
            issue = gh(f"/repos/{repo}/issues/{task.get('number', '')}")
            title = (issue or {}).get("title", f"{org} bounty #{task.get('number')}")
            body = (issue or {}).get("body", "")
            results.append(make_task(
                "algora",
                title, issue_url, budget, body,
                number=task.get("number"),
                repo=repo,
            ))
    print(f"  {len(results)} Algora API bounty/bounties from known orgs")
    return results


def scan_issuehunt():
    """IssueHunt bounties via GitHub label search."""
    print("Scanning IssueHunt (GitHub labels)...")
    results = []
    seen = set()
    queries = [
        "label:IssueHunt+state:open",
        "label:issuehunt+state:open",
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
                "issuehunt",
                item["title"], item["html_url"], budget,
                item.get("body"),
                number=item["number"],
                repo=repo,
            ))
    print(f"  {len(results)} IssueHunt bounty/bounties found")
    return results


def scan_gitcoin():
    """Gitcoin REST API filtered for infra/devops."""
    print("Scanning Gitcoin...")
    results = []
    keywords = "kubernetes,ansible,terraform,prometheus,grafana,devops,infrastructure,python,bash,monitoring"
    url = f"https://gitcoin.co/api/v0.1/bounties/?network=mainnet&status=open&keywords={keywords}&order_by=-web3_created&limit=30"
    data = fetch_json(url)
    for item in (data or []):
        if not item.get("is_open"):
            continue
        budget_eth = item.get("value_in_usdt_now") or item.get("value_in_usdt") or 0
        try:
            budget = int(float(budget_eth))
        except (TypeError, ValueError):
            budget = 0
        results.append(make_task(
            "gitcoin",
            item.get("title", "Untitled"),
            item.get("url") or item.get("github_url", ""),
            budget,
            item.get("issue_description", "") or item.get("description", ""),
            repo=item.get("github_url", "").replace("https://github.com/", "").rsplit("/issues/", 1)[0],
        ))
    print(f"  {len(results)} Gitcoin bounty/bounties found")
    return results


def scan_github_broad():
    """Broad GitHub label search covering common bounty label variants."""
    print("Scanning GitHub (broad label variants)...")
    results = []
    seen = set()

    skill_terms = "kubernetes+OR+ansible+OR+terraform+OR+prometheus+OR+devops+OR+infrastructure+OR+monitoring+OR+bash+OR+python"
    label_queries = [
        f"label:bounty+state:open+({skill_terms})",
        f"label:%22has+bounty%22+state:open",
        f"label:%22%24bounty%22+state:open",
        f"label:%22help+wanted%22+label:bounty+state:open",
        f"label:%22good+first+issue%22+label:bounty+state:open+({skill_terms})",
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


def scan_contra():
    """Contra — Replit's bounty platform successor. HTML scrape for DevOps/infra gigs."""
    print("Scanning Contra...")
    results = []
    html = fetch_html("https://contra.com/opportunities?category=development&subcategory=devops-sysadmin")
    # Extract opportunity titles and links from JSON embedded in page
    matches = re.findall(r'"title":"([^"]+)","slug":"([^"]+)"[^}]*"rateMin":(\d+)', html)
    for title, slug, rate in matches[:20]:
        budget = int(rate)
        url = f"https://contra.com/opportunity/{slug}"
        results.append(make_task("contra", title, url, budget, ""))
    print(f"  {len(results)} Contra gig(s) found")
    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_task(task):
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

    # Platform bonuses
    if task.get("platform") == "claude-builders-bounty":
        score += 1  # known platform, direct PR submission
    if task.get("platform") in ("algora", "issuehunt"):
        score += 1  # GitHub-based, can submit PR directly

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

    # Run all scanners
    all_tasks = []
    all_tasks.extend(scan_claude_builders())
    all_tasks.extend(scan_algora_github())
    all_tasks.extend(scan_algora_api())
    all_tasks.extend(scan_issuehunt())
    all_tasks.extend(scan_gitcoin())
    all_tasks.extend(scan_github_broad())
    all_tasks.extend(scan_contra())

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
            reason = "low-budget" if task.get("budget", 0) < MIN_BUDGET else f"score={score}"
            print(f"  SKIP ({reason}): {task['title'][:60]}")
            skipped_log.append({
                "date": today,
                "platform": task["platform"],
                "title": task["title"],
                "url": task["url"],
                "budget": f"${task.get('budget', 0)}",
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
