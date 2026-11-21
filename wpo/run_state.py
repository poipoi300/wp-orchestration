"""Private local run state per work item, separated from git-tracked files and GitHub issues.

In GitHub-backed projects (split state), GitHub owns the shared plan (issue body, dependencies,
milestones, board, PRs) while `<runs>/<id>/state.json` owns mutable inner-loop state: execution
status, progress entries, ticked steps, Try it text, handoffs, review and re-evaluation notes,
local routing/scope overrides, and linked PR metadata. Transcripts, prompts, findings and usage
indexes also live only in the local ledger.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from .platform import FileLock, NamedMutex, pid_alive, replace_file

STATE_FILE = "state.json"
WRITERS_DIR = ".writers"
OVERRIDE_KEYS = ("builder", "reviewer", "owns", "lane", "delegate", "mode", "skills", "verify")
RAW_TRANSCRIPT_SUFFIXES = (".log", ".prompt.md", ".system.md", ".stderr", ".inbox.log", ".resumed.log")
PRESERVED_RUN_FILES = frozenset({
    STATE_FILE,
    "attempts.json",
    "findings.json",
    "t3-workspace.json",
    "routing-mode.json",
    "session-verified-tree.txt",
    "verified-tree.txt",
    "handoff-submitted.md",
    "no-route.json",
    "route-wait",
})


class StateError(RuntimeError):
    """A private local `state.json` file could not be read or parsed."""


def fresh_state() -> dict:
    return {
        "version": 1,
        "status": None,
        "steps_done": [],
        "progress": [],
        "try_it": "",
        "handoff": "",
        "reviews": [],
        "reevaluations": [],
        "context_notes": [],
        "overrides": {},
        "approved_verify": None,
        "pr": None,
        "pr_delivery": None,
        "legacy_wp_id": None,
        "migrated_from_wp": False,
        "migrated_merged_on_main": False,
        "local_ff_merged": False,
        "local_ff_commit": None,
        "pending_issue_close": False,
        "worktree": None,
        "worktree_owner": None,
        "branch": None,
        "updated_at": None,
    }


def state_path(run_dir: Path) -> Path:
    return run_dir / STATE_FILE


def load(run_dir: Path) -> dict:
    path = state_path(run_dir)
    if not path.is_file():
        return fresh_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StateError(f"{path}: corrupt local run state ({error}); fix or remove it before continuing") from error
    if not isinstance(raw, dict):
        raise StateError(f"{path}: corrupt local run state (expected JSON object); fix or remove it before continuing")
    merged = {**fresh_state(), **raw}
    if not isinstance(merged["overrides"], dict):
        merged["overrides"] = {}
    return merged


def save(run_dir: Path, state: dict) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(run_dir)
    payload = {**fresh_state(), **state, "updated_at": time.time()}
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    replace_file(staging, path)
    return payload


def update(run_dir: Path, mutate) -> dict:
    """Atomically reads, mutates in place, and persists `state.json` under a per-item lock."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(run_dir / "state.lock"):
        state = load(run_dir)
        mutate(state)
        return save(run_dir, state)


def set_status(run_dir: Path, status: str) -> dict:
    return update(run_dir, lambda state: state.__setitem__("status", status))


def append_progress(run_dir: Path, message: str, actor: str = "runner", at: str | None = None) -> dict:
    entry = {"at": at or datetime.now().strftime("%Y-%m-%d %H:%M"), "actor": actor, "text": message.strip()}

    def _add(state: dict) -> None:
        state["progress"] = [*state.get("progress", []), entry]

    update(run_dir, _add)
    return entry


def tick_step(run_dir: Path, number: int) -> list[int]:
    if number < 1:
        raise ValueError(f"step number must be >= 1, got {number}")

    def _tick(state: dict) -> None:
        done = sorted({int(item) for item in state.get("steps_done", []) if isinstance(item, int)} | {number})
        state["steps_done"] = done

    return list(update(run_dir, _tick)["steps_done"])


