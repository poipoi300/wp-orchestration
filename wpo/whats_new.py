"""What merged into main lately and how to try it: `wpo whats-new [--since DATE] [--last N]`.

Supports both `wp-files` (integration commits on `main` whose subject starts with `WP-XXXX: `) and
`github` (merged PRs from `<runs>/github-cache.json` / local split state plus `GH-N:` commits).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from . import run_state, wpfile
from .project import Project, git

INTEGRATION_SUBJECT = re.compile(r"^((?:WP|GH)-\d+): ")
TRY_IT_SECTION = "Try it"
HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
# integrate.py logs "<date> <time> main fast-forwarded to wp/<id> (<sha>) ..." on every merge.
FAST_FORWARD_LINE = re.compile(r"(?m)^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) main fast-forwarded to ")
NOTHING_TO_TRY = "Nothing to try by hand:"


def try_it_text(wp: wpfile.WorkPackage) -> str:
    """The WP's Try it section without the template's comment; empty when unwritten."""
    return HTML_COMMENT.sub("", "\n".join(wp.section(TRY_IT_SECTION))).strip()


def try_it_problem(wp: wpfile.WorkPackage) -> str | None:
    """Why the WP's Try it section cannot go to review, or None. Checked before anything slow runs,
    so a builder hears at once what a reviewer would otherwise send back a round later: the section
    must hold a fenced block of the commands the user runs, or say why nothing can be tried."""
    text = try_it_text(wp)
    if not text:
        return (f"The '## {TRY_IT_SECTION}' section is missing or empty. Fill it before '## Handoff': a fenced block of the exact "
                "commands the user runs from the main checkout once this is merged, and what to look for.")
    commands, in_fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        elif in_fence and line.strip() and not line.strip().startswith("#"):
            commands.append(line.strip())
    if commands or any(line.strip().startswith(NOTHING_TO_TRY) for line in text.splitlines()):
        return None
    return (f"The '## {TRY_IT_SECTION}' section has no commands. Add a fenced block (```) of the exact commands the user runs from "
            f"the main checkout to see the work (open the app, run the replay or example that shows it), then what to look for; "
            f"or, when nothing can be tried by hand, a line starting '{NOTHING_TO_TRY}' saying why.")


@dataclass(frozen=True)
class Merged:
    wp_id: str
    merged_at: datetime
    commit: str
    title: str = ""
    pr_number: int | None = None
    pr_url: str | None = None


def merge_time(project: Project, wp_id: str) -> datetime | None:
    """When main last fast-forwarded to the WP, from its integrate log (local run state)."""
    log = project.runs_dir / wp_id / "integrate.log"
    if not log.exists():
        return None
    stamps = FAST_FORWARD_LINE.findall(log.read_text(encoding="utf-8", errors="replace"))
    return datetime.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S").astimezone() if stamps else None


def _parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone()
    except ValueError:
        return None


def _github_merged(project: Project) -> dict[str, Merged]:
    """Reads merged PR entries for GitHub-backed issues from `<runs>/github-cache.json`."""
    from .github import CACHE_FILE
    cache_path = project.runs_dir / CACHE_FILE
    if not cache_path.is_file():
        return {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found: dict[str, Merged] = {}
    for issue in cache.get("issues") or []:
        if not isinstance(issue, dict) or issue.get("number") is None:
            continue
        wp_id = f"GH-{int(issue['number'])}"
        linked = (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []
        merged_pr = next((pr for pr in linked if isinstance(pr, dict) and (pr.get("merged") or pr.get("state") == "MERGED")), None)
        if merged_pr is None:
            local_pr = run_state.load(project.runs_dir / wp_id).get("pr")
            if isinstance(local_pr, dict) and local_pr.get("merged"):
                merged_pr = local_pr
        if merged_pr is None:
            continue
        when = _parse_iso(merged_pr.get("mergedAt") or merged_pr.get("merged_at") or issue.get("closedAt"))
        if when is None:
            continue
        commit_obj = merged_pr.get("mergeCommit")
        commit = commit_obj.get("oid") if isinstance(commit_obj, dict) else str(merged_pr.get("merge_commit") or merged_pr.get("head_sha") or "")
        found[wp_id] = Merged(
            wp_id=wp_id,
            merged_at=when,
            commit=commit,
            title=str(issue.get("title") or ""),
            pr_number=merged_pr.get("number"),
            pr_url=merged_pr.get("url"),
        )
    return found


def merged_wps(project: Project, since: datetime | None, last: int | None) -> list[Merged]:
    """Newest merge first, one entry per work item."""
    found: dict[str, Merged] = _github_merged(project) if project.is_github else {}
    log_result = git(project.root, "log", "--format=%H%x00%cI%x00%s", project.main_branch, check=False)
    if log_result.returncode == 0:
        for line in log_result.stdout.splitlines():
            commit, stamp, subject = line.split("\0", 2)
            match = INTEGRATION_SUBJECT.match(subject)
            if match and match.group(1) not in found:
                wp_id = match.group(1)
                found[wp_id] = Merged(wp_id, merge_time(project, wp_id) or datetime.fromisoformat(stamp), commit)
    merged = sorted(found.values(), key=lambda entry: entry.merged_at, reverse=True)
    if since is not None:
        merged = [entry for entry in merged if entry.merged_at >= since.astimezone()]
    return merged[:last] if last is not None else merged


def evidence(project: Project, wp_id: str) -> list[str]:
    files: set[str] = set()
    for template in project.config["try_it_evidence"]:
        pattern = template.replace("{wp}", wp_id).replace("{wp_lower}", wp_id.lower())
        files.update(path.relative_to(project.root).as_posix() for path in project.root.glob(pattern) if path.is_file())
    return sorted(files)


def describe(project: Project, entry: Merged) -> str:
    if project.is_github or entry.wp_id.startswith("GH-"):
        local = run_state.load(project.runs_dir / entry.wp_id)
        try_it = HTML_COMMENT.sub("", str(local.get("try_it") or "")).strip()
        title = entry.title or entry.wp_id
        pr_info = f" PR #{entry.pr_number} ({entry.pr_url})" if entry.pr_number and entry.pr_url else (f" PR #{entry.pr_number}" if entry.pr_number else "")
        commit_text = f" ({entry.commit[:10]})" if entry.commit else ""
        body = try_it or "(No '## Try it' recorded in local ledger.)"
        files = evidence(project, entry.wp_id)
        shown = "\n".join(f"  {path}" for path in files[:12]) + (f"\n  ... and {len(files) - 12} more" if len(files) > 12 else "")
        evidence_text = f"\nEvidence:\n{shown}" if files else ""
        return f"## {entry.wp_id}: {title}\nMerged {entry.merged_at:%Y-%m-%d %H:%M}{commit_text}.{pr_info}\n\n{body}{evidence_text}\n"
    wp = wpfile.find(project.wp_dir(), entry.wp_id)
    wp_path = f"{project.wp_dir_relative}/{wp.file_name}"
    try_it = try_it_text(wp)
    body = try_it or f"(No '## {TRY_IT_SECTION}' section: merged before it existed. The Handoff in {wp_path} says what it delivered.)"
    files = evidence(project, entry.wp_id)
    shown = "\n".join(f"  {path}" for path in files[:12]) + (f"\n  ... and {len(files) - 12} more" if len(files) > 12 else "")
    evidence_text = f"\nEvidence:\n{shown}" if files else ""
    return f"## {entry.wp_id}: {wp.title}\nMerged {entry.merged_at:%Y-%m-%d %H:%M} ({entry.commit[:10]}). {wp_path}\n\n{body}{evidence_text}\n"


def whats_new(project: Project, since: datetime | None, last: int | None) -> str:
    entries = merged_wps(project, since, last)
    if not entries:
        return "Nothing merged in that range."
    return "\n".join(describe(project, entry) for entry in entries)
