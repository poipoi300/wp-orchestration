"""The `wpo` command line. Every subcommand finds its project from the working directory (any
worktree of the repository works) unless --project names one."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import ENTRY_POINT, SKILL_ROOT


def entrypoint() -> int:
    """Run the CLI with actionable errors for an unavailable T3 backend."""
    from .boot import BootError
    from .runner import EXIT_BACKEND
    from .t3 import T3Error

    try:
        return main()
    except T3Error as error:
        print(f"T3 backend: {error}", file=sys.stderr)
        return EXIT_BACKEND
    except BootError as error:
        print(f"Boot safety: {error}", file=sys.stderr)
        return EXIT_BACKEND


def split_list(values: list[str] | None) -> list[str]:
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


def command_names() -> frozenset[str]:
    """wpo's own subcommand names, read from the parser so other code cannot drift from it."""
    return frozenset(name for action in build_parser()._actions if isinstance(action, argparse._SubParsersAction)
                     for name in action.choices)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpo", description="Parallel work-package orchestration on git worktrees (SKILL.md).")
    parser.add_argument("--project", type=Path, help="any directory inside the project's repository (default: the working directory)")
    parser.add_argument("--session", help="T3 bridge session identity (provided in native task prompts)")
    commands = parser.add_subparsers(dest="command", required=True)

    from .t3 import add_cli
    add_cli(commands)
    from .continuous import add_cli as add_continuous_cli
    add_continuous_cli(commands)

    route = commands.add_parser("route", help="the tier router")
    route_commands = route.add_subparsers(dest="route_command", required=True)
    candidates = route_commands.add_parser("candidates")
    candidates.add_argument("--spec", required=True)
    candidates.add_argument("--families", action="append")
    candidates.add_argument("--refresh", action="store_true")
    candidates.add_argument("--gate", action="store_true", help="rank as a review or re-evaluation: gate-only routes, no free fallback")
    route_commands.add_parser("status")
    refresh = route_commands.add_parser("refresh")
    refresh.add_argument("--force", action="store_true")
    mark = route_commands.add_parser("mark")
    mark.add_argument("--route", required=True)
    mark.add_argument("--outcome", required=True)
    mark.add_argument("--detail", default="")
    mode = route_commands.add_parser("mode", help="show or set the project's routing mode (WPs keep the mode they started in)")
    mode.add_argument("mode", nargs="?", help="standard, or a mode the routing policy defines")
    clear = route_commands.add_parser("clear", help="drop cooldowns (a cap lifted early)")
    clear.add_argument("--prefix", default="", help="e.g. family:gpt; empty clears every cooldown")

    events = commands.add_parser("events", help="the orchestrator's event journal")
    event_commands = events.add_subparsers(dest="events_command", required=True)
    emit = event_commands.add_parser("emit")
    emit.add_argument("--kind", required=True)
    emit.add_argument("--message", required=True)
    emit.add_argument("--field", action="append", default=[])
    wait = event_commands.add_parser("wait")
    wait.add_argument("--after", type=int, required=True)
    wait.add_argument("--timeout-minutes", type=float, default=110)
    wait.add_argument("--quiet", action="append", help="attention kinds to treat as info for this wait (e.g. no-route)")
    tail = event_commands.add_parser("tail")
    tail.add_argument("--after", type=int, default=0)
    event_commands.add_parser("cursor")

    run = commands.add_parser("run", help="build, verify and review one WP in its worktree")
    run.add_argument("--wp", required=True)
    run.add_argument("--tree", type=Path, help="default: the WP's worktree")
    run.add_argument("--lane")
    run.add_argument("--builder", help="override the builder spec (a tier or pin:<route>)")
    run.add_argument("--reviewer", help="override the reviewer spec")
    run.add_argument("--families", action="append")
    run.add_argument("--idle-minutes", type=float)

    delegate = commands.add_parser("delegate", help="a lead builder's workers (inside a builder session)")
    delegate_commands = delegate.add_subparsers(dest="delegate_command", required=True)
    start = delegate_commands.add_parser("start")
    start.add_argument("--name", required=True)
    start.add_argument("task", nargs="?", help="the task, in full (or --prompt-file)")
    start.add_argument("--prompt-file")
    start.add_argument("--write-paths", action="append", required=True)
    start.add_argument("--tier")
    delegate_wait = delegate_commands.add_parser("wait")
    delegate_wait.add_argument("--name", required=True)
    delegate_wait.add_argument("--minutes", type=float, help="return after this long even if the worker still runs (default: when it finishes)")
    delegate_commands.add_parser("list")
    delegate_run = delegate_commands.add_parser("run")
    delegate_run.add_argument("--name", required=True)
    delegate_run.add_argument("--token", help=argparse.SUPPRESS)

    supervise = commands.add_parser("supervise", help="run a lane or milestone unattended (normally via `lanes resume`)")
    target = supervise.add_mutually_exclusive_group(required=True)
    target.add_argument("--lane")
    target.add_argument("--milestone")
    supervise.add_argument("--wps", action="append")
    supervise.add_argument("--families", action="append")
    supervise.add_argument("--builder")
    supervise.add_argument("--idle-minutes", type=float)

    integrate = commands.add_parser("integrate", help="merge a done WP into main")
    integrate.add_argument("--wp", required=True)
    integrate.add_argument("--verify", action="store_true", help="verify even if the branch already contains main")

    lanes = commands.add_parser("lanes", help="lane supervisors")
    lane_commands = lanes.add_subparsers(dest="lanes_command", required=True)
    resume = lane_commands.add_parser("resume")
    resume.add_argument("targets", nargs="*", metavar="LANE|WP", help="only these lanes, or the lanes of these WPs (default: every lane)")
    resume.add_argument("--dry-run", action="store_true")
    for name in ("stop", "release"):
        lane_commands.add_parser(name).add_argument("lane")
    hold = lane_commands.add_parser("hold")
    hold.add_argument("lane")
    hold.add_argument("--reason", default="held by the orchestrator")

    commands.add_parser("status", help="print the board (and write the board file when configured)")
    clean_builds = commands.add_parser("clean-builds", help="plan build-output cleanup for WPs done on main; preserves checkout/source/history")
    clean_builds.add_argument("--wp", action="append", default=[], help="only these WPs (repeatable; default: all)")
    clean_builds.add_argument("--apply", action="store_true", help="remove the reported output; default is a dry run")

    job = commands.add_parser("job", help="detached orchestrator jobs")
    job_commands = job.add_subparsers(dest="job_command", required=True)
    job_start = job_commands.add_parser("start")
    job_start.add_argument("--name", required=True)
    job_start.add_argument("job_arguments", nargs=argparse.REMAINDER)
    job_exec = job_commands.add_parser("exec")
    job_exec.add_argument("--name", required=True)
    job_exec.add_argument("--log", type=Path, required=True)
    job_exec.add_argument("job_arguments", nargs=argparse.REMAINDER)

    dispatch = commands.add_parser("dispatch", help="an ad-hoc tier-routed task")
    dispatch.add_argument("--tier", required=True)
    dispatch.add_argument("--name", required=True)
    prompt = dispatch.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    dispatch.add_argument("--access", choices=("read", "docs", "builder"), default="read")
    dispatch.add_argument("--write-paths", action="append")
    dispatch.add_argument("--families", action="append")
    dispatch.add_argument("--cwd", type=Path)

    commit = commands.add_parser("commit-main", help="commit orchestrator edits to main under the integration mutex")
    commit.add_argument("-m", "--message", required=True)
    commit.add_argument("paths", nargs="+")

    gate = commands.add_parser("gate", help="re-run a milestone's Verify commands on a clean build")
    gate.add_argument("--milestone", required=True)
    gate.add_argument("--skip-clean", action="store_true")

    news = commands.add_parser("whats-new", help="WPs merged into main lately, with how to try each")
    news.add_argument("--since", type=datetime.fromisoformat, help="ISO date or time, e.g. 2026-10-23 or 2026-10-23T18:00")
    news.add_argument("--last", type=int, help="only the N most recent merges")

    progress = commands.add_parser("log", help="(in a WP session) append a dated Progress log line, optionally ticking a step")
    progress.add_argument("--step", type=int, help="tick this step's checkbox (1 is the first step)")
    progress.add_argument("message", help="what was done; what is next; blockers")
    commands.add_parser("verify", help="(in a WP session) apply the fixers, run the WP's Verify commands and owns check; print what failed")
    try_it_cmd = commands.add_parser("try-it", help="(in a WP session) record the '## Try it' instructions (text, or - to read from stdin)")
    try_it_cmd.add_argument("text")
    block_cmd = commands.add_parser("block", help="(in a WP session) mark the work item blocked and log why")
    block_cmd.add_argument("reason")
    handoff = commands.add_parser("handoff", help="(in a WP builder session) hand the work to a fresh session: the handoff text, or - to read it from stdin")
    handoff.add_argument("text")
    finding = commands.add_parser("finding", help="(in a WP session) answer an open BLOCKING review finding")
    finding_commands = finding.add_subparsers(dest="finding_command", required=True)
    fixed = finding_commands.add_parser("fixed", help="the finding is fixed")
    fixed.add_argument("finding_id", metavar="id")
    fixed.add_argument("text", help="what changed, with file:line")
    veto = finding_commands.add_parser("veto", help="the finding breaks the review protocol; name the ground")
    veto.add_argument("finding_id", metavar="id")
    veto.add_argument("ground", help="no-basis, misquoted, outside-wp, not-reproduced or contradicts-wp")
    veto.add_argument("text", help="the evidence")
    ask = commands.add_parser("ask-approval", help="(in a WP session) ask the orchestrator to approve a command the sandbox denies")
    ask.add_argument("approval_command", metavar="command", help="the exact command")
    ask.add_argument("why", help="why the work needs it")
    run_approved = commands.add_parser("run-approved", help="(in a WP session) run an approved command once")
    run_approved.add_argument("approval_id", metavar="id")
    approve = commands.add_parser("approve", help="approve a session's command request; the session is told")
    approve.add_argument("approval_id", metavar="id")
    approve.add_argument("--note", default="", help="a note for the session")
    deny = commands.add_parser("deny", help="deny a session's command request; the session is told")
    deny.add_argument("approval_id", metavar="id")
    deny.add_argument("--reason", required=True)
    lint = commands.add_parser("lint", help="check WP files before scheduling: sections, Verify, owns, overlaps, dependencies, similar WPs")
    lint.add_argument("wps", nargs="*", help="WP ids (default: every open WP: todo, in flight or blocked)")
    lint.add_argument("--all", action="store_true", dest="include_done", help="every WP, done ones included")
    lint.add_argument("--json", action="store_true", help="print the findings and counts as JSON")
    audit = commands.add_parser("audit", help="lint every open WP into <runs>/audits/<date>.md, then a review session for the judgment parts")
    audit_mode = audit.add_mutually_exclusive_group()
    audit_mode.add_argument("--mechanical-only", action="store_true", help="only the lint report: no review session, no event")
    audit_mode.add_argument("--install-schedule", action="store_true", help="standalone: register the weekly Windows task or Linux user timer; T3: show native scheduling guidance")
    audit_mode.add_argument("--remove-schedule", action="store_true", help="standalone: remove this project's owned OS schedule; T3: show native removal guidance")
    audit.add_argument("--show-only", action="store_true", help="with a schedule action: print the actual platform plan without writes or service commands")
    audit.add_argument("--from-os-schedule", action="store_true", help=argparse.SUPPRESS)

    show = commands.add_parser("show", help="render session logs as readable HTML conversations")
    show.add_argument("log", nargs="?", type=Path, help="one session or verification log")
    show.add_argument("--wp", help="every log of this WP, with an index")
    show.add_argument("--open", action="store_true", help="open the page in the browser")

    message = commands.add_parser("message", help="send a message to the session running for a WP, between its turns")
    message.add_argument("--wp", help="the WP whose running session receives it")
    message.add_argument("--lane", help="the WP the lane's supervisor is running now")
    message.add_argument("text")
    wrap_up = commands.add_parser("wrap-up", help="ask running sessions to log their progress and stop; their rounds are not counted")
    wrap_up.add_argument("--wp", action="append", default=[], help="a WP to pause after its current session (repeatable)")
    wrap_up.add_argument("--lane", action="append", default=[], help="hold the lane and pause the WP it runs (repeatable)")
    wrap_up.add_argument("--reason", default="the orchestrator paused it")

    usage = commands.add_parser("usage", help="token, cache and time reports from the session logs, and the effect of tooling changes")
    usage_commands = usage.add_subparsers(dest="usage_command", required=True)
    usage_report = usage_commands.add_parser("report", help="sessions grouped by family, role, route, WP or day")
    usage_report.add_argument("--since", type=datetime.fromisoformat, help="ISO date or time (default: 7 days ago)")
    usage_report.add_argument("--until", type=datetime.fromisoformat)
    usage_report.add_argument("--by", default="family,role", help="comma-separated: family, role, route, wp, day")
    usage_changes = usage_commands.add_parser("changes", help="each tooling change with the sessions that ran under it")
    usage_changes.add_argument("--since", type=datetime.fromisoformat, help="ISO date or time (default: 7 days ago)")
    usage_changes.add_argument("--by", default="family,role")
    usage_changes.add_argument("--merge-minutes", type=int, default=120, help="commits this close together count as one change")

    dashboard = commands.add_parser("dashboard", help="the live progress dashboard: print its URL, starting it when it is not running")
    dashboard_mode = dashboard.add_mutually_exclusive_group()
    dashboard_mode.add_argument("--stop", action="store_true", help="stop it; no wpo command starts it again until `wpo dashboard`")
    # The server itself, and the short-lived process that starts it detached (dashboard.py).
    dashboard_mode.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    dashboard_mode.add_argument("--detach-server", action="store_true", help=argparse.SUPPRESS)

    place = commands.add_parser("place-request", help="(started by the dashboard) run the placement agent on a queued user request")
    place.add_argument("--id", required=True, dest="placement_id", help="the placement id under <runs>/placements/")

    gh_cmd = commands.add_parser("github", help="GitHub tracker sync, status and PR delivery")
    gh_sub = gh_cmd.add_subparsers(dest="github_command", required=True)
    gh_sync = gh_sub.add_parser("sync", help="refresh GitHub issues, board and PR cache, and reconcile merged/failed PRs")
    gh_sync.add_argument("--json", action="store_true")
    gh_status = gh_sub.add_parser("status", help="show GitHub tracker, board, auto-merge and open PR status")
    gh_status.add_argument("--json", action="store_true")
    gh_pr = gh_sub.add_parser("pr", help="open or update a sanitized PR (with auto-merge if configured) for a work item")
    gh_pr.add_argument("--wp", required=True)
    gh_pr.add_argument("--verify", action="store_true", help="force workspace verification before pushing")

    migrate_cmd = commands.add_parser("migrate", help="deterministic migration from legacy WP files to GitHub Issues + Projects v2 + split private ledger")
    migrate_sub = migrate_cmd.add_subparsers(dest="migrate_command", required=True)
    mig_gh = migrate_sub.add_parser("github", help="plan (default dry-run) or apply deterministic migration from WP files to GitHub")
    mig_gh.add_argument("--apply", action="store_true", help="execute the migration (default is a dry-run plan)")
    mig_gh.add_argument("--dry-run", action="store_true", help="explicitly preview the migration plan without writing")
    mig_gh.add_argument("--repo", help="GitHub repository (owner/name); defaults to git remote origin")
    mig_gh.add_argument("--project-number", type=int, help="existing GitHub Project v2 number to use")
    mig_gh.add_argument("--project-owner", help="GitHub Project owner login or @me")
    mig_gh.add_argument("--project-title", help="title for the GitHub Project v2 (default: project name)")
    mig_gh.add_argument("--no-create-project", action="store_true", help="do not create a GitHub Project v2 if none is specified")
    ledger_loc = mig_gh.add_mutually_exclusive_group()
    ledger_loc.add_argument("--external-ledger", action="store_true", default=True, help="move private transcripts and run state outside the git repo (default)")
    ledger_loc.add_argument("--keep-repo-ledger", action="store_false", dest="external_ledger", help="keep runs_dir inside the repository checkout")
    mig_gh.add_argument("--open-only", action="store_true", help="migrate only open/blocked WPs, skipping WPs already merged on main")
    mig_gh.add_argument("--remove-wp-files", action="store_true", help="delete migrated WP-*.md files from wp_dir after creating issues")
    mig_gh.add_argument("--enable-auto-merge", action="store_true", help="enable GitHub repository allow_auto_merge if currently off")
    mig_gh.add_argument("--allow-local-paths", action="store_true", help="allow personal filesystem paths in migrated issue text (secrets remain blocked)")
    mig_gh.add_argument("--json", action="store_true", help="print plan/result as JSON")

    ledger_cmd = commands.add_parser("ledger", help="inspect and maintain the private local ledger (transcripts, overrides, retention)")
    ledger_sub = ledger_cmd.add_subparsers(dest="ledger_command", required=True)
    ledger_sub.add_parser("path", help="print the resolved private ledger directory")
    ledger_status = ledger_sub.add_parser("status", help="summarize private transcripts, usage index and per-item state")
    ledger_status.add_argument("--wp", help="inspect only one work item")
    ledger_status.add_argument("--json", action="store_true")
    ledger_prune = ledger_sub.add_parser("prune", help="index usage then prune raw transcripts older than N days for settled items (dry-run by default)")
    ledger_prune.add_argument("--older-than-days", type=float, required=True, help="prune raw transcripts older than this many days")
    ledger_prune.add_argument("--apply", action="store_true", help="remove the candidate files (default is a dry run)")
    ledger_prune.add_argument("--include-open", action="store_true", help="also consider open work items")
    ledger_prune.add_argument("--json", action="store_true")
    ledger_override = ledger_sub.add_parser("override", help="set or clear private local execution overrides for an issue without modifying GitHub")
    ledger_override.add_argument("wp", help="work item id (e.g. GH-42 or 42)")
    ledger_override.add_argument("--builder", help="local builder tier or pin:<route>")
    ledger_override.add_argument("--reviewer", help="local reviewer tier or none")
    ledger_override.add_argument("--lane", help="local lane assignment")
    ledger_override.add_argument("--owns", action="append", help="local owns glob(s), comma-separated or repeatable")
    ledger_override.add_argument("--verify", action="append", help="local Verify command (repeatable)")
    ledger_override.add_argument("--delegate", help="local delegate tier or none")
    ledger_override.add_argument("--mode", choices=("single", "staged"), help="local execution mode")
    ledger_override.add_argument("--skills", action="append", help="local skills list, comma-separated or repeatable")
    ledger_override.add_argument("--clear", action="append", default=[], help="override key(s) to clear")

    commands.add_parser("install-shim", help="put the `wpo` command on PATH")
    init = commands.add_parser("init", help="add .orchestration/ to a repository (GitHub + external private ledger by default)")
    init.add_argument("--name", required=True)
    init_tracker = init.add_mutually_exclusive_group()
    init_tracker.add_argument("--github", action="store_true", dest="use_github", default=True,
                              help="configure GitHub Issues + auto-merge PR delivery + external private ledger (default)")
    init_tracker.add_argument("--wp-files", action="store_false", dest="use_github",
                              help="use legacy committed WP-*.md files instead of GitHub Issues")
    init.add_argument("--repo", help="GitHub repository (owner/name); defaults to git remote origin")
    init.add_argument("--project-number", type=int, help="GitHub Project v2 number")
    init.add_argument("--project-owner", help="GitHub Project owner login or @me")
    init_ledger = init.add_mutually_exclusive_group()
    init_ledger.add_argument("--external-ledger", action="store_true", dest="external_ledger", default=None,
                             help="store private transcripts/runs outside the git repository (default for GitHub)")
    init_ledger.add_argument("--keep-repo-ledger", action="store_false", dest="external_ledger",
                             help="store runs under .orchestration/runs/ inside the checkout (default for --wp-files)")
    return parser


