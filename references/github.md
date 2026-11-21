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
wpo init --name my-project --github --project-number 1
wpo github sync
wpo github status
wpo status
```

`wpo init --github` sets `"tracker": "github"`, `"delivery": "pr-automerge"`, and
`"runs_dir": "external"`, and detects `github.repo` from `origin`.

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
- **Scoped verification:** optional `## Verify` fenced command block (runs before `verify_round` in `wpo verify`).
- **Path ownership (`owns`):** optional `## Scope` / `## Owns` bulleted path globs and/or trailing `<!-- wpo {"owns": [...], "lane": "..."} -->`. When omitted and `require_owns` is `false` (default for GitHub), single-PR tasks are unrestricted while `wpo lint` warns if concurrent lanes run without explicit scopes.
- **Dependencies & hierarchy:** GitHub native `blocked-by` relationships and native `parent`/`subIssues`. Parent issues with open sub-issues are not scheduled as leaf tasks until their sub-issues close.
- **Lanes:** local override > Project v2 `Lane` field > `lane:<name>` label > `<!-- wpo -->` `lane` > `default_lane`.

Pull Requests opened by WPO (`wpo integrate` / `wpo github pr`):

- Start with `Fixes #<N>` so merging closes the issue and moves the GitHub Project item to `Done`.
- Include a sanitized summary, `## Try it` (recorded by the builder with `wpo try-it`), and a checklist of verification commands that passed.
- With `delivery: "pr-automerge"`, WPO enables `gh pr merge --auto --<merge_method>`. If the repository rejects `--auto` (auto-merge disabled or no branch protection), `github.auto_merge_fallback` controls whether WPO waits with an actionable attention event (`"wait"`, default) or merges directly when checks are green (`"direct-merge"`).
- Supervisors reconcile open PRs, fast-forward local `main` when a PR merges, run `post_merge_hooks` and `remove_after_merge` build cleanup, and automatically start newly unblocked dependent issues.

## Privacy firewall and ledger maintenance

Every outbound GitHub write (migration issue bodies, PR titles/bodies) passes through `wpo/privacy.py`,
which rewrites repo-root paths to repo-relative paths and blocks suspected credentials, personal home
paths, and raw session ledger markers before invoking `gh`.

Session commands for GitHub-backed work items write only to the local private ledger:

```sh
wpo log "Implemented parser" --step 1
wpo try-it - <<'EOF'
```sh
uv run pytest tests/test_parser.py
```
Expect all tests green.
EOF
wpo verify
wpo block "Waiting on upstream schema decision"
```

Manage the private ledger and per-issue local overrides (which never touch GitHub):

```sh
wpo ledger path
wpo ledger status
wpo ledger override GH-42 --builder pin:local-deepseek-max --owns "wpo/github.py,tests/test_github.py"
wpo ledger prune --older-than-days 30           # dry run; indexes usage/index.json first
wpo ledger prune --older-than-days 30 --apply   # removes old raw transcripts for merged items
```
