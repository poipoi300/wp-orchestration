"""Delivers a finished work item to the main branch: either by local fast-forward (`delivery: local-ff`)
or by pushing the branch, opening/updating a sanitized GitHub PR (`Fixes #N`), and arming GitHub
auto-merge (`delivery: pr-automerge | pr | pr-draft`).

Both paths commit uncommitted worktree changes, merge the base branch (reconciling configured lock
files on lockfile-only conflicts), run the full workspace checks (`verify_workspace`), and refuse
delivery if checks dirty the tree.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import wpfile
from .commands import run_commands
from .context import Context
from .platform import NamedMutex
from .project import git
from .runner import VERIFIED_TREE_FILE, session_env, unsettled
from .scope import scope_for, scoped_checks
from .t3 import Bridge, T3Error

EXIT_MERGED, EXIT_CONFLICT, EXIT_VERIFY_FAILED, EXIT_FAST_FORWARD_FAILED = 0, 5, 6, 7
EXIT_PR_OPENED = 9
# Outcomes of bringing main in again under the mutex (not exit codes).
MAIN_UNCHANGED, MAIN_MOVED = "main-unchanged", "main-moved"
# Verifications run unlocked before one runs under the mutex, so a busy main cannot starve a merge.
OPTIMISTIC_ATTEMPTS = 2
EXIT_MEANINGS = {
    EXIT_MERGED: "merged into main",
    EXIT_CONFLICT: "merge conflict with main",
    EXIT_VERIFY_FAILED: "verification failed after merging main",
    EXIT_FAST_FORWARD_FAILED: "main could not fast-forward",
    8: "the T3 backend or the boot fence failed",
    EXIT_PR_OPENED: "PR opened (auto-merge armed or awaiting checks/merge)",
}


def run_post_merge_hooks(
    context: Context,
    before: str,
    log_path: Path | None = None,
    *,
    after: str = "HEAD",
    wp_id: str | None = None,
    log_fn=None,
) -> None:
    """Runs configured `post_merge_hooks` in the main checkout for paths changed in `before..after`."""
    project = context.project
    target_log = log_path or (project.runs_dir / "post-merge.log")
    changed = git(project.root, "diff", "--name-only", before, after).stdout.splitlines()
    for hook in project.config["post_merge_hooks"]:
        patterns = [wpfile.glob_to_regex(pattern) for pattern in hook["when_changed"]]
        if not any(pattern.match(path) for path in changed for pattern in patterns):
            continue
        failures = run_commands(project.config["shell"], [hook["command"]], project.root, project.env_for(None), target_log,
                                project.config["command_timeout_minutes"])
        if log_fn is not None:
            log_fn(f"post-merge hook '{hook['command']}': {'failed' if failures else 'ok'}")
        if failures:
            prefix = f"{wp_id} merged" if wp_id else "merged"
            fields = {"wp": wp_id} if wp_id else {}
            context.journal.emit("integration-failed", f"{prefix}, but post-merge hook '{hook['command']}' failed (log {target_log})", fields)


def cleanup_merged_outputs(context: Context, wp_id: str, log_fn=None) -> None:
    """Removes configured per-WP build output after a merge (retaining T3 checkouts)."""
    from .artifacts import remove_output
    from .project import ProjectError

    def _log(msg: str) -> None:
        if log_fn is not None:
            log_fn(msg)

    try:
        output_paths = context.project.paths_removed_after_merge(wp_id)
    except (ProjectError, OSError, subprocess.CalledProcessError) as error:
        _log(f"build cleanup refused: {error}")
        context.journal.emit("cleanup-failed", f"{wp_id} merged, but build cleanup was refused: {error}", {"wp": wp_id})
        output_paths = []
    for path in output_paths:
        if path.exists():
            try:
                remove_output(path)
            except (OSError, ProjectError) as error:
                _log(f"build cleanup failed for {path}: {error}")
                context.journal.emit("cleanup-failed", f"{wp_id} merged, but build cleanup failed for {path}: {error}", {"wp": wp_id})
            else:
                _log(f"removed {path}")


class Integration:
    def __init__(self, context: Context, wp_id: str, force_verify: bool) -> None:
        self.context = context
        self.project = context.project
        self.wp_id = context.normalize_id(wp_id)
        self.force_verify = force_verify
        self.worktree = self.project.worktree(self.wp_id)
        self.branch = self.project.branch(self.wp_id)
        self.main = self.project.main_branch
        self.log_path = self.project.wp_run_dir(self.wp_id) / "integrate.log"
        self.last_verified_commands: list[str] = []

    def log(self, message: str) -> None:
        from datetime import datetime
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")

    def commit(self, root: Path, subject: str) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as message:
            message.write(subject + ("\n\n" + self.project.commit_footer if self.project.commit_footer else "") + "\n")
        try:
            git(root, "commit", "--quiet", "-F", message.name)
        finally:
            Path(message.name).unlink()

    def save_worktree_changes(self, subject: str) -> None:
        git(self.worktree, "add", "-A")
        if git(self.worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self.commit(self.worktree, subject)
        self.log(f"committed: {subject}")

    def merge_main(self, commit: str | None = None) -> bool | int:
        """Merges main (or `commit`, a pinned tip of main). Returns True when it brought new
        commits, False when the branch already contained it, or EXIT_CONFLICT after aborting a
        merge no reconciler can settle."""
        target = commit or self.main
        if git(self.worktree, "merge-base", "--is-ancestor", target, "HEAD", check=False).returncode == 0:
            return False
        if git(self.worktree, "merge", "--no-edit", target, check=False).returncode == 0:
            self.log(f"merged {target}")
            return True
        conflicts = [line for line in git(self.worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if line]
        reconcilers = {entry["file"]: entry["command"] for entry in self.project.config["lockfile_reconcilers"]}
        if conflicts and all(path in reconcilers for path in conflicts):
            # Both sides changed a generated lock file: keep this branch's and let its tool add what
            # the merged manifests need. Any failure aborts the merge, so no half-merge is left.
            try:
                for path in conflicts:
                    git(self.worktree, "checkout", "--ours", "--", path)
                    failures = run_commands(self.project.config["shell"], [reconcilers[path]], self.worktree,
                                            session_env(self.context, self.wp_id, self.worktree), self.log_path, self.project.config["command_timeout_minutes"])
                    if failures:
                        raise RuntimeError(f"reconciling {path} failed: {failures[0][:400]}")
                    git(self.worktree, "add", "--", path)
                git(self.worktree, "commit", "--quiet", "--no-edit")
            except Exception as error:
                git(self.worktree, "merge", "--abort", check=False)
                self.log(f"lock-file reconciliation failed, merge aborted: {error}")
                return EXIT_CONFLICT
            self.log(f"merged {target}; {', '.join(conflicts)} reconciled")
            return True
        git(self.worktree, "merge", "--abort", check=False)
        self.log(f"merge conflict with {target} in: {', '.join(conflicts)}; merge aborted")
        return EXIT_CONFLICT

    def paths_between(self, start: str, end: str) -> list[str]:
        return [line for line in git(self.worktree, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", start, end).stdout.splitlines() if line]

    def verify(self, base_ref: str | None = None) -> bool:
        base = base_ref or self.main
        commands, scope_line = scoped_checks(self.project.config, self.worktree, self.project.config["verify_workspace"], self.paths_between(base, "HEAD"))
        self.last_verified_commands = list(commands)
        self.log(scope_line)
        failures = run_commands(self.project.config["shell"], commands, self.worktree,
                                session_env(self.context, self.wp_id, self.worktree), self.log_path, self.project.config["command_timeout_minutes"])
        if failures:
            return False
        # Everything was committed before the checks ran, so any change is a check writing into the
        # tree (a test regenerating tracked evidence): main must not receive it, and no check may make it.
        written = git(self.worktree, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        if written:
            self.log("the workspace checks changed files in the tree: " + "; ".join(line.strip() for line in written[:20])
                     + "; checks must not write files (restore them, and make the check stop writing)")
            return False
        return True

    def bring_main_in_again(self, verify_under_mutex: bool) -> int | str:
        """Called under the integration mutex after verifying unlocked. MAIN_UNCHANGED when the
        branch holds main's tip and stands verified; MAIN_MOVED when the new merge needs verifying again;
        else an exit code."""
        verified_main = git(self.worktree, "merge-base", self.main, "HEAD").stdout.strip()
        main_tip = git(self.worktree, "rev-parse", self.main).stdout.strip()
        if main_tip == verified_main:
            return MAIN_UNCHANGED
        if self.merge_main(main_tip) == EXIT_CONFLICT:
            return EXIT_CONFLICT
        ours = scope_for(self.project.config, self.worktree, self.paths_between(main_tip, "HEAD"))
        theirs = scope_for(self.project.config, self.worktree, self.paths_between(verified_main, main_tip))
        if ours.packages is not None and theirs.packages is not None and not set(ours.packages) & set(theirs.packages):
            self.log("main moved during verification; its changes reach no package this WP reaches, so the merge stands verified")
            return MAIN_UNCHANGED
        if not verify_under_mutex:
            self.log("main moved during verification into packages this WP reaches; verifying the new merge")
            return MAIN_MOVED
        self.log("main moved during verification again; verifying under the mutex")
        if self.verify():
            return MAIN_UNCHANGED
        self.log("workspace verification failed; main not moved")
        return EXIT_VERIFY_FAILED

    def rounds_ran_the_full_checks(self, wp: wpfile.WorkPackage) -> bool:
        """Whether the runner's verification already included every integration check."""
        round_checks = wp.round_checks(self.project.config["verify_round"], self.project.config["verify_workspace"])
        return all(command in round_checks for command in self.project.config["verify_workspace"])

    def unfinished_merge(self) -> bool:
        merging = git(self.worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
        unmerged = bool(git(self.worktree, "diff", "--name-only", "--diff-filter=U").stdout.strip())
        return merging or unmerged

    def verified_tree_matches(self) -> bool:
        """Whether the tip differs from the tree the runner verified only in `verify_neutral_paths`."""
        record = self.project.wp_run_dir(self.wp_id) / VERIFIED_TREE_FILE
        if not record.exists():
            return False
        verified = record.read_text(encoding="utf-8").strip()
        if git(self.worktree, "rev-parse", "HEAD^{tree}").stdout.strip() == verified:
            return True
        patterns = [wpfile.glob_to_regex(pattern) for pattern in self.project.config["verify_neutral_paths"]]
        changed = git(self.worktree, "diff", "--name-only", verified, "HEAD", check=False)
        if not patterns or changed.returncode != 0:
            return False
        return all(any(pattern.match(path) for pattern in patterns) for path in changed.stdout.splitlines() if path)

    def post_merge(self, before: str) -> None:
        run_post_merge_hooks(self.context, before, self.log_path, wp_id=self.wp_id, log_fn=self.log)

    def cleanup(self, *, landed_tip: str | None = None) -> None:
        """Removes the worktree, branch and build output after main moved."""
        if self.context.machine.execution_mode == "t3":
            self.log("retained T3-owned worktree and branch; their lifecycle belongs to T3")
        elif self.project.is_github:
            from .github import _cleanup_merged_worktree
            _cleanup_merged_worktree(self.context, self.wp_id, {"branch": self.branch, "head_sha": landed_tip})
        else:
            self.cleanup_standalone_worktree()
        cleanup_merged_outputs(self.context, self.wp_id, self.log)

    def cleanup_standalone_worktree(self) -> None:
        try:
            git(self.project.root, "worktree", "remove", "--force", str(self.worktree))
            git(self.project.root, "branch", "--quiet", "-d", self.branch)
        except Exception as error:
            self.log(f"cleanup failed: {error}")
            self.context.journal.emit("cleanup-failed", f"{self.wp_id} merged into main, but removing its worktree or branch failed: {error}", {"wp": self.wp_id})

    def _check_outgoing_commits_privacy(self, base_ref: str, head_sha: str, *, enabled: bool, extra_forbidden: tuple[str, ...] = ()) -> None:
        """Fails closed: inspects EVERY outgoing commit in `base_ref..head_sha` (commit messages,
        touched files, legacy WP files, in-repo ledger paths, and every hunk-added diff line) before `git push`."""
        if not enabled:
            return
        from .privacy import PrivacyError, check_code_diff_text, check_public_text
        from .project import ProjectError

        try:
            regular_commits = [
                line.strip()
                for line in git(self.worktree, "rev-list", "--no-merges", f"{base_ref}..{head_sha}").stdout.splitlines()
                if line.strip()
            ]
            merge_commits = [
                line.strip()
                for line in git(self.worktree, "rev-list", "--merges", f"{base_ref}..{head_sha}").stdout.splitlines()
                if line.strip()
            ]
        except ProjectError as error:
            raise PrivacyError([f"could not enumerate outgoing commits for privacy check: {error}"]) from error

        ledger_prefixes = [".orchestration/runs", ".orchestration/tasks"]
        resolved_root = self.project.root.resolve()
        resolved_runs = self.project._resolve_runs_dir().resolve()
        if resolved_runs.is_relative_to(resolved_root):
            rel_runs = resolved_runs.relative_to(resolved_root).as_posix().strip("/")
            if rel_runs and rel_runs not in ledger_prefixes:
                ledger_prefixes.append(rel_runs)

        wp_dir_rel = self.project.wp_dir_relative
        violations: list[str] = []
        private_wp_sections = ("Progress log", "Handoff", "Review", "Re-evaluation", "Try it")
        private_wp_front_keys = ("builder", "reviewer", "delegate", "mode", "skills")

        def _extract_added_lines(patch_text: str, *, combined: bool = False) -> str:
            # A49: Track hunk boundaries (`@@` / `@@@`) so file headers (`+++ b/...`) are skipped
            # while real added lines starting with `++` (emitted as `+++...` in hunks) ARE scanned.
            added_chunks: list[str] = []
            in_hunk = False
            for line in patch_text.splitlines():
                if line.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
                    in_hunk = False
                elif line.startswith("@@"):
                    in_hunk = True
                elif in_hunk:
                    if combined:
                        # Combined diff (`--cc`) prefixes lines with one column per parent (e.g. `++`, `+ `, ` +`).
                        if len(line) >= 2 and "+" in line[:2]:
                            added_chunks.append(line.lstrip("+- "))
                        elif line.startswith(("-", " ", "\\")):
                            continue
                        else:
                            in_hunk = False
                    elif line.startswith("+"):
                        added_chunks.append(line[1:])
                    elif line.startswith(("-", " ", "\\")):
                        continue
                    else:
                        in_hunk = False
            return "\n".join(added_chunks)

        # A61 + A66: Scan every non-merge commit introduced by this branch with `--text --no-textconv --no-ext-diff`
        # (so binary blobs and `.gitattributes binary/-diff` files are scanned too), and for merge commits
        # (such as `merge_main`), scan commit messages + `--cc` conflict resolutions only so clean incoming
        # changes already on `main` never block this branch.
        commit_specs = [(sha, False) for sha in regular_commits] + [(sha, True) for sha in merge_commits]
        for sha, is_merge in commit_specs:
            short = sha[:10]
            try:
                commit_msg = git(self.worktree, "show", "-s", "--format=%B", sha).stdout
                if is_merge:
                    touched = git(
                        self.worktree, "diff-tree", "--cc", "--no-commit-id", "--name-only", "-r", sha,
                    ).stdout.splitlines()
                    patch_text = git(
                        self.worktree, "show", "--cc", "--text", "--no-textconv", "--no-ext-diff",
                        "--format=", "-U0", "--no-color", sha,
                    ).stdout
                else:
                    touched = git(
                        self.worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "--diff-filter=ACMR", sha,
                    ).stdout.splitlines()
                    patch_text = git(
                        self.worktree, "show", "--text", "--no-textconv", "--no-ext-diff",
                        "--format=", "-U0", "--no-color", sha,
                    ).stdout
            except ProjectError as error:
                raise PrivacyError([f"could not inspect outgoing commit {short} for privacy safety: {error}"]) from error

            for issue in check_code_diff_text(commit_msg, self.project.root, resolved_runs, extra_forbidden=extra_forbidden):
                violations.append(f"commit {short} message: {issue}")

            for raw_path in touched:
                rel = raw_path.replace("\\", "/").strip()
                if not rel:
                    continue
                if any(rel == prefix or rel.startswith(prefix + "/") for prefix in ledger_prefixes):
                    violations.append(f"commit {short}: private ledger/task artifact ({rel})")
                    continue
                if self.project.is_github and wpfile.is_wp_file(rel, wp_dir_rel) and not rel.endswith(f"/{wpfile.TEMPLATE_NAME}"):
                    try:
                        text = git(self.worktree, "show", f"{sha}:{rel}").stdout
                    except ProjectError as error:
                        raise PrivacyError([f"could not read outgoing WP blob {short}:{rel}: {error}"]) from error
                    try:
                        parsed = wpfile.parse_text(text, self.worktree / rel, retain_body=True)
                    except wpfile.WorkPackageError:
                        parsed = None
                    if parsed is not None:
                        if any(key in parsed.fields for key in private_wp_front_keys):
                            violations.append(f"commit {short}: legacy WP file carries private routing front matter ({rel})")
                        for sec in private_wp_sections:
                            if any(line.strip() and not line.strip().startswith("<!--") for line in parsed.section(sec)):
                                violations.append(f"commit {short}: legacy WP file carries private '## {sec}' content ({rel})")
                                break
                    for issue in check_public_text(text, project_root=None, allow_local_paths=False, extra_forbidden=extra_forbidden):
                        violations.append(f"commit {short} ({rel}): {issue}")

            added_lines = _extract_added_lines(patch_text, combined=is_merge)
            for issue in check_code_diff_text(added_lines, self.project.root, resolved_runs, extra_forbidden=extra_forbidden):
                violations.append(f"commit {short} diff: {issue}")

        if violations:
            raise PrivacyError(list(dict.fromkeys(violations)))

    def _deliver_pr(self, wp: wpfile.WorkPackage, delivery: str) -> int:
        """Pushes the verified branch, opens/updates a sanitized PR (`Fixes #N`), and arms auto-merge."""
        from . import run_state
        from .github import (
            GhClient,
            GhError,
            GitHubTracker,
            finalize_merged_pr,
            render_pr_body,
            sync_local_main,
        )
        from .privacy import (
            PrivacyError,
            private_route_identifiers,
            require_public_safe,
        )

        remote = str(self.project.github_config.get("remote") or "origin")
        # Refresh remote base branch when reachable; fall back to local main if no remote ref exists yet.
        fetched = git(self.worktree, "fetch", "--quiet", remote, self.main, check=False)
        remote_base = f"{remote}/{self.main}"
        base_ref = remote_base if fetched.returncode == 0 and git(self.worktree, "rev-parse", "--verify", remote_base, check=False).returncode == 0 else self.main

        self.save_worktree_changes(f"{self.wp_id}: {wp.title}")
        merged = self.merge_main(base_ref)
        if merged == EXIT_CONFLICT:
            return EXIT_CONFLICT

        # A21: Pin candidate HEAD and tree BEFORE verification so a concurrent commit cannot slip in.
        candidate_head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        candidate_tree = git(self.worktree, "rev-parse", "HEAD^{tree}").stdout.strip()

        must_verify = self.force_verify or bool(merged) or not self.rounds_ran_the_full_checks(wp) or not self.verified_tree_matches()
        if must_verify and not self.verify(base_ref):
            self.log("workspace verification failed; PR not pushed")
            return EXIT_VERIFY_FAILED

        head_sha = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        current_tree = git(self.worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
        if head_sha != candidate_head or current_tree != candidate_tree or git(self.worktree, "status", "--porcelain", "--untracked-files=all").stdout.strip():
            self.log(f"worktree changed during verification ({candidate_head[:10]} -> {head_sha[:10]}); PR not pushed")
            return EXIT_VERIFY_FAILED

        verify_cmds = self.last_verified_commands or wp.round_checks(self.project.config["verify_round"], self.project.config["verify_workspace"])
        actual_branch = git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        commits_count = int(git(self.worktree, "rev-list", "--count", f"{base_ref}..{head_sha}").stdout.strip())
        if commits_count == 0:
            self.log(f"branch {actual_branch} has no commits ahead of {base_ref}; refusing empty PR")
            return EXIT_FAST_FORWARD_FAILED
        base_sha_res = git(self.worktree, "merge-base", base_ref, head_sha, check=False)
        base_sha = base_sha_res.stdout.strip() if base_sha_res.returncode == 0 else None
        commit_headlines = list(reversed(
            git(self.worktree, "log", "--format=%s", f"{base_ref}..{head_sha}").stdout.splitlines()
        ))

        # Validate outbound PR text AND every outgoing commit before pushing so privacy violations fail fast.
        privacy_enabled = bool(self.project.github_config.get("privacy_check", True))
        extra_forbidden = private_route_identifiers(self.context.machine, self.project, getattr(self.context, "router", None))
        try:
            require_public_safe(wp.title, self.project.root, enabled=privacy_enabled, extra_forbidden=extra_forbidden)
            render_pr_body(
                wp,
                verify_commands=verify_cmds,
                project_root=self.project.root,
                privacy_check=privacy_enabled,
                extra_forbidden=extra_forbidden,
            )
            self._check_outgoing_commits_privacy(base_ref, head_sha, enabled=privacy_enabled, extra_forbidden=extra_forbidden)
        except PrivacyError as error:
            self.log(f"PR delivery refused before push: {error}")
            self.context.journal.emit("integration-failed", f"{self.wp_id}: PR delivery failed: {error}", {"wp": self.wp_id})
            return EXIT_FAST_FORWARD_FAILED

        pushed = git(self.worktree, "push", "-u", remote, f"{head_sha}:refs/heads/{actual_branch}", check=False)
        if pushed.returncode != 0:
            self.log(f"git push to {remote}/{actual_branch} failed: {(pushed.stderr or pushed.stdout).strip()}")
            return EXIT_FAST_FORWARD_FAILED
        self.log(f"pushed {actual_branch} ({head_sha[:10]}) to {remote}")

        tracker = self.context.tracker or GitHubTracker(
            self.project,
            GhClient(str(self.context.machine.raw.get("gh", "gh")), cwd=self.project.root),
            extra_forbidden=extra_forbidden,
        )
        try:
            result = tracker.deliver_pr(
                wp,
                actual_branch,
                head_sha,
                delivery,
                verify_commands=verify_cmds,
                commits_count=commits_count,
                base_sha=base_sha,
                commit_headlines=commit_headlines,
            )
        except (GhError, PrivacyError) as error:
            self.log(f"PR delivery failed: {error}")
            self.context.journal.emit("integration-failed", f"{self.wp_id}: PR delivery failed: {error}", {"wp": self.wp_id})
            return EXIT_FAST_FORWARD_FAILED

        self.log(f"PR #{result.number} ({result.url}) state={result.state} auto_merge={result.auto_merge_enabled} merged={result.merged}")
        if result.warning:
            self.log(f"warning: {result.warning}")
            self.context.journal.emit("pr-automerge-unavailable", f"{self.wp_id}: {result.warning}", {"wp": self.wp_id})
        if result.merged:
            sync_result = sync_local_main(self.context)
            run_dir = self.project.wp_run_dir(self.wp_id)
            pr_record = dict(run_state.load(run_dir).get("pr") or {})
            finalize_merged_pr(
                self.context,
                self.wp_id,
                pr_record,
                sync_result,
                log_path=self.log_path,
                log_fn=self.log,
            )
            return EXIT_MERGED

        self.context.journal.emit(
            "pr-opened",
            f"{self.wp_id} opened PR #{result.number} ({result.url})" + (" with auto-merge armed" if result.auto_merge_enabled else ""),
            {"wp": self.wp_id, "pr": str(result.number), "url": result.url},
        )
        if result.auto_merge_enabled:
            self.context.journal.emit("pr-automerge-enabled", f"{self.wp_id}: auto-merge armed on PR #{result.number} ({result.url})", {"wp": self.wp_id})
        return EXIT_PR_OPENED

    def run(self) -> int:
        from . import run_state
        with run_state.ledger_writer(self.project, "integrate", self.wp_id):
            return self._run_locked()

    def _run_locked(self) -> int:
        outstanding, delegation = unsettled(self.context, self.wp_id)
        if delegation:
            raise RuntimeError(f"{self.wp_id}'s delegated workers are not settled; run the WP (its runner stops its workers), then integrate")
        if outstanding:
            # Integration commits the checkout without a scope check: a session still writing
            # there, or one never checked, must be settled first (by running the WP).
            raise T3Error(f"{self.wp_id} has native sessions no runner has finished ({', '.join(r['id'] for r in outstanding)}); "
                          "run the WP (its runner settles them) or wait, then integrate")
        if self.context.machine.execution_mode == "t3":
            Bridge(self.context).validate_workspace(self.wp_id)
        if not self.worktree.exists():
            raise FileNotFoundError(f"no worktree for {self.wp_id} at {self.worktree}")
        if self.unfinished_merge():
            self.log("the worktree has an unfinished merge (MERGE_HEAD or unmerged paths); finish or abort it first")
            return EXIT_CONFLICT
        wp = self.context.wp_in_tree(self.wp_id, self.worktree)
        self.log(f"integrating {self.wp_id} ({wp.title}) via delivery={self.project.delivery_mode}")

        delivery = self.project.delivery_mode
        if delivery in ("pr", "pr-draft", "pr-automerge"):
            return self._deliver_pr(wp, delivery)

        current = git(self.project.root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
        if current != self.main:
            raise RuntimeError(f"the main checkout is on '{current or 'a detached HEAD'}', not {self.main}; integration refused")
        self.save_worktree_changes(f"{self.wp_id}: {wp.title}")
        merged = self.merge_main()
        if merged == EXIT_CONFLICT:
            return EXIT_CONFLICT
        must_verify = self.force_verify or not self.rounds_ran_the_full_checks(wp) or not self.verified_tree_matches()
        if must_verify and not self.verify():
            self.log("workspace verification failed; main not moved")
            return EXIT_VERIFY_FAILED
        for attempt in range(OPTIMISTIC_ATTEMPTS + 1):
            with NamedMutex(self.project.integration_mutex):
                outcome = self.bring_main_in_again(verify_under_mutex=attempt == OPTIMISTIC_ATTEMPTS)
                if outcome in (EXIT_CONFLICT, EXIT_VERIFY_FAILED):
                    return outcome
                if outcome == MAIN_UNCHANGED:
                    before = git(self.project.root, "rev-parse", "HEAD").stdout.strip()
                    tip = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
                    fast_forward = git(self.project.root, "merge", "--ff-only", "--quiet", tip, check=False)
                    if fast_forward.returncode != 0:
                        self.log(f"main could not fast-forward to {tip}: {(fast_forward.stderr or fast_forward.stdout).strip()}")
                        return EXIT_FAST_FORWARD_FAILED
                    self.log(f"main fast-forwarded to {self.branch} ({tip[:10]}){' after verification' if must_verify else ''}")
                    break
            must_verify = True
            if not self.verify():
                self.log("workspace verification failed; main not moved")
                return EXIT_VERIFY_FAILED
        else:
            raise RuntimeError("integration loop ended undecided; its last attempt verifies under the mutex")
        self.post_merge(before)
        if self.project.is_github:
            from . import run_state
            run_dir = self.project.wp_run_dir(self.wp_id)
            run_state.set_local_ff_merged(run_dir, tip, pending_issue_close=True)
            if self.context.tracker is not None:
                from .github import GhError
                try:
                    self.context.tracker.close_issue_completed(self.wp_id, commit=tip)
                    run_state.clear_pending_issue_close(run_dir)
                except GhError as error:
                    self.log(f"warning: local-ff merged {self.wp_id}, but closing GitHub issue failed (will retry on reconcile): {error}")
                    self.context.journal.emit(
                        "github-sync-failed",
                        f"{self.wp_id} merged locally ({tip[:10]}), but closing GitHub issue failed (queued for retry): {error}",
                        {"wp": self.wp_id},
                    )
        self.cleanup(landed_tip=tip)
        return EXIT_MERGED


def integrate(context: Context, wp_id: str, force_verify: bool = False) -> int:
    return Integration(context, wp_id, force_verify).run()
