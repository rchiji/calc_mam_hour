#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

JST = timezone(timedelta(hours=9))
UTC = timezone.utc


class GhCliError(RuntimeError):
    pass


@dataclass
class ActivityEvent:
    repo: str
    kind: str  # "commit" or "issue_event"
    timestamp: datetime
    detail: str
    commit_sha: str = ""
    additions: int = 0
    deletions: int = 0
    changed_lines: int = 0


@dataclass(frozen=True)
class EstimateConfig:
    target_day: date
    gap_minutes: int = 60
    min_single_minutes: int = 20
    event_bonus_minutes: int = 10
    commit_bonus_threshold_lines: int = 20
    commit_bonus_lines_per_minute: int = 25
    max_commit_bonus_minutes: int = 30
    include_archived: bool = False
    all_visible_repos: bool = False
    sleep_seconds: float = 0.0
    verbose: bool = False


def run_gh(args: Sequence[str]) -> Tuple[int, str, str]:
    cmd = ["gh", *args]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=False,
        )
    except FileNotFoundError as exc:
        raise GhCliError("gh CLI not found. Install GitHub CLI first.") from exc

    stdout_text = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (completed.stderr or b"").decode("utf-8", errors="replace")
    return completed.returncode, stdout_text, stderr_text


def run_gh_json(args: Sequence[str], allow_error_return: bool = False) -> Any:
    rc, stdout_text, stderr_text = run_gh(args)

    if rc != 0:
        msg = stderr_text.strip() or stdout_text.strip() or "gh command failed"
        if allow_error_return:
            raise RuntimeError(msg)
        raise GhCliError(f"gh command failed.\nCommand: {' '.join(['gh', *args])}\nSTDERR: {msg}")

    if not stdout_text.strip():
        return None

    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        msg = stdout_text[:2000]
        if allow_error_return:
            raise RuntimeError(f"Failed to parse JSON from gh output:\n{msg}")
        raise GhCliError(f"Failed to parse JSON from gh output.\n{msg}") from exc


def gh_api_get(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    allow_error_return: bool = False,
) -> Any:
    args = ["api", "-X", "GET", endpoint]
    for key, value in (params or {}).items():
        if value is None:
            continue
        args.extend(["-f", f"{key}={value}"])
    return run_gh_json(args, allow_error_return=allow_error_return)


def gh_api_paginated(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    per_page: int = 100,
) -> List[Any]:
    items: List[Any] = []
    page = 1

    while True:
        request_params = dict(params or {})
        request_params["per_page"] = per_page
        request_params["page"] = page
        request_desc = path

        try:
            if params is None:
                sep = "&" if "?" in path else "?"
                paged = f"{path}{sep}per_page={per_page}&page={page}"
                request_desc = paged
                data = run_gh_json(["api", paged], allow_error_return=True)
            else:
                data = gh_api_get(path, request_params, allow_error_return=True)
                request_desc = f"{path} {request_params}"
        except RuntimeError as e:
            msg = str(e).lower()
            if "http 409" in msg and "repository is empty" in msg:
                return []
            raise

        if data is None:
            break

        if not isinstance(data, list):
            raise RuntimeError(f"Expected list response for path={request_desc}, got {type(data)}")

        items.extend(data)
        if len(data) < per_page:
            break
        page += 1

    return items


def gh_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        args.extend(["-F", f"{k}={v}"])

    data = run_gh_json(args, allow_error_return=False)

    if data is None:
        raise RuntimeError("GraphQL response was empty")
    if not isinstance(data, dict):
        raise RuntimeError(f"GraphQL response was not a dict: {type(data)}")
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")

    return data


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Estimate daily work hours from GitHub activity.")
    p.add_argument("--date", required=True, help="YYYY-MM-DD or 'yesterday' or 'today'")
    p.add_argument("--gap-minutes", type=int, default=60, help="Session split gap in minutes")
    p.add_argument("--min-single-minutes", type=int, default=20, help="Minimum minutes for single-event session")
    p.add_argument("--event-bonus-minutes", type=int, default=10, help="Bonus minutes if session has issue/PR event")
    p.add_argument(
        "--commit-bonus-threshold-lines",
        type=int,
        default=20,
        help="Ignore the first N changed lines in a session before commit bonus starts",
    )
    p.add_argument(
        "--commit-bonus-lines-per-minute",
        type=int,
        default=25,
        help="Changed lines per 1 extra bonus minute after the threshold",
    )
    p.add_argument(
        "--max-commit-bonus-minutes",
        type=int,
        default=30,
        help="Maximum commit diff bonus minutes per session",
    )
    p.add_argument("--include-archived", action="store_true", help="Include archived repos")
    p.add_argument("--all-visible-repos", action="store_true", help="Scan all visible private repos")
    p.add_argument("--sleep-seconds", type=float, default=0.0, help="Sleep between repo scans")
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.add_argument("--verbose", action="store_true", help="Verbose logging to stderr")
    return p.parse_args()