def install_shim() -> int:
    from .machine import MachineConfig
    machine = MachineConfig.load()
    directory = machine.shim_dir
    directory.mkdir(parents=True, exist_ok=True)
    python, entry = Path(machine.python).as_posix(), ENTRY_POINT.as_posix()
    shell_python = f"/{python[0].lower()}{python[2:]}" if len(python) > 1 and python[1] == ":" else python
    shell_entry = f"/{entry[0].lower()}{entry[2:]}" if len(entry) > 1 and entry[1] == ":" else entry
    (directory / "wpo").write_text(f'#!/bin/sh\nexec "{shell_python}" -I -B "{shell_entry}" "$@"\n', encoding="utf-8", newline="\n")
    if sys.platform == "win32":
        (directory / "wpo.cmd").write_text(f'@"{machine.python}" -I -B "{ENTRY_POINT}" %*\r\n', encoding="utf-8", newline="")
    else:
        (directory / "wpo").chmod(0o755)
    print(f"installed wpo in {directory}")
    return 0


def init_project(start: Path, name: str, *, use_github: bool = True, repo: str | None = None,
                 project_number: int | None = None, project_owner: str | None = None,
                 external_ledger: bool | None = None) -> int:
    from .project import CONFIG_RELATIVE_PATH, git, main_checkout_of
    root = main_checkout_of(start.resolve())
    config_path = root / CONFIG_RELATIVE_PATH
    if config_path.exists():
        print(f"{config_path} exists; nothing done")
        return 1
    config_path.parent.mkdir(parents=True, exist_ok=True)
    template = json.loads((SKILL_ROOT / "templates" / "config.example.json").read_text(encoding="utf-8"))
    template["name"] = name
    use_external = use_github if external_ledger is None else external_ledger
    template["runs_dir"] = "external" if use_external else ".orchestration/runs"
    if use_github:
        from .github import parse_github_repo
        detected_repo = repo
        if not detected_repo:
            remote_res = git(root, "remote", "get-url", "origin", check=False)
            if remote_res.returncode == 0:
                detected_repo = parse_github_repo(remote_res.stdout)
        template["tracker"] = "github"
        template["delivery"] = "pr-automerge"
        gh_cfg = dict(template.get("github") or {})
        gh_cfg["repo"] = detected_repo
        if project_number is not None:
            gh_cfg["project_number"] = project_number
        if project_owner is not None:
            gh_cfg["project_owner"] = project_owner
        template["github"] = gh_cfg
    else:
        template["tracker"] = "wp-files"
        template["delivery"] = "auto"
    config_path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [line for line in (".orchestration/runs/", ".orchestration/tasks/") if line not in existing.splitlines()]
    if missing:
        gitignore.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + "\n".join(["# wp-orchestration run state", *missing]) + "\n", encoding="utf-8")
    print(f"wrote {config_path}; edit it, then commit it with .gitignore")
    return 0


