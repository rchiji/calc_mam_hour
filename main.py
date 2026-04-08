#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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


@dataclass
class ActivityEvent:
    repo: str
    kind: str
    timestamp: datetime
    detail: str


def run_gh_json(args: Sequence[str]) -> Any:
    """
    Run `gh ...` and parse JSON robustly.

    Windows対策:
    - text=True に任せると cp932 で落ちることがあるので bytes で受ける
    - UTF-8 優先で decode、だめなら置換しつつ読む
    """
    cmd = ["gh", *args]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=False,  # bytesで受ける
        )
    except FileNotFoundError:
        print("ERROR: gh CLI not found. Install GitHub CLI first.", file=sys.stderr)
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        stderr_text = (e.stderr or b"").decode("utf-8", errors="replace")
        print("ERROR: gh command failed.", file=sys.stderr)
        print("Command:", " ".join(cmd), file=sys.stderr)
        print("STDERR:", stderr_text.strip(), file=sys.stderr)
        sys.exit(2)

    stdout_bytes = completed.stdout or b""
    if not stdout_bytes.strip():
        return None

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        return None

    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        print("ERROR: Failed to parse JSON from gh output.", file=sys.stderr)
        print(stdout_text[:2000], file=sys.stderr)
        sys.exit(2)


def gh_api_paginated(path: str, per_page: int = 100) -> List[Any]:
    items: List[Any] = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        paged = f"{path}{sep}per_page={per_page}&page={page}"
        data = run_gh_json(["api", paged])

        if data is None:
            break
        if not isinstance(data, list):
            raise RuntimeError(f"Expected list response for path={paged}, got {type(data)}")

        items.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return items


def gh_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args.extend(["-F", f"{k}={v}"])

    data = run_gh_json(args)
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
    p.add_argument("--gap-minutes", type=int, default=60)
    p.add_argument("--min-single-minutes", type=int, default=20)
    p.add_argument("--event-bonus-minutes", type=int, default=10)
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--sleep-seconds", type=float, default=0.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def parse_target_date(s: str) -> date:
    s = s.strip().lower()
    now_jst = datetime.now(JST)
    if s == "today":
        return now_jst.date()
    if s == "yesterday":
        return (now_jst - timedelta(days=1)).date()
    return datetime.strptime(s, "%Y-%m-%d").date()


def jst_day_bounds_utc(target_day: date) -> Tuple[datetime, datetime]:
    start_jst = datetime.combine(target_day, dtime.min, tzinfo=JST)
    end_jst = datetime.combine(target_day, dtime.max, tzinfo=JST)
    return start_jst.astimezone(UTC), end_jst.astimezone(UTC)


