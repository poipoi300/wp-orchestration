"""Private local run state per work item, separated from git-tracked files and GitHub issues.

In GitHub-backed projects (split state), GitHub owns the shared plan (issue body, dependencies,
milestones, board, PRs) while `<runs>/<id>/state.json` owns mutable inner-loop state: execution
status, progress entries, ticked steps, Try it text, handoffs, review and re-evaluation notes,
local routing/scope overrides, and linked PR metadata. Transcripts, prompts, findings and usage
indexes also live only in the local ledger.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from .platform import FileLock, replace_file

STATE_FILE = "state.json"
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
        "pr": None,
        "legacy_wp_id": None,
        "migrated_merged_on_main": False,
        "worktree": None,
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


def set_workspace_binding(run_dir: Path, worktree: Path | str | None, branch: str | None) -> dict:
    def _set(state: dict) -> None:
        if worktree is not None:
            state["worktree"] = str(worktree)
        if branch is not None:
            state["branch"] = branch

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
    open_ids = {
        wp.id for wp in context.effective_wps()
        if wp.id not in merged and wp.status != "done" and wp.github_state != "CLOSED"
    }
    runs = context.project.runs_dir
    candidates: list[dict] = []
    skipped_open: list[str] = []
    if not runs.exists():
        return {"applied": apply, "older_than_days": older_than_days, "candidates": [], "removed_files": 0, "removed_bytes": 0, "skipped_open": []}

    for item_dir in sorted(path for path in runs.iterdir() if path.is_dir() and path.name.startswith(("WP-", "GH-"))):
        if not include_open and item_dir.name in open_ids:
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
    if apply:
        index_path = runs / "usage" / "index.json"
        lock_path = runs / "usage" / INDEX_LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Hold the same lock as `usage.load_sessions` across unlinking raw transcripts and marking
        # them pruned so concurrent usage reports never drop aggregate metrics for pruned files.
        with FileLock(lock_path):
            pruned_keys: set[str] = set()
            for entry in candidates:
                path = Path(entry["path"])
                try:
                    rel = path.relative_to(runs).as_posix()
                    path.unlink(missing_ok=True)
                    pruned_keys.add(rel)
                    removed_files += 1
                    removed_bytes += entry["bytes"]
                except OSError:
                    continue
            if pruned_keys and index_path.is_file():
                try:
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    index = None
                if isinstance(index, dict) and isinstance(index.get("logs"), dict):
                    changed = False
                    for key in pruned_keys:
                        if key in index["logs"] and isinstance(index["logs"][key], dict):
                            index["logs"][key]["pruned"] = True
                            changed = True
                    if changed:
                        staging = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
                        staging.write_text(json.dumps(index), encoding="utf-8")
                        replace_file(staging, index_path)
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
