"""Deterministic, idempotent migration from committed WP markdown files to GitHub Issues,
GitHub Projects v2, auto-merge PR delivery, and the split private local ledger.

Every step is deterministic and checkpointed in `migration-state.json`:
1. Preflight parses all committed and in-flight worktree WPs, computes topological dependency
   order, checks existing GitHub issues for `legacy_wp_id` markers to guarantee idempotency,
   and runs the outbound privacy guard on every candidate issue before writing anything.
2. GitHub provisioning ensures milestones, labels (`lane:*`, `blocked`), optional Project v2,
   creates issues in dependency order, wires native `blocked-by` relationships, rewrites
   `WP-XXXX` cross-references to `#<N>`, closes already-merged WPs, and sets board statuses.
3. Private ledger migration copies transcripts, usage indexes, findings/attempt ledgers, and
   extracts private/mutable sections (`Progress log`, ticked `Steps`, `Review`, `Re-evaluation`,
   `Try it`, `Handoff`, personal `builder`/`reviewer`/`mode`/`skills` overrides, and in-flight
   worktree bindings) into `<runs>/GH-<N>/state.json`—by default in the out-of-repo external
   ledger so transcripts can never be committed.
4. Updates `.orchestration/config.json` to `tracker: "github"`, `delivery: "pr-automerge"`,
   and refreshes the local GitHub cache.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import github, privacy, run_state, wpfile
from .context import Context
from .platform import replace_file
from .project import CONFIG_RELATIVE_PATH, Project, git

MIGRATION_STATE_FILE = "migration-state.json"
PROGRESS_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?:\[([^\]]*)\] )?(.*)$")
CHECKBOX_RE = re.compile(r"^\s*[-*] \[([ xX])\]")
PLACEHOLDER_MILESTONES = frozenset({"", "M?", "TBD", "none", "None"})
GLOBAL_RUN_ENTRIES = (
    "events.jsonl",
    "lane-holds.json",
    "routing-mode.json",
    "boot.json",
    "usage",
    "continuous",
    "t3",
    "audits",
    "placements",
)


class MigrationError(RuntimeError):
    """Preflight or execution failure during WP -> GitHub migration."""


@dataclass(frozen=True)
class MigrationOptions:
    apply: bool = False
    repo: str | None = None
    project_number: int | None = None
    project_owner: str | None = None
    project_title: str | None = None
    create_project: bool = True
    external_ledger: bool = True
    include_done: bool = True
    remove_wp_files: bool = False
    enable_auto_merge: bool = False
    allow_local_paths: bool = False


def _read_wp_files_from_git(project: Project) -> tuple[list[wpfile.WorkPackage], dict[str, str]]:
    """Reads WP files committed on `main_branch` directly from git, independent of `project.tracker`."""
    listing = git(project.root, "ls-tree", "-z", project.main_branch, "--", f"{project.wp_dir_relative}/", check=False)
    if listing.returncode != 0 or not listing.stdout:
        return [], {}
    blobs = []
    for entry in filter(None, listing.stdout.split("\0")):
        meta, _, path = entry.partition("\t")
        name = path.rsplit("/", 1)[-1]
        if meta.split()[1] == "blob" and name.startswith("WP-") and name.endswith(".md") and name != wpfile.TEMPLATE_NAME:
            blobs.append((meta.split()[2], path))
    if not blobs:
        return [], {}
    blobs.sort(key=lambda blob: blob[1])
    batch = subprocess.run(
        ["git", "-C", str(project.root), "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha, _ in blobs).encode(),
        capture_output=True,
        check=True,
    ).stdout
    wps, errors, offset = [], {}, 0
    for _sha, path in blobs:
        header_end = batch.index(b"\n", offset)
        size = int(batch[offset:header_end].split()[2])
        text = batch[header_end + 1:header_end + 1 + size].decode("utf-8-sig")
        offset = header_end + 1 + size + 1
        try:
            wps.append(wpfile.parse_text(text, project.root / path))
        except wpfile.WorkPackageError as error:
            errors[path] = str(error)
    return wps, errors


def _effective_wp_files(project: Project, main_wps: list[wpfile.WorkPackage]) -> tuple[list[wpfile.WorkPackage], dict[str, Path]]:
    """Overlays in-flight worktree WP copies onto main's WPs and returns `(effective, worktrees)`."""
    effective: list[wpfile.WorkPackage] = []
    worktrees: dict[str, Path] = {}
    for main_copy in main_wps:
        if main_copy.status == "done":
            effective.append(main_copy)
            continue
        wt = project.worktree(main_copy.id)
        wt_file = project.wp_dir(wt) / main_copy.file_name
        if wt.exists():
            worktrees[main_copy.id] = wt
        if wt_file.is_file():
            try:
                effective.append(wpfile.parse(wt_file))
                continue
            except wpfile.WorkPackageError:
                pass
        effective.append(main_copy)
    return effective, worktrees


