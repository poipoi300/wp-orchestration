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

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import privacy, run_state, wpfile
from .platform import FileLock, NamedMutex, replace_file
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
        parent { number repository { nameWithOwner } }
        subIssues(first: 50) {
          pageInfo { hasNextPage endCursor }
          nodes { number state repository { nameWithOwner } }
        }
        blockedBy(first: 50) {
          pageInfo { hasNextPage endCursor }
          nodes { number state repository { nameWithOwner } }
        }
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
            baseRefName
            baseRefOid
            isCrossRepository
            isDraft
            isInMergeQueue
            mergeStateStatus
            autoMergeRequest { enabledAt mergeMethod }
            statusCheckRollup { state }
            commits { totalCount }
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
          baseRefName
          baseRefOid
          isCrossRepository
          isDraft
          isInMergeQueue
          mergeStateStatus
          autoMergeRequest { enabledAt mergeMethod }
          statusCheckRollup { state }
          commits { totalCount }
        }
      }
    }
  }
}
"""

ISSUE_SUBISSUES_GRAPHQL = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { number state repository { nameWithOwner } }
      }
    }
  }
}
"""

ISSUE_BLOCKED_BY_GRAPHQL = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      blockedBy(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { number state repository { nameWithOwner } }
      }
    }
  }
}
"""

OPEN_PRS_GRAPHQL = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor, states: [OPEN, MERGED, CLOSED], orderBy: {field: UPDATED_AT, direction: DESC}) {
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
        baseRefOid
        isCrossRepository
        isDraft
        isInMergeQueue
        mergeStateStatus
        autoMergeRequest { enabledAt mergeMethod }
        statusCheckRollup { state }
        commits { totalCount }
      }
    }
  }
}
"""

PR_MERGE_QUEUE_GRAPHQL = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      isInMergeQueue
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


def issue_body_sha256(body: str) -> str:
    """Stable SHA-256 digest of a GitHub issue body (CRLF-normalized), used to bind local Verify
    command authorization and unfinished migration body checkpoints across platforms."""
    normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_issue_verify_commands(parsed: dict) -> list[str]:
    """Extracts executable Verify commands declared in a parsed GitHub issue body or its hidden metadata."""
    commands: list[str] = []
    in_fence = False
    for line in (parsed.get("sections") or {}).get("Verify") or ():
        if line.startswith("```"):
            in_fence = not in_fence
        elif in_fence and line.strip() and not line.strip().startswith("#"):
            cmd = line.strip()
            if cmd not in commands:
                commands.append(cmd)
    for extra in parsed.get("verify_extra") or ():
        cmd = str(extra).strip()
        if cmd and cmd not in commands:
            commands.append(cmd)
    return commands


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
    """Deterministically rewrites `WP-XXXX` mentions to `#<N>` when mapped, preserving
    `<!-- wpo ... -->` metadata comments verbatim so `legacy_wp_id` markers stay intact."""
    if not id_map or not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        wp_id = match.group(1)
        if wp_id in id_map:
            return f"#{id_map[wp_id]}"
        return wp_id

    parts: list[str] = []
    last = 0
    for meta_match in WPO_META_COMMENT.finditer(text):
        parts.append(WP_REF_RE.sub(_replace, text[last:meta_match.start()]))
        parts.append(meta_match.group(0))
        last = meta_match.end()
    parts.append(WP_REF_RE.sub(_replace, text[last:]))
    return "".join(parts)


def render_issue_body(wp: wpfile.WorkPackage, *, id_map: dict[str, int] | None = None,
                      project_root: Path | None = None, privacy_check: bool = True,
                      allow_local_paths: bool = False, migrated_merged: bool = False,
                      extra_forbidden: Iterable[str] = ()) -> str:
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
    return privacy.require_public_safe(
        rendered, project_root, enabled=privacy_check, allow_local_paths=allow_local_paths, extra_forbidden=extra_forbidden,
    )


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
                   allow_local_paths: bool = False, extra_forbidden: Iterable[str] = ()) -> str:
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
    return privacy.require_public_safe(
        rendered, project_root, enabled=privacy_check, allow_local_paths=allow_local_paths, extra_forbidden=extra_forbidden,
    )


# --------------------------------------------------------------------------- GitHub tracker


def _project_owner_from_url(url: str) -> str | None:
    match = re.search(r"/(?:users|orgs)/([^/]+)/projects/\d+", url or "")
    return match.group(1).lower() if match else None


