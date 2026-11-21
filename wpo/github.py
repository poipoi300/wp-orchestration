"""GitHub Issues, Projects v2, and Pull Request auto-merge backend for WPO.

Splits project state across two boundaries:
- GitHub owns the shared plan: issues, milestones, labels, native blocked-by dependencies,
  sub-issues, the Project v2 board, and pull requests (closing issues via `Fixes #N`).
- The local private ledger (`<runs>/<id>/`) owns transcripts, prompts, usage analytics,
  round/findings ledgers, progress logs, step ticks, Try it/Handoff text, and personal
  routing/scope overrides.

All GitHub reads go through a local read-through cache (`<runs>/github-cache.json`) so the
dashboard and scheduler never hammer the API. All outbound GitHub text passes through
`privacy.require_public_safe` so secrets, raw logs and personal paths cannot leak.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import privacy, run_state, wpfile
from .platform import FileLock, replace_file
from .project import Project, git

CACHE_FILE = "github-cache.json"
WPO_META_COMMENT = re.compile(r"<!--\s*wpo\s*(\{.*?\})\s*-->", re.DOTALL)
WPO_PR_COMMENT = re.compile(r"<!--\s*wpo-pr\s*(\{.*?\})\s*-->", re.DOTALL)
DONE_WHEN_HEADING = re.compile(r"(?im)^\s*\*\*Done when:?\*\*\s*(.*)$")
SCOPE_BULLET = re.compile(r"^\s*[-*]\s+`?([^`\s][^`]*)`?\s*$")
WP_REF_RE = re.compile(r"(?<![A-Za-z0-9_/-])(WP-\d+)(?!(?:[-_]|[A-Za-z0-9]|\.[A-Za-z0-9]))")
REMOTE_REPO_PATTERNS = (
    re.compile(r"^git@[^:]+:([^/]+/[^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@[^/]+/([^/]+/[^/]+?)(?:\.git)?$"),
    re.compile(r"^https?://[^/]+/([^/]+/[^/]+?)(?:\.git)?$"),
)

ISSUES_GRAPHQL = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    autoMergeAllowed
    defaultBranchRef { name }
    issues(first: 100, after: $cursor, states: [OPEN, CLOSED], orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        body
        state
        stateReason
        url
        updatedAt
        closedAt
        milestone { title number }
        labels(first: 50) { nodes { name } }
        parent { number }
        subIssues(first: 50) { nodes { number state } }
        blockedBy(first: 50) { nodes { number state } }
        closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number
            url
            state
            merged
            mergedAt
            mergeCommit { oid }
            headRefName
            headRefOid
            isDraft
            mergeStateStatus
            autoMergeRequest { enabledAt mergeMethod }
            statusCheckRollup { state }
          }
        }
        projectItems(first: 20) {
          nodes {
            id
            project {
              ... on ProjectV2 { id number title url }
            }
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2SingleSelectField { name } }
                }
                ... on ProjectV2ItemFieldTextValue {
                  text
                  field { ... on ProjectV2Field { name } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

ISSUE_CLOSED_BY_PRS_GRAPHQL = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 50, after: $cursor, includeClosedPrs: true) {
        pageInfo { hasNextPage endCursor }
        nodes {
          number
          url
          state
          merged
          mergedAt
          mergeCommit { oid }
          headRefName
          headRefOid
          isDraft
          mergeStateStatus
          autoMergeRequest { enabledAt mergeMethod }
          statusCheckRollup { state }
        }
      }
    }
  }
}
"""