def messaging(context, arguments) -> int:
    """`wpo message` and `wpo wrap-up`: inbox messages to running sessions, pause requests, lane holds."""
    from .inbox import (
        WRAP_UP_MESSAGE,
        deliver_if_live,
        lane_running_wp,
        live_session,
        post_message,
        request_pause,
    )
    from .lanes import hold
    runs = context.project.runs_dir
    if arguments.command == "message":
        if bool(arguments.wp) == bool(arguments.lane):
            print("give exactly one of --wp and --lane")
            return 2
        wp_id = context.normalize_id(arguments.wp) if arguments.wp else lane_running_wp(runs, arguments.lane)
        if wp_id is None:
            print(f"lane {arguments.lane} is running no WP now")
            return 1
        if not deliver_if_live(runs / wp_id, arguments.text):
            print(f"{wp_id}: no session that reads messages is running (it started before messaging existed, or runs on "
                  "OpenCode or agy); nothing sent")
            return 1
        print(f"{wp_id}: queued; the session reads it at its next turn (within about 15 s of it)")
        return 0
    targets = [context.normalize_id(wp_id) for wp_id in arguments.wp]
    for lane in arguments.lane:
        hold(context, lane, f"wrap-up: {arguments.reason}")
        running = lane_running_wp(runs, lane)
        if running is None:
            print(f"{lane}: running no WP; held")
        else:
            targets.append(running)
    if not targets and not arguments.lane:
        print("give --wp or --lane")
        return 2
    for wp_id in dict.fromkeys(targets):
        request_pause(runs / wp_id, arguments.reason)
        if live_session(runs / wp_id) is not None:
            post_message(runs / wp_id, WRAP_UP_MESSAGE)
            print(f"{wp_id}: asked to log its progress and stop; the run pauses when the session ends")
        else:
            print(f"{wp_id}: its session cannot receive messages; the run pauses when that session ends (or before the next one starts)")
    return 0


