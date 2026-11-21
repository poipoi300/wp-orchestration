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

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import github, privacy, run_state, usage, wpfile
from .context import Context
from .platform import FileLock, NamedMutex, pid_alive, replace_file
from .project import CONFIG_RELATIVE_PATH, Project, git

MIGRATION_STATE_FILE = "migration-state.json"
LEDGER_META_FILE = "ledger-meta.json"
PROGRESS_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?:\[([^\]]*)\] )?(.*)$")
CHECKBOX_RE = re.compile(r"^\s*[-*] \[([ xX])\]")
PLACEHOLDER_MILESTONES = frozenset({"", "M?", "TBD", "none", "None"})
GLOBAL_RUN_ENTRIES = (
    "events.jsonl",
    "lane-holds.json",
    "routing-mode.json",
    "boot.json",
    "usage",
    "dispatch",
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


def _read_wp_files_from_git(project: Project) -> tuple[list[wpfile.WorkPackage], dict[str, str], dict[str, str]]:
    """Reads WP files committed on `main_branch` directly from git, independent of `project.tracker`.
    Returns `(work_packages, parse_errors, raw_blob_text_by_wp_id)`."""
    listing = git(project.root, "ls-tree", "-z", project.main_branch, "--", f"{project.wp_dir_relative}/", check=False)
    if listing.returncode != 0 or not listing.stdout:
        return [], {}, {}
    blobs = []
    for entry in filter(None, listing.stdout.split("\0")):
        meta, _, path = entry.partition("\t")
        name = path.rsplit("/", 1)[-1]
        if meta.split()[1] == "blob" and name.startswith("WP-") and name.endswith(".md") and name != wpfile.TEMPLATE_NAME:
            blobs.append((meta.split()[2], path))
    if not blobs:
        return [], {}, {}
    blobs.sort(key=lambda blob: blob[1])
    batch = subprocess.run(
        ["git", "-C", str(project.root), "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha, _ in blobs).encode(),
        capture_output=True,
        check=True,
    ).stdout
    wps, errors, raw_blobs, offset = [], {}, {}, 0
    for _sha, path in blobs:
        header_end = batch.index(b"\n", offset)
        size = int(batch[offset:header_end].split()[2])
        text = batch[header_end + 1:header_end + 1 + size].decode("utf-8-sig")
        offset = header_end + 1 + size + 1
        try:
            parsed_wp = wpfile.parse_text(text, project.root / path, retain_body=True)
            wps.append(parsed_wp)
            raw_blobs[parsed_wp.id] = text
        except wpfile.WorkPackageError as error:
            errors[path] = str(error)
    return wps, errors, raw_blobs


def _effective_wp_files(project: Project, main_wps: list[wpfile.WorkPackage]) -> tuple[list[wpfile.WorkPackage], dict[str, Path]]:
    """Overlays separate in-flight worktree WP copies onto main's committed git blobs and returns `(effective, worktrees)`."""
    effective: list[wpfile.WorkPackage] = []
    worktrees: dict[str, Path] = {}
    main_resolved = project.root.resolve()
    for main_copy in main_wps:
        if main_copy.status == "done":
            effective.append(main_copy)
            continue
        wt = project.worktree(main_copy.id)
        if wt.exists() and wt.resolve() != main_resolved:
            worktrees[main_copy.id] = wt
            wt_file = project.wp_dir(wt) / main_copy.file_name
            if wt_file.is_file():
                try:
                    effective.append(wpfile.parse_text(wpfile.read_text(wt_file), wt_file, retain_body=True))
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


def _load_migration_state(paths: list[Path], target_repo: str | None) -> dict:
    """Loads checkpointed migration state strictly scoped to `target_repo` so checkpoints from
    one repository can never target or mutate issues in a different `--repo`."""
    merged: dict = {
        "version": 3,
        "loaded_version": 0,
        "repo": target_repo,
        "project_number": None,
        "project_owner": None,
        "project_title": None,
        "map": {},
        "written_bodies": {},
        "rewritten_refs": [],
        "deps_wired": [],
        "closed_merged": [],
        "project_items_added": [],
        "board_synced": [],
    }
    target_norm = (target_repo or "").strip().lower()
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        state_repo = str(data.get("repo") or "").strip().lower()
        if target_norm and state_repo and state_repo != target_norm:
            continue
        if target_norm and not state_repo:
            # Unbound checkpoint without a repository identity cannot be trusted across repos.
            continue
        try:
            ver = int(data.get("version") or 1)
        except (TypeError, ValueError):
            ver = 1
        merged["loaded_version"] = max(int(merged["loaded_version"]), ver)
        for key in ("repo", "project_number", "project_owner", "project_title"):
            if data.get(key) is not None:
                merged[key] = data[key]
        if isinstance(data.get("map"), dict):
            merged["map"].update({str(k): int(v) for k, v in data["map"].items()})
        if isinstance(data.get("written_bodies"), dict):
            merged["written_bodies"].update({str(k): str(v) for k, v in data["written_bodies"].items()})
        for list_key in ("rewritten_refs", "deps_wired", "closed_merged", "project_items_added", "board_synced"):
            if isinstance(data.get(list_key), list):
                merged[list_key] = sorted(set(merged[list_key]) | {str(item) for item in data[list_key]})
    return merged


def _save_migration_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replace_file(staging, path)


def _ledger_bound_repo(runs_dir: Path) -> str | None:
    """Returns the repository bound to `runs_dir` (from `ledger-meta.json`, `migration-state.json`,
    or `github-cache.json`), or `None` if unbound."""
    for filename in (LEDGER_META_FILE, MIGRATION_STATE_FILE, "github-cache.json"):
        path = runs_dir / filename
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("repo"):
            repo_val = str(data["repo"]).strip()
            if repo_val:
                return repo_val
    return None


def _write_ledger_meta(runs_dir: Path, repo: str) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    meta_path = runs_dir / LEDGER_META_FILE
    payload = {"version": 1, "repo": repo}
    staging = meta_path.with_name(f".{meta_path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replace_file(staging, meta_path)


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


def _move_dir_contents(src: Path, dst: Path) -> int:
    """Moves files from `src` into `dst` without losing data: deduplicates ONLY byte-identical
    files and preserves differing collisions under a deterministic `.legacy-<hash>` sibling name."""
    if not src.exists():
        return 0
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            src_bytes = src.read_bytes()
            if dst.is_file():
                if dst.read_bytes() == src_bytes:
                    src.unlink(missing_ok=True)
                    return 0
                digest = hashlib.sha256(src_bytes).hexdigest()[:8]
                alt = dst.with_name(f"{dst.stem}.legacy-{digest}{dst.suffix}")
                counter = 1
                while alt.exists():
                    if alt.is_file() and alt.read_bytes() == src_bytes:
                        src.unlink(missing_ok=True)
                        return 0
                    alt = dst.with_name(f"{dst.stem}.legacy-{digest}-{counter}{dst.suffix}")
                    counter += 1
                shutil.move(str(src), str(alt))
                return 1
            digest = hashlib.sha256(src_bytes).hexdigest()[:8]
            alt = dst.with_name(f"{dst.name}.legacy-{digest}")
            counter = 1
            while alt.exists():
                alt = dst.with_name(f"{dst.name}.legacy-{digest}-{counter}")
                counter += 1
            shutil.move(str(src), str(alt))
            return 1
        shutil.move(str(src), str(dst))
        return 1
    moved = 0
    if dst.exists() and not dst.is_dir():
        # A59: Destination file collides with source directory — preserve file under a `.legacy-<hash>` sibling.
        dst_bytes = dst.read_bytes()
        digest = hashlib.sha256(dst_bytes).hexdigest()[:8]
        alt = dst.with_name(f"{dst.stem}.legacy-{digest}{dst.suffix}")
        counter = 1
        while alt.exists():
            alt = dst.with_name(f"{dst.stem}.legacy-{digest}-{counter}{dst.suffix}")
            counter += 1
        shutil.move(str(dst), str(alt))
    dst.mkdir(parents=True, exist_ok=True)
    for child in list(src.iterdir()):
        moved += _move_dir_contents(child, dst / child.name)
    try:
        src.rmdir()
    except OSError:
        pass
    return moved


def _active_ledger_writers(project: Project, ordered: list[wpfile.WorkPackage],
                           extra_runs_dirs: tuple[Path, ...] = ()) -> list[str]:
    """Detects live supervisors, runners, direct dispatchers, streaming sessions, delegated workers,
    continuous schedulers, or in-flight T3 bridge requests still attached to the ledger."""
    runs_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    for candidate in (project._resolve_runs_dir(), *extra_runs_dirs):
        if candidate.is_dir():
            resolved = candidate.resolve()
            if resolved not in seen_dirs:
                seen_dirs.add(resolved)
                runs_dirs.append(candidate)

    active: list[str] = []
    wp_ids = {wp.id for wp in ordered}
    for runs in runs_dirs:
        active.extend(run_state.active_writer_leases(runs))
        for pid_file in sorted(runs.glob("supervisor-*.pid")):
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if pid_alive(pid):
                active.append(f"live supervisor {pid_file.stem} (pid {pid})")

        for prefix in ("WP-*", "GH-*"):
            for live_file in sorted(runs.glob(f"{prefix}/inbox/live.json")):
                try:
                    data = json.loads(live_file.read_text(encoding="utf-8"))
                    pid = int(data["pid"])
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if pid_alive(pid):
                    active.append(f"live streaming session for {live_file.parent.parent.name} (pid {pid})")
            for spawn_pid in sorted(runs.glob(f"{prefix}/delegate/*/spawn.pid")):
                try:
                    pid = int(spawn_pid.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    continue
                if pid_alive(pid):
                    active.append(f"live delegated worker {spawn_pid.parent.name} for {spawn_pid.parents[2].name} (pid {pid})")

        for entry in runs.iterdir():
            if entry.is_dir() and entry.name.startswith(("WP-", "GH-")):
                wp_ids.add(entry.name)

        req_dir = runs / "t3" / "requests"
        if req_dir.is_dir():
            for req_path in sorted(req_dir.glob("*.json")):
                try:
                    req = json.loads(req_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(req, dict):
                    continue
                state = req.get("state")
                req_id = req.get("id") or req_path.stem
                if state not in ("done", "failed", "cancelled"):
                    active.append(f"in-flight T3 {req.get('kind', 'request')} {req_id} (state={state})")
                elif state == "cancelled" and req.get("task_id"):
                    terminal = req.get("late_result") or req.get("result") or {}
                    if not (terminal.get("task_id") == req["task_id"] and terminal.get("outcome") and isinstance(terminal.get("text"), str)):
                        active.append(f"unsettled cancelled T3 {req.get('kind', 'request')} {req_id}")

    for wp_id in sorted(wp_ids):
        mutex = NamedMutex(project.mutex_name("wp", wp_id))
        if not mutex.acquire(blocking=False):
            active.append(f"active runner/supervisor claim for {wp_id}")
        else:
            mutex.release()

    sched_mutex = NamedMutex(project.mutex_name("continuous", "scheduler"))
    if not sched_mutex.acquire(blocking=False):
        active.append("active continuous scheduler")
    else:
        sched_mutex.release()

    return sorted(set(active))


LEGACY_ISSUES_GRAPHQL = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor, states: [OPEN, CLOSED], orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        body
        authorAssociation
        author { login }
      }
    }
  }
}
"""

TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _existing_legacy_issues(gh: github.GhClient, repo: str) -> dict[str, tuple[int, str]]:
    """Pages through every issue in `repo` via GraphQL to collect `(number, body)` for every
    trusted issue carrying a `legacy_wp_id` (A53: authored by OWNER/MEMBER/COLLABORATOR or the
    authenticated viewer). Rejects duplicate trusted live markers before any mutation (A6)."""
    owner, name = repo.split("/", 1)
    found: dict[str, tuple[int, str]] = {}
    duplicates: dict[str, list[int]] = {}
    viewer_login: str | None = None
    viewer_checked = False
    cursor: str | None = None
    while True:
        data = gh.graphql(LEGACY_ISSUES_GRAPHQL, {"owner": owner, "name": name, "cursor": cursor})
        conn = (data.get("repository") or {}).get("issues") or {}
        for issue in conn.get("nodes") or []:
            if not isinstance(issue, dict) or issue.get("number") is None:
                continue
            num = int(issue["number"])
            body = str(issue.get("body") or "")
            parsed = github.parse_issue_body(body)
            legacy = parsed.get("legacy_wp_id")
            if not legacy:
                continue
            assoc = str(issue.get("authorAssociation") or "").upper()
            trusted = assoc in TRUSTED_AUTHOR_ASSOCIATIONS
            if not trusted:
                author_login = str((issue.get("author") or {}).get("login") or "").strip().lower()
                if author_login:
                    if not viewer_checked:
                        viewer_checked = True
                        try:
                            viewer_login = gh.viewer_login().strip().lower()
                        except github.GhError:
                            viewer_login = None
                    if viewer_login and author_login == viewer_login:
                        trusted = True
            if not trusted:
                # Ignore untrusted third-party issues attempting to plant `legacy_wp_id` markers.
                continue
            if legacy in found and found[legacy][0] != num:
                duplicates.setdefault(legacy, [found[legacy][0]]).append(num)
            else:
                found[legacy] = (num, body)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]
    if duplicates:
        summary = "; ".join(f"{leg} -> {', '.join(f'#{n}' for n in sorted(set(nums)))}" for leg, nums in sorted(duplicates.items()))
        raise MigrationError(
            f"duplicate GitHub issues in {repo} carry the same legacy_wp_id marker ({summary}); "
            "resolve duplicate markers before migrating"
        )
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


def _rewrite_continuous_state(dest_runs: Path, old_runs: Path, id_map: dict[str, int]) -> int:
    """Re-keys `continuous/state.json` records, waiting entries, and owned inbox message paths
    from `WP-XXXX` + `old_runs` to `GH-N` + `dest_runs`."""
    cont_dir = dest_runs / "continuous"
    state_path = cont_dir / "state.json"
    if not state_path.is_file():
        return 0
    wp_to_gh = {legacy: f"GH-{num}" for legacy, num in id_map.items()}
    rewritten = 0
    with FileLock(cont_dir / "state.lock"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        changed = False
        records = data.get("records")
        if isinstance(records, dict):
            new_records: dict = {}
            for key, rec in records.items():
                target_key = wp_to_gh.get(key, key)
                if isinstance(rec, dict) and isinstance(rec.get("owned_message"), str):
                    mapped = _remap_run_path(rec["owned_message"], old_runs, dest_runs, wp_to_gh)
                    if mapped != rec["owned_message"]:
                        rec = {**rec, "owned_message": mapped}
                        changed = True
                if target_key != key:
                    changed = True
                    rewritten += 1
                    if target_key in records:
                        continue
                new_records[target_key] = rec
            if changed:
                data["records"] = new_records
        waiting = data.get("waiting")
        if isinstance(waiting, dict):
            new_waiting: dict = {}
            waiting_changed = False
            for key, val in waiting.items():
                target_key = wp_to_gh.get(key, key)
                if target_key != key:
                    waiting_changed = True
                    rewritten += 1
                    if target_key in waiting:
                        continue
                new_waiting[target_key] = val
            if waiting_changed:
                data["waiting"] = new_waiting
                changed = True
        if changed:
            _write_json_atomic(state_path, data)
    return rewritten


def _migrate_usage_index(dest_runs: Path, id_map: dict[str, int]) -> int:
    """Re-keys `usage/index.json` log entries from `WP-XXXX/...` to `GH-N/...` so every logical
    session has a single discoverable record after migration."""
    usage_dir = dest_runs / "usage"
    index_path = usage_dir / "index.json"
    if not index_path.is_file():
        return 0
    wp_to_gh = {legacy: f"GH-{num}" for legacy, num in id_map.items()}
    rekeyed = 0
    with FileLock(usage_dir / usage.INDEX_LOCK_FILE):
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(index, dict) or not isinstance(index.get("logs"), dict):
            return 0
        old_logs = index["logs"]
        new_logs: dict[str, dict] = {}
        changed = False
        for key, entry in old_logs.items():
            parts = key.split("/")
            if parts and parts[0] in wp_to_gh:
                gh_id = wp_to_gh[parts[0]]
                new_key = "/".join([gh_id, *parts[1:]])
                updated = dict(entry) if isinstance(entry, dict) else entry
                if isinstance(updated, dict) and isinstance(updated.get("record"), dict):
                    rec = dict(updated["record"])
                    rec["log"] = new_key
                    rec["wp"] = gh_id
                    updated["record"] = rec
                if new_key not in old_logs:
                    new_logs[new_key] = updated
                changed = True
                rekeyed += 1
            else:
                new_logs[key] = entry
        if changed:
            index["logs"] = new_logs
            staging = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
            staging.write_text(json.dumps(index), encoding="utf-8")
            replace_file(staging, index_path)
    return rekeyed


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

    main_wps, parse_errors, main_raw_blobs = _read_wp_files_from_git(project)
    if parse_errors:
        details = "; ".join(f"{path}: {err}" for path, err in sorted(parse_errors.items()))
        raise MigrationError(f"fix WP parse errors before migrating: {details}")

    # Warn about uncommitted drafts in wp_dir that are not yet on main.
    committed_names = {wp.file_name for wp in main_wps}
    uncommitted_drafts = sorted(
        path.name for path in project.wp_dir().glob("WP-*.md")
        if path.name != wpfile.TEMPLATE_NAME and path.name not in committed_names
    ) if project.wp_dir().is_dir() else []

    main_by_id = {wp.id: wp for wp in main_wps}
    done_on_main = {wp.id for wp in main_wps if wp.status == "done"}
    effective, worktrees = _effective_wp_files(project, main_wps)
    selected = [wp for wp in effective if options.include_done or wp.id not in done_on_main]
    ordered = topological_order(selected)

    repo = options.repo or project.github_config.get("repo") or github.resolve_repo(project)

    # A12: Resolve the requested destination ledger path and config value ONCE up front so
    # copying, checkpointing, and `.orchestration/config.json` always target the exact same path.
    config_path = project.root / CONFIG_RELATIVE_PATH
    raw_config_preview = json.loads(config_path.read_text(encoding="utf-8"))
    if options.external_ledger:
        dest_runs = project.external_runs_dir()
        target_runs_config = "external"
    else:
        configured_runs = str(raw_config_preview.get("runs_dir") or "").strip()
        if not configured_runs or configured_runs == "external":
            target_runs_config = ".orchestration/runs"
            dest_runs = project.root / target_runs_config
        else:
            target_runs_config = configured_runs
            expanded = Path(configured_runs).expanduser()
            dest_runs = expanded if expanded.is_absolute() else (project.root / expanded)

    source_runs = project._resolve_runs_dir()
    state_paths = [source_runs / MIGRATION_STATE_FILE]
    if dest_runs.resolve() != source_runs.resolve():
        state_paths.append(dest_runs / MIGRATION_STATE_FILE)
    mig_state = _load_migration_state(state_paths, repo)

    # A14 + A54: Canonicalize every outbound metadata string ONCE before preflight, validate the
    # exact canonical values, and transmit those exact canonical values to GitHub.
    lanes = sorted({wp.lane for wp in ordered if wp.lane})
    project_lane_options = sorted({
        privacy.sanitize_for_github(lane, project.root).replace(",", "-").strip()
        for lane in lanes
        if privacy.sanitize_for_github(lane, project.root).replace(",", "-").strip()
    })
    lane_prefix = str(project.github_config.get("lane_label_prefix") or "lane:")
    milestone_map: dict[str, str] = {}
    for wp in ordered:
        if wp.milestone not in PLACEHOLDER_MILESTONES and wp.milestone not in milestone_map:
            milestone_map[wp.milestone] = privacy.sanitize_for_github(wp.milestone, project.root)
    milestones = sorted(set(milestone_map.values()))
    lane_label_map: dict[str, str] = {
        lane: privacy.sanitize_for_github(f"{lane_prefix}{lane}", project.root)
        for lane in lanes
    }
    raw_blocked_label = project.github_config.get("blocked_label") or "blocked"
    blocked_label = privacy.sanitize_for_github(str(raw_blocked_label), project.root) if raw_blocked_label else ""
    labels_to_ensure: list[tuple[str, str, str]] = []
    seen_labels: set[str] = set()
    for lane in lanes:
        lbl_name = lane_label_map[lane]
        if lbl_name in seen_labels:
            continue
        seen_labels.add(lbl_name)
        lbl_desc = privacy.sanitize_for_github(f"WPO lane: {lane}", project.root)
        labels_to_ensure.append((lbl_name, "1d76db", lbl_desc))
    if blocked_label and blocked_label not in seen_labels:
        labels_to_ensure.append((blocked_label, "d93f0b", "Blocked work item awaiting unblock or decision"))

    raw_project_title = options.project_title or mig_state.get("project_title") or project.name
    project_title = privacy.sanitize_for_github(str(raw_project_title), project.root)
    project_owner = options.project_owner or mig_state.get("project_owner") or project.github_config.get("project_owner") or "@me"
    project_number = options.project_number if options.project_number is not None else (
        mig_state.get("project_number") if mig_state.get("project_number") is not None else project.github_config.get("project_number")
    )

    # Preflight privacy check on EVERY canonical outbound GitHub string before the first remote mutation.
    extra_forbidden = privacy.private_route_identifiers(context.machine, project, getattr(context, "router", None))
    privacy_violations: dict[str, list[str]] = {}
    if options.create_project or project_number is not None:
        title_check = privacy.check_public_text(
            project_title, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden,
        )
        if title_check:
            privacy_violations["project_title"] = title_check
        for lane_opt in project_lane_options:
            lane_check = privacy.check_public_text(
                lane_opt, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden,
            )
            if lane_check:
                privacy_violations[f"lane:{lane_opt}"] = lane_check
    for ms in milestones:
        ms_check = privacy.check_public_text(
            ms, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden,
        )
        if ms_check:
            privacy_violations[f"milestone:{ms}"] = ms_check
    for label_name, _color, label_desc in labels_to_ensure:
        lbl_check = (
            privacy.check_public_text(label_name, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden)
            + privacy.check_public_text(label_desc, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden)
        )
        if lbl_check:
            privacy_violations[f"label:{label_name}"] = lbl_check
    for wp in ordered:
        raw_title = privacy.sanitize_for_github(wp.title, project.root)
        title_issues = privacy.check_public_text(
            raw_title, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden,
        )
        body_draft = github.render_issue_body(wp, project_root=project.root, privacy_check=False)
        body_issues = privacy.check_public_text(
            body_draft, project_root=None, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden,
        )
        combined = title_issues + body_issues
        if combined:
            privacy_violations[wp.id] = combined

    active_writers = _active_ledger_writers(project, ordered, extra_runs_dirs=(dest_runs,))

    plan = {
        "applied": False,
        "repo": repo,
        "project_title": project_title,
        "project_owner": project_owner,
        "project_number": project_number,
        "source_runs_dir": str(source_runs),
        "destination_runs_dir": str(dest_runs),
        "external_ledger": options.external_ledger,
        "total_wps": len(ordered),
        "open_wps": sum(1 for wp in ordered if wp.id not in done_on_main),
        "done_wps": sum(1 for wp in ordered if wp.id in done_on_main),
        "in_flight_worktrees": sorted(worktrees),
        "uncommitted_drafts": uncommitted_drafts,
        "active_writers": active_writers,
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

    if active_writers:
        raise MigrationError(
            "cannot cut over ledger while active writers or T3 waiters are running: "
            + "; ".join(active_writers)
            + ". Stop/wrap-up active supervisors, runners, schedulers, and in-flight T3 sessions before re-running --apply."
        )

    # A39: Verify the destination ledger is not already bound to a different GitHub repository
    # BEFORE performing any remote mutation.
    bound_dest_repo = _ledger_bound_repo(dest_runs)
    if bound_dest_repo and bound_dest_repo.lower() != repo.lower():
        raise MigrationError(
            f"destination ledger {dest_runs} is already bound to repository {bound_dest_repo!r}, "
            f"refusing to mix state for {repo!r}; use an isolated checkout or clean ledger"
        )
    if dest_runs.resolve() == source_runs.resolve():
        configured_repo = str(project.github_config.get("repo") or "").strip()
        if configured_repo and configured_repo.lower() != repo.lower() and any(dest_runs.glob("GH-*/state.json")):
            raise MigrationError(
                f"destination ledger {dest_runs} contains GH-* state for {configured_repo!r}, "
                f"refusing to mix state for {repo!r}"
            )

    # ------------------------------------------------------------------ Apply migration

    # 1. Discover already-migrated issues on `repo` (paginated GraphQL) and verify every checkpointed
    # mapping & destination ledger directory BEFORE making any remote mutation (A6, A39).
    existing_legacy = _existing_legacy_issues(gh, repo)
    verified_checkpoint_map: dict[str, int] = {}
    live_bodies: dict[str, str] = {legacy: body for legacy, (_num, body) in existing_legacy.items()}
    for legacy_id, mapped_num in mig_state["map"].items():
        if legacy_id in existing_legacy:
            if existing_legacy[legacy_id][0] != mapped_num:
                raise MigrationError(
                    f"conflicting mapping for {legacy_id} in {repo}: checkpoint maps to #{mapped_num}, "
                    f"while live GitHub discovery found #{existing_legacy[legacy_id][0]}"
                )
            verified_checkpoint_map[legacy_id] = mapped_num
            continue
        viewed = gh.run("issue", "view", str(mapped_num), "-R", repo, "--json", "body", check=False)
        live_body = ""
        if viewed.returncode == 0 and viewed.stdout.strip():
            try:
                live_body = str(json.loads(viewed.stdout).get("body") or "")
            except ValueError:
                live_body = ""
        parsed_live = github.parse_issue_body(live_body)
        if parsed_live.get("legacy_wp_id") != legacy_id:
            raise MigrationError(
                f"checkpoint maps {legacy_id} -> #{mapped_num}, but issue #{mapped_num} in {repo} "
                f"does not carry legacy_wp_id={legacy_id!r}; refusing to mutate an unverified issue"
            )
        verified_checkpoint_map[legacy_id] = mapped_num
        live_bodies[legacy_id] = live_body

    id_map: dict[str, int] = {**verified_checkpoint_map}
    for legacy_id, (num, _body) in existing_legacy.items():
        if legacy_id in id_map and id_map[legacy_id] != num:
            raise MigrationError(
                f"conflicting mapping for {legacy_id} in {repo}: checkpoint #{id_map[legacy_id]} vs live #{num}"
            )
        id_map[legacy_id] = num

    # A39/A59: Verify no existing `GH-<N>/state.json` in `dest_runs` belongs to a different legacy WP
    # or carries stray non-empty unmigrated state from another workflow/repository.
    non_empty_state_keys = ("status", "progress", "pr", "pr_delivery", "approved_verify", "overrides", "handoff", "try_it", "worktree")
    if dest_runs.is_dir():
        for legacy_id, issue_num in id_map.items():
            existing_state_path = dest_runs / f"GH-{issue_num}" / run_state.STATE_FILE
            if existing_state_path.is_file():
                try:
                    existing_st = json.loads(existing_state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    existing_st = None
                if isinstance(existing_st, dict):
                    existing_leg = existing_st.get("legacy_wp_id")
                    if existing_leg and existing_leg != legacy_id:
                        raise MigrationError(
                            f"destination ledger {existing_state_path} already belongs to {existing_leg!r}, "
                            f"conflicting with {legacy_id!r} -> GH-{issue_num} for {repo!r}"
                        )
                    if not existing_leg and any(existing_st.get(key) for key in non_empty_state_keys):
                        raise MigrationError(
                            f"destination ledger {existing_state_path} contains existing unmigrated state without a matching "
                            f"legacy_wp_id for {legacy_id!r} -> GH-{issue_num}; refusing to merge stray state"
                        )

    # 2. Check repo auto-merge setting (optionally enable with typed JSON boolean `-F`).
    repo_info = gh.json("api", f"repos/{repo}")
    allow_auto_merge = bool(repo_info.get("allow_auto_merge")) if isinstance(repo_info, dict) else False
    if options.enable_auto_merge and not allow_auto_merge:
        gh.run("api", "-X", "PATCH", f"repos/{repo}", "-F", "allow_auto_merge=true")
        allow_auto_merge = True

    written_bodies: dict[str, str] = dict(mig_state.get("written_bodies") or {})
    rewritten_refs: set[str] = set(mig_state.get("rewritten_refs") or ())
    deps_wired: set[str] = set(mig_state.get("deps_wired") or ())
    closed_merged: set[str] = set(mig_state.get("closed_merged") or ())
    project_items_added: set[str] = set(mig_state.get("project_items_added") or ())
    board_synced: set[str] = set(mig_state.get("board_synced") or ())
    # A8 upgrade path: for checkpoints older than v3 (e.g. `6a9ffa6`) or when `migration-state.json`
    # was deleted after a completed migration, seed Pass-2 completion sets from verified `GH-N/state.json`
    # records whose `legacy_wp_id == wp.id` (Pass 3 only ever ran after Pass 2 finished).
    if int(mig_state.get("loaded_version") or 0) < 3:
        for wp in ordered:
            mapped_num = id_map.get(wp.id)
            if mapped_num is None:
                continue
            for candidate_root in (dest_runs, source_runs):
                st_path = candidate_root / f"GH-{mapped_num}" / run_state.STATE_FILE
                if not st_path.is_file():
                    continue
                try:
                    st_data = json.loads(st_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(st_data, dict) and st_data.get("legacy_wp_id") == wp.id:
                    deps_wired.add(wp.id)
                    closed_merged.add(wp.id)
                    break
    state_file = dest_runs / MIGRATION_STATE_FILE

    def _checkpoint() -> None:
        mig_state.update(
            version=3,
            repo=repo,
            project_number=project_number,
            project_owner=project_owner,
            project_title=project_title,
            map=dict(id_map),
            written_bodies=dict(written_bodies),
            rewritten_refs=sorted(rewritten_refs),
            deps_wired=sorted(deps_wired),
            closed_merged=sorted(closed_merged),
            project_items_added=sorted(project_items_added),
            board_synced=sorted(board_synced),
        )
        persisted = {k: v for k, v in mig_state.items() if k != "loaded_version"}
        _save_migration_state(state_file, persisted)
        _write_ledger_meta(dest_runs, repo)

    # 3. Ensure milestones, labels, and Project v2 using the exact canonical validated values.
    created_milestones = _ensure_milestones(gh, repo, milestones)
    _ensure_labels(gh, repo, labels_to_ensure)
    project_number, project_url, project_owner = _ensure_project(
        gh, repo, project_owner, project_title, project_number, project_lane_options, options.create_project,
    )
    _checkpoint()

    # 4. Pass 1: Create missing GitHub issues in topological order (checkpointing after every issue),
    # and ensure Project v2 membership for both newly created and reused issues.
    created_issues: dict[str, int] = {}
    for wp in ordered:
        if wp.id not in id_map:
            title = privacy.require_public_safe(
                wp.title, project.root, enabled=True, allow_local_paths=options.allow_local_paths, extra_forbidden=extra_forbidden,
            )
            body = github.render_issue_body(
                wp,
                id_map=id_map,
                project_root=project.root,
                privacy_check=True,
                allow_local_paths=options.allow_local_paths,
                migrated_merged=(wp.id in done_on_main),
                extra_forbidden=extra_forbidden,
            )
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".md", delete=False) as handle:
                handle.write(body)
                body_file = Path(handle.name)
            try:
                args = ["issue", "create", "-R", repo, "--title", title, "--body-file", str(body_file)]
                if wp.milestone not in PLACEHOLDER_MILESTONES:
                    args += ["--milestone", milestone_map[wp.milestone]]
                if wp.lane:
                    args += ["--label", lane_label_map[wp.lane]]
                if wp.status == "blocked" and blocked_label:
                    args += ["--label", blocked_label]
                output = gh.run(*args).stdout.strip()
            finally:
                body_file.unlink(missing_ok=True)
            issue_number = _issue_number_from_output(output)
            id_map[wp.id] = issue_number
            created_issues[wp.id] = issue_number
            live_bodies[wp.id] = body
            written_bodies[wp.id] = github.issue_body_sha256(body.strip())
            _checkpoint()

        if project_number is not None and wp.id not in project_items_added:
            issue_url = f"https://github.com/{repo}/issues/{id_map[wp.id]}"
            try:
                gh.run("project", "item-add", str(project_number), "--owner", project_owner, "--url", issue_url)
            except github.GhError as error:
                raise MigrationError(f"failed adding {wp.id} (#{id_map[wp.id]}) to project #{project_number}: {error}") from error
            project_items_added.add(wp.id)
            _checkpoint()

    # 5. Pass 2: Revalidate live issue body immediately before any unfinished body rewrite (A8),
    # wire native blocked-by dependencies once (checkpointed in `deps_wired`), and close WPs
    # already merged on main once (checkpointed in `closed_merged`).
    dependencies_wired = 0
    closed_issues: list[int] = []
    for wp in ordered:
        issue_number = id_map[wp.id]
        if wp.id not in rewritten_refs:
            # Always fetch the live GitHub body right before deciding whether to edit, so concurrent
            # human edits made after discovery can never be clobbered.
            viewed = gh.run("issue", "view", str(issue_number), "-R", repo, "--json", "body", check=False)
            if viewed.returncode != 0 or not viewed.stdout.strip():
                raise MigrationError(f"could not inspect live body of #{issue_number} in {repo} before unfinished rewrite")
            try:
                current_body = str(json.loads(viewed.stdout).get("body") or "")
            except ValueError as error:
                raise MigrationError(f"malformed issue view response for #{issue_number} in {repo}") from error
            live_bodies[wp.id] = current_body

            final_body = github.render_issue_body(
                wp,
                id_map=id_map,
                project_root=project.root,
                privacy_check=True,
                allow_local_paths=options.allow_local_paths,
                migrated_merged=(wp.id in done_on_main),
                extra_forbidden=extra_forbidden,
            )
            current_hash = github.issue_body_sha256(current_body.strip())
            expected_hash = written_bodies.get(wp.id)
            updated_body: str | None = None
            if final_body.replace("\r\n", "\n").strip() == current_body.replace("\r\n", "\n").strip():
                written_bodies[wp.id] = current_hash
                rewritten_refs.add(wp.id)
                _checkpoint()
            elif expected_hash is not None:
                # Only update when the live GitHub body still matches our unfinished Pass-1 write.
                if current_hash == expected_hash:
                    updated_body = final_body
                else:
                    # Human already edited this issue on GitHub; preserve their authoritative text.
                    rewritten_refs.add(wp.id)
                    _checkpoint()
            else:
                # Resumed without a local written_bodies checkpoint: only rewrite unmapped WP-XXXX
                # references inside the live body so human edits on GitHub are never clobbered.
                rewritten_live = github.rewrite_wp_references(current_body, id_map)
                if rewritten_live != current_body:
                    updated_body = privacy.require_public_safe(
                        rewritten_live,
                        project.root,
                        enabled=True,
                        allow_local_paths=options.allow_local_paths,
                        extra_forbidden=extra_forbidden,
                    )
                else:
                    rewritten_refs.add(wp.id)
                    _checkpoint()

            if updated_body is not None:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".md", delete=False) as handle:
                    handle.write(updated_body)
                    body_file = Path(handle.name)
                try:
                    gh.run("issue", "edit", str(issue_number), "-R", repo, "--body-file", str(body_file))
                    live_bodies[wp.id] = updated_body
                    written_bodies[wp.id] = github.issue_body_sha256(updated_body.strip())
                    rewritten_refs.add(wp.id)
                    _checkpoint()
                finally:
                    body_file.unlink(missing_ok=True)

        if wp.id not in deps_wired:
            dep_numbers = [str(id_map[dep]) for dep in wp.depends_on if dep in id_map and dep != wp.id]
            if dep_numbers:
                gh.run("issue", "edit", str(issue_number), "-R", repo, "--add-blocked-by", ",".join(dep_numbers))
                dependencies_wired += len(dep_numbers)
            deps_wired.add(wp.id)
            _checkpoint()

        if wp.id in done_on_main and wp.id not in closed_merged:
            gh.run("issue", "close", str(issue_number), "-R", repo, "--reason", "completed")
            closed_issues.append(issue_number)
            closed_merged.add(wp.id)
            _checkpoint()

    # 6. Pass 3 & Pass 4: Hold the project ledger cutover mutex across a fresh active-writer
    # verification, private ledger migration, and `.orchestration/config.json` switch (A11).
    global_files_copied = 0
    migrated_ledgers: list[str] = []
    migrated_statuses: dict[str, str] = {}
    with NamedMutex(project.mutex_name("ledger", "cutover")):
        active_now = _active_ledger_writers(project, ordered, extra_runs_dirs=(dest_runs,))
        if active_now:
            raise MigrationError(
                "cannot cut over ledger while active writers or T3 waiters are running: "
                + "; ".join(active_now)
                + ". Stop/wrap-up active supervisors, runners, schedulers, and in-flight T3 sessions before re-running --apply."
            )

        dest_runs.mkdir(parents=True, exist_ok=True)
        _write_ledger_meta(dest_runs, repo)
        same_runs_dir = dest_runs.resolve() == source_runs.resolve()
        if not same_runs_dir:
            for name in GLOBAL_RUN_ENTRIES:
                global_files_copied += _copy_missing(source_runs / name, dest_runs / name)
        _rewrite_t3_state(dest_runs, source_runs, id_map)
        _rewrite_continuous_state(dest_runs, source_runs, id_map)

        for wp in ordered:
            issue_number = id_map[wp.id]
            gh_id = f"GH-{issue_number}"
            old_run_dir = source_runs / wp.id
            new_run_dir = dest_runs / gh_id
            new_run_dir.mkdir(parents=True, exist_ok=True)
            if old_run_dir.is_dir() and old_run_dir.resolve() != new_run_dir.resolve():
                if same_runs_dir:
                    _move_dir_contents(old_run_dir, new_run_dir)
                else:
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

            # A1: Auto-approve Verify commands ONLY when the live GitHub issue revision matches the
            # exact trusted rendering of the committed WP on `main`.
            live_issue_body = live_bodies.get(wp.id) or ""
            committed_wp = main_by_id.get(wp.id)
            expected_trusted_body = (
                github.render_issue_body(
                    committed_wp,
                    id_map=id_map,
                    project_root=project.root,
                    privacy_check=False,
                    migrated_merged=(wp.id in done_on_main),
                )
                if committed_wp is not None
                else None
            )
            trusted_live_match = (
                expected_trusted_body is not None
                and live_issue_body.replace("\r\n", "\n").strip() == expected_trusted_body.replace("\r\n", "\n").strip()
            )
            if trusted_live_match:
                expected_cmds = github.extract_issue_verify_commands(github.parse_issue_body(expected_trusted_body))
                live_cmds = github.extract_issue_verify_commands(github.parse_issue_body(live_issue_body))
                migrated_verify_cmds = expected_cmds if live_cmds == expected_cmds else []
                migrated_body_sha = github.issue_body_sha256(live_issue_body) if migrated_verify_cmds else ""
            else:
                migrated_verify_cmds = []
                migrated_body_sha = ""

            def _populate_local_state(
                state: dict,
                _wp=wp,
                _new_dir=new_run_dir,
                _body_sha=migrated_body_sha,
                _verify_cmds=migrated_verify_cmds,
                _repo=repo,
            ) -> None:
                # A9: Recognize both the new `migrated_from_wp` marker and pre-hardening `6a9ffa6`
                # ledgers that already carry a verified `legacy_wp_id` binding for this WP.
                if state.get("migrated_from_wp") or state.get("legacy_wp_id") == _wp.id:
                    state["migrated_from_wp"] = True
                    if not state.get("repo"):
                        state["repo"] = _repo
                    return
                state["status"] = state.get("status") or ("done" if _wp.id in done_on_main else _wp.status)
                state["progress"] = state.get("progress") or _extract_progress_entries(_wp)
                state["steps_done"] = state.get("steps_done") or _extract_done_steps(_wp)
                state["try_it"] = state.get("try_it") or _extract_try_it(_wp)
                state["handoff"] = state.get("handoff") or "\n".join(_wp.section("Handoff")).strip()
                state["reviews"] = state.get("reviews") or _extract_section_blocks(_wp.section("Review"))
                state["reevaluations"] = state.get("reevaluations") or _extract_section_blocks(_wp.section("Re-evaluation"))
                state["overrides"] = {**_extract_overrides(project, _wp), **(state.get("overrides") or {})}
                state["legacy_wp_id"] = state.get("legacy_wp_id") or _wp.id
                state["repo"] = _repo
                state["migrated_merged_on_main"] = bool(state.get("migrated_merged_on_main") or (_wp.id in done_on_main))
                if _verify_cmds and _body_sha and state.get("approved_verify") is None:
                    state["approved_verify"] = {
                        "body_sha256": _body_sha,
                        "commands": list(_verify_cmds),
                    }
                if _wp.id in worktrees and not state.get("worktree"):
                    wt = worktrees[_wp.id]
                    state["worktree"] = str(wt)
                    state["worktree_owner"] = "t3" if (_new_dir / "t3-workspace.json").is_file() else "standalone"
                    branch_res = git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False)
                    if branch_res.returncode == 0 and branch_res.stdout.strip() and not state.get("branch"):
                        state["branch"] = branch_res.stdout.strip()
                state["migrated_from_wp"] = True

            saved_state = run_state.update(new_run_dir, _populate_local_state)
            migrated_ledgers.append(gh_id)
            migrated_statuses[gh_id] = str(saved_state["status"])

        _migrate_usage_index(dest_runs, id_map)

        # 7. Pass 4: Update `.orchestration/config.json` atomically while holding the cutover lock.
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["tracker"] = "github"
        raw_config["delivery"] = "pr-automerge"
        raw_config["runs_dir"] = target_runs_config
        gh_section = dict(raw_config.get("github") or {})
        gh_section["repo"] = repo
        if project_number is not None:
            gh_section["project_number"] = project_number
            gh_section["project_owner"] = project_owner
        raw_config["github"] = gh_section
        staging_cfg = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.tmp")
        staging_cfg.write_text(json.dumps(raw_config, indent=2) + "\n", encoding="utf-8")
        replace_file(staging_cfg, config_path)

    # 8. Optional removal of migrated WP files — compare each working-copy candidate against its
    # imported committed blob on `main` and retain modified working-copy files (A42).
    removed_files: list[str] = []
    retained_modified_files: list[str] = []
    if options.remove_wp_files:
        for wp in ordered:
            target = project.wp_dir() / wp.file_name
            if not target.is_file():
                continue
            committed_text = main_raw_blobs.get(wp.id)
            try:
                working_text = target.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                working_text = None
            if (
                committed_text is None
                or working_text is None
                or working_text.replace("\r\n", "\n") != committed_text.replace("\r\n", "\n")
            ):
                retained_modified_files.append(f"{project.wp_dir_relative}/{wp.file_name}")
                continue
            target.unlink()
            removed_files.append(f"{project.wp_dir_relative}/{wp.file_name}")

    # 9. Sync cache + push open Project v2 board statuses using the newly configured project.
    # Require confirmed strict provisioning (`strict=True`) before checkpointing `board_synced` (A28).
    migrated_context = Context.load(project.root, context.machine, gh_client=gh)
    if migrated_context.tracker is not None:
        try:
            migrated_context.tracker.sync(force=True)
            for wp in ordered:
                gh_id = f"GH-{id_map[wp.id]}"
                status_to_sync = migrated_statuses.get(gh_id, wp.status)
                if wp.id in done_on_main or status_to_sync == "todo" or wp.id in board_synced:
                    continue
                migrated_context.tracker.sync_issue_status(gh_id, status_to_sync, strict=True)
                board_synced.add(wp.id)
                _checkpoint()
            migrated_context.tracker.sync(force=True)
        except github.GhError as error:
            raise MigrationError(
                f"incomplete GitHub board/cache provisioning (re-run `wpo migrate github --apply` to resume): {error}"
            ) from error

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
        "retained_modified_files": retained_modified_files,
        "config_updated": str(config_path),
    }