def _status_from_project(
    issue: dict,
    project_number: int | None,
    status_field: str,
    lane_field: str,
    *,
    project_id: str | None = None,
    project_owner: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Returns (project_item_id, status_name, lane_name) strictly for the configured owner's project."""
    if project_number is None:
        return None, None, None
    owner_filter = None
    if project_owner and project_owner != "@me":
        owner_filter = str(project_owner).strip().lower()
    items = (issue.get("projectItems") or {}).get("nodes") or []
    for item in items:
        proj = item.get("project") or {}
        if project_id:
            if proj.get("id") != project_id:
                continue
        else:
            if proj.get("number") != project_number:
                continue
            if owner_filter:
                item_owner = _project_owner_from_url(str(proj.get("url") or ""))
                if not item_owner or item_owner != owner_filter:
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


def _pr_matches_repo_and_base(candidate: dict, main_branch: str) -> bool:
    if candidate.get("isCrossRepository") or candidate.get("is_cross_repository"):
        return False
    base = candidate.get("baseRefName") or candidate.get("base")
    return not base or str(base) == main_branch


def _pick_pr_for_issue(
    issue: dict,
    prs_by_branch: dict[str, dict],
    prs_by_number: dict[int, dict],
    branch_name: str,
    local_pr: dict | None,
    *,
    main_branch: str = "main",
    pr_delivery: dict | None = None,
) -> dict | None:
    linked_all = [node for node in (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or [] if isinstance(node, dict)]
    linked = [node for node in linked_all if _pr_matches_repo_and_base(node, main_branch)]
    tracked_num: int | None = None
    if isinstance(pr_delivery, dict) and pr_delivery.get("number") is not None:
        tracked_num = int(pr_delivery["number"])
    elif isinstance(local_pr, dict) and local_pr.get("number") is not None:
        tracked_num = int(local_pr["number"])

    # 1. Prefer same-repo merged PR targeting main_branch (preferring tracked PR number if present).
    for candidate in linked:
        if (candidate.get("merged") or candidate.get("state") == "MERGED") and candidate.get("number") == tracked_num:
            return _normalize_pr(candidate)
    for candidate in linked:
        if candidate.get("merged") or candidate.get("state") == "MERGED":
            return _normalize_pr(candidate)

    # 2. Reconcile tracked PR's live state FIRST (OPEN, MERGED, or CLOSED) so an unrelated open PR
    # can neither shadow WPO's open PR nor hide a tracked PR's terminal CLOSED-unmerged state.
    if tracked_num is not None:
        for candidate in linked_all:
            if candidate.get("number") == tracked_num:
                return _normalize_pr(candidate)
        if tracked_num in prs_by_number:
            return _normalize_pr(prs_by_number[tracked_num])

    # 3. Any same-repo OPEN PR targeting main_branch linked to the issue.
    for candidate in linked:
        if candidate.get("state") == "OPEN":
            return _normalize_pr(candidate)

    # 4. Same-repo branch match targeting main_branch, then closed linked PR, then local fallback.
    if branch_name in prs_by_branch and _pr_matches_repo_and_base(prs_by_branch[branch_name], main_branch):
        return _normalize_pr(prs_by_branch[branch_name])
    for candidate in reversed(linked):
        if candidate.get("state") == "CLOSED":
            return _normalize_pr(candidate)
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
    merge_method = None
    if isinstance(auto_req, dict) and auto_req.get("mergeMethod"):
        merge_method = str(auto_req["mergeMethod"]).lower()
    elif raw.get("merge_method"):
        merge_method = str(raw["merge_method"]).lower()
    commits_raw = raw.get("commits")
    commits_count: int | None = raw.get("commits_count")
    commit_headlines: list[str] | None = raw.get("commit_headlines")
    if isinstance(commits_raw, dict) and isinstance(commits_raw.get("totalCount"), int):
        commits_count = int(commits_raw["totalCount"])
    elif isinstance(commits_raw, list):
        commits_count = len(commits_raw)
        headlines = [str(c.get("messageHeadline") or "").strip() for c in commits_raw if isinstance(c, dict)]
        if headlines and all(headlines):
            commit_headlines = headlines
    return {
        "number": raw.get("number"),
        "url": raw.get("url"),
        "state": raw.get("state"),
        "merged": bool(raw.get("merged") or raw.get("state") == "MERGED"),
        "merged_at": raw.get("mergedAt") or raw.get("merged_at"),
        "merge_commit": commit_oid,
        "branch": raw.get("headRefName") or raw.get("branch"),
        "head_sha": raw.get("headRefOid") or raw.get("head_sha"),
        "base": raw.get("baseRefName") or raw.get("base"),
        "base_sha": raw.get("baseRefOid") or raw.get("base_sha"),
        "is_cross_repository": bool(raw.get("isCrossRepository") if "isCrossRepository" in raw else raw.get("is_cross_repository")),
        "draft": bool(raw.get("isDraft") if "isDraft" in raw else raw.get("draft")),
        "in_merge_queue": raw.get("isInMergeQueue") if "isInMergeQueue" in raw else raw.get("in_merge_queue"),
        "merge_state_status": raw.get("mergeStateStatus") or raw.get("merge_state_status"),
        "auto_merge": bool(auto_req) if "autoMergeRequest" in raw else bool(raw.get("auto_merge")),
        "merge_method": merge_method,
        "commits_count": commits_count,
        "commit_headlines": commit_headlines,
        "check_state": check_state,
    }


class GitHubTracker:
    """GitHub Issues + Projects v2 + PR tracker backed by a local read-through cache."""

    def __init__(self, project: Project, gh: GhClient, *, extra_forbidden: Iterable[str] = ()) -> None:
        self.project = project
        self.gh = gh
        self.config = project.github_config
        self.cache_path = project.runs_dir / CACHE_FILE
        self.extra_forbidden = tuple(extra_forbidden)

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

    def _finish_issue_connection_pages(self, owner: str, name: str, issue_node: dict, field_name: str, query: str) -> None:
        """Pages remaining GraphQL connection nodes on an issue when `pageInfo.hasNextPage` is true."""
        conn = issue_node.get(field_name)
        if not isinstance(conn, dict) or issue_node.get("number") is None:
            return
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            return
        nodes = list(conn.get("nodes") or [])
        cursor = page_info["endCursor"]
        number = int(issue_node["number"])
        while cursor:
            data = self.gh.graphql(query, {"owner": owner, "name": name, "number": number, "cursor": cursor})
            page_conn = ((data.get("repository") or {}).get("issue") or {}).get(field_name) or {}
            nodes.extend(node for node in page_conn.get("nodes") or [] if isinstance(node, dict))
            next_info = page_conn.get("pageInfo") or {}
            if not next_info.get("hasNextPage") or not next_info.get("endCursor"):
                break
            cursor = next_info["endCursor"]
        conn["nodes"] = nodes
        conn["pageInfo"] = {"hasNextPage": False, "endCursor": None}

    def _finish_issue_pr_pages(self, owner: str, name: str, issue_node: dict) -> None:
        """Pages remaining `closedByPullRequestsReferences` entries when an issue has >10 linked PRs."""
        self._finish_issue_connection_pages(owner, name, issue_node, "closedByPullRequestsReferences", ISSUE_CLOSED_BY_PRS_GRAPHQL)

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
                self._finish_issue_connection_pages(owner, name, issue_node, "subIssues", ISSUE_SUBISSUES_GRAPHQL)
                self._finish_issue_connection_pages(owner, name, issue_node, "blockedBy", ISSUE_BLOCKED_BY_GRAPHQL)
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

    def _issue_to_wp(
        self,
        issue: dict,
        prs_by_branch: dict[str, dict],
        prs_by_number: dict[int, dict],
        *,
        current_repo: str,
        project_id: str | None = None,
        project_owner: str | None = None,
        issues_by_number: dict[int, dict] | None = None,
    ) -> wpfile.WorkPackage:
        number = int(issue["number"])
        wp_id = f"GH-{number}"
        run_dir = self.project.runs_dir / wp_id
        local_state = run_state.load(run_dir)
        overrides = local_state.get("overrides") or {}
        raw_body = str(issue.get("body") or "")
        parsed = parse_issue_body(raw_body)

        labels = tuple(node["name"] for node in (issue.get("labels") or {}).get("nodes") or [] if isinstance(node, dict) and node.get("name"))
        project_number = self.config.get("project_number")
        _item_id, _board_status, board_lane = _status_from_project(
            issue,
            int(project_number) if project_number is not None else None,
            str(self.config.get("status_field") or "Status"),
            str(self.config.get("lane_field") or "Lane"),
            project_id=project_id,
            project_owner=project_owner,
        )

        branch_name = self.project.branch(wp_id)
        pr = _pick_pr_for_issue(
            issue, prs_by_branch, prs_by_number, branch_name, local_state.get("pr"),
            main_branch=self.project.main_branch,
            pr_delivery=local_state.get("pr_delivery"),
        )
        github_state = str(issue.get("state") or "OPEN").upper()
        has_merged_pr = bool(pr and pr.get("merged"))
        local_ff_merged = bool(local_state.get("local_ff_merged")) and main_contains_merge(self.project, local_state.get("local_ff_commit"))
        # Require trusted local migration completion record (never trust remote public comment alone).
        migrated_merged = bool(local_state.get("migrated_merged_on_main"))
        is_merged = has_merged_pr or local_ff_merged or (github_state == "CLOSED" and migrated_merged)

        # Status reconciliation: merged PR, verified local-ff merge, or trusted migrated-merged always yields
        # done; an issue closed without verified merge evidence is NOT merged and presents as blocked.
        # Project v2 board columns are strictly one-way (WPO -> GitHub) and never imported as status.
        blocked_label = self.config.get("blocked_label")
        if is_merged:
            public_status = "done"
        elif github_state == "CLOSED" or (blocked_label and blocked_label in labels):
            public_status = "blocked"
        elif pr and pr.get("state") == "OPEN":
            public_status = "review"
        else:
            public_status = "todo"

        if is_merged:
            status = "done"
        elif github_state == "CLOSED":
            status = "blocked"
        elif local_state.get("status") in wpfile.STATUSES:
            status = str(local_state["status"])
        elif blocked_label and blocked_label in labels:
            status = "blocked"
        else:
            status = "todo"

        # Lane resolution: local override > Project Lane field > lane:<name> label > issue meta > default_lane.
        lane_prefix = str(self.config.get("lane_label_prefix") or "lane:")
        label_lane = next((label[len(lane_prefix):] for label in labels if lane_prefix and label.startswith(lane_prefix)), "")
        public_lane = str(board_lane or label_lane or parsed["lane"] or self.project.config.get("default_lane") or "").strip()
        lane = str(overrides.get("lane") or public_lane).strip()

        # Ownership resolution: local override > issue Scope/Owns/meta.
        if isinstance(overrides.get("owns"), list):
            owns = [str(pattern).strip() for pattern in overrides["owns"] if str(pattern).strip()]
            owns_explicit = True
        else:
            owns = list(parsed["owns"])
            owns_explicit = bool(parsed["owns_explicit"] or self.project.require_owns)

        # Native blockedBy relationships (repo-qualified for cross-repo blockers) + optional meta fallback.
        deps: list[str] = []
        external_blockers: list[dict] = []
        current_repo_lower = current_repo.lower()
        for blocker in (issue.get("blockedBy") or {}).get("nodes") or []:
            if not isinstance(blocker, dict) or blocker.get("number") is None:
                continue
            blocker_repo = str(((blocker.get("repository") or {}).get("nameWithOwner")) or current_repo).strip()
            blocker_num = int(blocker["number"])
            blocker_state = str(blocker.get("state") or "OPEN").upper()
            if blocker_repo.lower() == current_repo_lower or not current_repo_lower:
                dep_id = f"GH-{blocker_num}"
                if dep_id != wp_id and dep_id not in deps:
                    deps.append(dep_id)
            else:
                ext_id = f"{blocker_repo}#{blocker_num}"
                external_blockers.append({"id": ext_id, "repo": blocker_repo, "number": blocker_num, "state": blocker_state})
                if blocker_state != "CLOSED" and ext_id not in deps:
                    deps.append(ext_id)
        for dep_id in parsed["blocked_by_meta"]:
            if dep_id != wp_id and dep_id not in deps:
                deps.append(dep_id)

        parent_node = issue.get("parent")
        parent_id: str | None = None
        if isinstance(parent_node, dict) and parent_node.get("number") is not None:
            parent_repo = str(((parent_node.get("repository") or {}).get("nameWithOwner")) or current_repo).strip()
            parent_id = (
                f"GH-{int(parent_node['number'])}"
                if (parent_repo.lower() == current_repo_lower or not current_repo_lower)
                else f"{parent_repo}#{int(parent_node['number'])}"
            )

        sub_issues_list: list[str] = []
        open_sub_issues_list: list[str] = []
        for node in (issue.get("subIssues") or {}).get("nodes") or []:
            if not isinstance(node, dict) or node.get("number") is None:
                continue
            child_repo = str(((node.get("repository") or {}).get("nameWithOwner")) or current_repo).strip()
            child_num = int(node["number"])
            same_repo = (child_repo.lower() == current_repo_lower) or not current_repo_lower
            child_id = f"GH-{child_num}" if same_repo else f"{child_repo}#{child_num}"
            if child_id not in sub_issues_list:
                sub_issues_list.append(child_id)
            child_state = str(node.get("state") or "OPEN").upper()
            child_pending_sync = False
            if same_repo and child_state == "CLOSED":
                child_local_state = run_state.load(self.project.runs_dir / child_id)
                child_local_pr = child_local_state.get("pr") if isinstance(child_local_state.get("pr"), dict) else {}
                child_issue_node = (issues_by_number or {}).get(child_num)
                child_live_pr = (
                    _pick_pr_for_issue(
                        child_issue_node,
                        prs_by_branch,
                        prs_by_number,
                        self.project.branch(child_id),
                        child_local_pr or None,
                        main_branch=self.project.main_branch,
                        pr_delivery=child_local_state.get("pr_delivery"),
                    )
                    if child_issue_node is not None
                    else (child_local_pr or None)
                )
                if child_live_pr and child_live_pr.get("merged"):
                    child_mc = child_live_pr.get("merge_commit") or child_local_pr.get("merge_commit")
                    if child_mc:
                        if not main_contains_merge(self.project, str(child_mc)) or child_local_pr.get("local_synced") is False:
                            child_pending_sync = True
                    elif child_local_pr.get("local_synced") is not True:
                        child_pending_sync = True
            if (child_state != "CLOSED" or child_pending_sync) and child_id not in open_sub_issues_list:
                open_sub_issues_list.append(child_id)

        milestone_node = issue.get("milestone")
        milestone = str(milestone_node["title"]) if isinstance(milestone_node, dict) and milestone_node.get("title") else ""

        # Local authorization gate for remote issue Verify commands (bound to exact reviewed issue body).
        issue_verify_cmds = extract_issue_verify_commands(parsed)
        body_sha = issue_body_sha256(raw_body)
        approved = local_state.get("approved_verify")
        has_local_override = isinstance(overrides.get("verify"), list)
        approved_matches = (
            isinstance(approved, dict)
            and approved.get("body_sha256") == body_sha
            and approved.get("commands") == issue_verify_cmds
        )
        verify_trusted = has_local_override or (not issue_verify_cmds) or approved_matches
        approved_verify_commands = tuple(approved["commands"]) if approved_matches else ()
        untrusted_verify_commands = () if verify_trusted else tuple(issue_verify_cmds)

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
            "verify_extra": [],
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
            sub_issues=tuple(sub_issues_list),
            open_sub_issues=tuple(open_sub_issues_list),
            external_blockers=tuple(external_blockers),
            labels=labels,
            github_state=github_state,
            is_merged=is_merged,
            legacy_wp_id=local_state.get("legacy_wp_id") or parsed["legacy_wp_id"],
            overrides=dict(overrides),
            verify_trusted=verify_trusted,
            approved_verify_commands=approved_verify_commands,
            untrusted_verify_commands=untrusted_verify_commands,
            public_lane=public_lane,
            public_status=public_status,
        )

    def work_packages(self, *, refresh: bool = False, read_only: bool = False) -> list[wpfile.WorkPackage]:
        """Returns every tracked GitHub issue overlaid with its private local run state."""
        cache = self.load_cache() if read_only else self.sync(force=refresh)
        if not cache:
            return []
        current_repo = str(cache.get("repo") or self.config.get("repo") or "").strip()
        project_schema = cache.get("project") if isinstance(cache.get("project"), dict) else None
        project_id = str(project_schema["id"]) if (project_schema and project_schema.get("id")) else None
        project_owner = str((project_schema or {}).get("owner") or self.config.get("project_owner") or "").strip() or None
        required_labels = set(self.config.get("issue_labels") or [])
        prs_by_branch: dict[str, dict] = {}
        prs_by_number: dict[int, dict] = {}
        for pr in cache.get("prs") or []:
            if not isinstance(pr, dict):
                continue
            if pr.get("number") is not None:
                prs_by_number[int(pr["number"])] = pr
            if pr.get("headRefName") and _pr_matches_repo_and_base(pr, self.project.main_branch):
                branch = str(pr["headRefName"])
                # Prefer open/merged same-repo PR for this branch (list is newest-updated first).
                if branch not in prs_by_branch:
                    prs_by_branch[branch] = pr
        issues_by_number: dict[int, dict] = {
            int(issue["number"]): issue
            for issue in (cache.get("issues") or [])
            if isinstance(issue, dict) and issue.get("number") is not None
        }
        wps: list[wpfile.WorkPackage] = []
        for issue in cache.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("number") is None:
                continue
            if required_labels:
                issue_label_names = {node["name"] for node in (issue.get("labels") or {}).get("nodes") or [] if isinstance(node, dict) and node.get("name")}
                if not (required_labels & issue_label_names):
                    continue
            wps.append(self._issue_to_wp(
                issue,
                prs_by_branch,
                prs_by_number,
                current_repo=current_repo,
                project_id=project_id,
                project_owner=project_owner,
                issues_by_number=issues_by_number,
            ))
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
                            project_number: int | None = None, project_id: str | None = None,
                            item_id: str | None = None,
                            status_field: str | None = None, status_value: str | None = None,
                            github_state: str | None = None) -> None:
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
            if github_state:
                target["state"] = github_state
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
                entry = next(
                    (
                        it for it in proj_items
                        if isinstance(it, dict) and (
                            it.get("id") == item_id
                            or (project_id and (it.get("project") or {}).get("id") == project_id)
                        )
                    ),
                    None,
                )
                if entry is None and not project_id:
                    entry = next(
                        (it for it in proj_items if isinstance(it, dict) and (it.get("project") or {}).get("number") == project_number),
                        None,
                    )
                if entry is None:
                    proj_meta: dict = {"number": project_number}
                    if project_id:
                        proj_meta["id"] = project_id
                    entry = {"id": item_id, "project": proj_meta, "fieldValues": {"nodes": []}}
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

    def sync_issue_status(self, wp_id: str, wpo_status: str, *, strict: bool = False) -> bool:
        """One-way push of WPO status to GitHub Project v2 Status & blocked label.

        Never moves a card to Done directly: merging the PR with `Fixes #N` closes the issue and
        triggers GitHub Project's built-in Done automation. When `strict=True`, raises `GhError`
        instead of silently skipping missing schema/fields/options or label-edit errors.
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
                res = self.gh.run("issue", "edit", str(issue_number), "-R", repo, "--add-label", str(blocked_label), check=strict)
                if res.returncode == 0:
                    self._patch_cached_issue(issue_number, add_label=str(blocked_label))
                    changed = True
            elif wpo_status != "blocked" and blocked_label in labels:
                res = self.gh.run("issue", "edit", str(issue_number), "-R", repo, "--remove-label", str(blocked_label), check=strict)
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
            if strict:
                raise GhError(f"cannot sync board status for {wp_id}: project #{project_number} schema or issue #{issue_number} unavailable")
            return changed

        status_field_name = str(self.config.get("status_field") or "Status")
        target_option = (self.config.get("status_map") or {}).get(wpo_status)
        field_info = (project_schema.get("fields") or {}).get(status_field_name)
        if not target_option or not field_info:
            if strict:
                raise GhError(f"cannot sync board status for {wp_id}: project field '{status_field_name}' or mapping for '{wpo_status}' missing")
            return changed
        option_id = (field_info.get("options") or {}).get(target_option)
        if not option_id:
            if strict:
                raise GhError(f"cannot sync board status for {wp_id}: option '{target_option}' missing on project field '{status_field_name}'")
            return changed

        item_id, current_status, _ = _status_from_project(
            issue,
            int(project_number),
            status_field_name,
            str(self.config.get("lane_field") or "Lane"),
            project_id=str(project_schema.get("id") or "") or None,
            project_owner=str(project_schema.get("owner") or self.config.get("project_owner") or "") or None,
        )
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
            if strict:
                raise GhError(f"cannot sync board status for {wp_id}: could not resolve project item id")
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
            project_id=str(project_schema.get("id") or "") or None,
            item_id=str(item_id),
            status_field=status_field_name,
            status_value=target_option,
        )
        return True

    def close_issue_completed(self, wp_id: str, *, commit: str | None = None) -> bool:
        """Closes a GitHub issue with reason `completed` after verified local-ff merge."""
        issue_number = wpfile.issue_number_of(wp_id)
        if issue_number is None:
            return False
        repo = self.repo()
        args = ["issue", "close", str(issue_number), "-R", repo, "--reason", "completed"]
        if commit:
            args += ["--comment", f"Merged into `{self.project.main_branch}` at `{commit[:10]}`."]
        self.gh.run(*args)
        self._patch_cached_issue(issue_number, github_state="CLOSED")
        return True

    def approve_verify(self, wp_id: str, *, clear: bool = False, refresh: bool = True) -> dict:
        """Authorizes (or revokes) remote issue `## Verify` commands bound to the current issue body revision."""
        issue_number = wpfile.issue_number_of(wp_id)
        if issue_number is None:
            raise GhError(f"{wp_id} is not a GitHub issue id")
        run_dir = self.project.wp_run_dir(f"GH-{issue_number}")
        if clear:
            run_state.set_approved_verify(run_dir, None)
            return {"wp": f"GH-{issue_number}", "approved": False, "commands": [], "body_sha256": None}
        if refresh:
            self.sync(force=True)
        _cache, issue = self._find_cached_issue(issue_number)
        if issue is None:
            raise GhError(f"GitHub issue #{issue_number} not found in cache")
        raw_body = str(issue.get("body") or "")
        parsed = parse_issue_body(raw_body)
        commands = extract_issue_verify_commands(parsed)
        digest = issue_body_sha256(raw_body)
        run_state.set_approved_verify(run_dir, digest, commands)
        return {"wp": f"GH-{issue_number}", "approved": True, "commands": commands, "body_sha256": digest}

    def find_open_pr(self, branch: str, *, base: str | None = None, head_sha: str | None = None) -> dict | None:
        repo = self.repo()
        repo_owner, repo_name = repo.split("/", 1)
        expected_base = base or self.project.main_branch
        data = self.gh.json(
            "pr", "list", "-R", repo, "--base", expected_base, "--head", branch, "--state", "open",
            "--json",
            "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,isCrossRepository,"
            "headRepositoryOwner,headRepository,mergeStateStatus,autoMergeRequest,statusCheckRollup,commits",
        )
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict):
                continue
            # Require positive matches on every identity field before reusing/editing an open PR.
            if item.get("isCrossRepository") is not False:
                continue
            if item.get("headRefName") != branch:
                continue
            if item.get("baseRefName") != expected_base:
                continue
            owner_obj = item.get("headRepositoryOwner")
            owner_login = owner_obj.get("login") if isinstance(owner_obj, dict) else None
            if not owner_login or str(owner_login).lower() != repo_owner.lower():
                continue
            head_repo = item.get("headRepository")
            head_repo_name = head_repo.get("name") if isinstance(head_repo, dict) else None
            if not head_repo_name or str(head_repo_name).lower() != repo_name.lower():
                continue
            if head_sha is not None and item.get("headRefOid") != head_sha:
                continue
            return _normalize_pr(item)
        return None

    def deliver_pr(self, wp: wpfile.WorkPackage, branch: str, head_sha: str, delivery: str,
                   verify_commands: Iterable[str] = (), *, commits_count: int | None = None,
                   base_sha: str | None = None, commit_headlines: list[str] | None = None) -> PRDeliveryResult:
        """Creates or updates a sanitized PR for `wp` and arms GitHub auto-merge when configured."""
        repo = self.repo()
        privacy_enabled = bool(self.config.get("privacy_check", True))
        title = privacy.require_public_safe(
            wp.title, self.project.root, enabled=privacy_enabled, extra_forbidden=self.extra_forbidden,
        )
        body = render_pr_body(
            wp,
            verify_commands=verify_commands,
            project_root=self.project.root,
            privacy_check=privacy_enabled,
            extra_forbidden=self.extra_forbidden,
        )
        draft = delivery == "pr-draft"

        # A51: Locate any existing same-repo PR for `(branch, main_branch)` without requiring
        # `headRefOid == head_sha` immediately after `git push` (GitHub updates `headRefOid`
        # asynchronously; head integrity on merge is enforced via `--match-head-commit`).
        existing = self.find_open_pr(branch, base=self.project.main_branch)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".md", delete=False) as handle:
            handle.write(body)
            body_file = Path(handle.name)
        try:
            if existing is not None:
                pr_number = int(existing["number"])
                pr_url = str(existing["url"])
                if commits_count is None and existing.get("commits_count") is not None:
                    commits_count = int(existing["commits_count"])
                if commit_headlines is None and existing.get("commit_headlines"):
                    commit_headlines = list(existing["commit_headlines"])
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
                    "--json", "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,mergeStateStatus,autoMergeRequest,commits",
                )
                if not isinstance(pr_info, dict) or pr_info.get("number") is None:
                    raise GhError(f"could not read created PR metadata for {created_url}")
                pr_number = int(pr_info["number"])
                pr_url = str(pr_info.get("url") or created_url)
                if commits_count is None and isinstance(pr_info.get("commits"), list):
                    commits_count = len(pr_info["commits"])
                if base_sha is None and pr_info.get("baseRefOid"):
                    base_sha = str(pr_info["baseRefOid"])
        finally:
            body_file.unlink(missing_ok=True)

        auto_merge_enabled = False
        merged = False
        merge_commit: str | None = None
        warning: str | None = None
        state = "OPEN"
        merge_method = str(self.config.get("merge_method") or "squash")
        direct_merge_attempted = False
        merge_queued = False

        if delivery == "pr-automerge":
            merge_args = [
                "pr", "merge", str(pr_number), "-R", repo,
                "--auto", f"--{merge_method}", "--match-head-commit", head_sha,
            ]
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
                    direct_args = [
                        "pr", "merge", str(pr_number), "-R", repo,
                        f"--{merge_method}", "--match-head-commit", head_sha,
                    ]
                    if self.config.get("delete_branch_on_merge", True):
                        direct_args.append("--delete-branch")
                    direct = self.gh.run(*direct_args, check=False)
                    if direct.returncode == 0:
                        direct_merge_attempted = True
                    else:
                        warning = f"auto-merge rejected ({detail}); direct merge failed ({(direct.stderr or direct.stdout).strip()})"
                else:
                    warning = (
                        f"could not enable GitHub auto-merge on PR #{pr_number} ({detail}). "
                        f"Enable auto-merge (`gh repo edit {repo} --enable-auto-merge`) or set github.auto_merge_fallback='direct-merge'."
                    )

            # Re-read live PR state using CLI-supported fields; only mark MERGED when GitHub confirms
            # `state == "MERGED"` (a merge-queue admission returns exit 0 while keeping state OPEN).
            view = self.gh.run(
                "pr", "view", str(pr_number), "-R", repo,
                "--json", "state,mergedAt,mergeCommit,autoMergeRequest,baseRefOid,commits",
                check=False,
            )
            if view.returncode == 0 and view.stdout.strip():
                try:
                    info = json.loads(view.stdout)
                except ValueError:
                    info = {}
                if isinstance(info, dict):
                    state = str(info.get("state") or state)
                    if commits_count is None and isinstance(info.get("commits"), list):
                        commits_count = len(info["commits"])
                    if base_sha is None and info.get("baseRefOid"):
                        base_sha = str(info["baseRefOid"])
                    if state == "MERGED" or bool(info.get("mergedAt")):
                        merged = True
                        state = "MERGED"
                        commit_obj = info.get("mergeCommit")
                        merge_commit = commit_obj.get("oid") if isinstance(commit_obj, dict) else None
                    elif info.get("autoMergeRequest"):
                        auto_merge_enabled = True
                    elif direct_merge_attempted:
                        # Queued in merge queue: stay OPEN and record durable queue state so
                        # reconciliation does not re-run `gh pr merge` every tick (A41/A58).
                        merge_queued = True

        run_dir = self.project.wp_run_dir(wp.id)
        # Immutable WPO delivery provenance stored separately from observed PR state (A2).
        run_state.set_pr_delivery(run_dir, {
            "repo": repo,
            "number": pr_number,
            "base": self.project.main_branch,
            "base_sha": base_sha,
            "branch": branch,
            "verified_head_sha": head_sha,
            "commits_count": commits_count,
            "commit_headlines": list(commit_headlines) if commit_headlines else None,
            "merge_method": merge_method,
        })
        run_state.set_pr(run_dir, {
            "delivered_by_wpo": True,
            "repo": repo,
            "base": self.project.main_branch,
            "base_sha": base_sha,
            "number": pr_number,
            "url": pr_url,
            "branch": branch,
            "head_sha": head_sha,
            "verified_head_sha": head_sha,
            "commits_count": commits_count,
            "commit_headlines": list(commit_headlines) if commit_headlines else None,
            "merge_method": merge_method,
            "state": state,
            "draft": draft,
            "auto_merge": auto_merge_enabled,
            "merge_queued": merge_queued,
            "merge_queued_head_sha": head_sha if merge_queued else None,
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


def _registered_linked_worktrees(project: Project) -> set[Path]:
    """Paths of linked git worktrees registered under `project.root` (excluding the main checkout)."""
    listed = git(project.root, "worktree", "list", "--porcelain", check=False)
    if listed.returncode != 0:
        return set()
    main_resolved = project.root.resolve()
    result: set[Path] = set()
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line[len("worktree "):].strip()).resolve()
            if candidate != main_resolved:
                result.add(candidate)
    return result


def _cleanup_merged_worktree(context, wp_id: str, pr_info: dict) -> None:
    """Safely removes a WPO-owned standalone worktree and its merged local branch once local `main`
    contains the merge. Verifies persisted standalone ownership, linked worktree identity, expected
    checked-out branch, clean/no-untracked state, and that the current tip actually landed."""
    project = context.project
    run_dir = project.wp_run_dir(wp_id)
    if context.machine.execution_mode == "t3" or (run_dir / "t3-workspace.json").is_file():
        return
    local_state = run_state.load(run_dir)
    if local_state.get("worktree_owner") != "standalone":
        wt_candidate = project.worktree(wp_id)
        if wt_candidate.exists():
            context.journal.emit(
                "cleanup-skipped",
                f"{wp_id} merged, but worktree {wt_candidate} lacks persisted standalone ownership; retained",
                {"wp": wp_id},
            )
        return

    wt = project.worktree(wp_id)
    branch = str(pr_info.get("branch") or local_state.get("branch") or project.branch(wp_id))
    # Accept ONLY the actual landed head (never an older `verified_head_sha` when a different head landed).
    landed_head = str(pr_info.get("head_sha") or "").strip()

    if wt.exists():
        wt_resolved = wt.resolve()
        if local_state.get("worktree") and Path(local_state["worktree"]).resolve() != wt_resolved:
            context.journal.emit(
                "cleanup-skipped",
                f"{wp_id} merged, but worktree path {wt} does not match recorded standalone checkout; retained",
                {"wp": wp_id},
            )
            return
        if wt_resolved == project.root.resolve() or wt_resolved not in _registered_linked_worktrees(project):
            context.journal.emit(
                "cleanup-skipped",
                f"{wp_id} merged, but {wt} is not a registered linked worktree of {project.root}; retained",
                {"wp": wp_id},
            )
            return
        checked_out = git(wt, "symbolic-ref", "--short", "HEAD", check=False)
        if checked_out.returncode != 0 or checked_out.stdout.strip() != branch:
            context.journal.emit(
                "cleanup-skipped",
                f"{wp_id} merged, but worktree {wt} is on '{checked_out.stdout.strip() or 'detached HEAD'}' instead of '{branch}'; retained",
                {"wp": wp_id},
            )
            return
        dirty = git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
        if dirty.returncode != 0 or dirty.stdout.strip():
            context.journal.emit(
                "cleanup-skipped",
                f"{wp_id} merged, but standalone worktree {wt} has uncommitted or untracked files; retained",
                {"wp": wp_id},
            )
            return
        wt_head_res = git(wt, "rev-parse", "HEAD", check=False)
        wt_head = wt_head_res.stdout.strip() if wt_head_res.returncode == 0 else ""
        head_is_merged = (
            bool(wt_head and landed_head and wt_head == landed_head)
            or (bool(wt_head) and git(project.root, "merge-base", "--is-ancestor", wt_head, project.main_branch, check=False).returncode == 0)
        )
        if not head_is_merged:
            context.journal.emit(
                "cleanup-skipped",
                f"{wp_id} merged, but worktree {wt} HEAD ({wt_head[:10] or 'unknown'}) does not match the landed head; retained",
                {"wp": wp_id},
            )
            return
        removed = git(project.root, "worktree", "remove", str(wt), check=False)
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
    branch_tip = branch_ref.stdout.strip()
    if landed_head and branch_tip == landed_head:
        deleted = git(project.root, "branch", "--quiet", "-D", branch, check=False)
    elif git(project.root, "merge-base", "--is-ancestor", branch_tip, project.main_branch, check=False).returncode == 0:
        deleted = git(project.root, "branch", "--quiet", "-d", branch, check=False)
    else:
        context.journal.emit(
            "cleanup-skipped",
            f"{wp_id} merged, but branch {branch} ({branch_tip[:10]}) does not match the landed head; retained",
            {"wp": wp_id},
        )
        return
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
    fields (such as `merge_commit`, `hooks_ran`, `pre_sync_sha`, `local_synced`, `delivered_by_wpo`) with `None`."""
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