def set_try_it(run_dir: Path, text: str) -> dict:
    return update(run_dir, lambda state: state.__setitem__("try_it", text.strip()))


def set_handoff(run_dir: Path, text: str) -> dict:
    return update(run_dir, lambda state: state.__setitem__("handoff", text.strip()))


def append_review(run_dir: Path, header: str, body: str, meta: dict | None = None) -> dict:
    entry = {"header": header, "body": body.strip(), "at": datetime.now().strftime("%Y-%m-%d %H:%M"), **(meta or {})}

    def _add(state: dict) -> None:
        state["reviews"] = [*state.get("reviews", []), entry]

    return update(run_dir, _add)


def append_reevaluation(run_dir: Path, header: str, body: str, meta: dict | None = None) -> dict:
    entry = {"header": header, "body": body.strip(), "at": datetime.now().strftime("%Y-%m-%d %H:%M"), **(meta or {})}

    def _add(state: dict) -> None:
        state["reevaluations"] = [*state.get("reevaluations", []), entry]

    return update(run_dir, _add)


def append_context_note(run_dir: Path, text: str, kind: str = "user", at: str | None = None) -> dict:
    entry = {"at": at or datetime.now().strftime("%Y-%m-%d %H:%M"), "kind": kind, "text": text.strip()}

    def _add(state: dict) -> None:
        state["context_notes"] = [*state.get("context_notes", []), entry]

    return update(run_dir, _add)


def set_override(run_dir: Path, key: str, value: object) -> dict:
    if key not in OVERRIDE_KEYS:
        raise ValueError(f"unknown override '{key}' (choose from {', '.join(OVERRIDE_KEYS)})")

    def _set(state: dict) -> None:
        overrides = dict(state.get("overrides") or {})
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
        state["overrides"] = overrides

    return update(run_dir, _set)


def set_pr(run_dir: Path, pr: dict | None) -> dict:
    def _set(state: dict) -> None:
        if pr is None:
            state["pr"] = None
        else:
            current = dict(state.get("pr") or {})
            current.update(pr)
            state["pr"] = current

    return update(run_dir, _set)


def set_pr_delivery(run_dir: Path, delivery: dict | None) -> dict:
    """Stores WPO's immutable PR delivery provenance separately from observed live PR state."""
    def _set(state: dict) -> None:
        state["pr_delivery"] = dict(delivery) if delivery is not None else None

    return update(run_dir, _set)


def set_approved_verify(run_dir: Path, body_sha256: str | None, commands: Iterable[str] = ()) -> dict:
    """Binds local authorization of GitHub issue Verify commands to a specific reviewed issue body revision."""
    def _set(state: dict) -> None:
        if body_sha256 is None:
            state["approved_verify"] = None
        else:
            state["approved_verify"] = {
                "body_sha256": str(body_sha256),
                "commands": [str(cmd).strip() for cmd in commands if str(cmd).strip()],
                "approved_at": time.time(),
            }

    return update(run_dir, _set)


def set_local_ff_merged(run_dir: Path, commit: str, *, pending_issue_close: bool = True) -> dict:
    """Records verified local-ff merge completion on `main` for a GitHub-tracked item."""
    def _set(state: dict) -> None:
        state["status"] = "done"
        state["local_ff_merged"] = True
        state["local_ff_commit"] = str(commit).strip()
        state["pending_issue_close"] = bool(pending_issue_close)

    return update(run_dir, _set)


def clear_pending_issue_close(run_dir: Path) -> dict:
    return update(run_dir, lambda state: state.__setitem__("pending_issue_close", False))