def topological_order(wps: list[wpfile.WorkPackage]) -> list[wpfile.WorkPackage]:
    """Deterministic topological sort of WPs (dependencies first; cycles broken by WP id)."""
    by_id = {wp.id: wp for wp in wps}
    remaining = {wp.id: {dep for dep in wp.depends_on if dep in by_id and dep != wp.id} for wp in wps}
    ordered: list[wpfile.WorkPackage] = []
    while remaining:
        ready = sorted(wp_id for wp_id, deps in remaining.items() if not deps)
        picked = ready[0] if ready else min(remaining)
        ordered.append(by_id[picked])
        del remaining[picked]
        for deps in remaining.values():
            deps.discard(picked)
    return ordered


def _extract_progress_entries(wp: wpfile.WorkPackage) -> list[dict]:
    entries: list[dict] = []
    for line in wp.section("Progress log"):
        if not line.startswith("- "):
            if entries and line.strip():
                entries[-1]["text"] += "\n" + line.strip()
            continue
        if match := PROGRESS_LINE_RE.match(line):
            entries.append({"at": match.group(1), "actor": match.group(2) or "", "text": match.group(3)})
        else:
            entries.append({"at": "", "actor": "", "text": line[2:]})
    return entries


def _extract_done_steps(wp: wpfile.WorkPackage) -> list[int]:
    done: list[int] = []
    index = 0
    for line in wp.section("Steps"):
        if match := CHECKBOX_RE.match(line):
            index += 1
            if match.group(1) in ("x", "X"):
                done.append(index)
    return done


def _extract_try_it(wp: wpfile.WorkPackage) -> str:
    from .whats_new import try_it_text
    return try_it_text(wp)


def _extract_section_blocks(lines: list[str]) -> list[dict]:
    """Splits `### ...` subsections (Review / Re-evaluation) into structured local records."""
    blocks: list[dict] = []
    current_header = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("### "):
            if current_header or any(item.strip() for item in current_lines):
                blocks.append({"header": current_header, "body": "\n".join(current_lines).strip()})
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)
    if current_header or any(item.strip() for item in current_lines):
        blocks.append({"header": current_header, "body": "\n".join(current_lines).strip()})
    return blocks


def _extract_overrides(project: Project, wp: wpfile.WorkPackage) -> dict:
    overrides: dict = {}
    default_builder = project.config.get("default_builder", "code")
    default_reviewer = project.config.get("default_reviewer", "code-hard")
    if wp.builder and wp.builder != default_builder:
        overrides["builder"] = wp.builder
    if wp.reviewer and wp.reviewer != default_reviewer:
        overrides["reviewer"] = wp.reviewer
    if "delegate" in wp.fields and wp.fields["delegate"] != project.config.get("delegate_default"):
        overrides["delegate"] = wp.fields["delegate"]
    if wp.mode and wp.mode != "single":
        overrides["mode"] = wp.mode
    if wp.skills:
        overrides["skills"] = list(wp.skills)
    return overrides


def _load_migration_state(paths: list[Path]) -> dict:
    merged = {"version": 1, "repo": None, "project_number": None, "project_owner": None, "project_title": None, "map": {}, "body_hashes": {}}
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for key in ("repo", "project_number", "project_owner", "project_title"):
                if data.get(key) is not None:
                    merged[key] = data[key]
            if isinstance(data.get("map"), dict):
                merged["map"].update({str(k): int(v) for k, v in data["map"].items()})
            if isinstance(data.get("body_hashes"), dict):
                merged["body_hashes"].update(data["body_hashes"])
    return merged


