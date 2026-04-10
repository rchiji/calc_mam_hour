#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

import altair as alt
import pandas as pd
import streamlit as st

from main import (
    JST,
    EstimateConfig,
    GhCliError,
    build_json_payload,
    calculate_daily_estimate,
    get_gh_auth_status,
    render_text_report,
)


st.set_page_config(
    page_title="GitHub Daily Work Estimator",
    layout="wide",
)


@st.cache_data(ttl=300, show_spinner=False)
def run_estimate(
    auth_identity: str,
    target_day_iso: str,
    gap_minutes: int,
    min_single_minutes: int,
    event_bonus_minutes: int,
    commit_bonus_threshold_lines: int,
    commit_bonus_lines_per_minute: int,
    max_commit_bonus_minutes: int,
    include_archived: bool,
    all_visible_repos: bool,
) -> Dict[str, Any]:
    _ = auth_identity
    config = EstimateConfig(
        target_day=datetime.fromisoformat(target_day_iso).date(),
        gap_minutes=gap_minutes,
        min_single_minutes=min_single_minutes,
        event_bonus_minutes=event_bonus_minutes,
        commit_bonus_threshold_lines=commit_bonus_threshold_lines,
        commit_bonus_lines_per_minute=commit_bonus_lines_per_minute,
        max_commit_bonus_minutes=max_commit_bonus_minutes,
        include_archived=include_archived,
        all_visible_repos=all_visible_repos,
    )
    return calculate_daily_estimate(config)


def repo_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for repo, stats in result["repo_estimates"].items():
        rows.append(
            {
                "repo": repo,
                "hours": stats["estimated_hours"],
                "minutes": stats["estimated_minutes"],
                "sessions": stats["session_count"],
                "commits": stats["commit_count"],
                "issue_events": stats["issue_event_count"],
                "changed_lines": stats["commit_changed_lines"],
            }
        )
    return rows


def repo_dataframe(result: Dict[str, Any]) -> pd.DataFrame:
    repo_df = pd.DataFrame(repo_rows(result))
    if repo_df.empty:
        return repo_df

    total_minutes = float(repo_df["minutes"].sum())
    if total_minutes > 0:
        repo_df["share_percent"] = (repo_df["minutes"] / total_minutes * 100).round(1)
    else:
        repo_df["share_percent"] = 0.0
    return repo_df


def repo_pie_chart(repo_df: pd.DataFrame) -> alt.Chart:
    return (
        alt.Chart(repo_df)
        .mark_arc(innerRadius=50)
        .encode(
            theta=alt.Theta("minutes:Q", title="Estimated Minutes"),
            color=alt.Color("repo:N", title="Repository"),
            tooltip=[
                alt.Tooltip("repo:N", title="Repository"),
                alt.Tooltip("hours:Q", title="Hours", format=".2f"),
                alt.Tooltip("minutes:Q", title="Minutes", format=".1f"),
                alt.Tooltip("share_percent:Q", title="Share %", format=".1f"),
                alt.Tooltip("commits:Q", title="Commits"),
                alt.Tooltip("issue_events:Q", title="Issue Events"),
                alt.Tooltip("changed_lines:Q", title="Changed Lines"),
            ],
        )
        .properties(height=360)
    )


def session_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx, session in enumerate(result["sessions"], start=1):
        rows.append(
            {
                "session": idx,
                "time_jst": f"{session['start_jst'].strftime('%H:%M')} - {session['end_jst'].strftime('%H:%M')}",
                "raw_span_minutes": session["raw_span_minutes"],
                "base_minutes": session["base_minutes"],
                "issue_bonus_minutes": session["issue_bonus_minutes"],
                "commit_bonus_minutes": session["commit_bonus_minutes"],
                "changed_lines": session["commit_changed_lines"],
                "estimated_minutes": session["estimated_minutes"],
                "repos": ", ".join(sorted({event.repo for event in session["events"]})),
            }
        )
    return rows