@contextlib.contextmanager
def ledger_writer(project, kind: str, key: str) -> Iterator[Path | None]:
    """Registers an active ledger writer lease for its full lifetime, synchronized with the
    project's ledger cutover mutex so migration cannot cut over mid-run or race new writers."""
    if not hasattr(project, "mutex_name") or not hasattr(project, "runs_dir"):
        yield None
        return
    safe_key = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(key))[:48] or "item"
    cutover = NamedMutex(project.mutex_name("ledger", "cutover"))
    lease_path: Path | None = None
    with cutover:
        # A11/A55: Reject writers whose in-memory Project predates a migration cutover of tracker
        # or resolved runs_dir (even when runs_dir was previously None / implicit default).
        root = getattr(project, "root", None)
        if isinstance(root, Path):
            cfg_path = root / ".orchestration" / "config.json"
            if cfg_path.is_file():
                try:
                    from .project import Project, ProjectError
                    reloaded = Project.locate(
                        root,
                        getattr(project, "machine_defaults", None),
                        getattr(project, "execution_mode", "standalone"),
                        data_dir=getattr(project, "data_dir", None),
                    )
                except (OSError, ValueError, ProjectError):
                    reloaded = None
                if reloaded is not None and (
                    reloaded.tracker != getattr(project, "tracker", reloaded.tracker)
                    or reloaded._resolve_runs_dir().resolve() != project._resolve_runs_dir().resolve()
                ):
                    raise StateError(
                        "project ledger or tracker configuration changed during migration cutover; reload before continuing"
                    )
        writers_dir = project.runs_dir / WRITERS_DIR
        writers_dir.mkdir(parents=True, exist_ok=True)
        lease_path = writers_dir / f"{kind}-{safe_key}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        payload = {"kind": kind, "key": str(key), "pid": os.getpid(), "started_at": time.time()}
        staging = lease_path.with_name(f".{lease_path.name}.tmp")
        staging.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        replace_file(staging, lease_path)
    try:
        yield lease_path
    finally:
        if lease_path is not None:
            lease_path.unlink(missing_ok=True)