OPEN_PRS_GRAPHQL = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor, states: [OPEN, MERGED], orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        url
        state
        merged
        mergedAt
        mergeCommit { oid }
        headRefName
        headRefOid
        baseRefName
        isDraft
        mergeStateStatus
        autoMergeRequest { enabledAt mergeMethod }
        statusCheckRollup { state }
      }
    }
  }
}
"""

USER_PROJECT_GRAPHQL = """
query($login: String!, $number: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      number
      title
      url
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
          ... on ProjectV2Field {
            id
            name
          }
        }
      }
    }
  }
}
"""

ORG_PROJECT_GRAPHQL = """
query($login: String!, $number: Int!) {
  organization(login: $login) {
    projectV2(number: $number) {
      id
      number
      title
      url
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
          ... on ProjectV2Field {
            id
            name
          }
        }
      }
    }
  }
}
"""


class GhError(RuntimeError):
    """A `gh` CLI or GitHub API call failed."""

    def __init__(self, message: str, *, command: list[str] | None = None, returncode: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.command = command or []
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class PRDeliveryResult:
    number: int
    url: str
    state: str
    head_sha: str
    branch: str
    draft: bool = False
    auto_merge_enabled: bool = False
    merged: bool = False
    merge_commit: str | None = None
    warning: str | None = None


def parse_github_repo(remote_url: str) -> str | None:
    """Extracts `owner/repo` from an SSH or HTTPS GitHub remote URL."""
    cleaned = remote_url.strip()
    for pattern in REMOTE_REPO_PATTERNS:
        if match := pattern.match(cleaned):
            return match.group(1)
    return None


def resolve_repo(project: Project) -> str:
    """Resolves `owner/repo` from project config or the configured Git remote."""
    configured = project.github_config.get("repo")
    if configured:
        return str(configured).strip()
    remote = str(project.github_config.get("remote") or "origin")
    completed = git(project.root, "remote", "get-url", remote, check=False)
    if completed.returncode == 0 and (repo := parse_github_repo(completed.stdout)):
        return repo
    raise GhError(f"could not determine GitHub repository from git remote '{remote}'; set github.repo in .orchestration/config.json")


class GhClient:
    """Thin wrapper over the `gh` CLI with an injectable runner for offline tests."""

    def __init__(self, executable: str = "gh", cwd: Path | None = None,
                 runner: Callable[[list[str], str | None], subprocess.CompletedProcess] | None = None) -> None:
        self.executable = executable
        self.cwd = cwd
        self._runner = runner

    def run(self, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        command = [self.executable, *args]
        if self._runner is not None:
            completed = self._runner(command, input_text)
        else:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.cwd) if self.cwd else None,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except FileNotFoundError as error:
                raise GhError(f"GitHub CLI '{self.executable}' not found on PATH; install gh or set 'gh' in machine config", command=command) from error
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise GhError(f"gh {' '.join(args[:4])} failed (exit {completed.returncode}): {detail}",
                          command=command, returncode=completed.returncode, stderr=detail)
        return completed

    def json(self, *args: str, input_text: str | None = None) -> object:
        output = self.run(*args, input_text=input_text).stdout.strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except ValueError as error:
            raise GhError(f"gh {' '.join(args[:4])} returned invalid JSON: {error}") from error

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        data = self.json("api", "graphql", "--input", "-", input_text=json.dumps(payload))
        if not isinstance(data, dict):
            raise GhError("gh api graphql returned a non-object response")
        if errors := data.get("errors"):
            messages = "; ".join(str(item.get("message", item)) for item in errors if isinstance(item, dict))
            raise GhError(f"GitHub GraphQL error: {messages or errors}")
        return data.get("data") or {}

    def viewer_login(self) -> str:
        data = self.json("api", "user")
        if not isinstance(data, dict) or not data.get("login"):
            raise GhError("could not determine authenticated GitHub login via `gh api user`")
        return str(data["login"])


# ----------------------------------------------------------------------- issue / PR conventions


def _strip_meta_comments(text: str) -> str:
    return WPO_META_COMMENT.sub("", text).strip()


def _extract_sections(body: str) -> tuple[list[str], dict[str, list[str]]]:
    """Splits markdown body into (leading lines before first `## `, {heading: lines})."""
    leading: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            leading.append(line)
        else:
            sections[current].append(line)
    return leading, sections


def _extract_done_when(lines: list[str]) -> tuple[list[str], list[str]]:
    """Separates leading Goal prose from a `**Done when:**` block used by standard GitHub issues."""
    goal: list[str] = []
    criteria: list[str] = []
    in_done_when = False
    for line in lines:
        if not in_done_when and (match := DONE_WHEN_HEADING.match(line)):
            in_done_when = True
            remainder = match.group(1).strip()
            if remainder:
                criteria.append(remainder)
            continue
        if in_done_when:
            criteria.append(line)
        else:
            goal.append(line)
    return goal, criteria


def _parse_scope_paths(lines: Iterable[str]) -> list[str]:
    paths: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if match := SCOPE_BULLET.match(stripped):
            candidate = match.group(1).strip().rstrip(",")
            if candidate and candidate not in paths:
                paths.append(candidate)
    return paths


def parse_issue_body(body: str) -> dict:
    """Parses a GitHub issue body using WPO + idiomatic GitHub conventions.

    Recognizes:
    - Optional `<!-- wpo {...} -->` JSON block (`owns`, `lane`, `verify`, `legacy_wp_id`, `blocked_by`)
    - Standard WPO `## ` sections (`Goal`, `Context`, `Deliverables`, `Steps`, `Acceptance criteria`,
      `Verify`, `Scope` / `Owns`, `Out of scope`)
    - Idiomatic GitHub issues: opening prose as Goal and `**Done when:**` as Acceptance criteria.
    """
    meta: dict = {}
    for match in WPO_META_COMMENT.finditer(body or ""):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            meta.update(parsed)

    cleaned = _strip_meta_comments(body or "")
    leading, sections = _extract_sections(cleaned)

    goal_lines = sections.get("Goal")
    criteria_lines = sections.get("Acceptance criteria")
    if goal_lines is None or criteria_lines is None:
        lead_goal, done_when = _extract_done_when(leading)
        if goal_lines is None and any(line.strip() for line in lead_goal):
            sections["Goal"] = lead_goal
        if criteria_lines is None and any(line.strip() for line in done_when):
            # Normalize bare bullet lines under **Done when:** into numbered criteria when needed.
            normalized_criteria = []
            index = 0
            for line in done_when:
                stripped = line.strip()
                if not stripped:
                    continue
                if re.match(r"^\d+\.\s", stripped):
                    normalized_criteria.append(stripped)
                elif stripped.startswith(("- ", "* ")):
                    index += 1
                    normalized_criteria.append(f"{index}. {stripped[2:].strip()}")
                else:
                    index += 1
                    normalized_criteria.append(f"{index}. {stripped}")
            sections["Acceptance criteria"] = normalized_criteria

    owns: list[str] = []
    owns_explicit = False
    if isinstance(meta.get("owns"), list):
        owns_explicit = True
        for pattern in meta["owns"]:
            text = str(pattern).strip()
            if text and text not in owns:
                owns.append(text)
    for heading in ("Scope", "Owns"):
        if heading in sections:
            parsed_paths = _parse_scope_paths(sections[heading])
            if parsed_paths:
                owns_explicit = True
                for pattern in parsed_paths:
                    if pattern not in owns:
                        owns.append(pattern)

    verify_meta = [str(cmd).strip() for cmd in meta.get("verify", []) if str(cmd).strip()] if isinstance(meta.get("verify"), list) else []
    blocked_meta = []
    if isinstance(meta.get("blocked_by"), list):
        for item in meta["blocked_by"]:
            norm = wpfile.normalize_wp_id(str(item), is_github=True)
            if wpfile.is_valid_wp_id(norm) and norm not in blocked_meta:
                blocked_meta.append(norm)

    return {
        "meta": meta,
        "sections": sections,
        "owns": owns,
        "owns_explicit": owns_explicit,
        "lane": str(meta.get("lane") or "").strip(),
        "verify_extra": verify_meta,
        "blocked_by_meta": blocked_meta,
        "legacy_wp_id": str(meta.get("legacy_wp_id") or "").strip() or None,
    }


def render_task_markdown(issue: dict, parsed: dict, local_state: dict) -> str:
    """Renders the unified read-only markdown view of a GitHub issue overlaid with private local
    run state (`steps_done`, `context_notes`, `progress`, `reevaluations`, `reviews`, `try_it`, `handoff`)."""
    sections = {key: list(lines) for key, lines in parsed["sections"].items()}

    if note_lines := run_state.format_context_note_lines(local_state):
        existing_context = sections.get("Context", [])
        sections["Context"] = [*existing_context, *([""] if existing_context and existing_context[-1].strip() else []), *note_lines]

    if "Steps" in sections and local_state.get("steps_done"):
        sections["Steps"] = run_state.apply_step_ticks(sections["Steps"], local_state["steps_done"])

    if isinstance((local_state.get("overrides") or {}).get("verify"), list):
        verify_cmds = [str(cmd).strip() for cmd in local_state["overrides"]["verify"] if str(cmd).strip()]
        sections["Verify"] = ["```", *verify_cmds, "```"] if verify_cmds else []

    sections["Progress log"] = run_state.format_progress_lines(local_state)

    if reevals := local_state.get("reevaluations") or []:
        reeval_lines: list[str] = []
        for entry in reevals:
            if entry.get("header"):
                reeval_lines.append(entry["header"])
            if entry.get("body"):
                reeval_lines.append(entry["body"])
            reeval_lines.append("")
        sections["Re-evaluation"] = reeval_lines

    if reviews := local_state.get("reviews") or []:
        review_lines: list[str] = []
        for entry in reviews:
            if entry.get("header"):
                review_lines.append(entry["header"])
            if entry.get("body"):
                review_lines.append(entry["body"])
            review_lines.append("")
        sections["Review"] = review_lines

    sections["Try it"] = str(local_state.get("try_it") or "").splitlines()
    sections["Handoff"] = str(local_state.get("handoff") or "").splitlines()

    ordered_headings = [
        "Goal", "Context", "Deliverables", "Steps", "Acceptance criteria",
        "Verify", "Scope", "Out of scope", "Progress log", "Re-evaluation",
        "Review", "Try it", "Handoff",
    ]
    for extra_heading in sections:
        if extra_heading not in ordered_headings and extra_heading != "Owns":
            ordered_headings.append(extra_heading)

    number = issue.get("number")
    url = issue.get("url") or ""
    header = [f"# #{number}: {issue.get('title', '')}"]
    if url:
        header.append(f"GitHub issue: {url}")
    blocks = ["\n".join(header)]
    for heading in ordered_headings:
        lines = sections.get(heading)
        if lines is None:
            continue
        body_block = "\n".join(lines).strip()
        blocks.append(f"## {heading}\n{body_block}" if body_block else f"## {heading}\n")
    return "\n\n".join(blocks).rstrip() + "\n"


def rewrite_wp_references(text: str, id_map: dict[str, int] | None) -> str:
    """Deterministically rewrites `WP-XXXX` mentions to `#<N>` when mapped."""
    if not id_map or not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        wp_id = match.group(1)
        if wp_id in id_map:
            return f"#{id_map[wp_id]}"
        return wp_id

    return WP_REF_RE.sub(_replace, text)


def render_issue_body(wp: wpfile.WorkPackage, *, id_map: dict[str, int] | None = None,
                      project_root: Path | None = None, privacy_check: bool = True,
                      allow_local_paths: bool = False, migrated_merged: bool = False) -> str:
    """Renders a clean, public-safe GitHub issue body from a WP file.

    Excludes private/mutable runtime sections (Progress log, Review, Re-evaluation, Handoff, Try it)
    and personal routing tiers (builder, reviewer, delegate, mode, skills).
    """
    public_sections = ("Goal", "Context", "Deliverables", "Steps", "Acceptance criteria", "Verify", "Out of scope")
    blocks: list[str] = []
    for heading in public_sections:
        lines = wp.section(heading)
        if not any(line.strip() for line in lines):
            continue
        if heading == "Steps":
            # Reset checkbox state to unchecked in the GitHub spec; completed ticks stay in local state.json.
            cleaned_lines = [re.sub(r"^(\s*[-*] )\[[xX]\]", r"\1[ ]", line) for line in lines]
        else:
            cleaned_lines = list(lines)
        text = "\n".join(cleaned_lines).strip()
        if heading != "Verify":
            text = rewrite_wp_references(text, id_map)
        blocks.append(f"## {heading}\n\n{text}")

    if wp.owns:
        scope_lines = "\n".join(f"- `{pattern}`" for pattern in wp.owns)
        blocks.append(f"## Scope\n\n{scope_lines}")

    meta: dict[str, object] = {}
    if wp.owns:
        meta["owns"] = list(wp.owns)
    if wp.lane:
        meta["lane"] = wp.lane
    if wp.id.startswith("WP-"):
        meta["legacy_wp_id"] = wp.id
    if migrated_merged:
        meta["migrated_merged"] = True
    if meta:
        blocks.append(f"<!-- wpo {json.dumps(meta, sort_keys=True)} -->")

    rendered = "\n\n".join(blocks).strip() + "\n"
    return privacy.require_public_safe(rendered, project_root, enabled=privacy_check, allow_local_paths=allow_local_paths)


def _public_verify_command(command: str, project_root: Path | None) -> str:
    """Normalizes a leading user-home executable path (e.g. a local venv `python`) to its basename
    for public PR checklists while leaving all arguments intact for `privacy.require_public_safe`."""
    cleaned = privacy.sanitize_for_github(command.strip(), project_root)
    if not cleaned:
        return ""
    match = re.match(r'^("([^"]+)"|\'([^\']+)\'|(\S+))(.*)$', cleaned, re.DOTALL)
    if not match:
        return cleaned
    exe = match.group(2) or match.group(3) or match.group(4) or ""
    rest = match.group(5) or ""
    if any(pattern.search(exe) for _, pattern in privacy.LOCAL_PATH_PATTERNS):
        basename = exe.replace("\\", "/").rsplit("/", 1)[-1]
        if basename:
            return f"{basename}{rest}"
    return cleaned


def render_pr_body(wp: wpfile.WorkPackage, *, summary: str = "", verify_commands: Iterable[str] = (),
                   project_root: Path | None = None, privacy_check: bool = True,
                   allow_local_paths: bool = False) -> str:
    """Renders a clean, sanitized Pull Request description with `Fixes #N` and `## Try it`."""
    blocks: list[str] = []
    if wp.issue_number is not None:
        blocks.append(f"Fixes #{wp.issue_number}")

    goal = summary.strip() or "\n".join(wp.section("Goal")).strip()
    if goal:
        blocks.append(f"## Summary\n\n{goal}")

    try_it = "\n".join(line for line in wp.section("Try it") if not line.strip().startswith("<!--")).strip()
    if try_it:
        blocks.append(f"## Try it\n\n{try_it}")

    commands = [_public_verify_command(cmd, project_root) for cmd in verify_commands if cmd.strip()]
    commands = [cmd for cmd in commands if cmd]
    if commands:
        checks = "\n".join(f"- [x] `{cmd}`" for cmd in commands)
        blocks.append(f"## Verification\n\n{checks}")

    pr_meta = {"task_id": wp.id, **({"issue": wp.issue_number} if wp.issue_number is not None else {})}
    blocks.append(f"<!-- wpo-pr {json.dumps(pr_meta, sort_keys=True)} -->")
    rendered = "\n\n".join(blocks).strip() + "\n"
    return privacy.require_public_safe(rendered, project_root, enabled=privacy_check, allow_local_paths=allow_local_paths)


# --------------------------------------------------------------------------- GitHub tracker


def _status_from_project(issue: dict, project_number: int | None, status_field: str, lane_field: str) -> tuple[str | None, str | None, str | None]:
    """Returns (project_item_id, status_name, lane_name) for the configured project."""
    items = (issue.get("projectItems") or {}).get("nodes") or []
    for item in items:
        proj = item.get("project") or {}
        if project_number is not None and proj.get("number") != project_number:
            continue
        status_name = None
        lane_name = None
        for value in (item.get("fieldValues") or {}).get("nodes") or []:
            field_name = (value.get("field") or {}).get("name")
            if field_name == status_field and value.get("name"):
                status_name = str(value["name"])
            elif field_name == lane_field:
                lane_name = str(value.get("name") or value.get("text") or "").strip() or None
        return item.get("id"), status_name, lane_name
    return None, None, None


def _reverse_status_map(status_map: dict[str, str]) -> dict[str, str]:
    reverse: dict[str, str] = {}
    # Prefer canonical primary WPO status when multiple map to the same board column.
    for wpo_status in ("todo", "in_progress", "review", "blocked", "done", "changes_requested"):
        label = status_map.get(wpo_status)
        if label and label.lower() not in reverse:
            reverse[label.lower()] = wpo_status
    return reverse


def _pick_pr_for_issue(issue: dict, prs_by_branch: dict[str, dict], branch_name: str, local_pr: dict | None) -> dict | None:
    linked = (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []
    # Prefer merged PR, then open PR, then matching branch PR, then local state.
    for candidate in linked:
        if candidate.get("merged") or candidate.get("state") == "MERGED":
            return _normalize_pr(candidate)
    for candidate in linked:
        if candidate.get("state") == "OPEN":
            return _normalize_pr(candidate)
    if branch_name in prs_by_branch:
        return _normalize_pr(prs_by_branch[branch_name])
    if local_pr:
        return dict(local_pr)
    return None


def _rollup_check_state(rollup: object) -> str | None:
    """Computes a normalized check state (`SUCCESS`, `PENDING`, `FAILURE`, `ERROR`, or `None`)
    from either GraphQL's `statusCheckRollup { state }` object or `gh pr view --json statusCheckRollup`'s
    list of CheckRun/StatusContext nodes."""
    if isinstance(rollup, dict):
        state = rollup.get("state")
        return str(state).upper() if state else None
    if isinstance(rollup, list):
        if not rollup:
            return None
        saw_pending = False
        saw_success = False
        for item in rollup:
            if not isinstance(item, dict):
                saw_pending = True
                continue
            if "conclusion" in item or "status" in item:
                status = str(item.get("status") or "").upper()
                conclusion = str(item.get("conclusion") or "").upper()
                if status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"):
                    saw_pending = True
                elif conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"):
                    return "FAILURE"
                elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                    saw_success = True
                else:
                    saw_pending = True
            else:
                state = str(item.get("state") or "").upper()
                if state in ("FAILURE", "ERROR"):
                    return state
                if state in ("PENDING", "EXPECTED", ""):
                    saw_pending = True
                elif state == "SUCCESS":
                    saw_success = True
                else:
                    saw_pending = True
        if saw_pending:
            return "PENDING"
        return "SUCCESS" if saw_success else None
    return None


def _is_direct_merge_ready(merge_state: object, check_state: object = None, rollup: object = None) -> bool:
    """Whether a PR is strictly safe to direct-merge: `mergeStateStatus == 'CLEAN'` and either
    all reported checks succeeded or no checks exist."""
    if str(merge_state or "").upper() != "CLEAN":
        return False
    effective = _rollup_check_state(rollup) if rollup is not None else None
    if effective is None and check_state is not None:
        effective = str(check_state).upper()
    return effective in ("SUCCESS", None)


def _normalize_pr(raw: dict) -> dict:
    merge_commit = raw.get("mergeCommit")
    commit_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else raw.get("merge_commit")
    rollup = raw.get("statusCheckRollup")
    check_state = _rollup_check_state(rollup) if rollup is not None else raw.get("check_state")
    auto_req = raw.get("autoMergeRequest")
    return {
        "number": raw.get("number"),
        "url": raw.get("url"),
        "state": raw.get("state"),
        "merged": bool(raw.get("merged") or raw.get("state") == "MERGED"),
        "merged_at": raw.get("mergedAt") or raw.get("merged_at"),
        "merge_commit": commit_oid,
        "branch": raw.get("headRefName") or raw.get("branch"),
        "head_sha": raw.get("headRefOid") or raw.get("head_sha"),
        "draft": bool(raw.get("isDraft") if "isDraft" in raw else raw.get("draft")),
        "merge_state_status": raw.get("mergeStateStatus") or raw.get("merge_state_status"),
        "auto_merge": bool(auto_req) if "autoMergeRequest" in raw else bool(raw.get("auto_merge")),
        "check_state": check_state,
    }


class GitHubTracker:
    """GitHub Issues + Projects v2 + PR tracker backed by a local read-through cache."""

    def __init__(self, project: Project, gh: GhClient) -> None:
        self.project = project
        self.gh = gh
        self.config = project.github_config
        self.cache_path = project.runs_dir / CACHE_FILE

    def repo(self) -> str:
        return resolve_repo(self.project)

    def load_cache(self) -> dict | None:
        if not self.cache_path.is_file():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save_cache(self, data: dict) -> dict:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.cache_path.with_suffix(".lock")):
            staging = self.cache_path.with_name(f".{self.cache_path.name}.{uuid.uuid4().hex}.tmp")
            staging.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            replace_file(staging, self.cache_path)
        return data

    def cache_age(self, cache: dict | None = None) -> float | None:
        cache = cache if cache is not None else self.load_cache()
        if not cache or not isinstance(cache.get("fetched_at"), (int, float)):
            return None
        return max(0.0, time.time() - float(cache["fetched_at"]))

    def _fetch_project_schema(self, owner: str, number: int) -> dict | None:
        login = self.gh.viewer_login() if owner == "@me" else owner
        for query, root_key in ((USER_PROJECT_GRAPHQL, "user"), (ORG_PROJECT_GRAPHQL, "organization")):
            try:
                data = self.gh.graphql(query, {"login": login, "number": int(number)})
            except GhError:
                continue
            project_node = (data.get(root_key) or {}).get("projectV2")
            if not project_node:
                continue
            fields: dict[str, dict] = {}
            for node in (project_node.get("fields") or {}).get("nodes") or []:
                if not isinstance(node, dict) or not node.get("name"):
                    continue
                options = {opt["name"]: opt["id"] for opt in node.get("options") or [] if isinstance(opt, dict) and opt.get("name") and opt.get("id")}
                fields[node["name"]] = {"id": node["id"], "options": options}
            return {
                "id": project_node["id"],
                "number": project_node["number"],
                "title": project_node.get("title") or "",
                "url": project_node.get("url") or "",
                "owner": login,
                "fields": fields,
            }
        return None

    def _finish_issue_pr_pages(self, owner: str, name: str, issue_node: dict) -> None:
        """Pages remaining `closedByPullRequestsReferences` entries when an issue has >10 linked PRs."""
        refs = issue_node.get("closedByPullRequestsReferences")
        if not isinstance(refs, dict) or issue_node.get("number") is None:
            return
        page_info = refs.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            return
        nodes = list(refs.get("nodes") or [])
        pr_cursor = page_info["endCursor"]
        number = int(issue_node["number"])
        while pr_cursor:
            data = self.gh.graphql(
                ISSUE_CLOSED_BY_PRS_GRAPHQL,
                {"owner": owner, "name": name, "number": number, "cursor": pr_cursor},
            )
            conn = ((data.get("repository") or {}).get("issue") or {}).get("closedByPullRequestsReferences") or {}
            nodes.extend(node for node in conn.get("nodes") or [] if isinstance(node, dict))
            next_info = conn.get("pageInfo") or {}
            if not next_info.get("hasNextPage") or not next_info.get("endCursor"):
                break
            pr_cursor = next_info["endCursor"]
        refs["nodes"] = nodes
        refs["pageInfo"] = {"hasNextPage": False, "endCursor": None}

    def sync(self, *, force: bool = False) -> dict:
        """Refreshes `<runs>/github-cache.json` from GitHub when missing, stale, or forced."""
        existing = self.load_cache()
        ttl = float(self.config.get("cache_ttl_seconds") or 60)
        if not force and existing is not None:
            age = self.cache_age(existing)
            if age is not None and age < ttl:
                return existing

        repo = self.repo()
        owner, name = repo.split("/", 1)
        issues: list[dict] = []
        cursor: str | None = None
        allow_auto_merge: bool | None = None
        default_branch: str | None = None
        while True:
            data = self.gh.graphql(ISSUES_GRAPHQL, {"owner": owner, "name": name, "cursor": cursor})
            repo_node = data.get("repository") or {}
            if allow_auto_merge is None and "autoMergeAllowed" in repo_node:
                allow_auto_merge = bool(repo_node.get("autoMergeAllowed"))
            if default_branch is None:
                default_branch = (repo_node.get("defaultBranchRef") or {}).get("name")
            conn = repo_node.get("issues") or {}
            page_issues = [node for node in conn.get("nodes") or [] if isinstance(node, dict)]
            for issue_node in page_issues:
                self._finish_issue_pr_pages(owner, name, issue_node)
            issues.extend(page_issues)
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            cursor = page_info["endCursor"]

        prs: list[dict] = []
        pr_cursor: str | None = None
        # Fetch up to 200 most recently updated open/merged PRs (enough for active lanes & recent merges).
        for _ in range(2):
            data = self.gh.graphql(OPEN_PRS_GRAPHQL, {"owner": owner, "name": name, "cursor": pr_cursor})
            conn = (data.get("repository") or {}).get("pullRequests") or {}
            prs.extend(node for node in conn.get("nodes") or [] if isinstance(node, dict))
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            pr_cursor = page_info["endCursor"]

        project_schema = None
        if self.config.get("project_number") is not None:
            project_owner = self.config.get("project_owner") or "@me"
            project_schema = self._fetch_project_schema(str(project_owner), int(self.config["project_number"]))

        snapshot = {
            "version": 1,
            "fetched_at": time.time(),
            "repo": repo,
            "allow_auto_merge": allow_auto_merge,
            "default_branch": default_branch or self.project.main_branch,
            "project": project_schema,
            "issues": issues,
            "prs": prs,
        }
        return self.save_cache(snapshot)

    def _issue_to_wp(self, issue: dict, prs_by_branch: dict[str, dict]) -> wpfile.WorkPackage:
        number = int(issue["number"])
        wp_id = f"GH-{number}"
        run_dir = self.project.runs_dir / wp_id
        local_state = run_state.load(run_dir)
        overrides = local_state.get("overrides") or {}
        parsed = parse_issue_body(issue.get("body") or "")

        labels = tuple(node["name"] for node in (issue.get("labels") or {}).get("nodes") or [] if isinstance(node, dict) and node.get("name"))
        project_number = self.config.get("project_number")
        _item_id, board_status, board_lane = _status_from_project(
            issue,
            int(project_number) if project_number is not None else None,
            str(self.config.get("status_field") or "Status"),
            str(self.config.get("lane_field") or "Lane"),
        )

        branch_name = self.project.branch(wp_id)
        pr = _pick_pr_for_issue(issue, prs_by_branch, branch_name, local_state.get("pr"))
        github_state = str(issue.get("state") or "OPEN").upper()
        has_merged_pr = bool(pr and pr.get("merged"))
        migrated_merged = bool(local_state.get("migrated_merged_on_main") or parsed["meta"].get("migrated_merged"))
        is_merged = has_merged_pr or (github_state == "CLOSED" and migrated_merged)

        # Status reconciliation: merged PR or migrated-merged always yields done; an issue closed
        # without a merged PR (e.g. NOT_PLANNED) is NOT merged (never unblocks dependents) and
        # presents as blocked so supervisors never build it. Otherwise local inner-loop state wins.
        blocked_label = self.config.get("blocked_label")
        if is_merged:
            status = "done"
        elif github_state == "CLOSED":
            status = "blocked"
        elif local_state.get("status") in wpfile.STATUSES:
            status = str(local_state["status"])
        elif blocked_label and blocked_label in labels:
            status = "blocked"
        elif board_status:
            reverse_map = _reverse_status_map(self.config.get("status_map") or {})
            status = reverse_map.get(board_status.lower(), "todo")
        else:
            status = "todo"

        # Lane resolution: local override > Project Lane field > lane:<name> label > issue meta > default_lane.
        lane_prefix = str(self.config.get("lane_label_prefix") or "lane:")
        label_lane = next((label[len(lane_prefix):] for label in labels if lane_prefix and label.startswith(lane_prefix)), "")
        lane = str(overrides.get("lane") or board_lane or label_lane or parsed["lane"] or self.project.config.get("default_lane") or "").strip()

        # Ownership resolution: local override > issue Scope/Owns/meta.
        if isinstance(overrides.get("owns"), list):
            owns = [str(pattern).strip() for pattern in overrides["owns"] if str(pattern).strip()]
            owns_explicit = True
        else:
            owns = list(parsed["owns"])
            owns_explicit = bool(parsed["owns_explicit"] or self.project.require_owns)

        # Native blockedBy relationships + optional meta fallback.
        deps: list[str] = []
        for blocker in (issue.get("blockedBy") or {}).get("nodes") or []:
            if isinstance(blocker, dict) and blocker.get("number") is not None:
                dep_id = f"GH-{int(blocker['number'])}"
                if dep_id != wp_id and dep_id not in deps:
                    deps.append(dep_id)
        for dep_id in parsed["blocked_by_meta"]:
            if dep_id != wp_id and dep_id not in deps:
                deps.append(dep_id)

        parent_node = issue.get("parent")
        parent_id = f"GH-{int(parent_node['number'])}" if isinstance(parent_node, dict) and parent_node.get("number") is not None else None
        sub_issues = tuple(
            f"GH-{int(node['number'])}"
            for node in (issue.get("subIssues") or {}).get("nodes") or []
            if isinstance(node, dict) and node.get("number") is not None
        )

        milestone_node = issue.get("milestone")
        milestone = str(milestone_node["title"]) if isinstance(milestone_node, dict) and milestone_node.get("title") else ""

        builder = str(overrides.get("builder") or self.project.config.get("default_builder") or "code")
        reviewer = str(overrides.get("reviewer") or self.project.config.get("default_reviewer") or "code-hard")
        fields: dict = {
            "id": wp_id,
            "title": str(issue.get("title") or f"Issue #{number}"),
            "milestone": milestone,
            "status": status,
            "depends_on": deps,
            "builder": builder,
            "reviewer": reviewer,
            "owns": owns,
            "lane": lane,
            "verify_extra": parsed["verify_extra"],
        }
        if overrides.get("delegate") is not None:
            fields["delegate"] = overrides["delegate"]
        if overrides.get("mode"):
            fields["mode"] = overrides["mode"]
        if isinstance(overrides.get("skills"), list):
            fields["skills"] = list(overrides["skills"])

        body_text = render_task_markdown(issue, parsed, local_state)
        return wpfile.WorkPackage(
            path=run_state.state_path(run_dir),
            fields=fields,
            source="github",
            body_text=body_text,
            issue_number=number,
            url=issue.get("url"),
            pr=pr,
            owns_explicit=owns_explicit,
            parent_id=parent_id,
            sub_issues=sub_issues,
            labels=labels,
            github_state=github_state,
            is_merged=is_merged,
            legacy_wp_id=local_state.get("legacy_wp_id") or parsed["legacy_wp_id"],
            overrides=dict(overrides),
        )

    def work_packages(self, *, refresh: bool = False, read_only: bool = False) -> list[wpfile.WorkPackage]:
        """Returns every tracked GitHub issue overlaid with its private local run state."""
        cache = self.load_cache() if read_only else self.sync(force=refresh)
        if not cache:
            return []
        required_labels = set(self.config.get("issue_labels") or [])
        prs_by_branch: dict[str, dict] = {}
        for pr in cache.get("prs") or []:
            if isinstance(pr, dict) and pr.get("headRefName"):
                branch = str(pr["headRefName"])
                # Keep newest/open or merged PR per branch (list is newest-updated first).
                prs_by_branch.setdefault(branch, pr)
        wps: list[wpfile.WorkPackage] = []
        for issue in cache.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("number") is None:
                continue
            if required_labels:
                issue_label_names = {node["name"] for node in (issue.get("labels") or {}).get("nodes") or [] if isinstance(node, dict) and node.get("name")}
                if not (required_labels & issue_label_names):
                    continue
            wps.append(self._issue_to_wp(issue, prs_by_branch))
        return sorted(wps, key=lambda wp: (wp.issue_number or 0, wp.id))

    def _find_cached_issue(self, issue_number: int) -> tuple[dict | None, dict | None]:
        cache = self.load_cache()
        if not cache:
            return None, None
        for issue in cache.get("issues") or []:
            if isinstance(issue, dict) and issue.get("number") == issue_number:
                return cache, issue
        return cache, None

    def _patch_cached_issue(self, issue_number: int, *, add_label: str | None = None, remove_label: str | None = None,
                            project_number: int | None = None, item_id: str | None = None,
                            status_field: str | None = None, status_value: str | None = None) -> None:
        """Applies our own one-way GitHub mutations into `<runs>/github-cache.json` so subsequent
        transitions and read-only dashboard views see them immediately without an extra GraphQL call."""
        lock_path = self.cache_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(lock_path):
            cache = self.load_cache()
            if not cache:
                return
            target = next((item for item in (cache.get("issues") or []) if isinstance(item, dict) and item.get("number") == issue_number), None)
            if target is None:
                return
            if add_label or remove_label:
                nodes = [n for n in (target.get("labels") or {}).get("nodes", []) if isinstance(n, dict) and n.get("name")]
                names = {str(n["name"]) for n in nodes}
                if add_label and add_label not in names:
                    nodes.append({"name": add_label})
                if remove_label:
                    nodes = [n for n in nodes if n.get("name") != remove_label]
                target["labels"] = {"nodes": nodes}
            if project_number is not None and item_id and status_field and status_value:
                proj_items = list((target.get("projectItems") or {}).get("nodes") or [])
                entry = next((it for it in proj_items if isinstance(it, dict) and (it.get("project") or {}).get("number") == project_number), None)
                if entry is None:
                    entry = {"id": item_id, "project": {"number": project_number}, "fieldValues": {"nodes": []}}
                    proj_items.append(entry)
                field_nodes = [v for v in (entry.get("fieldValues") or {}).get("nodes", []) if isinstance(v, dict)]
                updated_field = False
                for v in field_nodes:
                    if (v.get("field") or {}).get("name") == status_field:
                        v["name"] = status_value
                        updated_field = True
                        break
                if not updated_field:
                    field_nodes.append({"name": status_value, "field": {"name": status_field}})
                entry["id"] = item_id
                entry["fieldValues"] = {"nodes": field_nodes}
                target["projectItems"] = {"nodes": proj_items}
            staging = self.cache_path.with_name(f"{self.cache_path.name}.{uuid.uuid4().hex}.tmp")
            staging.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
            replace_file(staging, self.cache_path)

    def sync_issue_status(self, wp_id: str, wpo_status: str) -> bool:
        """One-way push of WPO status to GitHub Project v2 Status & blocked label.

        Never moves a card to Done directly: merging the PR with `Fixes #N` closes the issue and
        triggers GitHub Project's built-in Done automation.
        """
        if not self.config.get("sync_board_status", True):
            return False
        issue_number = wpfile.issue_number_of(wp_id)
        if issue_number is None:
            return False
        repo = self.repo()
        changed = False

        # Toggle blocked label cleanly.
        blocked_label = self.config.get("blocked_label")
        if blocked_label:
            _cache, issue = self._find_cached_issue(issue_number)
            labels = {node["name"] for node in (issue or {}).get("labels", {}).get("nodes", []) if isinstance(node, dict) and node.get("name")}
            if wpo_status == "blocked" and blocked_label not in labels:
                res = self.gh.run("issue", "edit", str(issue_number), "-R", repo, "--add-label", str(blocked_label), check=False)
                if res.returncode == 0:
                    self._patch_cached_issue(issue_number, add_label=str(blocked_label))
                    changed = True
            elif wpo_status != "blocked" and blocked_label in labels:
                res = self.gh.run("issue", "edit", str(issue_number), "-R", repo, "--remove-label", str(blocked_label), check=False)
                if res.returncode == 0:
                    self._patch_cached_issue(issue_number, remove_label=str(blocked_label))
                    changed = True

        # Never set Done manually; PR merge owns Done.
        if wpo_status == "done":
            return changed

        project_number = self.config.get("project_number")
        if project_number is None:
            return changed
        cache, issue = self._find_cached_issue(issue_number)
        project_schema = (cache or {}).get("project")
        if not project_schema:
            cache = self.sync(force=True)
            project_schema = cache.get("project")
            _, issue = self._find_cached_issue(issue_number)
        if not project_schema or not issue:
            return changed

        status_field_name = str(self.config.get("status_field") or "Status")
        target_option = (self.config.get("status_map") or {}).get(wpo_status)
        field_info = (project_schema.get("fields") or {}).get(status_field_name)
        if not target_option or not field_info:
            return changed
        option_id = (field_info.get("options") or {}).get(target_option)
        if not option_id:
            return changed

        item_id, current_status, _ = _status_from_project(issue, int(project_number), status_field_name, str(self.config.get("lane_field") or "Lane"))
        if current_status == target_option:
            return changed
        if not item_id:
            added = self.gh.json(
                "project", "item-add", str(project_number),
                "--owner", str(project_schema.get("owner") or self.config.get("project_owner") or "@me"),
                "--url", str(issue["url"]),
                "--format", "json",
            )
            item_id = added.get("id") if isinstance(added, dict) else None
        if not item_id:
            return changed
        self.gh.run(
            "project", "item-edit",
            "--id", str(item_id),
            "--project-id", str(project_schema["id"]),
            "--field-id", str(field_info["id"]),
            "--single-select-option-id", str(option_id),
        )
        self._patch_cached_issue(
            issue_number,
            project_number=int(project_number),
            item_id=str(item_id),
            status_field=status_field_name,
            status_value=target_option,
        )
        return True

    def find_open_pr(self, branch: str) -> dict | None:
        repo = self.repo()
        data = self.gh.json(
            "pr", "list", "-R", repo, "--head", branch, "--state", "open",
            "--json", "number,url,state,isDraft,headRefName,headRefOid,mergeStateStatus,autoMergeRequest,statusCheckRollup",
        )
        if isinstance(data, list) and data:
            return _normalize_pr(data[0])
        return None

    def deliver_pr(self, wp: wpfile.WorkPackage, branch: str, head_sha: str, delivery: str,
                   verify_commands: Iterable[str] = ()) -> PRDeliveryResult:
        """Creates or updates a sanitized PR for `wp` and arms GitHub auto-merge when configured."""
        repo = self.repo()
        privacy_enabled = bool(self.config.get("privacy_check", True))
        title = privacy.require_public_safe(wp.title, self.project.root, enabled=privacy_enabled)
        body = render_pr_body(wp, verify_commands=verify_commands, project_root=self.project.root, privacy_check=privacy_enabled)
        draft = delivery == "pr-draft"

        existing = self.find_open_pr(branch)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(body)
            body_file = Path(handle.name)
        try:
            if existing is not None:
                pr_number = int(existing["number"])
                pr_url = str(existing["url"])
                self.gh.run("pr", "edit", str(pr_number), "-R", repo, "--title", title, "--body-file", str(body_file))
                if not draft and existing.get("draft"):
                    self.gh.run("pr", "ready", str(pr_number), "-R", repo)
            else:
                args = [
                    "pr", "create", "-R", repo,
                    "--base", self.project.main_branch,
                    "--head", branch,
                    "--title", title,
                    "--body-file", str(body_file),
                ]
                if draft:
                    args.append("--draft")
                created_url = self.gh.run(*args).stdout.strip().splitlines()[-1].strip()
                pr_info = self.gh.json(
                    "pr", "view", created_url, "-R", repo,
                    "--json", "number,url,state,isDraft,headRefName,headRefOid,mergeStateStatus,autoMergeRequest",
                )
                if not isinstance(pr_info, dict) or pr_info.get("number") is None:
                    raise GhError(f"could not read created PR metadata for {created_url}")
                pr_number = int(pr_info["number"])
                pr_url = str(pr_info.get("url") or created_url)
        finally:
            body_file.unlink(missing_ok=True)

        auto_merge_enabled = False
        merged = False
        merge_commit: str | None = None
        warning: str | None = None
        state = "OPEN"

        if delivery == "pr-automerge":
            merge_method = str(self.config.get("merge_method") or "squash")
            merge_args = ["pr", "merge", str(pr_number), "-R", repo, "--auto", f"--{merge_method}"]
            if self.config.get("delete_branch_on_merge", True):
                merge_args.append("--delete-branch")
            attempt = self.gh.run(*merge_args, check=False)
            if attempt.returncode == 0:
                auto_merge_enabled = True
            else:
                detail = (attempt.stderr or attempt.stdout or "").strip()
                # Check whether the PR is already clean/mergeable and repo lacks branch rules or auto-merge.
                fallback = str(self.config.get("auto_merge_fallback") or "wait")
                if fallback == "direct-merge" and self._can_direct_merge(repo, pr_number):
                    direct_args = ["pr", "merge", str(pr_number), "-R", repo, f"--{merge_method}"]
                    if self.config.get("delete_branch_on_merge", True):
                        direct_args.append("--delete-branch")
                    direct = self.gh.run(*direct_args, check=False)
                    if direct.returncode == 0:
                        merged = True
                        state = "MERGED"
                    else:
                        warning = f"auto-merge rejected ({detail}); direct merge failed ({(direct.stderr or direct.stdout).strip()})"
                else:
                    warning = (
                        f"could not enable GitHub auto-merge on PR #{pr_number} ({detail}). "
                        f"Enable auto-merge (`gh repo edit {repo} --enable-auto-merge`) or set github.auto_merge_fallback='direct-merge'."
                    )

            # Re-read PR state in case GitHub merged it immediately.
            view = self.gh.run(
                "pr", "view", str(pr_number), "-R", repo,
                "--json", "state,merged,mergedAt,mergeCommit,autoMergeRequest",
                check=False,
            )
            if view.returncode == 0 and view.stdout.strip():
                try:
                    info = json.loads(view.stdout)
                except ValueError:
                    info = {}
                if isinstance(info, dict):
                    state = str(info.get("state") or state)
                    if info.get("merged") or state == "MERGED":
                        merged = True
                        state = "MERGED"
                        commit_obj = info.get("mergeCommit")
                        merge_commit = commit_obj.get("oid") if isinstance(commit_obj, dict) else None
                    elif info.get("autoMergeRequest"):
                        auto_merge_enabled = True

        run_dir = self.project.wp_run_dir(wp.id)
        run_state.set_pr(run_dir, {
            "number": pr_number,
            "url": pr_url,
            "branch": branch,
            "head_sha": head_sha,
            "state": state,
            "draft": draft,
            "auto_merge": auto_merge_enabled,
            "merged": merged,
            "merge_commit": merge_commit,
            "warning": warning,
        })
        if not merged:
            try:
                self.sync_issue_status(wp.id, "review")
            except GhError:
                pass
        return PRDeliveryResult(
            number=pr_number,
            url=pr_url,
            state=state,
            head_sha=head_sha,
            branch=branch,
            draft=draft,
            auto_merge_enabled=auto_merge_enabled,
            merged=merged,
            merge_commit=merge_commit,
            warning=warning,
        )

    def _can_direct_merge(self, repo: str, pr_number: int) -> bool:
        view = self.gh.run("pr", "view", str(pr_number), "-R", repo, "--json", "mergeStateStatus,statusCheckRollup", check=False)
        if view.returncode != 0 or not view.stdout.strip():
            return False
        try:
            info = json.loads(view.stdout)
        except ValueError:
            return False
        if not isinstance(info, dict):
            return False
        return _is_direct_merge_ready(info.get("mergeStateStatus"), rollup=info.get("statusCheckRollup"))

    def status_report(self) -> dict:
        cache = self.load_cache()
        age = self.cache_age(cache)
        open_prs = []
        for wp in self.work_packages(read_only=True):
            if wp.pr and not wp.pr.get("merged") and wp.pr.get("state") == "OPEN":
                open_prs.append({"wp": wp.id, "issue": wp.issue_number, **wp.pr})
        return {
            "repo": (cache or {}).get("repo") or self.config.get("repo"),
            "delivery": self.project.delivery_mode,
            "runs_dir": str(self.project.runs_dir),
            "external_ledger": self.project.is_external_ledger,
            "allow_auto_merge": (cache or {}).get("allow_auto_merge"),
            "project": (cache or {}).get("project"),
            "cache_age_seconds": round(age, 1) if age is not None else None,
            "issues_cached": len((cache or {}).get("issues") or []),
            "open_prs": open_prs,
        }


def main_contains_merge(project: Project, merge_commit: str | None) -> bool:
    """Whether local `main` already contains `merge_commit`."""
    if not merge_commit:
        return False
    return git(project.root, "merge-base", "--is-ancestor", merge_commit, project.main_branch, check=False).returncode == 0


def local_main_matches_remote(project: Project) -> bool:
    """Whether local `main` is on the main branch, clean, and matches `<remote>/<main>`."""
    remote = str(project.github_config.get("remote") or "origin")
    main = project.main_branch
    if git(project.root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip() != main:
        return False
    if git(project.root, "status", "--porcelain", check=False).stdout.strip():
        return False
    local_head = git(project.root, "rev-parse", main, check=False)
    remote_head = git(project.root, "rev-parse", f"{remote}/{main}", check=False)
    return local_head.returncode == 0 and remote_head.returncode == 0 and local_head.stdout.strip() == remote_head.stdout.strip()


def _cleanup_merged_worktree(context, wp_id: str, pr_info: dict) -> None:
    """Removes a standalone worktree and its merged local branch once local `main` has the merge.
    Never touches T3-owned worktrees or branches."""
    if context.machine.execution_mode == "t3":
        return
    project = context.project
    wt = project.worktree(wp_id)
    branch = str(pr_info.get("branch") or project.branch(wp_id))
    head_sha = pr_info.get("head_sha")
    if wt.exists():
        removed = git(project.root, "worktree", "remove", "--force", str(wt), check=False)
        if removed.returncode != 0:
            context.journal.emit(
                "cleanup-failed",
                f"{wp_id} merged, but removing standalone worktree {wt} failed: {(removed.stderr or removed.stdout).strip()}",
                {"wp": wp_id},
            )
            return
    branch_ref = git(project.root, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    if branch_ref.returncode != 0:
        return
    # Squash merges are not git ancestors of main; only force-delete the local branch when its tip
    # still matches the exact PR HEAD that merged (otherwise keep any newer local commits safe).
    flag = "-D" if (head_sha and branch_ref.stdout.strip() == head_sha) else "-d"
    deleted = git(project.root, "branch", "--quiet", flag, branch, check=False)
    if deleted.returncode != 0:
        context.journal.emit(
            "cleanup-failed",
            f"{wp_id} merged, but removing branch {branch} failed: {(deleted.stderr or deleted.stdout).strip()}",
            {"wp": wp_id},
        )


@dataclass(frozen=True)
class MainSyncResult:
    fetched: bool
    moved: bool
    before: str | None
    after: str | None


def _merge_pr_records(local_pr: dict | None, live_pr: dict, **extra: object) -> dict:
    """Merges live PR fields onto stored local PR metadata without clobbering known non-null
    fields (such as `merge_commit`, `hooks_ran`, `pre_sync_sha`, `local_synced`) with `None`."""
    merged = dict(local_pr or {})
    for key, val in live_pr.items():
        if val is not None or key not in merged:
            merged[key] = val
    for key, val in extra.items():
        if val is not None or key not in merged:
            merged[key] = val
    return merged


def sync_local_main(context) -> MainSyncResult:
    """Fetches `origin/<main>` and fast-forwards the main checkout when clean and on `main`."""
    from .platform import NamedMutex

    project = context.project
    remote = str(project.github_config.get("remote") or "origin")
    main = project.main_branch
    fetched = git(project.root, "fetch", "--quiet", remote, main, check=False)
    if fetched.returncode != 0:
        return MainSyncResult(fetched=False, moved=False, before=None, after=None)
    current_branch = git(project.root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    if current_branch != main:
        return MainSyncResult(fetched=True, moved=False, before=None, after=None)
    if git(project.root, "status", "--porcelain", check=False).stdout.strip():
        return MainSyncResult(fetched=True, moved=False, before=None, after=None)
    with NamedMutex(project.integration_mutex):
        before = git(project.root, "rev-parse", "HEAD").stdout.strip()
        target = f"{remote}/{main}"
        if git(project.root, "rev-parse", target, check=False).returncode != 0:
            return MainSyncResult(fetched=True, moved=False, before=before, after=before)
        if git(project.root, "rev-parse", target).stdout.strip() == before:
            return MainSyncResult(fetched=True, moved=False, before=before, after=before)
        ff = git(project.root, "merge", "--ff-only", "--quiet", target, check=False)
        if ff.returncode != 0:
            return MainSyncResult(fetched=True, moved=False, before=before, after=before)
        after = git(project.root, "rev-parse", "HEAD").stdout.strip()
        return MainSyncResult(fetched=True, moved=after != before, before=before, after=after)


def _main_checkout_clean_on_main(project: Project) -> bool:
    """Whether the main checkout is currently on `main` with no uncommitted changes."""
    if git(project.root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip() != project.main_branch:
        return False
    return not git(project.root, "status", "--porcelain", check=False).stdout.strip()


def finalize_merged_pr(
    context,
    wp_id: str,
    pr_info: dict,
    sync_result: MainSyncResult,
    *,
    log_path: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
    cleanup_fn: Callable[[], None] | None = None,
    ran_hook_ranges: set[tuple[str, str]] | None = None,
) -> bool:
    """Verifies the main checkout is clean on `main` and contains the merged PR, runs
    `post_merge_hooks` once over the exact merged range (`pre_sync_sha..post_sync_sha`,
    `sync_result.before..sync_result.after`, or `merge_commit^1..merge_commit`), cleans up
    standalone worktrees/branches and build outputs, and marks `local_synced = True`."""
    project = context.project
    run_dir = project.wp_run_dir(wp_id)
    if not _main_checkout_clean_on_main(project):
        return False
    merge_commit = pr_info.get("merge_commit")
    synced = (
        main_contains_merge(project, str(merge_commit))
        if merge_commit
        else (sync_result.fetched and local_main_matches_remote(project))
    )
    if not synced:
        return False

    if not pr_info.get("hooks_ran"):
        if not project.config["post_merge_hooks"]:
            run_state.set_pr(run_dir, {"hooks_ran": True})
        else:
            hook_base: str | None = None
            hook_after: str | None = None
            if merge_commit:
                parent = git(project.root, "rev-parse", "--verify", f"{merge_commit}^1", check=False)
                if parent.returncode == 0 and parent.stdout.strip():
                    hook_base = parent.stdout.strip()
                    hook_after = str(merge_commit)
            if not hook_base and not merge_commit:
                if pr_info.get("pre_sync_sha"):
                    hook_base = str(pr_info["pre_sync_sha"])
                    hook_after = str(pr_info.get("post_sync_sha") or "HEAD")
                elif sync_result.moved and sync_result.before:
                    hook_base = str(sync_result.before)
                    hook_after = str(sync_result.after or "HEAD")
            if not hook_base or not hook_after:
                # Cannot determine changed files for configured post-merge hooks yet; retry later.
                return False
            hook_range = (hook_base, hook_after)
            if ran_hook_ranges is None or hook_range not in ran_hook_ranges:
                from .integrate import run_post_merge_hooks
                run_post_merge_hooks(context, hook_base, log_path=log_path, after=hook_after, wp_id=wp_id, log_fn=log_fn)
                if ran_hook_ranges is not None:
                    ran_hook_ranges.add(hook_range)
            run_state.set_pr(run_dir, {"hooks_ran": True})

    if cleanup_fn is not None:
        cleanup_fn()
    else:
        from .integrate import cleanup_merged_outputs
        _cleanup_merged_worktree(context, wp_id, pr_info)
        cleanup_merged_outputs(context, wp_id)
    run_state.set_pr(run_dir, {"local_synced": True})
    return True


def reconcile_open_prs(context, *, force_sync: bool = True) -> dict:
    """Refreshes GitHub state and reconciles open WPO pull requests.

    - Newly merged PRs: record GitHub merge, emit `wp-merged`, and retry local `main` fast-forward,
      post-merge hooks, and worktree/build cleanup until local `main` actually contains the merge
      commit (`local_synced = True`).
    - Failed CI or merge conflicts: emit `pr-checks-failed` once per head SHA.
    - Direct-merge fallback: when `mergeStateStatus == 'CLEAN'` and checks succeed on a PR where
      `--auto` wasn't available and `auto_merge_fallback == 'direct-merge'`, merge it now.
    """
    if not context.project.is_github or context.tracker is None:
        return {"merged": [], "failed_checks": [], "open": [], "pending_local_sync": []}
    tracker: GitHubTracker = context.tracker
    tracker.sync(force=force_sync)
    wps = tracker.work_packages(read_only=True)
    merged_now: list[str] = []
    pending_local_sync: list[tuple[str, dict]] = []
    failed_checks: list[str] = []
    still_open: list[str] = []

    for wp in wps:
        run_dir = context.project.runs_dir / wp.id
        local_state = run_state.load(run_dir)
        local_pr = local_state.get("pr")
        live_pr = wp.pr
        if not live_pr and not local_pr:
            continue
        merged_before = bool(local_pr and local_pr.get("merged"))
        local_synced_before = bool(local_pr and local_pr.get("local_synced"))
        if live_pr and (live_pr.get("merged") or live_pr.get("state") == "MERGED"):
            pr_record = _merge_pr_records(local_pr, live_pr, merged=True, state="MERGED")
            if not merged_before:
                pr_record["local_synced"] = False
                pr_record.setdefault("hooks_ran", False)
                run_state.set_pr(run_dir, pr_record)
                run_state.set_status(run_dir, "done")
                merged_now.append(wp.id)
                context.journal.emit("wp-merged", f"{wp.id} merged via PR #{live_pr.get('number')} ({live_pr.get('url', '')})", {"wp": wp.id})
                pending_local_sync.append((wp.id, pr_record))
            elif not local_synced_before:
                run_state.set_pr(run_dir, pr_record)
                pending_local_sync.append((wp.id, pr_record))
            continue
        if live_pr and live_pr.get("state") == "OPEN":
            still_open.append(wp.id)
            run_state.set_pr(run_dir, _merge_pr_records(local_pr, live_pr))
            head_sha = live_pr.get("head_sha")
            check_state = live_pr.get("check_state")
            merge_state = live_pr.get("merge_state_status")
            if check_state in ("FAILURE", "ERROR") or merge_state == "DIRTY":
                if (local_pr or {}).get("last_failed_check_sha") != head_sha:
                    reason = "merge conflict with base branch" if merge_state == "DIRTY" else f"CI checks reported {check_state}"
                    run_state.set_pr(run_dir, {"last_failed_check_sha": head_sha})
                    context.journal.emit(
                        "pr-checks-failed",
                        f"{wp.id} PR #{live_pr.get('number')} needs attention ({reason}): {live_pr.get('url', '')}",
                        {"wp": wp.id},
                    )
                    failed_checks.append(wp.id)
            elif (
                context.project.delivery_mode == "pr-automerge"
                and not live_pr.get("auto_merge")
                and str(context.project.github_config.get("auto_merge_fallback") or "wait") == "direct-merge"
                and _is_direct_merge_ready(merge_state, check_state)
            ):
                repo = tracker.repo()
                merge_method = str(context.project.github_config.get("merge_method") or "squash")
                args = ["pr", "merge", str(live_pr["number"]), "-R", repo, f"--{merge_method}"]
                if context.project.github_config.get("delete_branch_on_merge", True):
                    args.append("--delete-branch")
                attempt = tracker.gh.run(*args, check=False)
                if attempt.returncode == 0:
                    merge_commit = None
                    view = tracker.gh.run("pr", "view", str(live_pr["number"]), "-R", repo, "--json", "mergeCommit", check=False)
                    if view.returncode == 0 and view.stdout.strip():
                        try:
                            info = json.loads(view.stdout)
                            if isinstance(info, dict) and isinstance(info.get("mergeCommit"), dict):
                                merge_commit = info["mergeCommit"].get("oid")
                        except ValueError:
                            pass
                    pr_record = _merge_pr_records(
                        local_pr, live_pr,
                        merged=True, state="MERGED", merge_commit=merge_commit, local_synced=False, hooks_ran=False,
                    )
                    run_state.set_pr(run_dir, pr_record)
                    run_state.set_status(run_dir, "done")
                    merged_now.append(wp.id)
                    still_open.remove(wp.id)
                    pending_local_sync.append((wp.id, pr_record))
                    context.journal.emit("wp-merged", f"{wp.id} merged via PR #{live_pr.get('number')} ({live_pr.get('url', '')})", {"wp": wp.id})

    still_pending_sync: list[str] = []
    if pending_local_sync:
        sync_result = sync_local_main(context)
        if sync_result.moved and sync_result.before:
            for wp_id, pr_info in pending_local_sync:
                if not pr_info.get("hooks_ran") and not pr_info.get("merge_commit") and not pr_info.get("pre_sync_sha"):
                    pr_info["pre_sync_sha"] = sync_result.before
                    pr_info["post_sync_sha"] = sync_result.after
                    run_state.set_pr(
                        context.project.wp_run_dir(wp_id),
                        {"pre_sync_sha": sync_result.before, "post_sync_sha": sync_result.after},
                    )
        ran_hook_ranges: set[tuple[str, str]] = set()
        for wp_id, pr_info in pending_local_sync:
            if not finalize_merged_pr(context, wp_id, pr_info, sync_result, ran_hook_ranges=ran_hook_ranges):
                still_pending_sync.append(wp_id)

    return {"merged": merged_now, "failed_checks": failed_checks, "open": still_open, "pending_local_sync": still_pending_sync}