def _save_migration_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replace_file(staging, path)


def _copy_missing(src: Path, dst: Path) -> int:
    """Recursively copies files from `src` to `dst` without overwriting existing destination files."""
    if not src.exists():
        return 0
    if src.is_file():
        if dst.exists():
            return 0
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        copied += _copy_missing(child, dst / child.name)
    return copied


LEGACY_ISSUES_GRAPHQL = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor, states: [OPEN, CLOSED], orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        body
      }
    }
  }
}
"""


def _existing_legacy_issues(gh: github.GhClient, repo: str) -> dict[str, tuple[int, str]]:
    """Pages through every issue in `repo` via GraphQL to collect `(number, body)` for every
    issue carrying a `legacy_wp_id`, preventing duplicates past 500 issues and supporting resume."""
    owner, name = repo.split("/", 1)
    found: dict[str, tuple[int, str]] = {}
    cursor: str | None = None
    while True:
        data = gh.graphql(LEGACY_ISSUES_GRAPHQL, {"owner": owner, "name": name, "cursor": cursor})
        conn = (data.get("repository") or {}).get("issues") or {}
        for issue in conn.get("nodes") or []:
            if not isinstance(issue, dict) or issue.get("number") is None:
                continue
            body = str(issue.get("body") or "")
            parsed = github.parse_issue_body(body)
            if legacy := parsed.get("legacy_wp_id"):
                found[legacy] = (int(issue["number"]), body)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]
    return found


def _remap_run_path(value: str, old_runs: Path, dest_runs: Path, wp_to_gh: dict[str, str]) -> str:
    """Rewrites paths under `old_runs/<WP-XXXX>/...` or `old_runs/t3/...` to their migrated
    locations under `dest_runs/<GH-N>/...` or `dest_runs/t3/...`.

    Resolves both paths first so Windows 8.3 short paths (`RUNNER~1` vs `runneradmin`), drive
    casing differences, and macOS `/var` -> `/private/var` symlinks all match reliably.
    """
    if not value:
        return value
    rel_parts: tuple[str, ...] | None = None
    try:
        resolved_old = old_runs.resolve()
        resolved_val = Path(value).resolve()
        rel_parts = resolved_val.relative_to(resolved_old).parts
    except (OSError, ValueError):
        rel_parts = None

    if rel_parts is None:
        old_variants = sorted(
            {str(old_runs.resolve()), old_runs.resolve().as_posix(), str(old_runs), old_runs.as_posix()},
            key=len,
            reverse=True,
        )
        for prefix in old_variants:
            if not prefix:
                continue
            for sep in ("/", "\\"):
                base = prefix.rstrip("/\\") + sep
                matched = value.lower().startswith(base.lower()) if os.name == "nt" else value.startswith(base)
                if matched:
                    rel = value[len(base):].replace("\\", "/").strip("/")
                    rel_parts = tuple(p for p in rel.split("/") if p)
                    break
            if rel_parts is not None:
                break

    if not rel_parts:
        return value
    head = wp_to_gh.get(rel_parts[0], rel_parts[0])
    return str(dest_runs.joinpath(head, *rel_parts[1:]))


def _write_json_atomic(path: Path, data: dict) -> None:
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    replace_file(staging, path)


def _rewrite_t3_state(dest_runs: Path, old_runs: Path, id_map: dict[str, int]) -> int:
    """Re-keys copied T3 bridge requests, session identities, and scope leases from `WP-XXXX` +
    `old_runs` to `GH-N` + `dest_runs` so in-flight native sessions stay visible to `Bridge`."""
    t3_dir = dest_runs / "t3"
    if not t3_dir.is_dir():
        return 0
    wp_to_gh = {legacy: f"GH-{num}" for legacy, num in id_map.items()}
    rewritten = 0

    req_dir = t3_dir / "requests"
    if req_dir.is_dir():
        for req_path in sorted(req_dir.glob("*.json")):
            try:
                data = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
                continue
            payload = data["payload"]
            changed = False
            old_wp = payload.get("wp")
            gh_id = wp_to_gh.get(old_wp) if isinstance(old_wp, str) else None
            if gh_id:
                payload["wp"] = gh_id
                if isinstance(payload.get("title"), str) and payload["title"].startswith(f"{old_wp}:"):
                    payload["title"] = f"{gh_id}{payload['title'][len(old_wp):]}"
                # Point progress_file at the migrated split local state.json for GitHub tasks.
                payload["progress_file"] = str(dest_runs / gh_id / run_state.STATE_FILE)
                changed = True
            for key in ("log_path", "heartbeat_file", "progress_file"):
                val = payload.get(key)
                if isinstance(val, str):
                    mapped = _remap_run_path(val, old_runs, dest_runs, wp_to_gh)
                    if mapped != val:
                        payload[key] = mapped
                        changed = True
            if changed:
                _write_json_atomic(req_path, data)
                rewritten += 1

    sess_dir = t3_dir / "sessions"
    if sess_dir.is_dir():
        for sess_path in sorted(sess_dir.glob("*.json")):
            try:
                data = json.loads(sess_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("env"), dict):
                continue
            old_wp = data["env"].get("WPO_WORK_PACKAGE")
            if isinstance(old_wp, str) and old_wp in wp_to_gh:
                data["env"]["WPO_WORK_PACKAGE"] = wp_to_gh[old_wp]
                _write_json_atomic(sess_path, data)
                rewritten += 1

    scope_dir = t3_dir / "scope"
    if scope_dir.is_dir():
        for scope_path in sorted(scope_dir.glob("*.json")):
            try:
                data = json.loads(scope_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("log_path"), str):
                continue
            mapped = _remap_run_path(data["log_path"], old_runs, dest_runs, wp_to_gh)
            if mapped != data["log_path"]:
                data["log_path"] = mapped
                _write_json_atomic(scope_path, data)
                rewritten += 1

    return rewritten


def _ensure_milestones(gh: github.GhClient, repo: str, titles: list[str]) -> list[str]:
    if not titles:
        return []
    existing_raw = gh.json("api", f"repos/{repo}/milestones?state=all&per_page=100")
    existing = {str(item["title"]) for item in existing_raw if isinstance(item, dict) and item.get("title")} if isinstance(existing_raw, list) else set()
    created: list[str] = []
    for title in titles:
        if title in existing:
            continue
        gh.run("api", "-X", "POST", f"repos/{repo}/milestones", "-f", f"title={title}")
        created.append(title)
    return created


def _ensure_labels(gh: github.GhClient, repo: str, labels: list[tuple[str, str, str]]) -> list[str]:
    ensured: list[str] = []
    for name, color, description in labels:
        gh.run("label", "create", name, "-R", repo, "--color", color, "--description", description, "--force")
        ensured.append(name)
    return ensured


def _ensure_project(gh: github.GhClient, repo: str, owner: str, title: str, number: int | None, lanes: list[str],
                    create_if_missing: bool) -> tuple[int | None, str | None, str]:
    """Resolves or creates the GitHub Project v2, links it to `repo`, and ensures the Lane field."""
    resolved_owner = owner or "@me"
    if number is None and not create_if_missing:
        return None, None, resolved_owner
    if number is None:
        listed = gh.run("project", "list", "--owner", resolved_owner, "--format", "json", check=False)
        if listed.returncode == 0 and listed.stdout.strip():
            try:
                projects = json.loads(listed.stdout).get("projects", [])
            except (ValueError, AttributeError):
                projects = []
            for proj in projects:
                if isinstance(proj, dict) and str(proj.get("title", "")).strip() == title:
                    number = int(proj["number"])
                    break
    if number is None:
        created = gh.json("project", "create", "--owner", resolved_owner, "--title", title, "--format", "json")
        if not isinstance(created, dict) or created.get("number") is None:
            raise MigrationError(f"could not create GitHub Project '{title}'")
        number = int(created["number"])
    repo_name = repo.split("/", 1)[1]
    gh.run("project", "link", str(number), "--owner", resolved_owner, "--repo", repo_name, check=False)

    if lanes:
        fields_raw = gh.run("project", "field-list", str(number), "--owner", resolved_owner, "--format", "json", check=False)
        existing_fields: set[str] = set()
        if fields_raw.returncode == 0 and fields_raw.stdout.strip():
            try:
                existing_fields = {f["name"] for f in json.loads(fields_raw.stdout).get("fields", []) if isinstance(f, dict) and f.get("name")}
            except (ValueError, AttributeError):
                existing_fields = set()
        if "Lane" not in existing_fields:
            gh.run(
                "project", "field-create", str(number), "--owner", resolved_owner,
                "--name", "Lane", "--data-type", "SINGLE_SELECT",
                "--single-select-options", ",".join(sorted(lanes)),
                check=False,
            )

    view_raw = gh.run("project", "view", str(number), "--owner", resolved_owner, "--format", "json", check=False)
    url = None
    if view_raw.returncode == 0 and view_raw.stdout.strip():
        try:
            url = json.loads(view_raw.stdout).get("url")
        except (ValueError, AttributeError):
            url = None
    return number, url, resolved_owner


def _issue_number_from_output(text: str) -> int:
    match = re.search(r"/issues/(\d+)\b", text)
    if not match:
        raise MigrationError(f"could not parse created issue number from gh output: {text!r}")
    return int(match.group(1))


def migrate_to_github(context: Context, options: MigrationOptions, *, gh_client: github.GhClient | None = None) -> dict:
    """Plans (default) or executes (`options.apply=True`) deterministic migration from WP files to
    GitHub Issues + Project v2 + split private local ledger."""
    project = context.project
    gh = gh_client or github.GhClient(str(context.machine.raw.get("gh", "gh")), cwd=project.root)

    main_wps, parse_errors = _read_wp_files_from_git(project)
    if parse_errors:
        details = "; ".join(f"{path}: {err}" for path, err in sorted(parse_errors.items()))
        raise MigrationError(f"fix WP parse errors before migrating: {details}")

    # Warn about uncommitted drafts in wp_dir that are not yet on main.
    committed_names = {wp.file_name for wp in main_wps}
    uncommitted_drafts = sorted(
        path.name for path in project.wp_dir().glob("WP-*.md")
        if path.name != wpfile.TEMPLATE_NAME and path.name not in committed_names
    ) if project.wp_dir().is_dir() else []

    done_on_main = {wp.id for wp in main_wps if wp.status == "done"}
    effective, worktrees = _effective_wp_files(project, main_wps)
    selected = [wp for wp in effective if options.include_done or wp.id not in done_on_main]
    ordered = topological_order(selected)

    repo = options.repo or project.github_config.get("repo") or github.resolve_repo(project)
    dest_runs = project.external_runs_dir() if options.external_ledger else project.runs_dir
    state_paths = [project.runs_dir / MIGRATION_STATE_FILE]
    if dest_runs.resolve() != project.runs_dir.resolve():
        state_paths.append(dest_runs / MIGRATION_STATE_FILE)
    mig_state = _load_migration_state(state_paths)

    milestones = sorted({wp.milestone for wp in ordered if wp.milestone not in PLACEHOLDER_MILESTONES})
    lanes = sorted({wp.lane for wp in ordered if wp.lane})
    lane_prefix = str(project.github_config.get("lane_label_prefix") or "lane:")
    blocked_label = project.github_config.get("blocked_label") or "blocked"
    labels_to_ensure = [(f"{lane_prefix}{lane}", "1d76db", f"WPO lane: {lane}") for lane in lanes]
    if blocked_label:
        labels_to_ensure.append((str(blocked_label), "d93f0b", "Blocked work item awaiting unblock or decision"))

    # Preflight privacy check on EVERY candidate issue before touching GitHub.
    privacy_violations: dict[str, list[str]] = {}
    for wp in ordered:
        raw_title = privacy.sanitize_for_github(wp.title, project.root)
        title_issues = privacy.check_public_text(raw_title, project.root, allow_local_paths=options.allow_local_paths)
        body_draft = github.render_issue_body(wp, project_root=project.root, privacy_check=False)
        body_issues = privacy.check_public_text(body_draft, project.root, allow_local_paths=options.allow_local_paths)
        combined = title_issues + body_issues
        if combined:
            privacy_violations[wp.id] = combined

    project_title = options.project_title or mig_state.get("project_title") or project.name
    project_owner = options.project_owner or mig_state.get("project_owner") or project.github_config.get("project_owner") or "@me"
    project_number = options.project_number if options.project_number is not None else (
        mig_state.get("project_number") if mig_state.get("project_number") is not None else project.github_config.get("project_number")
    )

    plan = {
        "applied": False,
        "repo": repo,
        "project_title": project_title,
        "project_owner": project_owner,
        "project_number": project_number,
        "source_runs_dir": str(project.runs_dir),
        "destination_runs_dir": str(dest_runs),
        "external_ledger": options.external_ledger,
        "total_wps": len(ordered),
        "open_wps": sum(1 for wp in ordered if wp.id not in done_on_main),
        "done_wps": sum(1 for wp in ordered if wp.id in done_on_main),
        "in_flight_worktrees": sorted(worktrees),
        "uncommitted_drafts": uncommitted_drafts,
        "milestones": milestones,
        "lanes": lanes,
        "labels": [label[0] for label in labels_to_ensure],
        "already_mapped": dict(mig_state["map"]),
        "privacy_violations": privacy_violations,
        "wps": [
            {
                "id": wp.id,
                "title": wp.title,
                "status": wp.status,
                "merged_on_main": wp.id in done_on_main,
                "milestone": wp.milestone,
                "lane": wp.lane,
                "depends_on": [dep for dep in wp.depends_on if dep in {item.id for item in ordered}],
                "owns_count": len(wp.owns),
                "has_worktree": wp.id in worktrees,
                "existing_issue": mig_state["map"].get(wp.id),
            }
            for wp in ordered
        ],
    }

    if privacy_violations:
        if options.apply:
            summary = "; ".join(f"{wp_id}: {', '.join(errs)}" for wp_id, errs in sorted(privacy_violations.items()))
            raise MigrationError(f"privacy preflight blocked migration before writing to GitHub: {summary}")
        return plan

    if not options.apply:
        return plan

    # ------------------------------------------------------------------ Apply migration

    # 1. Check repo auto-merge setting (optionally enable).
    repo_info = gh.json("api", f"repos/{repo}")
    allow_auto_merge = bool(repo_info.get("allow_auto_merge")) if isinstance(repo_info, dict) else False
    if options.enable_auto_merge and not allow_auto_merge:
        gh.run("api", "-X", "PATCH", f"repos/{repo}", "-f", "allow_auto_merge=true")
        allow_auto_merge = True

    # 2. Discover any already-migrated issues on GitHub (paginated GraphQL) for full idempotency.
    existing_legacy = _existing_legacy_issues(gh, repo)
    id_map: dict[str, int] = {**mig_state["map"], **{legacy: num for legacy, (num, _body) in existing_legacy.items()}}
    live_bodies: dict[str, str] = {legacy: body for legacy, (_num, body) in existing_legacy.items()}
    state_file = dest_runs / MIGRATION_STATE_FILE

    # 3. Ensure milestones, labels, and Project v2.
    created_milestones = _ensure_milestones(gh, repo, milestones)
    _ensure_labels(gh, repo, labels_to_ensure)
    project_number, project_url, project_owner = _ensure_project(
        gh, repo, project_owner, project_title, project_number, lanes, options.create_project,
    )
    mig_state.update(repo=repo, project_number=project_number, project_owner=project_owner, project_title=project_title, map=id_map)
    _save_migration_state(state_file, mig_state)

    # 4. Pass 1: Create missing GitHub issues in topological order (checkpointing after every issue).
    created_issues: dict[str, int] = {}
    for wp in ordered:
        if wp.id in id_map:
            continue
        title = privacy.require_public_safe(wp.title, project.root, enabled=True, allow_local_paths=options.allow_local_paths)
        body = github.render_issue_body(
            wp,
            id_map=id_map,
            project_root=project.root,
            privacy_check=True,
            allow_local_paths=options.allow_local_paths,
            migrated_merged=(wp.id in done_on_main),
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(body)
            body_file = Path(handle.name)
        try:
            args = ["issue", "create", "-R", repo, "--title", title, "--body-file", str(body_file)]
            if wp.milestone not in PLACEHOLDER_MILESTONES:
                args += ["--milestone", wp.milestone]
            if wp.lane:
                args += ["--label", f"{lane_prefix}{wp.lane}"]
            if wp.status == "blocked" and blocked_label:
                args += ["--label", str(blocked_label)]
            output = gh.run(*args).stdout.strip()
        finally:
            body_file.unlink(missing_ok=True)
        issue_number = _issue_number_from_output(output)
        issue_url = output.splitlines()[-1].strip() if "http" in output else f"https://github.com/{repo}/issues/{issue_number}"
        if project_number is not None:
            gh.run("project", "item-add", str(project_number), "--owner", project_owner, "--url", issue_url, check=False)
        id_map[wp.id] = issue_number
        created_issues[wp.id] = issue_number
        live_bodies[wp.id] = body
        mig_state["map"] = dict(id_map)
        _save_migration_state(state_file, mig_state)

    # 5. Pass 2: Rewrite forward WP-XXXX cross-references (including on resumed runs), wire native
    # blocked-by dependencies, and close WPs already merged on main.
    dependencies_wired = 0
    closed_issues: list[int] = []
    for wp in ordered:
        issue_number = id_map[wp.id]
        final_body = github.render_issue_body(
            wp,
            id_map=id_map,
            project_root=project.root,
            privacy_check=True,
            allow_local_paths=options.allow_local_paths,
            migrated_merged=(wp.id in done_on_main),
        )
        current_body = live_bodies.get(wp.id)
        if current_body is None:
            viewed = gh.run("issue", "view", str(issue_number), "-R", repo, "--json", "body", check=False)
            if viewed.returncode == 0 and viewed.stdout.strip():
                try:
                    current_body = str(json.loads(viewed.stdout).get("body") or "")
                except ValueError:
                    current_body = ""
        if current_body is None or final_body.strip() != current_body.strip():
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
                handle.write(final_body)
                body_file = Path(handle.name)
            try:
                gh.run("issue", "edit", str(issue_number), "-R", repo, "--body-file", str(body_file))
                live_bodies[wp.id] = final_body
            finally:
                body_file.unlink(missing_ok=True)

        dep_numbers = [str(id_map[dep]) for dep in wp.depends_on if dep in id_map and dep != wp.id]
        if dep_numbers:
            gh.run("issue", "edit", str(issue_number), "-R", repo, "--add-blocked-by", ",".join(dep_numbers))
            dependencies_wired += len(dep_numbers)

        if wp.id in done_on_main:
            gh.run("issue", "close", str(issue_number), "-R", repo, "--reason", "completed", check=False)
            closed_issues.append(issue_number)

    # 6. Pass 3: Migrate private local ledger (transcripts, findings, attempts, T3 state, state.json,
    # worktree bindings) without clobbering post-migration updates on idempotent re-runs.
    dest_runs.mkdir(parents=True, exist_ok=True)
    global_files_copied = 0
    if dest_runs.resolve() != project.runs_dir.resolve():
        for name in GLOBAL_RUN_ENTRIES:
            global_files_copied += _copy_missing(project.runs_dir / name, dest_runs / name)
    _rewrite_t3_state(dest_runs, project.runs_dir, id_map)

    migrated_ledgers: list[str] = []
    migrated_statuses: dict[str, str] = {}
    for wp in ordered:
        issue_number = id_map[wp.id]
        gh_id = f"GH-{issue_number}"
        old_run_dir = project.runs_dir / wp.id
        new_run_dir = dest_runs / gh_id
        new_run_dir.mkdir(parents=True, exist_ok=True)
        if old_run_dir.is_dir() and old_run_dir.resolve() != new_run_dir.resolve():
            _copy_missing(old_run_dir, new_run_dir)
        # Re-key t3-workspace.json if present so native T3 validation recognises the new GH-<N> id.
        t3_ws = new_run_dir / "t3-workspace.json"
        if t3_ws.is_file():
            try:
                ws_data = json.loads(t3_ws.read_text(encoding="utf-8"))
            except ValueError:
                ws_data = None
            if isinstance(ws_data, dict):
                ws_data["wp"] = gh_id
                ws_data["legacy_wp_id"] = wp.id
                _write_json_atomic(t3_ws, ws_data)

        # Populate private local state.json from WP sections only where fields are not already set,
        # so re-running migration never discards post-migration progress, steps, Try it, or status.
        existing_local = run_state.load(new_run_dir)
        new_state = {
            **existing_local,
            "status": existing_local.get("status") or ("done" if wp.id in done_on_main else wp.status),
            "progress": existing_local.get("progress") or _extract_progress_entries(wp),
            "steps_done": existing_local.get("steps_done") or _extract_done_steps(wp),
            "try_it": existing_local.get("try_it") or _extract_try_it(wp),
            "handoff": existing_local.get("handoff") or "\n".join(wp.section("Handoff")).strip(),
            "reviews": existing_local.get("reviews") or _extract_section_blocks(wp.section("Review")),
            "reevaluations": existing_local.get("reevaluations") or _extract_section_blocks(wp.section("Re-evaluation")),
            "overrides": {**_extract_overrides(project, wp), **(existing_local.get("overrides") or {})},
            "legacy_wp_id": existing_local.get("legacy_wp_id") or wp.id,
            "migrated_merged_on_main": bool(existing_local.get("migrated_merged_on_main") or (wp.id in done_on_main)),
        }
        if wp.id in worktrees and not new_state.get("worktree"):
            wt = worktrees[wp.id]
            new_state["worktree"] = str(wt)
            branch_res = git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False)
            if branch_res.returncode == 0 and branch_res.stdout.strip() and not new_state.get("branch"):
                new_state["branch"] = branch_res.stdout.strip()
        run_state.save(new_run_dir, new_state)
        migrated_ledgers.append(gh_id)
        migrated_statuses[gh_id] = str(new_state["status"])

    # 7. Pass 4: Update `.orchestration/config.json` atomically.
    config_path = project.root / CONFIG_RELATIVE_PATH
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["tracker"] = "github"
    raw_config["delivery"] = "pr-automerge"
    if options.external_ledger:
        raw_config["runs_dir"] = "external"
    gh_section = dict(raw_config.get("github") or {})
    gh_section["repo"] = repo
    if project_number is not None:
        gh_section["project_number"] = project_number
        gh_section["project_owner"] = project_owner
    raw_config["github"] = gh_section
    staging_cfg = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.tmp")
    staging_cfg.write_text(json.dumps(raw_config, indent=2) + "\n", encoding="utf-8")
    replace_file(staging_cfg, config_path)

    # 8. Optional removal of migrated WP files.
    removed_files: list[str] = []
    if options.remove_wp_files:
        for wp in ordered:
            target = project.wp_dir() / wp.file_name
            if target.is_file():
                target.unlink()
                removed_files.append(f"{project.wp_dir_relative}/{wp.file_name}")

    # 9. Sync cache + push open Project v2 board statuses using the newly configured project.
    migrated_context = Context.load(project.root, context.machine, gh_client=gh)
    if migrated_context.tracker is not None:
        try:
            migrated_context.tracker.sync(force=True)
            for wp in ordered:
                gh_id = f"GH-{id_map[wp.id]}"
                status_to_sync = migrated_statuses.get(gh_id, wp.status)
                if wp.id in done_on_main or status_to_sync == "todo":
                    continue
                migrated_context.tracker.sync_issue_status(gh_id, status_to_sync)
            migrated_context.tracker.sync(force=True)
        except github.GhError:
            pass

    migrated_context.journal.emit(
        "migration-done",
        f"migrated {len(ordered)} work package(s) to GitHub {repo} (project #{project_number}); private ledger at {dest_runs}",
    )

    return {
        **plan,
        "applied": True,
        "project_number": project_number,
        "project_url": project_url,
        "allow_auto_merge": allow_auto_merge,
        "id_map": {wp_id: f"GH-{num}" for wp_id, num in sorted(id_map.items())},
        "created_issues": {wp_id: f"GH-{num}" for wp_id, num in sorted(created_issues.items())},
        "created_milestones": created_milestones,
        "dependencies_wired": dependencies_wired,
        "closed_issues": closed_issues,
        "global_files_copied": global_files_copied,
        "migrated_ledgers": migrated_ledgers,
        "removed_files": removed_files,
        "config_updated": str(config_path),
    }