def active_writer_leases(runs_dir: Path) -> list[str]:
    """Returns descriptions of all live ledger writer leases under `<runs>/.writers/`."""
    writers_dir = runs_dir / WRITERS_DIR
    if not writers_dir.is_dir():
        return []
    active: list[str] = []
    for lease_file in sorted(writers_dir.glob("*.json")):
        try:
            data = json.loads(lease_file.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid_alive(pid):
            active.append(f"active {data.get('kind', 'writer')} for {data.get('key', lease_file.stem)} (pid {pid})")
    return active


def set_workspace_binding(run_dir: Path, worktree: Path | str | None, branch: str | None, *, owner: str | None = None) -> dict:
    def _set(state: dict) -> None:
        if worktree is not None:
            state["worktree"] = str(worktree)
        if branch is not None:
            state["branch"] = branch
        if owner is not None:
            state["worktree_owner"] = owner

    return update(run_dir, _set)


def latest_progress_line(state: dict) -> str:
    entries = state.get("progress") or []
    if not entries:
        return ""
    last = entries[-1]
    actor = f"[{last['actor']}] " if last.get("actor") else ""
    stamp = f"{last['at']} " if last.get("at") else ""
    return f"{stamp}{actor}{last.get('text', '')}".strip()


def format_progress_lines(state: dict) -> list[str]:
    lines = []
    for entry in state.get("progress") or []:
        actor = f"[{entry['actor']}] " if entry.get("actor") else ""
        stamp = f"{entry['at']} " if entry.get("at") else ""
        lines.append(f"- {stamp}{actor}{entry.get('text', '')}".strip())
    return lines


def format_context_note_lines(state: dict) -> list[str]:
    lines = []
    for entry in state.get("context_notes") or []:
        label = "User note" if entry.get("kind") == "user" else "Re-evaluation note" if entry.get("kind") == "reevaluation" else "Note"
        parts = str(entry.get("text", "")).strip().splitlines()
        if not parts:
            continue
        stamp = entry.get("at") or ""
        prefix = f"- {stamp} {label}: " if stamp else f"- {label}: "
        lines.append(prefix + parts[0].rstrip())
        lines.extend(f"  {line.rstrip()}" if line.strip() else "" for line in parts[1:])
    return lines


def _is_raw_transcript(path: Path) -> bool:
    name = path.name
    if name in PRESERVED_RUN_FILES:
        return False
    return any(name.endswith(suffix) for suffix in RAW_TRANSCRIPT_SUFFIXES)


def ledger_summary(project, wp_id: str | None = None) -> dict:
    """Summarizes the local private ledger: location, transcript sizes, usage index and item count."""
    runs = project.runs_dir
    external = not runs.resolve().is_relative_to(project.root.resolve())
    targets = [runs / wp_id] if wp_id else [path for path in sorted(runs.iterdir()) if path.is_dir() and path.name.startswith(("WP-", "GH-"))] if runs.exists() else []
    transcript_files, transcript_bytes = 0, 0
    items = []
    for directory in targets:
        if not directory.is_dir():
            continue
        item_files, item_bytes = 0, 0
        for root, _dirs, files in os.walk(directory):
            for name in files:
                file_path = Path(root) / name
                if _is_raw_transcript(file_path) or file_path.parent.name == "view":
                    try:
                        size = file_path.stat().st_size
                    except OSError:
                        continue
                    item_files += 1
                    item_bytes += size
        transcript_files += item_files
        transcript_bytes += item_bytes
        st = load(directory)
        items.append({
            "id": directory.name,
            "status": st.get("status"),
            "legacy_wp_id": st.get("legacy_wp_id"),
            "overrides": st.get("overrides") or {},
            "pr": st.get("pr"),
            "transcript_files": item_files,
            "transcript_bytes": item_bytes,
        })
    usage_index = runs / "usage" / "index.json"
    indexed_sessions = 0
    if usage_index.is_file():
        try:
            indexed_sessions = len(json.loads(usage_index.read_text(encoding="utf-8")).get("logs", {}))
        except (OSError, ValueError):
            indexed_sessions = 0
    return {
        "runs_dir": str(runs),
        "external": external,
        "tracker": project.config.get("tracker", "wp-files"),
        "delivery": project.delivery_mode,
        "items": items,
        "transcript_files": transcript_files,
        "transcript_bytes": transcript_bytes,
        "indexed_sessions": indexed_sessions,
    }


def prune_transcripts(context, older_than_days: float, *, apply: bool = False, include_open: bool = False) -> dict:
    """Indexes all session logs into `usage/index.json` first, then plans or removes raw session
    transcripts and rendered HTML views older than `older_than_days` for settled work items.
    Structured ledgers (`state.json`, `attempts.json`, `findings.json`, `usage/index.json`) stay."""
    from .usage import INDEX_LOCK_FILE, load_sessions

    # Ensure aggregate token/time telemetry is captured before any raw transcript can be deleted.
    load_sessions(context.project)

    cutoff = time.time() - (timedelta(days=older_than_days).total_seconds())
    merged = context.merged_done_ids()
    runs = context.project.runs_dir
    candidates: list[dict] = []
    skipped_open: list[str] = []
    if not runs.exists():
        return {"applied": apply, "older_than_days": older_than_days, "candidates": [], "removed_files": 0, "removed_bytes": 0, "skipped_open": []}

    for item_dir in sorted(path for path in runs.iterdir() if path.is_dir() and path.name.startswith(("WP-", "GH-"))):
        # Require positive verified merge evidence before pruning unless `--include-open` is passed.
        if not include_open and item_dir.name not in merged:
            skipped_open.append(item_dir.name)
            continue
        for root, dirs, files in os.walk(item_dir):
            root_path = Path(root)
            if root_path.name == "view":
                for name in files:
                    file_path = root_path / name
                    try:
                        stat_result = file_path.stat()
                    except OSError:
                        continue
                    if stat_result.st_mtime < cutoff:
                        candidates.append({"path": str(file_path), "bytes": stat_result.st_size, "wp": item_dir.name})
                dirs.clear()
                continue
            for name in files:
                file_path = root_path / name
                if not _is_raw_transcript(file_path):
                    continue
                try:
                    stat_result = file_path.stat()
                except OSError:
                    continue
                if stat_result.st_mtime < cutoff:
                    candidates.append({"path": str(file_path), "bytes": stat_result.st_size, "wp": item_dir.name})

    removed_files, removed_bytes = 0, 0
    skipped_changed: list[str] = []
    if apply:
        from .usage import SESSION_LOG
        index_path = runs / "usage" / "index.json"
        lock_path = runs / "usage" / INDEX_LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Hold the same lock as `usage.load_sessions`, revalidate each session log's current
        # (size, mtime) against `usage/index.json` so appended/unindexed logs are never lost,
        # durably mark indexed candidates pruned BEFORE unlinking, then delete.
        with FileLock(lock_path):
            index = None
            if index_path.is_file():
                try:
                    loaded = json.loads(index_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("logs"), dict):
                        index = loaded
                except (OSError, ValueError):
                    index = None

            validated: list[tuple[dict, Path, str, bool]] = []
            for entry in candidates:
                path = Path(entry["path"])
                try:
                    rel = path.relative_to(runs).as_posix()
                    st = path.stat()
                except (ValueError, OSError):
                    continue
                is_session_log = (
                    path.parent.name != "view"
                    and bool(SESSION_LOG.match(path.name))
                    and SESSION_LOG.match(path.name).group("stem") not in ("worker-process", "integrate", "orchestrator-verify")
                )
                if is_session_log:
                    cached = index["logs"].get(rel) if index is not None else None
                    if (
                        not isinstance(cached, dict)
                        or cached.get("size") != st.st_size
                        or cached.get("mtime") != st.st_mtime
                    ):
                        skipped_changed.append(rel)
                        continue
                validated.append((entry, path, rel, is_session_log))

            if validated and index is not None:
                changed = False
                for _entry, _path, rel, _is_sess in validated:
                    if rel in index["logs"] and isinstance(index["logs"][rel], dict) and not index["logs"][rel].get("pruned"):
                        index["logs"][rel]["pruned"] = True
                        changed = True
                if changed:
                    staging = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
                    staging.write_text(json.dumps(index), encoding="utf-8")
                    replace_file(staging, index_path)

            for entry, path, _rel, _is_sess in validated:
                try:
                    path.unlink(missing_ok=True)
                    removed_files += 1
                    removed_bytes += entry["bytes"]
                except OSError:
                    continue
        # Remove now-empty rendered view directories.
        for item_dir in sorted(path for path in runs.iterdir() if path.is_dir()):
            view_dir = item_dir / "view"
            if view_dir.is_dir() and not any(view_dir.iterdir()):
                shutil.rmtree(view_dir, ignore_errors=True)
        context.journal.emit(
            "ledger-pruned",
            f"pruned {removed_files} raw transcript file(s) ({removed_bytes:,} bytes) older than {older_than_days:g} days; usage index preserved",
        )
    return {
        "applied": apply,
        "older_than_days": older_than_days,
        "candidates": candidates,
        "candidate_files": len(candidates),
        "candidate_bytes": sum(entry["bytes"] for entry in candidates),
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "skipped_open": skipped_open,
        "skipped_changed": skipped_changed,
    }


def apply_step_ticks(lines: Iterable[str], steps_done: Iterable[int]) -> list[str]:
    """Overlays locally ticked 1-based step numbers onto markdown checkbox lines."""
    done_set = {int(number) for number in steps_done}
    if not done_set:
        return list(lines)
    import re
    box_re = re.compile(r"^(\s*[-*] )\[[ xX]\](.*)$")
    result: list[str] = []
    step_index = 0
    for line in lines:
        match = box_re.match(line)
        if match:
            step_index += 1
            if step_index in done_set:
                result.append(f"{match.group(1)}[x]{match.group(2)}")
                continue
        result.append(line)
    return result