def config_from_args(args: argparse.Namespace) -> EstimateConfig:
    return EstimateConfig(
        target_day=parse_target_date(args.date),
        gap_minutes=args.gap_minutes,
        min_single_minutes=args.min_single_minutes,
        event_bonus_minutes=args.event_bonus_minutes,
        commit_bonus_threshold_lines=args.commit_bonus_threshold_lines,
        commit_bonus_lines_per_minute=args.commit_bonus_lines_per_minute,
        max_commit_bonus_minutes=args.max_commit_bonus_minutes,
        include_archived=args.include_archived,
        all_visible_repos=args.all_visible_repos,
        sleep_seconds=args.sleep_seconds,
        verbose=args.verbose,
    )


def parse_target_date(s: str) -> date:
    s = s.strip().lower()
    now_jst = datetime.now(JST)
    if s == "today":
        return now_jst.date()
    if s == "yesterday":
        return (now_jst - timedelta(days=1)).date()
    return datetime.strptime(s, "%Y-%m-%d").date()


def jst_day_bounds(target_day: date) -> Tuple[datetime, datetime]:
    start_jst = datetime.combine(target_day, dtime.min, tzinfo=JST)
    end_jst = datetime.combine(target_day, dtime.max, tzinfo=JST)
    return start_jst, end_jst


def jst_day_bounds_utc(target_day: date) -> Tuple[datetime, datetime]:
    start_jst, end_jst = jst_day_bounds(target_day)
    return start_jst.astimezone(UTC), end_jst.astimezone(UTC)