def isoformat_z(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def list_repo_commits(repo: str, since_utc: datetime, until_utc: datetime) -> List[Dict[str, Any]]:
    path = f"repos/{repo}/commits" f"?since={isoformat_z(since_utc)}" f"&until={isoformat_z(until_utc)}"
    return gh_api_paginated(path)


def list_repo_issue_events(repo: str) -> List[Dict[str, Any]]:
    path = f"repos/{repo}/issues/events"
    return gh_api_paginated(path)


def extract_commit_events(
    repo: str,
    commits: Iterable[Dict[str, Any]],
    viewer_login: str,
    since_utc: datetime,
    until_utc: datetime,
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

        try:
            ts = datetime.fromisoformat(commit_time_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ts = ts.astimezone(UTC)

        if not (since_utc <= ts <= until_utc):
            continue

        if author_login != viewer_login:
            continue

        sha = (c.get("sha") or "")[:7]
        msg = ((commit_info.get("message") or "").splitlines() or [""])[0]
        out.append(ActivityEvent(repo=repo, kind="commit", timestamp=ts, detail=f"{sha} {msg}".strip()))

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

        created_raw = ev.get("created_at")
        if not created_raw:
            continue

        try:
            ts = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ts = ts.astimezone(UTC)

        if not (since_utc <= ts <= until_utc):
            continue

        event_name = ev.get("event") or "unknown"
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


def group_sessions(
    events: List[ActivityEvent],
    gap_minutes: int,
    min_single_minutes: int,
    event_bonus_minutes: int,
) -> List[Dict[str, Any]]:
    if not events:
        return []

    events = sorted(events, key=lambda e: e.timestamp)
    sessions: List[List[ActivityEvent]] = []
    current = [events[0]]

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
        bonus = event_bonus_minutes if has_issue_event else 0
        est_minutes = int(round(base_minutes + bonus))

        out.append(
            {
                "start_utc": start.astimezone(UTC),
                "end_utc": end.astimezone(UTC),
                "start_jst": start.astimezone(JST),
                "end_jst": end.astimezone(JST),
                "raw_span_minutes": round(raw_minutes, 1),
                "estimated_minutes": est_minutes,
                "has_issue_event": has_issue_event,
                "events": sess,
            }
        )
    return out


def summarize_by_repo(events: List[ActivityEvent]) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = defaultdict(lambda: {"commit": 0, "issue_event": 0})
    for e in events:
        summary[e.repo][e.kind] += 1
    return dict(summary)


def render_text_report(
    target_day: date,
    viewer_login: str,
    repos_scanned: int,
    all_events: List[ActivityEvent],
    sessions: List[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append(f"GitHub daily work estimate for {target_day.isoformat()} (JST)")
    lines.append(f"Viewer: {viewer_login}")
    lines.append(f"Repos scanned: {repos_scanned}")
    lines.append("")

    if not all_events:
        lines.append("No matching GitHub activity found.")
        lines.append("Estimated work: 0.0 h")
        return "\n".join(lines)

    repo_summary = summarize_by_repo(all_events)
    total_minutes = sum(s["estimated_minutes"] for s in sessions)

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
        lines.append(
            f"  {idx}. {start_jst}-{end_jst} JST | "
            f"raw_span={s['raw_span_minutes']} min | "
            f"estimated={s['estimated_minutes']} min | "
            f"issue_bonus={'yes' if s['has_issue_event'] else 'no'}"
        )
        for ev in s["events"]:
            t = ev.timestamp.astimezone(JST).strftime("%H:%M:%S")
            lines.append(f"     - [{t}] {ev.repo} {ev.kind}: {ev.detail}")

    lines.append("")
    lines.append(f"Estimated work: {total_minutes / 60.0:.2f} h ({total_minutes} min)")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    target_day = parse_target_date(args.date)
    since_utc, until_utc = jst_day_bounds_utc(target_day)

    viewer = current_viewer()
    viewer_login = viewer["login"]
    repos = list_all_private_repos(
        include_archived=args.include_archived,
        verbose=args.verbose,
    )

    if args.verbose:
        print(f"[viewer] {viewer_login}", file=sys.stderr)
        print(f"[date] {target_day.isoformat()} JST", file=sys.stderr)
        print(f"[repos] total={len(repos)}", file=sys.stderr)

    all_events: List[ActivityEvent] = []

    for idx, repo_info in enumerate(repos, start=1):
        repo = repo_info["nameWithOwner"]
        if args.verbose:
            print(f"[{idx}/{len(repos)}] scanning {repo}", file=sys.stderr)

        try:
            commits = list_repo_commits(repo, since_utc, until_utc)
            commit_events = extract_commit_events(
                repo=repo,
                commits=commits,
                viewer_login=viewer_login,
                since_utc=since_utc,
                until_utc=until_utc,
            )

            issue_events_raw = list_repo_issue_events(repo)
            issue_events = extract_issue_events(
                repo=repo,
                events=issue_events_raw,
                viewer_login=viewer_login,
                since_utc=since_utc,
                until_utc=until_utc,
            )

            all_events.extend(commit_events)
            all_events.extend(issue_events)

        except Exception as e:
            print(f"[warn] failed to scan {repo}: {e}", file=sys.stderr)

        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    all_events.sort(key=lambda e: e.timestamp)
    sessions = group_sessions(
        events=all_events,
        gap_minutes=args.gap_minutes,
        min_single_minutes=args.min_single_minutes,
        event_bonus_minutes=args.event_bonus_minutes,
    )
    total_minutes = sum(s["estimated_minutes"] for s in sessions)

    if args.json:
        payload = {
            "target_date_jst": target_day.isoformat(),
            "viewer_login": viewer_login,
            "repos_scanned": len(repos),
            "total_estimated_minutes": total_minutes,
            "total_estimated_hours": round(total_minutes / 60.0, 2),
            "events": [
                {
                    "repo": e.repo,
                    "kind": e.kind,
                    "timestamp_utc": e.timestamp.astimezone(UTC).isoformat(),
                    "timestamp_jst": e.timestamp.astimezone(JST).isoformat(),
                    "detail": e.detail,
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
                    "estimated_minutes": s["estimated_minutes"],
                    "has_issue_event": s["has_issue_event"],
                    "event_count": len(s["events"]),
                }
                for s in sessions
            ],
            "repo_summary": summarize_by_repo(all_events),
            "parameters": {
                "gap_minutes": args.gap_minutes,
                "min_single_minutes": args.min_single_minutes,
                "event_bonus_minutes": args.event_bonus_minutes,
                "include_archived": args.include_archived,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(
        render_text_report(
            target_day=target_day,
            viewer_login=viewer_login,
            repos_scanned=len(repos),
            all_events=all_events,
            sessions=sessions,
        )
    )


if __name__ == "__main__":
    main()
