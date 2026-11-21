# GitHub Issues, Projects v2, Auto-Merge PRs, and Split Private Ledger

WPO supports splitting project tracking across two clean planes:

- **GitHub (Control Plane & Plan):** issues (`GH-<N>` / `#<N>`), native `blocked-by` dependencies,
  native sub-issues, milestones, labels, a GitHub Project (v2) board, and Pull Requests with
  `Fixes #<N>` and auto-merge.
- **Private Local Ledger (`runs_dir: "external"`):** transcripts, prompts, rendered HTML views,
  `usage/index.json` analytics, `attempts.json`, `findings.json`, `state.json` (inner-loop status,
  progress log, step ticks, `Try it`, `Handoff`, review/re-evaluation notes, personal routing/scope
  overrides), and the read-through `github-cache.json`.

Keeping transcripts and run state in an external directory under `machine.data_dir`
(`~/.local/share/wp-orchestration/projects/<slug>-<hash>/runs`) makes it structurally impossible
to accidentally commit transcripts or package them into a release.

## Start a new GitHub-backed project

```sh
wpo init --name my-project --project-number 1
wpo github sync
wpo github status
wpo status
```

`wpo init` defaults to `"tracker": "github"`, `"delivery": "pr-automerge"`, and
`"runs_dir": "external"`, and detects `github.repo` from `origin` (pass `--wp-files` for
the legacy committed `WP-*.md` tracker or `--keep-repo-ledger` to keep runs in `.orchestration/runs/`).

## Deterministic migration from WP markdown files

`wpo migrate github` migrates an existing `WP-*.md` repository deterministically and idempotently:

```sh
# 1. Preview plan and run the outbound privacy preflight (dry run by default):
wpo migrate github

# 2. Execute migration (creates/reuses Project v2, milestones, labels, issues,
#    wires native blocked-by, rewrites WP-XXXX refs to #N, closes already-merged WPs,
#    migrates transcripts/ledgers/in-flight worktrees to the external private ledger,
#    and updates .orchestration/config.json):
wpo migrate github --apply --enable-auto-merge
```

Useful options:

- `--project-number N` / `--project-owner LOGIN` / `--project-title TITLE`: reuse or name a specific Project v2 (`--no-create-project` skips creating one).
- `--external-ledger` (default) / `--keep-repo-ledger`: move private run state out of the checkout or keep it in `.orchestration/runs/`.
- `--open-only`: migrate only open/blocked WPs and skip WPs already merged on `main`.
- `--remove-wp-files`: remove migrated `WP-*.md` files from `wp_dir` after creating issues.
- `--enable-auto-merge`: enable `allow_auto_merge` on the GitHub repository if currently off.

Migration checkpoints every created issue to `migration-state.json` and scans existing GitHub
issues for `legacy_wp_id` markers, so interrupting and re-running `--apply` never creates duplicates.
Private sections (`Progress log`, `Review`, `Re-evaluation`, `Handoff`, `Try it`) and personal
routing settings (`builder`, `reviewer`, `delegate`, `mode`, `skills`) are extracted into the local
private ledger and never pushed to GitHub.

## Issue and PR conventions

GitHub issues stay readable and idiomatic. WPO parses:

- **Goal:** `## Goal` section, or the opening paragraphs before the first `## ` heading.
- **Acceptance criteria:** `## Acceptance criteria`, or a `**Done when:**` block.
- **Scoped verification:** optional `## Verify` fenced command block. Remote issue `## Verify` commands are **not executed** until locally authorized (`wpo ledger approve-verify GH-<N>`, bound to that exact reviewed issue body revision) or set via `wpo ledger override GH-<N> --verify ...`. Migrated committed WPs are automatically authorized for their migrated body revision, and any subsequent remote edit invalidates the authorization.
- **Path ownership (`owns`):** optional `## Scope` / `## Owns` bulleted path globs and/or trailing `<!-- wpo {"owns": [...], "lane": "..."} -->`. When omitted and `require_owns` is `false` (default for GitHub), single-PR tasks are unrestricted while `wpo lint` warns if concurrent lanes run without explicit scopes.
- **Dependencies & hierarchy:** GitHub native `blocked-by` relationships (repo-qualified for cross-repo blockers) and native `parent`/`subIssues` (paginated across all children). Parent issues are gated on open sub-issues.
- **Board status:** Project v2 Status is strictly one-way (`WPO -> GitHub`); only verified merge evidence (`Fixes #N` PR merged and synced into local `main`, or verified `local-ff` merge) marks a work item `done` and unblocks dependents.
- **Lanes:** local override > Project v2 `Lane` field > `lane:<name>` label > `<!-- wpo -->` `lane` > `default_lane`.

Pull Requests opened by WPO (`wpo integrate` / `wpo github pr`):

- Start with `Fixes #<N>` so merging closes the issue and moves the GitHub Project item to `Done`.
- Include a sanitized summary, `## Try it` (recorded by the builder with `wpo try-it`), and a checklist of verification commands that passed.
- Pre-push privacy checks validate PR title/body and outgoing commits (blocking private ledger artifacts, modified legacy `WP-*.md` private sections/routing, secrets, raw session JSONL, and private routing pins) before pushing the pinned verified commit SHA.
- With `delivery: "pr-automerge"`, WPO arms `gh pr merge --auto --<merge_method> --match-head-commit <sha>`. Direct-merge fallback (`github.auto_merge_fallback: "direct-merge"`) only runs for PRs delivered by WPO matching the same repository, base, PR number, and verified head SHA.
- Supervisors reconcile open, merged, and closed-unmerged PRs, fast-forward local `main` when a PR merges, run `post_merge_hooks` once under `integration_mutex` over the exact landed PR range (squash, merge commit, or multi-commit rebase), safely clean verified clean standalone worktrees (never dirty, unowned, or T3 checkouts), and unblock dependent issues only once local `main` contains the prerequisite merge.

## Privacy firewall and ledger maintenance

Every outbound GitHub write (migration project/milestone/label metadata, issue titles/bodies, PR titles/bodies, and outgoing PR commits) passes through `wpo/privacy.py`,
which rewrites repo-root paths to repo-relative paths and blocks suspected credentials, personal home
paths, raw session JSONL (any field order), private routing pins (`pin:<route>`), configured private route identifiers, and ledger markers before invoking `gh`.

Session commands for GitHub-backed work items write only to the local private ledger:

```sh
wpo log "Implemented parser" --step 1
wpo try-it --file .tmp-try-it.md --remove   # sandbox-safe multiline form (also accepts `-` stdin or single-line text)
wpo verify
wpo block "Waiting on upstream schema decision"
```

Manage the private ledger, verify approvals, and per-issue local overrides (which never touch GitHub):

```sh
wpo ledger path
wpo ledger status
wpo ledger approve-verify GH-42                   # authorize GH-42's current reviewed '## Verify' commands
wpo ledger approve-verify GH-42 --clear           # revoke local Verify authorization
wpo ledger override GH-42 --builder pin:local-deepseek-max --owns "wpo/github.py,tests/test_github.py"
wpo ledger prune --older-than-days 30             # dry run; indexes usage/index.json first
wpo ledger prune --older-than-days 30 --apply     # removes old raw transcripts for verified merged items only
wpo ledger hooks                                  # inspect post-merge hook journal entries (including interrupted hooks)
wpo ledger hooks --mark-done "<hook-key>"         # mark a manually reconciled interrupted hook done
wpo ledger hooks --retry "<hook-key>"             # clear an interrupted hook entry so the next reconcile re-runs it
```