def isoformat_z(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_github_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def current_viewer() -> Dict[str, Any]:
    query = """
    query {
      viewer {
        login
        databaseId
      }
    }
    """
    data = gh_graphql(query, {})
    return data["data"]["viewer"]


def list_all_private_repos(include_archived: bool = False, verbose: bool = False) -> List[Dict[str, Any]]:
    query = """
    query($cursor: String) {
      viewer {
        repositories(
          first: 100,
          after: $cursor,
          privacy: PRIVATE,
          affiliations: [OWNER, ORGANIZATION_MEMBER, COLLABORATOR]
        ) {
          nodes {
            nameWithOwner
            isArchived
            isPrivate
            viewerPermission
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """

    repos: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while True:
        variables = {"cursor": cursor} if cursor else {}
        data = gh_graphql(query, variables)
        payload = data["data"]["viewer"]["repositories"]

        for repo in payload["nodes"]:
            if not include_archived and repo["isArchived"]:
                continue
            repos.append(repo)

        if verbose:
            print(f"[repos] fetched total={len(repos)}", file=sys.stderr)

        if not payload["pageInfo"]["hasNextPage"]:
            break

        cursor = payload["pageInfo"]["endCursor"]

    repos.sort(key=lambda r: r["nameWithOwner"].lower())
    return repos


def _repo_obj(full_name: str, archived: bool = False) -> Dict[str, Any]:
    return {
        "nameWithOwner": full_name,
        "isArchived": archived,
        "isPrivate": True,
        "viewerPermission": None,
    }


def search_api_all(
    endpoint: str,
    params: Dict[str, Any],
    per_page: int = 100,
    max_pages: int = 10,
) -> List[Dict[str, Any]]:
    """
    Generic paginated search collector.
    GitHub search API usually returns:
      { "total_count": ..., "items": [...] }
    """
    items: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        request_params = dict(params)
        request_params["per_page"] = per_page
        request_params["page"] = page
        data = gh_api_get(endpoint, request_params, allow_error_return=True)
        if not isinstance(data, dict):
            break
        batch = data.get("items") or []
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
    return items


def search_repos_by_commit(
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
    include_archived: bool = False,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    query = f"author:{viewer_login} committer-date:{isoformat_z(since_utc)}..{isoformat_z(until_utc)} is:private"

    try:
        items = search_api_all(
            endpoint="search/commits",
            params={
                "q": query,
                "sort": "committer-date",
                "order": "desc",
            },
            per_page=100,
            max_pages=10,
        )
    except RuntimeError as e:
        if verbose:
            print(f"[warn] commit search failed: {e}", file=sys.stderr)
        return []

    repo_map: Dict[str, Dict[str, Any]] = {}
    for item in items:
        repo_info = item.get("repository") or {}
        full_name = repo_info.get("full_name")
        if not full_name:
            continue
        archived = bool(repo_info.get("archived", False))
        if not include_archived and archived:
            continue
        repo_map[full_name] = _repo_obj(full_name, archived=archived)

    repos = sorted(repo_map.values(), key=lambda r: r["nameWithOwner"].lower())
    if verbose:
        print(f"[repos] touched-by-commit total={len(repos)}", file=sys.stderr)
    return repos


def search_repos_by_issue_pr(
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
    include_archived: bool = False,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Repos where the user touched issues / PRs during the UTC-converted JST day range.

    We use a UTC timestamp range:
      updated:START..END

    Queries:
      - commenter:<you>
      - author:<you>
      - assignee:<you>
      - involves:<you>
      - reviewed-by:<you>   # for PR review activity
      - review-requested:<you> # optional signal that PR touched your workflow
    """
    updated_range = f"{isoformat_z(since_utc)}..{isoformat_z(until_utc)}"

    queries = [
        f"commenter:{viewer_login} updated:{updated_range} is:private",
        f"author:{viewer_login} updated:{updated_range} is:private",
        f"assignee:{viewer_login} updated:{updated_range} is:private",
        f"involves:{viewer_login} updated:{updated_range} is:private",
        f"reviewed-by:{viewer_login} updated:{updated_range} is:private is:pr",
    ]

    repo_map: Dict[str, Dict[str, Any]] = {}

    for q in queries:
        try:
            items = search_api_all(
                endpoint="search/issues",
                params={
                    "q": q,
                    "sort": "updated",
                    "order": "desc",
                },
                per_page=100,
                max_pages=10,
            )
        except RuntimeError as e:
            if verbose:
                print(f"[warn] issue/pr search failed for query={q!r}: {e}", file=sys.stderr)
            continue

        for item in items:
            repo_url = item.get("repository_url") or ""
            if "/repos/" not in repo_url:
                continue
            full_name = repo_url.split("/repos/", 1)[1].strip("/")
            if not full_name:
                continue
            repo_map[full_name] = _repo_obj(full_name, archived=False)

    repos = sorted(repo_map.values(), key=lambda r: r["nameWithOwner"].lower())
    if verbose:
        print(f"[repos] touched-by-issue-pr total={len(repos)}", file=sys.stderr)
    return repos


def search_touched_repos(
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
    include_archived: bool = False,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    repo_map: Dict[str, Dict[str, Any]] = {}

    for repo in search_repos_by_commit(
        viewer_login=viewer_login,
        since_utc=since_utc,
        until_utc=until_utc,
        include_archived=include_archived,
        verbose=verbose,
    ):
        repo_map[repo["nameWithOwner"]] = repo

    for repo in search_repos_by_issue_pr(
        viewer_login=viewer_login,
        since_utc=since_utc,
        until_utc=until_utc,
        include_archived=include_archived,
        verbose=verbose,
    ):
        repo_map[repo["nameWithOwner"]] = repo

    repos = sorted(repo_map.values(), key=lambda r: r["nameWithOwner"].lower())
    if verbose:
        print(f"[repos] touched-total={len(repos)}", file=sys.stderr)
    return repos


def list_repo_commits(repo: str, since_utc: datetime, until_utc: datetime) -> List[Dict[str, Any]]:
    return gh_api_paginated(
        f"repos/{repo}/commits",
        params={
            "since": isoformat_z(since_utc),
            "until": isoformat_z(until_utc),
        },
    )


def get_repo_commit_detail(repo: str, sha: str) -> Dict[str, Any]:
    data = gh_api_get(f"repos/{repo}/commits/{sha}", allow_error_return=True)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected dict response for commit detail repo={repo} sha={sha}, got {type(data)}")
    return data


def list_repo_issue_events(repo: str, since_utc: datetime) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page = 1
    per_page = 100

    while True:
        batch = gh_api_get(
            f"repos/{repo}/issues/events",
            params={"per_page": per_page, "page": page},
            allow_error_return=True,
        )
        if batch is None:
            break
        if not isinstance(batch, list):
            raise RuntimeError(f"Expected list response for repo issue events: {type(batch)}")
        if not batch:
            break

        items.extend(batch)

        oldest_ts = None
        for ev in batch:
            ts = parse_github_datetime(ev.get("created_at"))
            if ts is None:
                continue
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts

        if len(batch) < per_page:
            break
        if oldest_ts is not None and oldest_ts < since_utc:
            break
        page += 1

    return items


def list_repo_issue_comments(repo: str, since_utc: datetime) -> List[Dict[str, Any]]:
    return gh_api_paginated(
        f"repos/{repo}/issues/comments",
        params={"since": isoformat_z(since_utc)},
    )


def list_repo_pull_review_comments(repo: str, since_utc: datetime) -> List[Dict[str, Any]]:
    return gh_api_paginated(
        f"repos/{repo}/pulls/comments",
        params={"since": isoformat_z(since_utc)},
    )


def extract_commit_events(
    repo: str,
    commits: Iterable[Dict[str, Any]],
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
    verbose: bool = False,
) -> List[ActivityEvent]:
    out: List[ActivityEvent] = []

    for c in commits:
        author_login = None
        if isinstance(c.get("author"), dict):
            author_login = c["author"].get("login")

        commit_info = c.get("commit") or {}
        commit_author = commit_info.get("author") or {}
        commit_time_raw = commit_author.get("date")
        if not commit_time_raw:
            continue

        ts = parse_github_datetime(commit_time_raw)
        if ts is None:
            continue

        if not (since_utc <= ts <= until_utc):
            continue

        if author_login != viewer_login:
            continue

        sha_full = c.get("sha") or ""
        sha = sha_full[:7]
        msg = ((commit_info.get("message") or "").splitlines() or [""])[0].strip()
        detail = f"{sha} {msg}".strip()

        additions = 0
        deletions = 0
        changed_lines = 0
        if sha_full:
            try:
                commit_detail = get_repo_commit_detail(repo, sha_full)
                stats = commit_detail.get("stats") or {}
                additions = max(0, int(stats.get("additions") or 0))
                deletions = max(0, int(stats.get("deletions") or 0))
                changed_lines = max(0, int(stats.get("total") or (additions + deletions)))
            except Exception as e:
                if verbose:
                    print(f"[warn] failed to fetch commit stats {repo}@{sha}: {type(e).__name__}: {e}", file=sys.stderr)

        out.append(
            ActivityEvent(
                repo=repo,
                kind="commit",
                timestamp=ts,
                detail=detail,
                commit_sha=sha_full,
                additions=additions,
                deletions=deletions,
                changed_lines=changed_lines,
            )
        )

    return out


def extract_issue_events(
    repo: str,
    events: Iterable[Dict[str, Any]],
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
) -> List[ActivityEvent]:
    out: List[ActivityEvent] = []

    for ev in events:
        actor = ev.get("actor") or {}
        actor_login = actor.get("login")
        if actor_login != viewer_login:
            continue

        ts = parse_github_datetime(ev.get("created_at"))
        if ts is None:
            continue

        if not (since_utc <= ts <= until_utc):
            continue

        event_name = (ev.get("event") or "unknown").strip()
        issue = ev.get("issue") or {}
        number = issue.get("number")
        html_url = issue.get("html_url") or ""

        detail = event_name
        if number is not None:
            detail += f" #{number}"
        if html_url:
            detail += f" {html_url}"

        out.append(ActivityEvent(repo=repo, kind="issue_event", timestamp=ts, detail=detail))

    return out


def extract_issue_comment_events(
    repo: str,
    comments: Iterable[Dict[str, Any]],
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
) -> List[ActivityEvent]:
    out: List[ActivityEvent] = []

    for comment in comments:
        user = comment.get("user") or {}
        if user.get("login") != viewer_login:
            continue

        ts = parse_github_datetime(comment.get("created_at"))
        if ts is None or not (since_utc <= ts <= until_utc):
            continue

        issue_url = (comment.get("issue_url") or "").rstrip("/")
        issue_number = issue_url.rsplit("/", 1)[-1] if issue_url else ""
        html_url = comment.get("html_url") or ""

        detail = "comment"
        if issue_number.isdigit():
            detail += f" #{issue_number}"
        if html_url:
            detail += f" {html_url}"

        out.append(ActivityEvent(repo=repo, kind="issue_event", timestamp=ts, detail=detail))

    return out


def extract_pull_review_comment_events(
    repo: str,
    comments: Iterable[Dict[str, Any]],
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
) -> List[ActivityEvent]:
    out: List[ActivityEvent] = []

    for comment in comments:
        user = comment.get("user") or {}
        if user.get("login") != viewer_login:
            continue

        ts = parse_github_datetime(comment.get("created_at"))
        if ts is None or not (since_utc <= ts <= until_utc):
            continue

        pr_url = (comment.get("pull_request_url") or "").rstrip("/")
        pr_number = pr_url.rsplit("/", 1)[-1] if pr_url else ""
        html_url = comment.get("html_url") or ""

        detail = "review_comment"
        if pr_number.isdigit():
            detail += f" PR#{pr_number}"
        if html_url:
            detail += f" {html_url}"

        out.append(ActivityEvent(repo=repo, kind="issue_event", timestamp=ts, detail=detail))

    return out


def group_sessions(
    events: List[ActivityEvent],
    gap_minutes: int,
    min_single_minutes: int,
    event_bonus_minutes: int,
    commit_bonus_threshold_lines: int,
    commit_bonus_lines_per_minute: int,
    max_commit_bonus_minutes: int,
) -> List[Dict[str, Any]]:
    if not events:
        return []

    events = sorted(events, key=lambda e: e.timestamp)

    sessions: List[List[ActivityEvent]] = []
    current: List[ActivityEvent] = [events[0]]

    for ev in events[1:]:
        prev = current[-1]
        gap = (ev.timestamp - prev.timestamp).total_seconds() / 60.0
        if gap > gap_minutes:
            sessions.append(current)
            current = [ev]
        else:
            current.append(ev)

    sessions.append(current)

    out: List[Dict[str, Any]] = []
    for sess in sessions:
        start = sess[0].timestamp
        end = sess[-1].timestamp
        raw_minutes = (end - start).total_seconds() / 60.0
        base_minutes = max(raw_minutes, float(min_single_minutes))
        has_issue_event = any(e.kind == "issue_event" for e in sess)
        issue_bonus_minutes = event_bonus_minutes if has_issue_event else 0
        unique_commits: Dict[str, ActivityEvent] = {}
        for ev in sess:
            if ev.kind != "commit":
                continue
            commit_key = ev.commit_sha or f"{ev.repo}:{ev.detail}"
            unique_commits.setdefault(commit_key, ev)

        commit_changed_lines = sum(ev.changed_lines for ev in unique_commits.values())
        commit_additions = sum(ev.additions for ev in unique_commits.values())
        commit_deletions = sum(ev.deletions for ev in unique_commits.values())
        commit_bonus_minutes = estimate_commit_bonus_minutes(
            changed_lines=commit_changed_lines,
            threshold_lines=commit_bonus_threshold_lines,
            lines_per_minute=commit_bonus_lines_per_minute,
            max_bonus_minutes=max_commit_bonus_minutes,
        )
        est_minutes = int(round(base_minutes + issue_bonus_minutes + commit_bonus_minutes))

        out.append(
            {
                "start_utc": start.astimezone(UTC),
                "end_utc": end.astimezone(UTC),
                "start_jst": start.astimezone(JST),
                "end_jst": end.astimezone(JST),
                "raw_span_minutes": round(raw_minutes, 1),
                "base_minutes": round(base_minutes, 1),
                "estimated_minutes": est_minutes,
                "has_issue_event": has_issue_event,
                "issue_bonus_minutes": issue_bonus_minutes,
                "commit_bonus_minutes": commit_bonus_minutes,
                "commit_additions": commit_additions,
                "commit_deletions": commit_deletions,
                "commit_changed_lines": commit_changed_lines,
                "events": sess,
            }
        )

    return out


def summarize_by_repo(events: List[ActivityEvent]) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = defaultdict(lambda: {"commit": 0, "issue_event": 0})
    for e in events:
        summary[e.repo][e.kind] += 1
    return dict(summary)


def estimate_commit_bonus_minutes(
    changed_lines: int,
    threshold_lines: int,
    lines_per_minute: int,
    max_bonus_minutes: int,
) -> int:
    threshold_lines = max(0, threshold_lines)
    lines_per_minute = max(1, lines_per_minute)
    max_bonus_minutes = max(0, max_bonus_minutes)

    effective_lines = max(0, changed_lines - threshold_lines)
    if effective_lines <= 0:
        return 0

    bonus_minutes = math.ceil(effective_lines / lines_per_minute)
    return min(max_bonus_minutes, bonus_minutes)


def estimate_minutes_by_repo(events: List[ActivityEvent], sessions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    repo_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "estimated_minutes": 0.0,
            "session_count": 0,
            "commit_count": 0,
            "issue_event_count": 0,
            "event_count": 0,
            "commit_additions": 0,
            "commit_deletions": 0,
            "commit_changed_lines": 0,
        }
    )

    for e in events:
        repo_stats[e.repo]["event_count"] += 1
        if e.kind == "commit":
            repo_stats[e.repo]["commit_count"] += 1
            repo_stats[e.repo]["commit_additions"] += e.additions
            repo_stats[e.repo]["commit_deletions"] += e.deletions
            repo_stats[e.repo]["commit_changed_lines"] += e.changed_lines
        elif e.kind == "issue_event":
            repo_stats[e.repo]["issue_event_count"] += 1

    for sess in sessions:
        repo_counts: Dict[str, int] = defaultdict(int)
        for e in sess["events"]:
            repo_counts[e.repo] += 1

        total_events = sum(repo_counts.values())
        if total_events <= 0:
            continue

        for repo, count in repo_counts.items():
            allocated = sess["estimated_minutes"] * (count / total_events)
            repo_stats[repo]["estimated_minutes"] += allocated
            repo_stats[repo]["session_count"] += 1

    for repo in repo_stats:
        repo_stats[repo]["estimated_minutes"] = round(repo_stats[repo]["estimated_minutes"], 1)
        repo_stats[repo]["estimated_hours"] = round(repo_stats[repo]["estimated_minutes"] / 60.0, 2)

    return dict(sorted(repo_stats.items(), key=lambda kv: (-kv[1]["estimated_minutes"], kv[0].lower())))


def render_text_report(
    target_day: date,
    viewer_login: str,
    repos_scanned: int,
    scan_mode: str,
    all_events: List[ActivityEvent],
    sessions: List[Dict[str, Any]],
    repo_estimates: Dict[str, Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append(f"GitHub daily work estimate for {target_day.isoformat()} (JST)")
    lines.append(f"Viewer: {viewer_login}")
    lines.append(f"Scan mode: {scan_mode}")
    lines.append(f"Repos scanned: {repos_scanned}")
    lines.append("")

    if not all_events:
        lines.append("No matching GitHub activity found.")
        lines.append("Estimated work: 0.00 h")
        return "\n".join(lines)

    repo_summary = summarize_by_repo(all_events)
    total_minutes = sum(s["estimated_minutes"] for s in sessions)

    lines.append("Repository estimates:")
    for repo, stats in repo_estimates.items():
        lines.append(
            f"  - {repo}: "
            f"{stats['estimated_hours']:.2f} h "
            f"({stats['estimated_minutes']:.1f} min), "
            f"sessions={stats['session_count']}, "
            f"commits={stats['commit_count']}, "
            f"issue_events={stats['issue_event_count']}, "
            f"changed_lines={stats['commit_changed_lines']}"
        )
    lines.append("")

    lines.append("Repo summary:")
    for repo in sorted(repo_summary):
        c = repo_summary[repo]["commit"]
        i = repo_summary[repo]["issue_event"]
        lines.append(f"  - {repo}: commits={c}, issue_events={i}")
    lines.append("")

    lines.append("Sessions:")
    for idx, s in enumerate(sessions, start=1):
        start_jst = s["start_jst"].strftime("%H:%M")
        end_jst = s["end_jst"].strftime("%H:%M")
        session_repos = sorted({e.repo for e in s["events"]})
        lines.append(
            f"  {idx}. {start_jst}-{end_jst} JST | "
            f"raw_span={s['raw_span_minutes']} min | "
            f"base={s['base_minutes']} min | "
            f"estimated={s['estimated_minutes']} min | "
            f"issue_bonus={s['issue_bonus_minutes']} min | "
            f"commit_bonus={s['commit_bonus_minutes']} min | "
            f"changed_lines={s['commit_changed_lines']} | "
            f"repos={', '.join(session_repos)}"
        )
        for ev in s["events"]:
            t = ev.timestamp.astimezone(JST).strftime("%H:%M:%S")
            if ev.kind == "commit":
                diff_text = (
                    f" (+{ev.additions}/-{ev.deletions}, total={ev.changed_lines})"
                    if ev.changed_lines > 0
                    else ""
                )
            else:
                diff_text = ""
            lines.append(f"     - [{t}] {ev.repo} {ev.kind}: {ev.detail}{diff_text}")

    lines.append("")
    lines.append(f"Estimated work total: {total_minutes / 60.0:.2f} h ({total_minutes} min)")
    lines.append("")
    lines.append("Caveats:")
    lines.append("  - This is estimated from GitHub-visible activity only.")
    lines.append("  - Local investigation / experiments without commits or issue activity are not counted.")
    lines.append("  - Default scan target is repos touched by your commits or issue/PR activity on that day.")
    lines.append("  - Repo-wise time is allocated by event share within each session.")

    return "\n".join(lines)


def calculate_daily_estimate(config: EstimateConfig) -> Dict[str, Any]:
    target_day = config.target_day
    since_utc, until_utc = jst_day_bounds_utc(target_day)

    viewer = current_viewer()
    viewer_login = viewer["login"]

    if config.all_visible_repos:
        repos = list_all_private_repos(
            include_archived=config.include_archived,
            verbose=config.verbose,
        )
        scan_mode = "all_visible_private_repos"
    else:
        repos = search_touched_repos(
            viewer_login=viewer_login,
            since_utc=since_utc,
            until_utc=until_utc,
            include_archived=config.include_archived,
            verbose=config.verbose,
        )
        scan_mode = "repos_touched_by_commit_or_issue_pr"

    if config.verbose:
        print(f"[viewer] {viewer_login}", file=sys.stderr)
        print(f"[date] {target_day.isoformat()} JST", file=sys.stderr)
        print(f"[scan_mode] {scan_mode}", file=sys.stderr)
        print(f"[repos] total={len(repos)}", file=sys.stderr)

    all_events: List[ActivityEvent] = []

    for idx, repo_info in enumerate(repos, start=1):
        repo = repo_info["nameWithOwner"]

        if config.verbose:
            print(f"[{idx}/{len(repos)}] scanning {repo}", file=sys.stderr)

        try:
            commits = list_repo_commits(repo, since_utc, until_utc)
            commit_events = extract_commit_events(
                repo=repo,
                commits=commits,
                viewer_login=viewer_login,
                since_utc=since_utc,
                until_utc=until_utc,
                verbose=config.verbose,
            )

            issue_events_raw = list_repo_issue_events(repo, since_utc=since_utc)
            issue_events = extract_issue_events(
                repo=repo,
                events=issue_events_raw,
                viewer_login=viewer_login,
                since_utc=since_utc,
                until_utc=until_utc,
            )

            issue_comments_raw = list_repo_issue_comments(repo, since_utc=since_utc)
            issue_comment_events = extract_issue_comment_events(
                repo=repo,
                comments=issue_comments_raw,
                viewer_login=viewer_login,
                since_utc=since_utc,
                until_utc=until_utc,
            )

            pull_review_comments_raw = list_repo_pull_review_comments(repo, since_utc=since_utc)
            pull_review_comment_events = extract_pull_review_comment_events(
                repo=repo,
                comments=pull_review_comments_raw,
                viewer_login=viewer_login,
                since_utc=since_utc,
                until_utc=until_utc,
            )

            repo_events = commit_events + issue_events + issue_comment_events + pull_review_comment_events
            if repo_events:
                all_events.extend(repo_events)

        except Exception as e:
            print(f"[warn] failed to scan {repo}: {type(e).__name__}: {e}", file=sys.stderr)

        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    all_events.sort(key=lambda e: e.timestamp)

    sessions = group_sessions(
        events=all_events,
        gap_minutes=config.gap_minutes,
        min_single_minutes=config.min_single_minutes,
        event_bonus_minutes=config.event_bonus_minutes,
        commit_bonus_threshold_lines=config.commit_bonus_threshold_lines,
        commit_bonus_lines_per_minute=config.commit_bonus_lines_per_minute,
        max_commit_bonus_minutes=config.max_commit_bonus_minutes,
    )

    total_minutes = sum(s["estimated_minutes"] for s in sessions)
    repo_estimates = estimate_minutes_by_repo(all_events, sessions)

    return {
        "target_day": target_day,
        "viewer_login": viewer_login,
        "scan_mode": scan_mode,
        "repos_scanned": len(repos),
        "all_events": all_events,
        "sessions": sessions,
        "repo_estimates": repo_estimates,
        "total_estimated_minutes": total_minutes,
        "total_estimated_hours": round(total_minutes / 60.0, 2),
        "parameters": {
            "gap_minutes": config.gap_minutes,
            "min_single_minutes": config.min_single_minutes,
            "event_bonus_minutes": config.event_bonus_minutes,
            "commit_bonus_threshold_lines": config.commit_bonus_threshold_lines,
            "commit_bonus_lines_per_minute": config.commit_bonus_lines_per_minute,
            "max_commit_bonus_minutes": config.max_commit_bonus_minutes,
            "include_archived": config.include_archived,
            "all_visible_repos": config.all_visible_repos,
            "sleep_seconds": config.sleep_seconds,
        },
    }


def build_json_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    target_day = result["target_day"]
    all_events = result["all_events"]
    sessions = result["sessions"]

    return {
        "target_date_jst": target_day.isoformat(),
        "viewer_login": result["viewer_login"],
        "scan_mode": result["scan_mode"],
        "repos_scanned": result["repos_scanned"],
        "total_estimated_minutes": result["total_estimated_minutes"],
        "total_estimated_hours": result["total_estimated_hours"],
        "repo_estimates": result["repo_estimates"],
        "events": [
            {
                "repo": e.repo,
                "kind": e.kind,
                "timestamp_utc": e.timestamp.astimezone(UTC).isoformat(),
                "timestamp_jst": e.timestamp.astimezone(JST).isoformat(),
                "detail": e.detail,
                "commit_sha": e.commit_sha,
                "additions": e.additions,
                "deletions": e.deletions,
                "changed_lines": e.changed_lines,
            }
            for e in all_events
        ],
        "sessions": [
            {
                "start_utc": s["start_utc"].isoformat(),
                "end_utc": s["end_utc"].isoformat(),
                "start_jst": s["start_jst"].isoformat(),
                "end_jst": s["end_jst"].isoformat(),
                "raw_span_minutes": s["raw_span_minutes"],
                "base_minutes": s["base_minutes"],
                "estimated_minutes": s["estimated_minutes"],
                "has_issue_event": s["has_issue_event"],
                "issue_bonus_minutes": s["issue_bonus_minutes"],
                "commit_bonus_minutes": s["commit_bonus_minutes"],
                "commit_additions": s["commit_additions"],
                "commit_deletions": s["commit_deletions"],
                "commit_changed_lines": s["commit_changed_lines"],
                "event_count": len(s["events"]),
                "repos": sorted({e.repo for e in s["events"]}),
            }
            for s in sessions
        ],
        "parameters": result["parameters"],
    }


def main() -> None:
    args = parse_args()

    try:
        config = config_from_args(args)
        result = calculate_daily_estimate(config)
    except GhCliError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(build_json_payload(result), ensure_ascii=False, indent=2))
        return

    print(
        render_text_report(
            target_day=result["target_day"],
            viewer_login=result["viewer_login"],
            repos_scanned=result["repos_scanned"],
            scan_mode=result["scan_mode"],
            all_events=result["all_events"],
            sessions=result["sessions"],
            repo_estimates=result["repo_estimates"],
        )
    )


if __name__ == "__main__":
    main()