def dashboard_command(context, arguments) -> int:
    """`wpo dashboard [--stop]`, and the detached server processes it starts."""
    from . import dashboard
    project, machine = context.project, context.machine
    if arguments.serve:
        from .dashboard_server import serve
        return serve(project, machine)
    if arguments.detach_server:
        return dashboard.detach_server(project, machine)
    try:
        if arguments.stop:
            print(dashboard.stop(project))
            return 0
        started, message = dashboard.start_and_report(project, machine)
    except dashboard.DashboardError as error:
        print(error)
        return 1
    print(message)
    return 0 if started else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    arguments = build_parser().parse_args(argv)
    if arguments.command == "install-shim":
        return install_shim()
    if arguments.command == "init":
        return init_project(
            arguments.project or Path.cwd(),
            arguments.name,
            use_github=arguments.use_github,
            repo=arguments.repo,
            project_number=arguments.project_number,
            project_owner=arguments.project_owner,
            external_ledger=arguments.external_ledger,
        )

    from .machine import MachineConfig
    machine = MachineConfig.load()

    if arguments.command == "audit" and arguments.from_os_schedule and machine.execution_mode != "standalone":
        print("audit: this OS schedule requires execution_mode=standalone; replace it with native T3 scheduling", file=sys.stderr)
        return 2

    if arguments.command == "audit" and (arguments.install_schedule or arguments.remove_schedule or arguments.show_only):
        # Schedule planning must not instantiate router/run state or autostart a dashboard.
        from types import SimpleNamespace

        from .audit import AuditError, change_schedule
        from .project import Project
        if arguments.show_only and not (arguments.install_schedule or arguments.remove_schedule):
            print("--show-only goes with --install-schedule or --remove-schedule", file=sys.stderr)
            return 2
        if arguments.session:
            print("schedule administration cannot run inside a delegated session", file=sys.stderr)
            return 2
        project = Project.locate(arguments.project, machine.raw.get("project_defaults"), machine.execution_mode, data_dir=getattr(machine, "data_dir", None))
        try:
            return change_schedule(SimpleNamespace(project=project, machine=machine), arguments.install_schedule, arguments.show_only)
        except (AuditError, OSError) as error:
            print(f"audit: {error}", file=sys.stderr)
            return 2

    from .context import Context
    context = Context.load(arguments.project, machine)
    if arguments.session:
        from .t3 import Bridge, T3Error, read_json
        bridge = Bridge(context)
        identity = read_json(bridge.directory / "sessions" / f"{bridge.request_path(arguments.session).stem}.json")
        if identity is None or Path(identity["project"]).resolve() != context.project.root:
            raise T3Error("Unknown T3 session identity for this project")
        os.environ.update(identity["env"])

    if arguments.command == "t3":
        from .t3 import cli_command
        return cli_command(context, arguments)

    if arguments.command == "continuous":
        from .continuous import cli_command
        try:
            return cli_command(context, arguments)
        except (TypeError, ValueError) as error:
            print(f"continuous: {error}", file=sys.stderr)
            return 2

    if arguments.command == "dashboard":
        return dashboard_command(context, arguments)
    if arguments.command == "clean-builds":
        from .artifacts import clean_merged
        from .project import ProjectError
        try:
            print(json.dumps(clean_merged(context, [context.normalize_id(wp_id) for wp_id in arguments.wp], arguments.apply), indent=2))
        except (ProjectError, OSError, subprocess.CalledProcessError) as error:
            print(f"Build cleanup refused: {error}", file=sys.stderr)
            return 2
        return 0
    from .dashboard import autostart
    autostart(context.project, context.machine)

    if arguments.command == "whats-new":
        from .whats_new import whats_new
        since = arguments.since if arguments.since is not None or arguments.last is not None else datetime.now() - timedelta(days=1)
        print(whats_new(context.project, since, arguments.last))
        return 0

    if arguments.command in ("log", "verify", "handoff", "try-it", "block"):
        from .session_commands import (
            SessionCommandError,
            block,
            handoff,
            log,
            try_it,
            verify,
        )
        try:
            if arguments.command == "log":
                print(log(context, arguments.message, arguments.step))
                return 0
            if arguments.command == "try-it":
                print(try_it(context, sys.stdin.read() if arguments.text == "-" else arguments.text))
                return 0
            if arguments.command == "block":
                print(block(context, arguments.reason))
                return 0
            if arguments.command == "handoff":
                print(handoff(context, sys.stdin.read() if arguments.text == "-" else arguments.text))
                return 0
            passed, report = verify(context)
        except SessionCommandError as error:
            print(error)
            return 2
        print(report)
        print("Verify passed." if passed else "Verify failed.")
        return 0 if passed else 1

    if arguments.command == "github":
        from .github import GhError, reconcile_open_prs
        if not context.project.is_github or context.tracker is None:
            print("github: this project is using tracker='wp-files'; run `wpo migrate github` or set tracker='github' in .orchestration/config.json", file=sys.stderr)
            return 2
        try:
            if arguments.github_command == "sync":
                result = reconcile_open_prs(context, force_sync=True)
                context.journal.emit(
                    "github-synced",
                    f"synced GitHub ({len(result['open'])} open PR(s), {len(result['merged'])} newly merged, {len(result['failed_checks'])} check failure(s))",
                )
                if arguments.json:
                    print(json.dumps(result, indent=2))
                else:
                    print(f"Synced GitHub: {len(result['open'])} open PR(s), {len(result['merged'])} newly merged, {len(result['failed_checks'])} check failure(s).")
                return 0
            if arguments.github_command == "status":
                report = context.tracker.status_report()
                if arguments.json:
                    print(json.dumps(report, indent=2))
                else:
                    print(f"Repo: {report['repo'] or '(unset)'} | Delivery: {report['delivery']} | Auto-merge enabled: {report['allow_auto_merge']}")
                    proj = report.get("project") or {}
                    if proj:
                        print(f"Project: #{proj.get('number')} {proj.get('title')} ({proj.get('url')})")
                    print(f"Private ledger: {report['runs_dir']} ({'external' if report['external_ledger'] else 'in-repo'})")
                    age = f"{report['cache_age_seconds']:.0f}s ago" if report["cache_age_seconds"] is not None else "never synced"
                    print(f"Cache: {report['issues_cached']} issues ({age}) | Open PRs: {len(report['open_prs'])}")
                    for pr in report["open_prs"]:
                        print(f"  - {pr['wp']} -> PR #{pr.get('number')} ({pr.get('url')}) auto_merge={pr.get('auto_merge')} checks={pr.get('check_state') or 'unknown'}")
                return 0
            from .integrate import EXIT_MEANINGS, integrate
            wp_id = context.normalize_id(arguments.wp)
            code = integrate(context, wp_id, arguments.verify)
            print(f"{wp_id}: {EXIT_MEANINGS.get(code, 'failed')} (exit {code})")
            return code
        except GhError as error:
            print(f"github: {error}", file=sys.stderr)
            return 2

    if arguments.command == "migrate":
        from .github import GhError
        from .migrate import MigrationError, MigrationOptions, migrate_to_github
        from .privacy import PrivacyError
        options = MigrationOptions(
            apply=bool(arguments.apply and not arguments.dry_run),
            repo=arguments.repo,
            project_number=arguments.project_number,
            project_owner=arguments.project_owner,
            project_title=arguments.project_title,
            create_project=not arguments.no_create_project,
            external_ledger=arguments.external_ledger,
            include_done=not arguments.open_only,
            remove_wp_files=arguments.remove_wp_files,
            enable_auto_merge=arguments.enable_auto_merge,
            allow_local_paths=arguments.allow_local_paths,
        )
        try:
            report = migrate_to_github(context, options)
        except (MigrationError, GhError, PrivacyError) as error:
            print(f"migrate: {error}", file=sys.stderr)
            return 2
        if arguments.json:
            print(json.dumps(report, indent=2))
            return 0 if not report.get("privacy_violations") else 1
        if not report["applied"]:
            print(f"Migration plan (dry run — pass --apply to execute) for {report['repo']}:")
            print(f"  Work packages: {report['total_wps']} ({report['open_wps']} open, {report['done_wps']} done on main)")
            print(f"  Milestones: {', '.join(report['milestones']) or 'none'}")
            print(f"  Lanes: {', '.join(report['lanes']) or 'none'}")
            print(f"  Private ledger destination: {report['destination_runs_dir']} ({'external' if report['external_ledger'] else 'in-repo'})")
            if report["in_flight_worktrees"]:
                print(f"  In-flight worktrees preserved: {', '.join(report['in_flight_worktrees'])}")
            if report["uncommitted_drafts"]:
                print(f"  Uncommitted drafts skipped (commit first if needed): {', '.join(report['uncommitted_drafts'])}")
            if report["privacy_violations"]:
                print("  PRIVACY VIOLATIONS (must resolve before --apply):")
                for wp_id, issues in sorted(report["privacy_violations"].items()):
                    print(f"    - {wp_id}: {'; '.join(issues)}")
                return 1
            return 0
        print(f"Migrated {report['total_wps']} work package(s) to GitHub {report['repo']}:")
        if report.get("project_number"):
            print(f"  Project v2: #{report['project_number']} {report.get('project_url') or ''}")
        print(f"  Dependencies wired (native blocked-by): {report['dependencies_wired']}")
        print(f"  Closed merged issues: {len(report['closed_issues'])}")
        print(f"  Private ledger: {report['destination_runs_dir']} ({len(report['migrated_ledgers'])} item(s))")
        print(f"  Config updated: {report['config_updated']}")
        if not report.get("allow_auto_merge"):
            print(f"  Note: repository auto-merge is currently disabled. Run `gh repo edit {report['repo']} --enable-auto-merge` (or re-run with --enable-auto-merge).")
        for legacy_id, gh_id in report.get("id_map", {}).items():
            print(f"  {legacy_id} -> {gh_id}")
        return 0

    if arguments.command == "ledger":
        from . import run_state
        if arguments.ledger_command == "path":
            print(context.project.runs_dir)
            return 0
        if arguments.ledger_command == "status":
            target_wp = context.normalize_id(arguments.wp) if arguments.wp else None
            summary = run_state.ledger_summary(context.project, target_wp)
            if arguments.json:
                print(json.dumps(summary, indent=2))
            else:
                print(f"Ledger: {summary['runs_dir']} ({'external' if summary['external'] else 'in-repo'})")
                print(f"Tracker: {summary['tracker']} | Delivery: {summary['delivery']} | Indexed sessions: {summary['indexed_sessions']}")
                print(f"Raw transcripts: {summary['transcript_files']} file(s), {summary['transcript_bytes']:,} bytes across {len(summary['items'])} item(s)")
                for item in summary["items"]:
                    extra = f" (legacy {item['legacy_wp_id']})" if item.get("legacy_wp_id") else ""
                    ovr = f" overrides={sorted(item['overrides'])}" if item.get("overrides") else ""
                    print(f"  - {item['id']}{extra}: status={item.get('status') or '-'}, transcripts={item['transcript_files']} ({item['transcript_bytes']:,} B){ovr}")
            return 0
        if arguments.ledger_command == "prune":
            report = run_state.prune_transcripts(context, arguments.older_than_days, apply=arguments.apply, include_open=arguments.include_open)
            if arguments.json:
                print(json.dumps(report, indent=2))
            elif arguments.apply:
                print(f"Pruned {report['removed_files']} raw transcript file(s) ({report['removed_bytes']:,} bytes); usage index preserved.")
            else:
                print(f"Dry run (pass --apply to remove): {report['candidate_files']} raw transcript file(s) ({report['candidate_bytes']:,} bytes) older than {arguments.older_than_days:g} days.")
            return 0
        wp_id = context.normalize_id(arguments.wp)
        run_dir = context.project.wp_run_dir(wp_id)
        try:
            for key in arguments.clear:
                run_state.set_override(run_dir, key, None)
            if arguments.builder is not None:
                run_state.set_override(run_dir, "builder", arguments.builder)
            if arguments.reviewer is not None:
                run_state.set_override(run_dir, "reviewer", arguments.reviewer)
            if arguments.lane is not None:
                run_state.set_override(run_dir, "lane", arguments.lane)
            if arguments.owns is not None:
                run_state.set_override(run_dir, "owns", split_list(arguments.owns))
            if arguments.verify is not None:
                run_state.set_override(run_dir, "verify", [cmd.strip() for cmd in arguments.verify if cmd.strip()])
            if arguments.delegate is not None:
                run_state.set_override(run_dir, "delegate", arguments.delegate)
            if arguments.mode is not None:
                run_state.set_override(run_dir, "mode", arguments.mode)
            if arguments.skills is not None:
                run_state.set_override(run_dir, "skills", split_list(arguments.skills))
        except ValueError as error:
            print(f"ledger override: {error}", file=sys.stderr)
            return 2
        current = run_state.load(run_dir).get("overrides") or {}
        print(json.dumps({"wp": wp_id, "overrides": current}, indent=2))
        return 0

    if arguments.command == "finding":
        from .findings import answer
        from .session_commands import SessionCommandError
        try:
            print(answer(context, arguments.finding_id, arguments.finding_command, getattr(arguments, "ground", None), arguments.text))
        except SessionCommandError as error:
            print(error)
            return 2
        return 0

    if arguments.command in ("ask-approval", "run-approved", "approve", "deny"):
        from .approvals import ApprovalError, decide, request, run_approved
        from .session_commands import SessionCommandError
        try:
            if arguments.command == "ask-approval":
                approval = request(context, arguments.approval_command, arguments.why)
                print(f"Request {approval['id']} sent. Carry on with other work; the answer arrives as a message.")
                return 0
            if arguments.command == "run-approved":
                return run_approved(context, arguments.approval_id)
            print(decide(context, arguments.approval_id, arguments.command == "approve",
                         arguments.note if arguments.command == "approve" else arguments.reason))
            return 0
        except (ApprovalError, SessionCommandError) as error:
            print(error)
            return 2

    if arguments.command == "lint":
        from .lint import LintConfigError, findings_json, lint, summary_lines
        try:
            findings = lint(context, arguments.wps, arguments.include_done)
        except LintConfigError as error:
            print(f"lint settings: {error}", file=sys.stderr)
            return 2
        if arguments.json:
            print(json.dumps(findings_json(findings), indent=2))
        else:
            for finding in findings:
                print(finding)
            for line in summary_lines(findings):
                print(line)
        return 1 if any(finding.severity == "error" for finding in findings) else 0

    if arguments.command == "audit":
        from .audit import AuditError, run_audit
        try:
            return run_audit(context, arguments.mechanical_only)
        except AuditError as error:
            print(f"audit: {error}", file=sys.stderr)
            return 2

    if arguments.command == "show":
        from .transcript import render_one, render_wp
        if bool(arguments.log) == bool(arguments.wp):
            print("give a log path or --wp")
            return 2
        wp_id = context.normalize_id(arguments.wp) if arguments.wp else None
        target = render_wp(context.project.runs_dir / wp_id) if wp_id else render_one(arguments.log.resolve())
        print(target)
        if arguments.open:
            import webbrowser
            webbrowser.open(target.as_uri())
        return 0

    if arguments.command in ("message", "wrap-up"):
        return messaging(context, arguments)

    if arguments.command == "usage":
        from .usage import changes_report, report, write_report
        since = arguments.since or datetime.now() - timedelta(days=7)
        by = tuple(name.strip() for name in arguments.by.split(",") if name.strip())
        if arguments.usage_command == "report":
            text = report(context.project, since, arguments.until, by)
        else:
            text = changes_report(context.project, since, by, timedelta(minutes=arguments.merge_minutes))
        path = write_report(context.project, arguments.usage_command, text)
        print(text)
        print(f"written to {path}")
        return 0

    if arguments.command == "route":
        from .routing import RoutingError, project_mode, routing_modes, set_project_mode
        router = context.router
        try:
            if arguments.route_command == "candidates":
                from .routing import with_mode
                # As the project's next session would rank it; outside a project, as standard.
                spec = with_mode(arguments.spec, project_mode(context.project.runs_dir)) if context.project else arguments.spec
                print(json.dumps(asdict(router.select(spec, split_list(arguments.families) or None, arguments.refresh,
                                                      free_last_resort=not arguments.gate, gate=arguments.gate)), indent=2))
            elif arguments.route_command == "status":
                print(router.status_text())
            elif arguments.route_command == "refresh":
                catalog = router.load_catalog(force_refresh=arguments.force)
                print(json.dumps({key: catalog[key] for key in ("refreshed_at", "unrated", "errors")} | {"routes": len(catalog["routes"])}, indent=2))
            elif arguments.route_command == "mark":
                print(json.dumps(router.mark(arguments.route, arguments.outcome, arguments.detail)))
            elif arguments.route_command == "mode":
                if context.project is None:
                    raise RoutingError("a routing mode belongs to a project: run wpo route mode inside one, or pass --project")
                runs_dir, modes = context.project.runs_dir, routing_modes(router.policy)
                if arguments.mode is None:
                    current = project_mode(runs_dir)
                    unconfigured = "" if current in modes else ", not configured: routes as standard"
                    print(f"{current} (modes: {', '.join(modes)}{unconfigured})")
                    return 0
                if arguments.mode not in modes:
                    raise RoutingError(f"unknown routing mode '{arguments.mode}' (modes: {', '.join(modes)})")
                previous = project_mode(runs_dir)
                set_project_mode(runs_dir, arguments.mode)
                context.journal.emit("routing-mode", f"routing mode {previous} -> {arguments.mode}; WPs that already ran keep theirs")
                print(arguments.mode)
            else:
                print(f"cleared: {', '.join(router.clear_cooldowns(arguments.prefix)) or 'nothing'}")
        except RoutingError as error:
            print(f"routing error: {error}", file=sys.stderr)
            return 2
        return 0

    if arguments.command == "events":
        from .events import format_event, print_events
        journal = context.journal
        if arguments.events_command == "emit":
            fields = dict(pair.split("=", 1) for pair in arguments.field)
            print(format_event(journal.emit(arguments.kind, arguments.message, fields)))
            return 0
        if arguments.events_command == "wait":
            return journal.wait(arguments.after, arguments.timeout_minutes, split_list(arguments.quiet))
        if arguments.events_command == "tail":
            events = journal.read(arguments.after)
            print_events(events)
            print(f"cursor={arguments.after + len(events)}")
            return 0
        print(journal.cursor())
        return 0

    if arguments.command == "run":
        from .lanes import write_board
        from .runner import RunOptions, run_wp
        wp_id = context.normalize_id(arguments.wp)
        tree = arguments.tree or context.project.worktree(wp_id)
        if machine.execution_mode == "t3" and arguments.tree is None:
            from .t3 import Bridge
            tree = Bridge(context).ensure_workspace(wp_id)
        options = RunOptions(arguments.builder, arguments.reviewer, tuple(split_list(arguments.families)), arguments.idle_minutes, arguments.lane)
        code = run_wp(context, wp_id, tree.resolve(), options)
        write_board(context)
        return code

    if arguments.command == "delegate":
        from .delegate import DelegateError, Delegation
        try:
            delegation = Delegation(context)
            if arguments.delegate_command == "start":
                delegation.start(arguments.name, arguments.task, arguments.prompt_file, arguments.write_paths, arguments.tier)
            elif arguments.delegate_command == "wait":
                delegation.wait(arguments.name, arguments.minutes)
            elif arguments.delegate_command == "list":
                delegation.list()
            else:
                delegation.run_worker(arguments.name, arguments.token)
        except DelegateError as error:
            print(f"delegate error: {error}", file=sys.stderr)
            return 2
        return 0

    if arguments.command == "supervise":
        from .supervisor import Supervisor
        options = {"families": split_list(arguments.families), "builder_override": arguments.builder, "idle_minutes": arguments.idle_minutes}
        name = arguments.lane or arguments.milestone
        Supervisor(context, name, bool(arguments.lane), split_list(arguments.wps), options).run()
        return 0

    if arguments.command == "integrate":
        from .integrate import EXIT_MEANINGS, integrate
        from .lanes import write_board
        wp_id = context.normalize_id(arguments.wp)
        code = integrate(context, wp_id, arguments.verify)
        print(f"{wp_id}: {EXIT_MEANINGS.get(code, 'failed')} (exit {code})")
        write_board(context)
        return code

    if arguments.command == "lanes":
        from . import lanes
        if arguments.lanes_command == "resume":
            lanes.resume(context, arguments.dry_run, arguments.targets)
        elif arguments.lanes_command == "stop":
            lanes.stop(context, arguments.lane)
        elif arguments.lanes_command == "hold":
            lanes.hold(context, arguments.lane, arguments.reason)
        else:
            lanes.release(context, arguments.lane)
        return 0

    if arguments.command == "status":
        from .lanes import board, write_board
        print("\n".join(board(context)))
        written = write_board(context)
        if written:
            print(f"\nwrote {written}")
        return 0

    if arguments.command == "job":
        from .orchestrator import exec_job, start_job
        command = arguments.job_arguments[1:] if arguments.job_arguments[:1] == ["--"] else arguments.job_arguments
        if not command:
            print("job: give the command after --", file=sys.stderr)
            return 2
        if arguments.job_command == "start":
            start_job(context, arguments.name, command)
            return 0
        return exec_job(context, arguments.name, arguments.log, command)

    if arguments.command == "dispatch":
        from .orchestrator import dispatch
        prompt = arguments.prompt if arguments.prompt is not None else arguments.prompt_file.read_text(encoding="utf-8")
        return dispatch(context, arguments.tier, arguments.name, prompt, arguments.access, split_list(arguments.write_paths),
                        split_list(arguments.families), arguments.cwd)

    if arguments.command == "commit-main":
        from .orchestrator import commit_main
        return commit_main(context, arguments.message, arguments.paths)

    if arguments.command == "place-request":
        from .user_actions import UserActionError, place_request
        try:
            return place_request(context, arguments.placement_id)
        except UserActionError as error:
            print(error)
            return 2

    if arguments.command == "gate":
        from .orchestrator import gate_verify
        return gate_verify(context, arguments.milestone, arguments.skip_clean)
    raise AssertionError(f"unhandled command {arguments.command}")
