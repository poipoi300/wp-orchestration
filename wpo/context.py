"""The objects every command needs: the project, the machine settings, the router, the journal and
the active task tracker (`wp-files` or `github` with private local split state)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import privacy, run_state, wpfile
from .boot import ensure_boot
from .events import Journal
from .machine import MachineConfig
from .project import Project, git
from .routing import Router

UNCHECKED_STEP_RE = re.compile(r"^(\s*[-*] )\[ \]")
STEP_CHECKBOX_RE = re.compile(r"^\s*[-*] \[[ xX]\]")


@dataclass
class Context:
    project: Project
    machine: MachineConfig
    router: Router
    journal: Journal
    tracker: object | None = None
    # WP files or tracker sources that failed to parse in the last listing: path -> error. Callers report them.
    parse_errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, start: Path | None = None, machine: MachineConfig | None = None, *, gh_client=None) -> Context:
        machine = machine or MachineConfig.load()
        project = Project.locate(start, machine.raw.get("project_defaults"), machine.execution_mode, data_dir=getattr(machine, "data_dir", None))
        journal = Journal(project.runs_dir)
        ensure_boot(project.runs_dir, journal)
        router = Router(machine, project.config["routing"], lambda kind, message: journal.emit(kind, message), project=project)
        tracker = None
        if getattr(project, "is_github", False):
            from .github import GhClient, GitHubTracker
            client = gh_client or GhClient(str(machine.raw.get("gh", "gh")), cwd=project.root)
            tracker = GitHubTracker(project, client, extra_forbidden=privacy.private_route_identifiers(machine, project, router))
        context = cls(project, machine, router, journal, tracker=tracker)
        if machine.execution_mode == "t3":
            from .t3 import Bridge
            journal.on_attention = Bridge(context).attention
        return context

    def normalize_id(self, wp_id: str) -> str:
        normalized = wpfile.normalize_wp_id(wp_id, is_github=self.project.is_github)
        if self.project.is_github and normalized.startswith("WP-") and hasattr(self.project, "resolve_legacy_wp_id"):
            return self.project.resolve_legacy_wp_id(normalized)
        return normalized

    # ------------------------------------------------------------------------------ work packages

    def main_wps(self, *, refresh: bool = False, read_only: bool = False) -> list[wpfile.WorkPackage]:
        """The work packages from the authoritative plan source: committed WP files on `main`
        for `wp-files` projects, or the GitHub issue tracker (with local run-state overlays)
        for `github` projects."""
        if self.project.is_github and self.tracker is not None:
            from .github import GhError
            try:
                return self.tracker.work_packages(refresh=refresh, read_only=read_only)
            except GhError as error:
                if not read_only and self.tracker.load_cache() is not None:
                    self.parse_errors["github"] = f"using cached GitHub state after sync error: {error}"
                    return self.tracker.work_packages(read_only=True)
                raise
        project = self.project
        listing = git(project.root, "ls-tree", "-z", project.main_branch, "--", f"{project.wp_dir_relative}/").stdout
        blobs = []
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            name = path.rsplit("/", 1)[-1]
            if meta.split()[1] == "blob" and name.startswith("WP-") and name.endswith(".md") and name != wpfile.TEMPLATE_NAME:
                blobs.append((meta.split()[2], path))
        blobs.sort(key=lambda blob: blob[1])
        batch = subprocess.run(["git", "-C", str(project.root), "cat-file", "--batch"], input="".join(f"{sha}\n" for sha, _ in blobs).encode(),
                               capture_output=True, check=True).stdout
        wps, offset = [], 0
        for _sha, path in blobs:
            header_end = batch.index(b"\n", offset)
            size = int(batch[offset:header_end].split()[2])
            text = batch[header_end + 1:header_end + 1 + size].decode("utf-8-sig")
            offset = header_end + 1 + size + 1
            try:
                wps.append(wpfile.parse_text(text, project.root / path))
            except wpfile.WorkPackageError as error:
                self.parse_errors[path] = str(error)
        return wps

    def effective_wps(self, main_copies: list[wpfile.WorkPackage] | None = None, *, refresh: bool = False,
                      read_only: bool = False) -> list[wpfile.WorkPackage]:
        """Effective work items with live local status.

        For `github` projects, each issue is already overlaid with its private local `state.json`.
        For `wp-files` projects, a WP with a worktree is read from its worktree copy until merged on main.
        """
        if self.project.is_github:
            if main_copies is not None:
                return list(main_copies)
            return self.main_wps(refresh=refresh, read_only=read_only)
        effective = []
        for main_copy in self.main_wps() if main_copies is None else main_copies:
            if main_copy.status == "done":
                effective.append(main_copy)
                continue
            worktree_copy = self.project.wp_dir(self.project.worktree(main_copy.id)) / main_copy.file_name
            if not worktree_copy.exists():
                effective.append(main_copy)
                continue
            try:
                effective.append(wpfile.parse(worktree_copy))
            except wpfile.WorkPackageError as error:
                self.parse_errors[str(worktree_copy)] = str(error)
                effective.append(main_copy)
        return effective

    def wp_in_tree(self, wp_id: str, tree: Path) -> wpfile.WorkPackage:
        normalized = self.normalize_id(wp_id)
        if self.project.is_github and self.tracker is not None:
            for read_only in (True, False):
                for wp in self.tracker.work_packages(refresh=not read_only, read_only=read_only):
                    if wp.id == normalized:
                        return wp
            raise wpfile.WorkPackageError(f"no GitHub issue found for {normalized}")
        return wpfile.find(self.project.wp_dir(tree), normalized)

    def merged_done_ids(self, main_copies: list[wpfile.WorkPackage] | None = None) -> set[str]:
        """Work items completed with trusted local verification that their merge landed on `main`."""
        if self.project.is_github:
            from .github import main_contains_merge
            items = self.main_wps(read_only=True) if main_copies is None else main_copies
            merged: set[str] = set()
            for wp in items:
                if not wp.is_merged:
                    continue
                local_state = run_state.load(self.project.runs_dir / wp.id)
                local_pr = local_state.get("pr") if isinstance(local_state.get("pr"), dict) else {}
                if local_state.get("local_ff_merged"):
                    if main_contains_merge(self.project, local_state.get("local_ff_commit")):
                        merged.add(wp.id)
                    continue
                if wp.pr and wp.pr.get("merged"):
                    merge_commit = wp.pr.get("merge_commit") or local_pr.get("merge_commit")
                    if merge_commit:
                        if main_contains_merge(self.project, str(merge_commit)) and local_pr.get("local_synced") is not False:
                            merged.add(wp.id)
                    elif local_pr.get("local_synced") is True:
                        merged.add(wp.id)
                    continue
                # Require a trusted local migration completion record (never a remote issue comment alone).
                if local_state.get("migrated_merged_on_main"):
                    merged.add(wp.id)
            return merged
        return {wp.id for wp in (self.main_wps() if main_copies is None else main_copies) if wp.status == "done"}

    # ------------------------------------------------------------------ backend-aware mutations

    def _reload_github_wp(self, wp_id: str) -> wpfile.WorkPackage:
        assert self.tracker is not None
        for wp in self.tracker.work_packages(read_only=True):
            if wp.id == wp_id:
                return wp
        raise wpfile.WorkPackageError(f"no cached GitHub issue for {wp_id}")

    def set_wp_status(self, wp: wpfile.WorkPackage, status: str, *, sync_remote: bool = True) -> wpfile.WorkPackage:
        if status not in wpfile.STATUSES:
            raise wpfile.WorkPackageError(f"invalid status '{status}'")
        if wp.is_github:
            run_state.set_status(self.project.wp_run_dir(wp.id), status)
            if sync_remote and self.tracker is not None:
                from .github import GhError
                try:
                    self.tracker.sync_issue_status(wp.id, status)
                except GhError as error:
                    self.journal.emit("github-sync-failed", f"{wp.id}: board status sync to '{status}' failed: {error}", {"wp": wp.id})
            return self._reload_github_wp(wp.id)
        return wpfile.set_status(wp, status)

    def log_wp_progress(self, wp: wpfile.WorkPackage, message: str, actor: str = "runner") -> str:
        if wp.is_github:
            run_state.append_progress(self.project.wp_run_dir(wp.id), message.strip(), actor)
            return run_state.latest_progress_line(run_state.load(self.project.wp_run_dir(wp.id)))
        wpfile.log_progress(wp, message.strip(), actor)
        return wpfile.parse(wp.path).latest_progress()

    def tick_wp_step(self, wp: wpfile.WorkPackage, number: int) -> str:
        boxes = [line for line in wp.section("Steps") if STEP_CHECKBOX_RE.match(line)]
        if not boxes:
            raise wpfile.WorkPackageError(f"{wp.id} has no checkbox steps under '## Steps'")
        if not 1 <= number <= len(boxes):
            raise wpfile.WorkPackageError(f"{wp.id} has {len(boxes)} steps; there is no step {number}")
        if wp.is_github:
            run_state.tick_step(self.project.wp_run_dir(wp.id), number)
            return UNCHECKED_STEP_RE.sub(r"\1[x]", boxes[number - 1], count=1).strip()
        text = wp.text()
        newline = wpfile.newline_of(text)
        lines = text.splitlines()
        start = lines.index("## Steps")
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
        indices = [index for index in range(start + 1, end) if STEP_CHECKBOX_RE.match(lines[index])]
        target = indices[number - 1]
        lines[target] = UNCHECKED_STEP_RE.sub(r"\1[x]", lines[target], count=1)
        wpfile.write_text(wp.path, newline.join(lines) + newline)
        return lines[target].strip()

    def set_wp_try_it(self, wp: wpfile.WorkPackage, text: str) -> wpfile.WorkPackage:
        cleaned = text.strip()
        if wp.is_github:
            run_state.set_try_it(self.project.wp_run_dir(wp.id), cleaned)
            return self._reload_github_wp(wp.id)
        wpfile.ensure_section(wp, "Try it", before="Handoff")
        current = wpfile.parse(wp.path)
        full = current.text()
        newline = wpfile.newline_of(full)
        lines = full.splitlines()
        start = lines.index("## Try it")
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
        replacement = ["## Try it", *cleaned.splitlines(), ""]
        lines[start:end] = replacement
        wpfile.write_text(current.path, newline.join(lines) + newline)
        return wpfile.parse(current.path)

    def set_wp_handoff(self, wp: wpfile.WorkPackage, text: str) -> wpfile.WorkPackage:
        cleaned = text.strip()
        if wp.is_github:
            run_state.set_handoff(self.project.wp_run_dir(wp.id), cleaned)
            return self._reload_github_wp(wp.id)
        wpfile.ensure_section(wp, "Handoff", before="__end__")
        wpfile.append_to_section(wpfile.parse(wp.path), "Handoff", cleaned.splitlines())
        return wpfile.parse(wp.path)

    def append_wp_review(self, wp: wpfile.WorkPackage, header: str, body: str, meta: dict | None = None) -> None:
        if wp.is_github:
            run_state.append_review(self.project.wp_run_dir(wp.id), header, body, meta)
            return
        wpfile.append_to_section(wpfile.parse(wp.path), "Review", [header, body] if header else [body])

    def append_wp_reevaluation(self, wp: wpfile.WorkPackage, header: str, body: str, meta: dict | None = None) -> None:
        if wp.is_github:
            run_state.append_reevaluation(self.project.wp_run_dir(wp.id), header, body, meta)
            return
        wpfile.ensure_section(wpfile.parse(wp.path), "Re-evaluation", before="Review")
        wpfile.append_to_section(wpfile.parse(wp.path), "Re-evaluation", [header, body])

    def add_wp_context_note(self, wp: wpfile.WorkPackage, note: str, kind: str = "user") -> None:
        if wp.is_github:
            run_state.append_context_note(self.project.wp_run_dir(wp.id), note, kind=kind)
            return
        wpfile.ensure_section(wp, "Context", before="Deliverables")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        label = "User note" if kind == "user" else "Re-evaluation note"
        first, *rest = note.strip().splitlines()
        lines = [f"- {stamp} {label}: {first.rstrip()}", *(f"  {line.rstrip()}" if line.strip() else "" for line in rest)]
        wpfile.append_to_section(wpfile.parse(wp.path), "Context", lines)

    def set_wp_field(self, wp: wpfile.WorkPackage, name: str, value: object) -> wpfile.WorkPackage:
        if wp.is_github:
            run_state.set_override(self.project.wp_run_dir(wp.id), name, value)
            return self._reload_github_wp(wp.id)
        return wpfile.set_field(wp, name, str(value))

    def wp_prompt_text(self, wp: wpfile.WorkPackage, tree: Path) -> str:
        """Returns the inlined task specification for a session prompt.

        For `wp-files`, inlines the WP markdown file from `tree`. For `github`, renders the
        GitHub issue overlaid with private local run state and stores its snapshot inside the
        private per-issue ledger (`<runs>/<id>/issue.md`), never inside the git worktree.
        """
        from .session_brief import included_files
        if not wp.is_github:
            return included_files(tree, [f"{self.project.wp_dir_relative}/{wp.file_name}"])
        snapshot_path = self.project.wp_run_dir(wp.id) / "issue.md"
        text = wp.text()
        snapshot_path.write_text(text, encoding="utf-8")
        meta_lines = [
            f"Task {wp.id} (GitHub issue #{wp.issue_number}): {wp.title}",
            f"Status: {wp.status} | Milestone: {wp.milestone or 'none'} | Lane: {wp.lane or 'none'}",
            f"Owns: {', '.join(wp.owns) if wp.owns else ('unrestricted (no explicit Scope)' if not wp.owns_explicit else 'none')}",
        ]
        if wp.depends_on:
            meta_lines.append(f"Depends on: {', '.join(wp.depends_on)}")
        if wp.url:
            meta_lines.append(f"URL: {wp.url}")
        return "\n".join(meta_lines) + "\n\n" + text