def event_rows(events: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for event in events:
        rows.append(
            {
                "time_jst": event.timestamp.astimezone(JST).strftime("%H:%M:%S"),
                "repo": event.repo,
                "kind": event.kind,
                "detail": event.detail,
                "changed_lines": event.changed_lines,
                "additions": event.additions,
                "deletions": event.deletions,
            }
        )
    return rows


gh_status = get_gh_auth_status()
gh_login = gh_status["login"].strip()
gh_identity = gh_login or gh_status["detail"] or "gh"

st.title("GitHub Daily Work Estimator")
st.caption("Estimate daily work time from GitHub activity using the local GitHub CLI session.")

with st.sidebar:
    st.header("Authentication")
    if gh_status["authenticated"]:
        st.success(f"Using `gh` login `{gh_login or 'authenticated user'}`")
    else:
        st.error(gh_status["detail"])
        st.code("gh auth login")

    if gh_status["version"]:
        st.caption(gh_status["version"])

    with st.expander("How this works", expanded=False):
        st.markdown(
            """
- This build uses the local `gh` session for the current user.
- Run `gh auth login` once in a terminal before starting Streamlit.
- Token input and GitHub OAuth are not used in this local build.
"""
        )

    st.divider()
    st.header("Parameters")
    with st.form("estimate_form"):
        target_day = st.date_input("Target Date (JST)", value=datetime.now(JST).date())
        gap_minutes = st.number_input("Session Gap (min)", min_value=1, max_value=24 * 60, value=60, step=5)
        min_single_minutes = st.number_input("Minimum Session (min)", min_value=0, max_value=24 * 60, value=20, step=5)
        event_bonus_minutes = st.number_input("Issue/PR Bonus (min)", min_value=0, max_value=240, value=10, step=1)

        st.subheader("Commit Bonus")
        commit_bonus_threshold_lines = st.number_input(
            "Threshold Lines",
            min_value=0,
            max_value=100000,
            value=20,
            step=5,
        )
        commit_bonus_lines_per_minute = st.number_input(
            "Lines Per Minute",
            min_value=1,
            max_value=100000,
            value=25,
            step=1,
        )
        max_commit_bonus_minutes = st.number_input(
            "Max Commit Bonus (min)",
            min_value=0,
            max_value=24 * 60,
            value=30,
            step=1,
        )

        include_archived = st.checkbox("Include archived repos", value=False)
        all_visible_repos = st.checkbox("Scan all visible private repos", value=False)
        submitted = st.form_submit_button("Estimate", use_container_width=True)

if not submitted:
    st.info("Adjust the parameters in the sidebar and press `Estimate`.")
    st.stop()

if not gh_status["authenticated"]:
    st.error("GitHub CLI authentication is required. Run `gh auth login` and try again.")
    st.stop()

try:
    with st.spinner("Collecting GitHub activity..."):
        result = run_estimate(
            gh_identity,
            target_day.isoformat(),
            int(gap_minutes),
            int(min_single_minutes),
            int(event_bonus_minutes),
            int(commit_bonus_threshold_lines),
            int(commit_bonus_lines_per_minute),
            int(max_commit_bonus_minutes),
            bool(include_archived),
            bool(all_visible_repos),
        )
except GhCliError as e:
    st.error(str(e))
    st.stop()
except Exception as e:
    st.exception(e)
    st.stop()

payload = build_json_payload(result)
text_report = render_text_report(
    target_day=result["target_day"],
    viewer_login=result["viewer_login"],
    repos_scanned=result["repos_scanned"],
    scan_mode=result["scan_mode"],
    all_events=result["all_events"],
    sessions=result["sessions"],
    repo_estimates=result["repo_estimates"],
)
repo_df = repo_dataframe(result)

metric_cols = st.columns(4)
metric_cols[0].metric("Estimated Hours", f"{result['total_estimated_hours']:.2f} h")
metric_cols[1].metric("Estimated Minutes", f"{result['total_estimated_minutes']} min")
metric_cols[2].metric("Repos Scanned", str(result["repos_scanned"]))
metric_cols[3].metric("Viewer", result["viewer_login"])

st.caption(f"Scan mode: `{result['scan_mode']}`")

st.subheader("Repository Breakdown")
repo_chart_col, repo_table_col = st.columns([1, 1])
with repo_chart_col:
    if repo_df.empty:
        st.info("Repository data is empty.")
    else:
        st.altair_chart(repo_pie_chart(repo_df), use_container_width=True)
with repo_table_col:
    st.dataframe(repo_df, use_container_width=True, hide_index=True)

st.subheader("Sessions")
st.dataframe(session_rows(result), use_container_width=True, hide_index=True)

for idx, session in enumerate(result["sessions"], start=1):
    title = (
        f"Session {idx}: "
        f"{session['start_jst'].strftime('%H:%M')} - {session['end_jst'].strftime('%H:%M')} "
        f"({session['estimated_minutes']} min)"
    )
    with st.expander(title):
        st.write(
            {
                "raw_span_minutes": session["raw_span_minutes"],
                "base_minutes": session["base_minutes"],
                "issue_bonus_minutes": session["issue_bonus_minutes"],
                "commit_bonus_minutes": session["commit_bonus_minutes"],
                "commit_changed_lines": session["commit_changed_lines"],
                "repos": sorted({event.repo for event in session["events"]}),
            }
        )
        st.dataframe(event_rows(session["events"]), use_container_width=True, hide_index=True)

st.subheader("Downloads")
download_cols = st.columns(2)
download_cols[0].download_button(
    "Download JSON",
    data=json.dumps(payload, ensure_ascii=False, indent=2),
    file_name=f"github-work-estimate-{result['target_day'].isoformat()}.json",
    mime="application/json",
    use_container_width=True,
)
download_cols[1].download_button(
    "Download Text Report",
    data=text_report,
    file_name=f"github-work-estimate-{result['target_day'].isoformat()}.txt",
    mime="text/plain",
    use_container_width=True,
)

with st.expander("Raw JSON"):
    st.json(payload)