POST_MERGE_HOOKS_JOURNAL = "post-merge-hooks.json"
POST_MERGE_HOOKS_LOCK = "post-merge-hooks.lock"


def _load_hook_journal(runs_dir: Path) -> dict[str, dict]:
    path = runs_dir / POST_MERGE_HOOKS_JOURNAL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_hook_journal(runs_dir: Path, journal: dict[str, dict]) -> None:
    path = runs_dir / POST_MERGE_HOOKS_JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replace_file(staging, path)


def reconcile_hook_journal(runs_dir: Path, *, key: str | None = None, action: str = "list") -> dict:
    """Inspects or reconciles interrupted entries in `<runs>/post-merge-hooks.json` (A50).

    `action`: `"list"`, `"mark-done"` (marks an interrupted hook completed after manual
    verification), or `"retry"` (clears the interrupted entry so the next reconcile runs it).
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(runs_dir / POST_MERGE_HOOKS_LOCK):
        journal = _load_hook_journal(runs_dir)
        if action == "list":
            return {"action": "list", "entries": journal}
        if not key or key not in journal:
            raise GhError(f"hook journal entry not found: {key!r}")
        if action == "mark-done":
            entry = dict(journal[key]) if isinstance(journal[key], dict) else {}
            entry.update(status="done", ok=True, reconciled_manually=True, finished_at=time.time())
            journal[key] = entry
            _save_hook_journal(runs_dir, journal)
            return {"action": "mark-done", "key": key, "entry": entry}
        if action == "retry":
            removed = journal.pop(key)
            _save_hook_journal(runs_dir, journal)
            return {"action": "retry", "key": key, "removed": removed}
        raise GhError(f"unknown hook journal action: {action!r}")


def _fetch_pr_commit_evidence(
    context,
    current_pr: dict,
    *,
    force_live: bool = False,
) -> tuple[str | None, int | None, list[str] | None, str | None, str | None]:
    """Queries GitHub for `(merge_commit, commits_count, commit_headlines, base_sha, head_sha)`.

    When `force_live=True` (used for single-parent landings so post-delivery human commits on the
    PR never leave stale delivery-time headlines/counts), live PR metadata overrides stored values.
    """
    merge_commit = current_pr.get("merge_commit")
    commits_count = current_pr.get("commits_count")
    commit_headlines = current_pr.get("commit_headlines")
    base_sha = current_pr.get("base_sha")
    head_sha = current_pr.get("head_sha")
    tracker = getattr(context, "tracker", None)
    pr_number = current_pr.get("number")
    need_query = (
        force_live
        or (not merge_commit)
        or (not isinstance(commits_count, int))
        or (isinstance(commits_count, int) and commits_count > 1 and not commit_headlines)
    )
    if need_query and tracker is not None and pr_number is not None:
        try:
            repo = tracker.repo()
            view = tracker.gh.run(
                "pr", "view", str(pr_number), "-R", repo,
                "--json", "mergeCommit,commits,baseRefOid,headRefOid", check=False,
            )
            if view.returncode == 0 and view.stdout.strip():
                parsed_view = json.loads(view.stdout)
                if isinstance(parsed_view, dict):
                    mc_obj = parsed_view.get("mergeCommit")
                    if isinstance(mc_obj, dict) and mc_obj.get("oid") and (force_live or not merge_commit):
                        merge_commit = str(mc_obj["oid"])
                    if parsed_view.get("baseRefOid") and (force_live or not base_sha):
                        base_sha = str(parsed_view["baseRefOid"])
                    if parsed_view.get("headRefOid") and (force_live or not head_sha):
                        head_sha = str(parsed_view["headRefOid"])
                    commits_raw = parsed_view.get("commits")
                    if isinstance(commits_raw, list) and commits_raw:
                        commits_count = len(commits_raw)
                        headlines = [str(c.get("messageHeadline") or "").strip() for c in commits_raw if isinstance(c, dict)]
                        if headlines and all(headlines):
                            commit_headlines = headlines
                        elif force_live:
                            commit_headlines = None
        except (GhError, ValueError):
            pass
    return (
        str(merge_commit) if merge_commit else None,
        int(commits_count) if isinstance(commits_count, int) and commits_count >= 1 else None,
        list(commit_headlines) if isinstance(commit_headlines, list) and commit_headlines else None,
        str(base_sha) if base_sha else None,
        str(head_sha) if head_sha else None,
    )


def _resolve_merge_hook_base(context, project: Project, current_pr: dict, merge_commit: str) -> str | None:
    """Determines the exact landed base commit before `merge_commit` from git graph & live PR
    commit evidence (never guessing from local merge_method preference), returning `None` when ambiguous."""
    parents_res = git(project.root, "rev-list", "--parents", "-n", "1", merge_commit, check=False)
    if parents_res.returncode != 0 or not parents_res.stdout.strip():
        return None
    parts = parents_res.stdout.strip().split()
    parents = parts[1:]
    if not parents:
        return None
    # 1. True 2-parent merge commit: first parent is `main` before the merge (unambiguous).
    if len(parents) >= 2:
        return parents[0]

    # A20: Fetch live merged PR commits & headRefOid so post-delivery commits pushed to the PR
    # never get masked by stale delivery-time `commits_count` / `commit_headlines`.
    _mc, commits_count, commit_headlines, base_sha, head_sha = _fetch_pr_commit_evidence(
        context, current_pr, force_live=True,
    )
    # 2. Single-commit PR: squash and rebase both land exactly 1 commit (`merge_commit^1`).
    if commits_count == 1:
        return parents[0]
    if not isinstance(commits_count, int) or commits_count < 1:
        return None

    # 3. Multi-commit single-parent landing (N > 1): prove whether it was squashed (1 commit) or
    # rebased/fast-forwarded (N commits) from commit graph & PR commit headlines; defer if ambiguous.
    n_base_res = git(project.root, "rev-parse", "--verify", f"{merge_commit}~{commits_count}", check=False)
    n_base = n_base_res.stdout.strip() if n_base_res.returncode == 0 and n_base_res.stdout.strip() else None

    if n_base is None:
        # Fewer than N ancestors exist on main -> cannot be an N-commit rebase; must be a squash!
        return parents[0]

    # If `base_sha` is known and is NOT an ancestor of `merge_commit~N`, going back N commits
    # reaches commits from BEFORE the PR's base -> cannot be an N-commit rebase; proven squash!
    if (
        base_sha
        and git(project.root, "cat-file", "-e", f"{base_sha}^{{commit}}", check=False).returncode == 0
        and git(project.root, "merge-base", "--is-ancestor", base_sha, n_base, check=False).returncode != 0
    ):
        return parents[0]

    # Exact fast-forward / unmodified tip match: `merge_commit == head_sha` proves all N commits landed!
    if head_sha and merge_commit == head_sha:
        return n_base

    # Compare the N commit subjects ending at `merge_commit` against the PR's N commit headlines.
    if commit_headlines and len(commit_headlines) == commits_count:
        log_res = git(project.root, "log", "--format=%s", "-n", str(commits_count), merge_commit, check=False)
        if log_res.returncode == 0:
            landed_subjects = list(reversed(log_res.stdout.splitlines()))
            if landed_subjects == commit_headlines:
                return n_base
            # Subjects do not match an N-commit rebase; confirm squash if tip subject differs from last PR commit
            # or carries GitHub's `(#N)` squash suffix.
            tip_subject = landed_subjects[-1] if landed_subjects else ""
            pr_num = current_pr.get("number")
            if tip_subject != commit_headlines[-1] or (pr_num is not None and f"(#{pr_num})" in tip_subject):
                return parents[0]

    # Ambiguous multi-commit single-parent landing: defer rather than running hooks over the wrong diff.
    return None


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
    """Serialized under `project.integration_mutex`: re-reads durable PR state inside the lock,
    verifies the main checkout is clean on `main` and contains the merged PR, proves the exact
    landed commit range, journals each post-merge hook BEFORE execution and marks it done right
    after (surfacing interrupted invocations instead of blindly re-running them), cleans up
    verified standalone checkouts, and checkpoints `hooks_ran` and `local_synced`."""
    project = context.project
    run_dir = project.wp_run_dir(wp_id)

    with NamedMutex(project.integration_mutex):
        stored_state = run_state.load(run_dir)
        stored_pr = stored_state.get("pr")
        # Durable state loaded inside the lock MUST win over any pre-lock `pr_info` snapshot.
        current_pr = _merge_pr_records(pr_info, stored_pr or {})
        # Enrich with delivery metadata ONLY when PR number AND head SHA still match delivery (A20).
        delivery_rec = stored_state.get("pr_delivery")
        if (
            isinstance(delivery_rec, dict)
            and delivery_rec.get("number") == current_pr.get("number")
            and delivery_rec.get("verified_head_sha")
            and delivery_rec.get("verified_head_sha") == current_pr.get("head_sha")
        ):
            for key in ("commits_count", "base_sha", "commit_headlines"):
                if current_pr.get(key) is None and delivery_rec.get(key) is not None:
                    current_pr[key] = delivery_rec[key]
        if current_pr.get("local_synced"):
            return True
        if not _main_checkout_clean_on_main(project):
            return False

        merge_commit = current_pr.get("merge_commit")
        if not merge_commit:
            fetched_mc, fetched_cnt, fetched_hl, fetched_base, _fetched_head = _fetch_pr_commit_evidence(context, current_pr)
            if fetched_mc:
                merge_commit = fetched_mc
                current_pr["merge_commit"] = fetched_mc
                patch: dict[str, object] = {"merge_commit": fetched_mc}
                if fetched_cnt is not None and current_pr.get("commits_count") is None:
                    current_pr["commits_count"] = fetched_cnt
                    patch["commits_count"] = fetched_cnt
                if fetched_hl is not None and current_pr.get("commit_headlines") is None:
                    current_pr["commit_headlines"] = fetched_hl
                    patch["commit_headlines"] = fetched_hl
                if fetched_base is not None and current_pr.get("base_sha") is None:
                    current_pr["base_sha"] = fetched_base
                    patch["base_sha"] = fetched_base
                run_state.set_pr(run_dir, patch)

        synced = main_contains_merge(project, str(merge_commit)) if merge_commit else False
        if not synced:
            return False

        if not current_pr.get("hooks_ran"):
            hooks_cfg = project.config["post_merge_hooks"]
            if not hooks_cfg:
                run_state.set_pr(run_dir, {"hooks_ran": True})
            else:
                hook_base = _resolve_merge_hook_base(context, project, current_pr, str(merge_commit))
                if not hook_base:
                    # Ambiguous landed range: defer hooks rather than running over wrong/unrelated commits.
                    return False
                hook_after = str(merge_commit)
                hook_range = (hook_base, hook_after)
                if ran_hook_ranges is None or hook_range not in ran_hook_ranges:
                    from .commands import run_commands
                    changed = git(project.root, "diff", "--name-only", hook_base, hook_after).stdout.splitlines()
                    target_log = log_path or (project.runs_dir / "post-merge.log")
                    hook_lock = FileLock(project.runs_dir / POST_MERGE_HOOKS_LOCK)
                    for idx, hook in enumerate(hooks_cfg):
                        patterns = [wpfile.glob_to_regex(pattern) for pattern in hook["when_changed"]]
                        if not any(pattern.match(path) for path in changed for pattern in patterns):
                            continue
                        key = f"{hook_base}..{hook_after}::{idx}::{hook['command']}"
                        with hook_lock:
                            journal = _load_hook_journal(project.runs_dir)
                            existing = journal.get(key)
                            if isinstance(existing, dict):
                                if existing.get("status") == "done":
                                    continue
                                if existing.get("status") == "running":
                                    # Previous process crashed mid-hook after potential side effect:
                                    # surface ONCE with recovery CLI commands (A19/A50).
                                    if not existing.get("alerted"):
                                        journal[key] = {**existing, "alerted": True}
                                        _save_hook_journal(project.runs_dir, journal)
                                        quoted_key = shlex.quote(key)
                                        context.journal.emit(
                                            "integration-failed",
                                            (
                                                f"{wp_id}: post-merge hook '{hook['command']}' was interrupted mid-execution ({key}); "
                                                f"reconcile with `wpo ledger hooks --mark-done {quoted_key}` or `wpo ledger hooks --retry {quoted_key}`"
                                            ),
                                            {"wp": wp_id},
                                        )
                                    return False
                            journal[key] = {
                                "status": "running",
                                "wp": wp_id,
                                "range": f"{hook_base}..{hook_after}",
                                "command": hook["command"],
                                "started_at": time.time(),
                                "alerted": False,
                            }
                            _save_hook_journal(project.runs_dir, journal)
                        failures = run_commands(
                            project.config["shell"], [hook["command"]], project.root,
                            project.env_for(None), target_log, project.config["command_timeout_minutes"],
                        )
                        with hook_lock:
                            journal = _load_hook_journal(project.runs_dir)
                            journal[key] = {
                                **(journal.get(key) if isinstance(journal.get(key), dict) else {}),
                                "status": "done",
                                "ok": not failures,
                                "finished_at": time.time(),
                            }
                            _save_hook_journal(project.runs_dir, journal)
                        if log_fn is not None:
                            log_fn(f"post-merge hook '{hook['command']}': {'failed' if failures else 'ok'}")
                        if failures:
                            context.journal.emit(
                                "integration-failed",
                                f"{wp_id} merged, but post-merge hook '{hook['command']}' failed (log {target_log})",
                                {"wp": wp_id},
                            )
                    if ran_hook_ranges is not None:
                        ran_hook_ranges.add(hook_range)
                run_state.set_pr(run_dir, {"hooks_ran": True})

        if cleanup_fn is not None:
            cleanup_fn()
        else:
            from .integrate import cleanup_merged_outputs
            _cleanup_merged_worktree(context, wp_id, current_pr)
            cleanup_merged_outputs(context, wp_id)
        run_state.set_pr(run_dir, {"local_synced": True})
        return True


def is_wpo_delivered_pr(local_state: dict, live_pr: dict | None, *, repo: str, main_branch: str) -> bool:
    """Checks immutable WPO delivery provenance (`state.json['pr_delivery']`) against `live_pr`
    for the exact repository, PR number, base branch, same-repo flag, and verified head SHA."""
    if not isinstance(local_state, dict) or not isinstance(live_pr, dict):
        return False
    delivery = local_state.get("pr_delivery")
    if not isinstance(delivery, dict):
        return False
    if str(delivery.get("repo") or "").lower() != repo.lower():
        return False
    if delivery.get("number") is None or delivery.get("number") != live_pr.get("number"):
        return False
    if str(delivery.get("base") or "") != main_branch:
        return False
    if live_pr.get("base") and str(live_pr["base"]) != main_branch:
        return False
    if live_pr.get("is_cross_repository"):
        return False
    verified_head = str(delivery.get("verified_head_sha") or "").strip()
    return bool(verified_head and live_pr.get("head_sha") == verified_head)


def reconcile_open_prs(context, *, force_sync: bool = True) -> dict:
    """Refreshes GitHub state and reconciles open WPO pull requests & pending local-ff issue closes."""
    if not context.project.is_github or context.tracker is None:
        return {
            "merged": [], "failed_checks": [], "open": [], "closed_unmerged": [],
            "pending_local_sync": [], "pending_issue_close": [],
        }
    tracker: GitHubTracker = context.tracker
    tracker.sync(force=force_sync)
    wps = tracker.work_packages(read_only=True)
    cache = tracker.load_cache() or {}
    cached_pr_numbers: set[int] = {
        int(pr["number"]) for pr in (cache.get("prs") or [])
        if isinstance(pr, dict) and pr.get("number") is not None
    }
    for issue_node in cache.get("issues") or []:
        if not isinstance(issue_node, dict):
            continue
        for pr_node in (issue_node.get("closedByPullRequestsReferences") or {}).get("nodes") or []:
            if isinstance(pr_node, dict) and pr_node.get("number") is not None:
                cached_pr_numbers.add(int(pr_node["number"]))
    repo = tracker.repo()
    merged_now: list[str] = []
    pending_local_sync: list[tuple[str, dict]] = []
    failed_checks: list[str] = []
    still_open: list[str] = []
    closed_unmerged: list[str] = []
    pending_issue_close: list[str] = []

    for wp in wps:
        run_dir = context.project.runs_dir / wp.id
        local_state = run_state.load(run_dir)

        # A18: Retry any persisted pending GitHub issue close from verified `local-ff` delivery.
        if local_state.get("pending_issue_close"):
            if wp.github_state == "CLOSED":
                run_state.clear_pending_issue_close(run_dir)
            else:
                try:
                    tracker.close_issue_completed(wp.id, commit=local_state.get("local_ff_commit"))
                    run_state.clear_pending_issue_close(run_dir)
                except GhError as error:
                    pending_issue_close.append(wp.id)
                    context.journal.emit(
                        "github-sync-failed",
                        f"{wp.id}: retrying GitHub issue close after local-ff merge failed: {error}",
                        {"wp": wp.id},
                    )

        local_pr = local_state.get("pr")
        live_pr = wp.pr
        if not live_pr and not local_pr:
            continue
        # If local state thinks a tracked PR is still OPEN and it did not appear in the synced cache,
        # query GitHub directly to catch terminal CLOSED/MERGED transitions outside the top-200 window.
        if (
            isinstance(local_pr, dict)
            and local_pr.get("state") == "OPEN"
            and local_pr.get("number") is not None
            and int(local_pr["number"]) not in cached_pr_numbers
        ):
            view = tracker.gh.run(
                "pr", "view", str(local_pr["number"]), "-R", repo,
                "--json", "state,mergedAt,mergeCommit,autoMergeRequest,baseRefName,baseRefOid,headRefName,headRefOid,isCrossRepository,isDraft,mergeStateStatus,statusCheckRollup,commits",
                check=False,
            )
            if view.returncode == 0 and view.stdout.strip():
                try:
                    parsed_view = json.loads(view.stdout)
                except ValueError:
                    parsed_view = None
                if isinstance(parsed_view, dict) and parsed_view.get("state"):
                    parsed_view.setdefault("number", local_pr["number"])
                    parsed_view.setdefault("url", local_pr.get("url"))
                    live_pr = _normalize_pr(parsed_view)

        merged_before = bool(local_pr and local_pr.get("merged"))
        local_synced_before = bool(local_pr and local_pr.get("local_synced"))
        if live_pr and (live_pr.get("merged") or live_pr.get("state") == "MERGED"):
            pr_record = _merge_pr_records(local_pr, live_pr, merged=True, state="MERGED", merge_queued=False, merge_queued_head_sha=None)
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
        if live_pr and live_pr.get("state") == "CLOSED" and not live_pr.get("merged"):
            pr_record = _merge_pr_records(local_pr, live_pr, state="CLOSED", merged=False, auto_merge=False, merge_queued=False, merge_queued_head_sha=None)
            pr_num = live_pr.get("number")
            if (local_pr or {}).get("closed_notified_number") != pr_num:
                pr_record["closed_notified_number"] = pr_num
                context.journal.emit(
                    "pr-closed-unmerged",
                    f"{wp.id} PR #{pr_num} was closed without merging ({live_pr.get('url', '')})",
                    {"wp": wp.id},
                )
            run_state.set_pr(run_dir, pr_record)
            closed_unmerged.append(wp.id)
            continue
        if live_pr and live_pr.get("state") == "OPEN":
            still_open.append(wp.id)
            head_sha = live_pr.get("head_sha")
            was_queued_for_head = bool(
                (local_pr or {}).get("merge_queued")
                and head_sha
                and (local_pr or {}).get("merge_queued_head_sha") == head_sha
            )
            already_queued = was_queued_for_head
            dequeued_now = False
            if was_queued_for_head:
                # A63: Verify the PR is still actually in GitHub's merge queue (`isInMergeQueue`);
                # if dropped from the queue while staying OPEN on the same head, clear `merge_queued`
                # and surface an explicit `pr-checks-failed` alert so the lane never hangs forever.
                in_queue = live_pr.get("in_merge_queue")
                if in_queue is None and live_pr.get("number") is not None:
                    try:
                        q_owner, q_name = repo.split("/", 1)
                        q_data = tracker.gh.graphql(
                            PR_MERGE_QUEUE_GRAPHQL,
                            {"owner": q_owner, "name": q_name, "number": int(live_pr["number"])},
                        )
                        pr_node = (q_data.get("repository") or {}).get("pullRequest")
                        if isinstance(pr_node, dict) and "isInMergeQueue" in pr_node:
                            in_queue = pr_node["isInMergeQueue"]
                    except GhError:
                        in_queue = None
                if in_queue is False:
                    already_queued = False
                    dequeued_now = True
            prev_removed_sha = (local_pr or {}).get("merge_queue_removed_head_sha")
            removed_for_head = bool(head_sha and prev_removed_sha == head_sha) or dequeued_now
            run_state.set_pr(
                run_dir,
                _merge_pr_records(
                    local_pr,
                    live_pr,
                    merge_queued=already_queued,
                    merge_queued_head_sha=head_sha if already_queued else None,
                    merge_queue_removed_head_sha=head_sha if removed_for_head else None,
                ),
            )
            check_state = live_pr.get("check_state")
            merge_state = live_pr.get("merge_state_status")
            if dequeued_now:
                if prev_removed_sha != head_sha:
                    context.journal.emit(
                        "pr-checks-failed",
                        f"{wp.id} PR #{live_pr.get('number')} was removed from the merge queue ({live_pr.get('url', '')})",
                        {"wp": wp.id},
                    )
                    failed_checks.append(wp.id)
            elif check_state in ("FAILURE", "ERROR") or merge_state == "DIRTY":
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
                and not already_queued
                and not removed_for_head
                and str(context.project.github_config.get("auto_merge_fallback") or "wait") == "direct-merge"
                and _is_direct_merge_ready(merge_state, check_state)
                and is_wpo_delivered_pr(local_state, live_pr, repo=repo, main_branch=context.project.main_branch)
            ):
                delivery_rec = local_state["pr_delivery"]
                verified_head = str(delivery_rec["verified_head_sha"])
                merge_method = str(context.project.github_config.get("merge_method") or "squash")
                args = [
                    "pr", "merge", str(live_pr["number"]), "-R", repo,
                    f"--{merge_method}", "--match-head-commit", verified_head,
                ]
                if context.project.github_config.get("delete_branch_on_merge", True):
                    args.append("--delete-branch")
                attempt = tracker.gh.run(*args, check=False)
                if attempt.returncode == 0:
                    # A41: Confirm live PR state via `gh pr view` (exit 0 can mean merge-queue admission).
                    confirmed_state = "OPEN"
                    confirmed_merged = False
                    merge_commit = None
                    commits_count = live_pr.get("commits_count")
                    commit_headlines = live_pr.get("commit_headlines")
                    view = tracker.gh.run(
                        "pr", "view", str(live_pr["number"]), "-R", repo,
                        "--json", "state,mergedAt,mergeCommit,autoMergeRequest,commits", check=False,
                    )
                    if view.returncode == 0 and view.stdout.strip():
                        try:
                            info = json.loads(view.stdout)
                            if isinstance(info, dict):
                                confirmed_state = str(info.get("state") or confirmed_state)
                                confirmed_merged = (confirmed_state == "MERGED") or bool(info.get("mergedAt"))
                                if isinstance(info.get("mergeCommit"), dict):
                                    merge_commit = info["mergeCommit"].get("oid")
                                if isinstance(info.get("commits"), list) and info["commits"]:
                                    commits_count = len(info["commits"])
                                    hl = [str(c.get("messageHeadline") or "").strip() for c in info["commits"] if isinstance(c, dict)]
                                    if hl and all(hl):
                                        commit_headlines = hl
                        except ValueError:
                            pass
                    if confirmed_merged:
                        pr_record = _merge_pr_records(
                            local_pr, live_pr,
                            merged=True, state="MERGED", merge_commit=merge_commit,
                            merge_method=merge_method, commits_count=commits_count,
                            commit_headlines=commit_headlines,
                            merge_queued=False, merge_queued_head_sha=None,
                            local_synced=False, hooks_ran=False,
                        )
                        run_state.set_pr(run_dir, pr_record)
                        run_state.set_status(run_dir, "done")
                        merged_now.append(wp.id)
                        still_open.remove(wp.id)
                        pending_local_sync.append((wp.id, pr_record))
                        context.journal.emit("wp-merged", f"{wp.id} merged via PR #{live_pr.get('number')} ({live_pr.get('url', '')})", {"wp": wp.id})
                    else:
                        # A41/A58: Queued in merge queue or awaiting GitHub merge: keep pending as
                        # OPEN with durable `merge_queued` flag so direct merge does not re-fire every tick.
                        run_state.set_pr(
                            run_dir,
                            _merge_pr_records(
                                local_pr, live_pr,
                                state=confirmed_state, merged=False,
                                merge_queued=True, merge_queued_head_sha=head_sha,
                            ),
                        )

    still_pending_sync: list[str] = []
    if pending_local_sync:
        sync_result = sync_local_main(context)
        ran_hook_ranges: set[tuple[str, str]] = set()
        for wp_id, pr_info in pending_local_sync:
            if not finalize_merged_pr(context, wp_id, pr_info, sync_result, ran_hook_ranges=ran_hook_ranges):
                still_pending_sync.append(wp_id)

    return {
        "merged": merged_now,
        "failed_checks": failed_checks,
        "open": still_open,
        "closed_unmerged": closed_unmerged,
        "pending_local_sync": still_pending_sync,
        "pending_issue_close": pending_issue_close,
    }
