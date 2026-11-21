"""Offline unit and integration tests for split-state GitHub tracking, auto-merge PR delivery,
outbound privacy firewall, external private ledger pruning, and deterministic WP -> GitHub migration."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from wpo import (
    cli,
    github,
    lint,
    migrate,
    privacy,
    run_state,
    session_commands,
    user_actions,
    whats_new,
)
from wpo.context import Context
from wpo.dashboard_data import DashboardData
from wpo.integrate import (
    EXIT_FAST_FORWARD_FAILED,
    EXIT_MERGED,
    EXIT_PR_OPENED,
    integrate,
)
from wpo.lanes import lane_outcome
from wpo.machine import MachineConfig
from wpo.runner import RunOptions, WorkPackageRun
from wpo.supervisor import Supervisor


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


class FakeGh:
    """In-memory deterministic GitHub CLI simulator covering GraphQL, issues, labels, milestones,
    Projects v2, and pull requests (including auto-merge and direct-merge fallback)."""

    def __init__(self, repo: str = "octo/demo", *, allow_auto_merge: bool = True, auto_merge_supported: bool = True,
                 immediate_merge: bool = False, bare_remote: Path | None = None,
                 issue_page_size: int | None = None, closed_pr_page_size: int | None = None,
                 omit_open_prs_query: bool = False) -> None:
        self.repo = repo
        self.allow_auto_merge = allow_auto_merge
        self.auto_merge_supported = auto_merge_supported
        self.immediate_merge = immediate_merge
        self.bare_remote = bare_remote
        self.issue_page_size = issue_page_size
        self.closed_pr_page_size = closed_pr_page_size
        self.omit_open_prs_query = omit_open_prs_query
        self.calls: list[list[str]] = []
        self.next_issue = 1
        self.next_pr = 100
        self.next_project = 7
        self.issues: dict[int, dict] = {}
        self.prs: dict[int, dict] = {}
        self.milestones: dict[str, int] = {}
        self.labels: set[str] = set()
        self.projects: dict[int, dict] = {}
        self.viewer = "octo"

    def add_project(self, title: str, *, number: int | None = None, owner: str = "octo",
                    status_options: list[str] | None = None, lane_options: list[str] | None = None) -> int:
        num = number if number is not None else self.next_project
        self.next_project = max(self.next_project, num + 1)
        statuses = status_options or ["Todo", "In Progress", "In Review", "Blocked", "Done"]
        fields: dict[str, dict] = {
            "Status": {"id": f"FLD_STATUS_{num}", "options": {name: f"OPT_S_{i}" for i, name in enumerate(statuses, 1)}},
        }
        if lane_options:
            fields["Lane"] = {"id": f"FLD_LANE_{num}", "options": {name: f"OPT_L_{i}" for i, name in enumerate(lane_options, 1)}}
        self.projects[num] = {
            "id": f"PVT_{num}",
            "number": num,
            "title": title,
            "url": f"https://github.com/users/{owner}/projects/{num}",
            "owner": owner,
            "fields": fields,
            "items": {},  # item_id -> {issue_number, fields: {field_name: option_name}}
        }
        return num

    def add_issue(self, title: str, body: str = "", *, number: int | None = None, state: str = "OPEN",
                  state_reason: str | None = None, milestone: str | None = None, labels: list[str] | None = None,
                  blocked_by: list[int] | None = None, parent: int | None = None,
                  sub_issues: list[int] | None = None, project_number: int | None = None,
                  board_status: str | None = None, board_lane: str | None = None) -> int:
        num = number if number is not None else self.next_issue
        self.next_issue = max(self.next_issue, num + 1)
        self.issues[num] = {
            "id": f"ISSUE_NODE_{num}",
            "number": num,
            "title": title,
            "body": body,
            "state": state,
            "stateReason": state_reason,
            "url": f"https://github.com/{self.repo}/issues/{num}",
            "updatedAt": "2026-11-21T10:00:00Z",
            "closedAt": "2026-11-21T10:05:00Z" if state == "CLOSED" else None,
            "milestone": {"title": milestone, "number": 1} if milestone else None,
            "labels": list(labels or []),
            "blocked_by": list(blocked_by or []),
            "parent": parent,
            "sub_issues": list(sub_issues or []),
        }
        if project_number is not None and project_number in self.projects:
            item_id = f"PVTI_{project_number}_{num}"
            fvals = {}
            if board_status:
                fvals["Status"] = board_status
            if board_lane:
                fvals["Lane"] = board_lane
            self.projects[project_number]["items"][item_id] = {"issue_number": num, "fields": fvals}
        return num

    def merge_pr_in_remote(self, pr_number: int) -> str:
        """Simulates GitHub merging the PR on the bare remote and closing its linked issue."""
        pr = self.prs[pr_number]
        merge_sha = pr["headRefOid"]
        if self.bare_remote is not None:
            branch = pr["headRefName"]
            # Fast-forward or advance main ref in bare remote to the PR branch tip.
            sha = git(self.bare_remote, "rev-parse", f"refs/heads/{branch}").strip()
            git(self.bare_remote, "update-ref", "refs/heads/main", sha)
            merge_sha = sha
        pr["state"] = "MERGED"
        pr["merged"] = True
        pr["mergedAt"] = "2026-11-21T12:00:00Z"
        pr["mergeCommit"] = {"oid": merge_sha}
        for match in re.finditer(r"(?i)\b(?:fixes|closes|resolves)\s+#(\d+)\b", pr.get("body") or ""):
            issue_num = int(match.group(1))
            if issue_num in self.issues:
                self.issues[issue_num]["state"] = "CLOSED"
                self.issues[issue_num]["closedAt"] = pr["mergedAt"]
                for proj in self.projects.values():
                    for item in proj["items"].values():
                        if item["issue_number"] == issue_num:
                            item["fields"]["Status"] = "Done"
        return merge_sha

    def _graphql_issue(self, issue: dict, *, include_closed_prs: bool = True, pr_cursor: str | None = None) -> dict:
        num = issue["number"]
        all_linked = [
            self._graphql_pr(pr)
            for pr in self.prs.values()
            if re.search(rf"(?i)\b(?:fixes|closes|resolves)\s+#{num}\b", pr.get("body") or "")
            and (include_closed_prs or pr["state"] == "OPEN")
        ]
        pr_start = int(pr_cursor) if pr_cursor is not None else 0
        if self.closed_pr_page_size:
            linked_prs = all_linked[pr_start:pr_start + self.closed_pr_page_size]
            pr_has_next = pr_start + self.closed_pr_page_size < len(all_linked)
            pr_end_cursor = str(pr_start + self.closed_pr_page_size) if pr_has_next else None
        else:
            linked_prs = all_linked[pr_start:]
            pr_has_next = False
            pr_end_cursor = None
        proj_items = []
        for proj in self.projects.values():
            for item_id, item in proj["items"].items():
                if item["issue_number"] != num:
                    continue
                fvals = [{"name": val, "field": {"name": fname}} for fname, val in item["fields"].items()]
                proj_items.append({
                    "id": item_id,
                    "project": {"id": proj["id"], "number": proj["number"], "title": proj["title"], "url": proj["url"]},
                    "fieldValues": {"nodes": fvals},
                })
        return {
            "id": issue["id"],
            "number": num,
            "title": issue["title"],
            "body": issue["body"],
            "state": issue["state"],
            "stateReason": issue.get("stateReason"),
            "url": issue["url"],
            "updatedAt": issue["updatedAt"],
            "closedAt": issue["closedAt"],
            "milestone": issue["milestone"],
            "labels": {"nodes": [{"name": name} for name in issue["labels"]]},
            "parent": {"number": issue["parent"]} if issue["parent"] else None,
            "subIssues": {"nodes": [{"number": s, "state": self.issues.get(s, {}).get("state", "OPEN")} for s in issue["sub_issues"]]},
            "blockedBy": {"nodes": [{"number": b, "state": self.issues.get(b, {}).get("state", "OPEN")} for b in issue["blocked_by"]]},
            "closedByPullRequestsReferences": {
                "pageInfo": {"hasNextPage": pr_has_next, "endCursor": pr_end_cursor},
                "nodes": linked_prs,
            },
            "projectItems": {"nodes": proj_items},
        }

    @staticmethod
    def _graphql_pr(pr: dict) -> dict:
        return {
            "number": pr["number"],
            "title": pr["title"],
            "body": pr["body"],
            "url": pr["url"],
            "state": pr["state"],
            "merged": pr["merged"],
            "mergedAt": pr.get("mergedAt"),
            "mergeCommit": pr.get("mergeCommit"),
            "headRefName": pr["headRefName"],
            "headRefOid": pr["headRefOid"],
            "baseRefName": pr.get("baseRefName", "main"),
            "isDraft": pr.get("isDraft", False),
            "mergeStateStatus": pr.get("mergeStateStatus", "CLEAN"),
            "autoMergeRequest": pr.get("autoMergeRequest"),
            "statusCheckRollup": pr["rollup"] if "rollup" in pr else ({"state": pr["checkState"]} if pr.get("checkState") else None),
        }

    def _ok(self, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["gh"], 0, stdout=stdout, stderr="")

    def _fail(self, stderr: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["gh"], 1, stdout="", stderr=stderr)

    def __call__(self, command: list[str], input_text: str | None) -> subprocess.CompletedProcess:
        args = command[1:]
        self.calls.append(args)

        if args[:2] == ["api", "graphql"]:
            payload = json.loads(input_text or "{}")
            query = payload.get("query", "")
            variables = payload.get("variables") or {}
            # Enforce real GitHub GraphQL schema field name (R13: autoMergeAllowed, never allowAutoMerge).
            if "allowAutoMerge" in query:
                return self._fail("Field 'allowAutoMerge' doesn't exist on type 'Repository'")
            if "issue(number:" in query and "closedByPullRequestsReferences(" in query:
                include_closed = "includeClosedPrs: true" in query
                issue = self.issues[int(variables["number"])]
                node = self._graphql_issue(issue, include_closed_prs=include_closed, pr_cursor=variables.get("cursor"))
                return self._ok(json.dumps({
                    "data": {"repository": {"issue": {"closedByPullRequestsReferences": node["closedByPullRequestsReferences"]}}}
                }))
            if "issues(" in query:
                include_closed = "includeClosedPrs: true" in query
                all_nodes = [self._graphql_issue(self.issues[num], include_closed_prs=include_closed) for num in sorted(self.issues)]
                cursor = variables.get("cursor")
                start = int(cursor) if cursor is not None else 0
                if self.issue_page_size:
                    page = all_nodes[start:start + self.issue_page_size]
                    has_next = start + self.issue_page_size < len(all_nodes)
                    end_cursor = str(start + self.issue_page_size) if has_next else None
                else:
                    page = all_nodes[start:]
                    has_next = False
                    end_cursor = None
                return self._ok(json.dumps({
                    "data": {
                        "repository": {
                            "autoMergeAllowed": self.allow_auto_merge,
                            "defaultBranchRef": {"name": "main"},
                            "issues": {"pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}, "nodes": page},
                        }
                    }
                }))
            if "pullRequests(" in query:
                nodes = [] if self.omit_open_prs_query else [self._graphql_pr(pr) for pr in reversed(list(self.prs.values()))]
                return self._ok(json.dumps({
                    "data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}
                }))
            if "projectV2(" in query:
                number = int(variables["number"])
                proj = self.projects.get(number)
                root_key = "user" if "user(" in query else "organization"
                if not proj:
                    return self._ok(json.dumps({"data": {root_key: {"projectV2": None}}}))
                field_nodes = [
                    {"id": info["id"], "name": fname, "options": [{"id": oid, "name": oname} for oname, oid in info["options"].items()]}
                    for fname, info in proj["fields"].items()
                ]
                return self._ok(json.dumps({
                    "data": {root_key: {"projectV2": {
                        "id": proj["id"], "number": proj["number"], "title": proj["title"], "url": proj["url"],
                        "fields": {"nodes": field_nodes},
                    }}}
                }))
            return self._fail(f"unsupported graphql query: {query[:80]}")

        if args[:2] == ["api", "user"]:
            return self._ok(json.dumps({"login": self.viewer}))

        if args[0] == "api":
            if "-X" in args and args[args.index("-X") + 1] == "PATCH":
                if any("allow_auto_merge=true" in a for a in args):
                    self.allow_auto_merge = True
                return self._ok(json.dumps({"allow_auto_merge": self.allow_auto_merge}))
            if "-X" in args and args[args.index("-X") + 1] == "POST" and any("milestones" in a for a in args):
                title = next(a.split("title=", 1)[1] for a in args if a.startswith("title="))
                self.milestones.setdefault(title, len(self.milestones) + 1)
                return self._ok(json.dumps({"title": title, "number": self.milestones[title]}))
            if any("milestones?" in a for a in args):
                return self._ok(json.dumps([{"title": t, "number": n} for t, n in self.milestones.items()]))
            if any(a == f"repos/{self.repo}" for a in args):
                return self._ok(json.dumps({"allow_auto_merge": self.allow_auto_merge, "default_branch": "main"}))
            return self._ok("{}")

        if args[0] == "label" and args[1] == "create":
            self.labels.add(args[2])
            return self._ok("")

        if args[0] == "project":
            sub = args[1]
            if sub == "list":
                return self._ok(json.dumps({"projects": [{"number": p["number"], "title": p["title"], "url": p["url"]} for p in self.projects.values()]}))
            if sub == "create":
                title = args[args.index("--title") + 1]
                owner = args[args.index("--owner") + 1]
                owner = self.viewer if owner == "@me" else owner
                num = self.add_project(title, owner=owner)
                return self._ok(json.dumps({"number": num, "title": title, "url": self.projects[num]["url"]}))
            if sub == "link":
                return self._ok("")
            if sub == "field-list":
                num = int(args[2])
                proj = self.projects[num]
                return self._ok(json.dumps({"fields": [{"id": info["id"], "name": name} for name, info in proj["fields"].items()]}))
            if sub == "field-create":
                num = int(args[2])
                name = args[args.index("--name") + 1]
                opts = args[args.index("--single-select-options") + 1].split(",")
                self.projects[num]["fields"][name] = {"id": f"FLD_{name}_{num}", "options": {o: f"OPT_{name}_{i}" for i, o in enumerate(opts, 1)}}
                return self._ok("")
            if sub == "view":
                num = int(args[2])
                return self._ok(json.dumps({"number": num, "url": self.projects[num]["url"], "title": self.projects[num]["title"]}))
            if sub == "item-add":
                num = int(args[2])
                url = args[args.index("--url") + 1]
                issue_num = int(url.rstrip("/").split("/")[-1])
                item_id = f"PVTI_{num}_{issue_num}"
                self.projects[num]["items"].setdefault(item_id, {"issue_number": issue_num, "fields": {"Status": "Todo"}})
                return self._ok(json.dumps({"id": item_id}))
            if sub == "item-edit":
                item_id = args[args.index("--id") + 1]
                proj_id = args[args.index("--project-id") + 1]
                field_id = args[args.index("--field-id") + 1]
                option_id = args[args.index("--single-select-option-id") + 1]
                for proj in self.projects.values():
                    if proj["id"] == proj_id and item_id in proj["items"]:
                        for fname, finfo in proj["fields"].items():
                            if finfo["id"] == field_id:
                                opt_name = next((n for n, oid in finfo["options"].items() if oid == option_id), None)
                                if opt_name:
                                    proj["items"][item_id]["fields"][fname] = opt_name
                return self._ok("")

        if args[0] == "issue":
            sub = args[1]
            if sub == "list":
                return self._ok(json.dumps([{"number": i["number"], "body": i["body"]} for i in self.issues.values()]))
            if sub == "view":
                num = int(args[2])
                issue = self.issues[num]
                return self._ok(json.dumps({"number": num, "body": issue["body"], "state": issue["state"]}))
            if sub == "create":
                title = args[args.index("--title") + 1]
                body_file = Path(args[args.index("--body-file") + 1])
                body = body_file.read_text(encoding="utf-8")
                milestone = args[args.index("--milestone") + 1] if "--milestone" in args else None
                labels = []
                for idx, token in enumerate(args):
                    if token == "--label":
                        labels.append(args[idx + 1])
                num = self.add_issue(title, body, milestone=milestone, labels=labels)
                return self._ok(self.issues[num]["url"] + "\n")
            if sub == "edit":
                num = int(args[2])
                issue = self.issues[num]
                if "--body-file" in args:
                    issue["body"] = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
                if "--add-blocked-by" in args:
                    deps = [int(x) for x in args[args.index("--add-blocked-by") + 1].split(",") if x]
                    for dep in deps:
                        if dep not in issue["blocked_by"]:
                            issue["blocked_by"].append(dep)
                if "--add-label" in args:
                    lbl = args[args.index("--add-label") + 1]
                    if lbl not in issue["labels"]:
                        issue["labels"].append(lbl)
                if "--remove-label" in args:
                    lbl = args[args.index("--remove-label") + 1]
                    issue["labels"] = [x for x in issue["labels"] if x != lbl]
                return self._ok("")
            if sub == "close":
                num = int(args[2])
                self.issues[num]["state"] = "CLOSED"
                self.issues[num]["closedAt"] = "2026-11-21T12:00:00Z"
                for proj in self.projects.values():
                    for item in proj["items"].values():
                        if item["issue_number"] == num:
                            item["fields"]["Status"] = "Done"
                return self._ok("")

        if args[0] == "pr":
            sub = args[1]
            if sub == "list":
                head = args[args.index("--head") + 1] if "--head" in args else None
                matches = [self._graphql_pr(pr) for pr in self.prs.values() if pr["state"] == "OPEN" and (head is None or pr["headRefName"] == head)]
                return self._ok(json.dumps(matches))
            if sub == "create":
                title = args[args.index("--title") + 1]
                body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
                head = args[args.index("--head") + 1]
                base = args[args.index("--base") + 1]
                draft = "--draft" in args
                num = self.next_pr
                self.next_pr += 1
                head_sha = git(self.bare_remote, "rev-parse", f"refs/heads/{head}").strip() if self.bare_remote else "deadbeef00"
                self.prs[num] = {
                    "number": num,
                    "title": title,
                    "body": body,
                    "url": f"https://github.com/{self.repo}/pull/{num}",
                    "state": "OPEN",
                    "merged": False,
                    "headRefName": head,
                    "headRefOid": head_sha,
                    "baseRefName": base,
                    "isDraft": draft,
                    "mergeStateStatus": "CLEAN",
                    "autoMergeRequest": None,
                    "checkState": "PENDING",
                }
                return self._ok(self.prs[num]["url"] + "\n")
            if sub == "edit":
                num = int(args[2])
                if "--title" in args:
                    self.prs[num]["title"] = args[args.index("--title") + 1]
                if "--body-file" in args:
                    self.prs[num]["body"] = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
                return self._ok("")
            if sub == "ready":
                num = int(args[2])
                self.prs[num]["isDraft"] = False
                return self._ok("")
            if sub == "view":
                target = args[2]
                num = int(target.rstrip("/").split("/")[-1])
                return self._ok(json.dumps(self._graphql_pr(self.prs[num])))
            if sub == "merge":
                num = int(args[2])
                pr = self.prs[num]
                if "--auto" in args:
                    if not self.auto_merge_supported or not self.allow_auto_merge:
                        return self._fail("Pull request Auto merge is not allowed for this repository")
                    pr["autoMergeRequest"] = {"enabledAt": "2026-11-21T11:30:00Z", "mergeMethod": "SQUASH"}
                    if self.immediate_merge:
                        self.merge_pr_in_remote(num)
                    return self._ok("")
                self.merge_pr_in_remote(num)
                return self._ok("")

        return self._fail(f"unhandled fake gh command: {args}")


def make_repo_with_remote(base: Path, config: dict) -> tuple[Path, Path, MachineConfig]:
    bare = base / "remote.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-q", "-b", "main")
    repo = base / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    git(repo, "remote", "add", "origin", str(bare))
    (repo / ".gitignore").write_text(".orchestration/runs/\n.orchestration/tasks/\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".orchestration").mkdir()
    (repo / ".orchestration" / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "push", "-q", "-u", "origin", "main")

    machine_path = base / "machine.json"
    machine_cfg = {
        "execution_mode": "standalone",
        "state_dir": str(base / "state"),
        "data_dir": str(base / "data"),
        "dashboard": {"autostart": False},
    }
    machine_path.write_text(json.dumps(machine_cfg, indent=2), encoding="utf-8")
    return repo, bare, MachineConfig.load(machine_path)


class PrivacyFirewallTests(unittest.TestCase):
    def test_blocks_secrets_personal_paths_and_ledger_markers(self) -> None:
        self.assertTrue(privacy.check_public_text("token = ghp_1234567890abcdefghij1234567890"))
        self.assertTrue(privacy.check_public_text("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234"))
        self.assertTrue(privacy.check_public_text("-----BEGIN RSA PRIVATE KEY-----\nabc"))
        self.assertTrue(privacy.check_public_text('{"type": "assistant", "message": {}}'))
        self.assertTrue(privacy.check_public_text("See /home/alice/.ssh/config for details"))
        self.assertTrue(privacy.check_public_text(r"C:\Users\Alice\secrets.txt"))
        self.assertTrue(privacy.check_public_text("Logs in .orchestration/runs/WP-0001/attempt.log"))

    def test_rewrites_repo_root_but_blocks_secrets(self) -> None:
        root = Path("/tmp/demo-repo").resolve()
        cleaned = privacy.require_public_safe(f"Edited {root.as_posix()}/src/app.py cleanly.", root)
        self.assertEqual(cleaned, "Edited src/app.py cleanly.")
        with self.assertRaises(privacy.PrivacyError):
            privacy.require_public_safe("Leaked key AKIAIOSFODNN7EXAMPLE in body", root)


class IssueConventionTests(unittest.TestCase):
    def test_parses_idiomatic_done_when_issue(self) -> None:
        body = (
            "The parser drops trailing whitespace on empty rows.\n\n"
            "Fix `parse_row` so trailing whitespace is preserved.\n\n"
            "**Done when:**\n"
            "- Unit test covers trailing whitespace.\n"
            "- Existing parser tests stay green.\n"
        )
        parsed = github.parse_issue_body(body)
        self.assertTrue(any("parser drops trailing whitespace" in line for line in parsed["sections"]["Goal"]))
        self.assertEqual(parsed["sections"]["Acceptance criteria"], [
            "1. Unit test covers trailing whitespace.",
            "2. Existing parser tests stay green.",
        ])
        self.assertFalse(parsed["owns_explicit"])

    def test_parses_wpo_sections_and_meta_comment(self) -> None:
        body = (
            "## Goal\nAdd retry backoff.\n\n"
            "## Scope\n- `src/retry.py`\n- `tests/test_retry.py`\n\n"
            "## Verify\n```\npytest tests/test_retry.py\n```\n\n"
            '<!-- wpo {"lane": "core", "owns": ["src/retry.py"], "legacy_wp_id": "WP-0009"} -->\n'
        )
        parsed = github.parse_issue_body(body)
        self.assertTrue(parsed["owns_explicit"])
        self.assertEqual(parsed["owns"], ["src/retry.py", "tests/test_retry.py"])
        self.assertEqual(parsed["lane"], "core")
        self.assertEqual(parsed["legacy_wp_id"], "WP-0009")

    def test_parse_github_repo_formats(self) -> None:
        self.assertEqual(github.parse_github_repo("git@github.com:poipoi300/wp-orchestration.git"), "poipoi300/wp-orchestration")
        self.assertEqual(github.parse_github_repo("https://github.com/poipoi300/wp-orchestration.git"), "poipoi300/wp-orchestration")
        self.assertEqual(github.parse_github_repo("ssh://git@github.com/poipoi300/wp-orchestration"), "poipoi300/wp-orchestration")


class SplitStateAndAutoMergePRTests(unittest.TestCase):
    def test_end_to_end_github_verify_pr_automerge_and_reconcile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-gh-e2e-") as tmp:
            base = Path(tmp)
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "verify_round": [f"{sys.executable} -c \"import pathlib; assert pathlib.Path('src/app.py').exists()\""],
                "verify_workspace": [f"{sys.executable} -c \"import pathlib; assert 'VALUE = 2' in pathlib.Path('src/app.py').read_text()\""],
                "github": {"repo": "octo/demo", "project_number": 7, "project_owner": "octo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            fake_gh.add_project("demo", number=7, lane_options=["core"])
            i1 = fake_gh.add_issue(
                "Update VALUE to 2",
                "## Goal\nSet VALUE = 2 in src/app.py.\n\n## Steps\n- [ ] Edit src/app.py\n\n"
                "## Acceptance criteria\n1. `src/app.py` defines `VALUE = 2`.\n\n"
                "## Scope\n- `src/app.py`\n",
                milestone="M1",
                labels=["lane:core"],
                project_number=7,
                board_status="Todo",
                board_lane="core",
            )
            i2 = fake_gh.add_issue(
                "Follow-up depending on #1",
                "Use VALUE = 2.\n\n**Done when:**\n- Uses VALUE = 2.\n",
                milestone="M1",
                labels=["lane:core"],
                blocked_by=[i1],
                project_number=7,
                board_status="Todo",
                board_lane="core",
            )
            epic = fake_gh.add_issue(
                "Epic container",
                "Tracks #1 and #2.\n\n**Done when:**\n- Both sub-issues closed.\n",
                milestone="M1",
                labels=["lane:core"],
                sub_issues=[i1, i2],
                project_number=7,
                board_status="Todo",
            )
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)

            # External ledger must live outside the repo checkout.
            self.assertTrue(context.project.is_external_ledger)
            self.assertFalse(context.project.runs_dir.resolve().is_relative_to(repo.resolve()))

            wps = context.effective_wps(refresh=True)
            by_id = {wp.id: wp for wp in wps}
            self.assertEqual(set(by_id), {f"GH-{i1}", f"GH-{i2}", f"GH-{epic}"})
            self.assertEqual(by_id[f"GH-{i2}"].depends_on, [f"GH-{i1}"])
            self.assertEqual(by_id[f"GH-{epic}"].sub_issues, (f"GH-{i1}", f"GH-{i2}"))

            # Epic with open sub-issues is NOT scheduled; GH-2 is blocked by GH-1; only GH-1 is runnable.
            _msg, ids = lane_outcome(context, "core", list(wps), context.merged_done_ids(), {})
            self.assertEqual(ids, [f"GH-{i1}", f"GH-{i2}", f"GH-{epic}"])
            # Specifically check runnable filtering via a lane with only epic + blocked GH-2:
            msg_blocked, ids_blocked = lane_outcome(context, "core", [by_id[f"GH-{i2}"], by_id[f"GH-{epic}"]], context.merged_done_ids(), {})
            self.assertIsNone(ids_blocked)
            self.assertIn("waiting on dependencies", msg_blocked)

            # Create worktree for GH-1 and simulate a builder session using split local state commands.
            wt = context.project.worktree(f"GH-{i1}")
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-q", "-b", context.project.branch(f"GH-{i1}"), str(wt), "main")
            context.set_wp_status(by_id[f"GH-{i1}"], "in_progress")
            self.assertEqual(fake_gh.projects[7]["items"][f"PVTI_7_{i1}"]["fields"]["Status"], "In Progress")

            old_env = dict(os.environ)
            try:
                os.environ["WPO_WORK_PACKAGE"] = f"GH-{i1}"
                os.environ["WPO_TREE"] = str(wt)
                os.environ["WPO_ROLE"] = "builder"
                os.environ["WPO_ROUTE"] = "test-route"
                # Edit owned file + test outside_owns enforcement first.
                (wt / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
                (wt / "stray.txt").write_text("oops\n", encoding="utf-8")
                session_commands.try_it(context, "```sh\npython -c \"import src.app\"\n```\nConfirm VALUE == 2.")
                passed, report = session_commands.verify(context)
                self.assertFalse(passed)
                self.assertIn("stray.txt", report)
                (wt / "stray.txt").unlink()

                progress_line = session_commands.log(context, "Updated VALUE to 2", step=1)
                self.assertIn("Updated VALUE to 2", progress_line)
                passed, report = session_commands.verify(context)
                self.assertTrue(passed, report)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            # Verify zero tracked/untracked WPO metadata polluted the git working tree!
            status_lines = git(wt, "status", "--porcelain").splitlines()
            self.assertEqual(status_lines, [" M src/app.py"])

            # Verify board status moved to In Review, local status is review, and step 1 is ticked.
            refreshed_wp = context.wp_in_tree(f"GH-{i1}", wt)
            self.assertEqual(refreshed_wp.status, "review")
            self.assertIn("- [x] Edit src/app.py", refreshed_wp.section("Steps"))
            self.assertEqual(fake_gh.projects[7]["items"][f"PVTI_7_{i1}"]["fields"]["Status"], "In Review")

            # Runner recognizes the verified tree without re-running checks!
            run = WorkPackageRun(context, f"GH-{i1}", wt, RunOptions())
            self.assertTrue(run.session_verified())

            # Mark locally done (as after reviewer PASS) and deliver via PR + auto-merge!
            context.set_wp_status(refreshed_wp, "done")
            # Confirm set_wp_status("done") NEVER manually flips board Status to Done!
            self.assertEqual(fake_gh.projects[7]["items"][f"PVTI_7_{i1}"]["fields"]["Status"], "In Review")

            code = integrate(context, f"GH-{i1}")
            self.assertEqual(code, EXIT_PR_OPENED)
            self.assertEqual(len(fake_gh.prs), 1)
            pr = fake_gh.prs[100]
            self.assertIn(f"Fixes #{i1}", pr["body"])
            self.assertIn("## Try it", pr["body"])
            self.assertIsNotNone(pr["autoMergeRequest"])
            self.assertNotIn("test-route", pr["body"])  # No internal route/log leak!

            # Simulate CI failure on PR -> emits pr-checks-failed once per head SHA.
            pr["checkState"] = "FAILURE"
            rec1 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec1["failed_checks"], [f"GH-{i1}"])
            rec2 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec2["failed_checks"], [])  # deduplicated for same head SHA

            # Simulate GitHub completing auto-merge -> reconcile fast-forwards local main & unblocks GH-2!
            fake_gh.merge_pr_in_remote(100)
            rec3 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec3["merged"], [f"GH-{i1}"])
            self.assertIn("VALUE = 2", (repo / "src" / "app.py").read_text(encoding="utf-8"))
            self.assertIn(f"GH-{i1}", context.merged_done_ids())
            self.assertEqual(fake_gh.projects[7]["items"][f"PVTI_7_{i1}"]["fields"]["Status"], "Done")

            # Dashboard reads read-only from cache (no extra gh calls!) and exposes GitHub + PR links.
            calls_before_dash = len(fake_gh.calls)
            dash = DashboardData(repo, machine)
            dash._context = context  # reuse injectable fake gh client
            dash._runs = context.project.runs_dir
            snap = dash.snapshot()
            self.assertEqual(len(fake_gh.calls), calls_before_dash)
            self.assertEqual(snap["project"]["tracker"], "github")
            self.assertEqual(snap["project"]["github"]["project_url"], fake_gh.projects[7]["url"])
            self.assertEqual(snap["merged"][0]["id"], f"GH-{i1}")
            self.assertEqual(snap["merged"][0]["pr_number"], 100)
            detail = dash.wp_detail(f"GH-{i1}")
            self.assertEqual(detail["pr"]["number"], 100)
            self.assertTrue(detail["pr"]["merged"])

            # Whats-new reads merged PR + local Try it cleanly.
            news = whats_new.whats_new(context.project, None, 5)
            self.assertIn(f"GH-{i1}", news)
            self.assertIn("PR #100", news)
            self.assertIn("Confirm VALUE == 2", news)

            # Unblock on a GitHub task updates local state + removes blocked label without touching git or spawning lanes.
            context.set_wp_status(context.wp_in_tree(f"GH-{i2}", repo), "blocked")
            self.assertIn("blocked", fake_gh.issues[i2]["labels"])
            with mock.patch("wpo.user_actions.restart_lane", return_value=["core: mocked"]):
                user_actions.unblock(context, f"GH-{i2}", "Proceed with defaults", status="todo")
            self.assertNotIn("blocked", fake_gh.issues[i2]["labels"])
            self.assertEqual(context.wp_in_tree(f"GH-{i2}", repo).status, "todo")
            self.assertIn("Proceed with defaults", "\n".join(context.wp_in_tree(f"GH-{i2}", repo).section("Context")))

    def test_automerge_fallback_direct_merge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-gh-fallback-") as tmp:
            base = Path(tmp)
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "verify_workspace": [f"{sys.executable} -c \"import pathlib; assert pathlib.Path('src/app.py').exists()\""],
                "github": {"repo": "octo/demo", "auto_merge_fallback": "direct-merge"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", allow_auto_merge=False, auto_merge_supported=False, bare_remote=bare)
            i1 = fake_gh.add_issue("Direct merge test", "Goal.\n\n**Done when:**\n- Done.\n")
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)

            wt = context.project.worktree(f"GH-{i1}")
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-q", "-b", context.project.branch(f"GH-{i1}"), str(wt), "main")
            (wt / "src" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
            context.set_wp_try_it(context.wp_in_tree(f"GH-{i1}", wt), "Nothing to try by hand: test only.")
            context.set_wp_status(context.wp_in_tree(f"GH-{i1}", wt), "done")

            # Mark PR checks clean upon creation by clearing default PENDING after creation?
            # Actually FakeGh sets checkState=PENDING, so auto_merge_fallback waits on first call then merges when SUCCESS!
            code = integrate(context, f"GH-{i1}")
            self.assertEqual(code, EXIT_PR_OPENED)
            fake_gh.prs[100]["checkState"] = "SUCCESS"
            rec = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec["merged"], [f"GH-{i1}"])
            self.assertIn("VALUE = 99", (repo / "src" / "app.py").read_text(encoding="utf-8"))

    def test_lint_on_github_issues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-gh-lint-") as tmp:
            base = Path(tmp)
            config = {
                "name": "demo",
                "tracker": "github",
                "runs_dir": "external",
                "verify_round": ["pytest"],
                "github": {"repo": "octo/demo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i1 = fake_gh.add_issue("Concurrent task A", "Do thing A.\n\n**Done when:**\n- Thing A works.\n", labels=["lane:alpha"])
            i2 = fake_gh.add_issue("Concurrent task B", "Do thing B.\n\n**Done when:**\n- Thing B works.\n", labels=["lane:beta"])
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)

            findings = lint.lint(context, [])
            # No hard section errors on idiomatic **Done when:** issues!
            errors = [f for f in findings if f.severity == "error"]
            self.assertEqual(errors, [])
            # Warns about concurrent unscoped lanes (no ## Scope)!
            self.assertTrue(any(f.check == lint.CHECK_OVERLAP for f in findings))
            # Adding local owns override eliminates unscoped warning cleanly.
            run_state.set_override(context.project.wp_run_dir(f"GH-{i1}"), "owns", ["src/a.py"])
            run_state.set_override(context.project.wp_run_dir(f"GH-{i2}"), "owns", ["src/b.py"])
            findings2 = lint.lint(context, [])
            self.assertFalse(any(f.check == lint.CHECK_OVERLAP for f in findings2))

    def test_pr_delivery_refuses_secret_before_pushing_and_sanitizes_exe_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-gh-priv-pr-") as tmp:
            base = Path(tmp)
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "verify_workspace": [f"{sys.executable} -c \"import pathlib; assert pathlib.Path('src/app.py').exists()\""],
                "github": {"repo": "octo/demo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", immediate_merge=True, bare_remote=bare)
            i1 = fake_gh.add_issue("Check secret guard", "Goal.\n\n**Done when:**\n- Done.\n")
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)

            wt = context.project.worktree(f"GH-{i1}")
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-q", "-b", context.project.branch(f"GH-{i1}"), str(wt), "main")
            (wt / "src" / "app.py").write_text("VALUE = 7\n", encoding="utf-8")

            # Inject a GitHub token into Try it -> integrate MUST refuse BEFORE pushing!
            context.set_wp_try_it(
                context.wp_in_tree(f"GH-{i1}", wt),
                "```sh\nTOKEN=ghp_1234567890abcdefghij1234567890 python -c 'pass'\n```\n",
            )
            context.set_wp_status(context.wp_in_tree(f"GH-{i1}", wt), "done")
            code = integrate(context, f"GH-{i1}")
            self.assertEqual(code, EXIT_FAST_FORWARD_FAILED)
            self.assertEqual(fake_gh.prs, {})
            remote_branches = git(bare, "branch").strip()
            self.assertNotIn(f"wp/GH-{i1}", remote_branches)

            # Replacing Try it with clean instructions succeeds and immediate auto-merge returns EXIT_MERGED!
            context.set_wp_try_it(context.wp_in_tree(f"GH-{i1}", wt), "Nothing to try by hand: verified in unit tests.")
            code2 = integrate(context, f"GH-{i1}")
            self.assertEqual(code2, EXIT_MERGED)
            pr_body = fake_gh.prs[100]["body"]
            self.assertNotIn("/home/", pr_body)
            self.assertIn("python", pr_body)

    def test_supervisor_pr_current_and_automerge_wait_helpers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-gh-sup-") as tmp:
            base = Path(tmp)
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "github": {"repo": "octo/demo", "poll_seconds": 15},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i1 = fake_gh.add_issue("First", "Goal.\n\n**Done when:**\n- Done.\n", labels=["lane:core"])
            i2 = fake_gh.add_issue("Second", "Goal.\n\n**Done when:**\n- Done.\n", labels=["lane:core"], blocked_by=[i1])
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)

            wt = context.project.worktree(f"GH-{i1}")
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-q", "-b", context.project.branch(f"GH-{i1}"), str(wt), "main")
            (wt / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
            git(wt, "add", "src/app.py")
            git(wt, "commit", "-q", "-m", "GH-1: update")
            head = git(wt, "rev-parse", "HEAD").strip()
            run_state.set_pr(context.project.wp_run_dir(f"GH-{i1}"), {
                "number": 100,
                "url": "https://github.com/octo/demo/pull/100",
                "branch": context.project.branch(f"GH-{i1}"),
                "head_sha": head,
                "state": "OPEN",
                "auto_merge": True,
                "merged": False,
                "check_state": "PENDING",
            })

            sup = Supervisor(context, "core", True, [], {})
            effective = context.effective_wps()
            by_id = {wp.id: wp for wp in effective}
            self.assertTrue(sup._pr_already_current(by_id[f"GH-{i1}"]))
            waiting = sup._awaiting_automerge_prs(effective, set())
            self.assertEqual([w.id for w in waiting], [f"GH-{i1}"])
            self.assertIn(f"GH-{i2}", by_id)

            # A failed check stops auto-merge polling so supervisor does not spin forever.
            run_state.set_pr(context.project.wp_run_dir(f"GH-{i1}"), {
                "number": 100,
                "url": "https://github.com/octo/demo/pull/100",
                "branch": context.project.branch(f"GH-{i1}"),
                "head_sha": head,
                "state": "OPEN",
                "auto_merge": True,
                "merged": False,
                "check_state": "FAILURE",
            })
            waiting_after_fail = sup._awaiting_automerge_prs(context.effective_wps(), set())
            self.assertEqual(waiting_after_fail, [])


class CliGithubAndLedgerTests(unittest.TestCase):
    def test_init_github_and_ledger_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-cli-gh-") as tmp:
            base = Path(tmp)
            repo = base / "proj"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "test")
            git(repo, "remote", "add", "origin", "git@github.com:octo/proj.git")
            (repo / "README.md").write_text("# proj\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-q", "-m", "init")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--project", str(repo), "init", "--name", "proj", "--project-number", "12"])
            self.assertEqual(rc, 0)
            cfg = json.loads((repo / ".orchestration" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["tracker"], "github")
            self.assertEqual(cfg["delivery"], "pr-automerge")
            self.assertEqual(cfg["runs_dir"], "external")
            self.assertEqual(cfg["github"]["repo"], "octo/proj")
            self.assertEqual(cfg["github"]["project_number"], 12)

            # Verify --wp-files opt-in and ledger location flags.
            legacy_repo = base / "legacy-proj"
            legacy_repo.mkdir()
            git(legacy_repo, "init", "-q", "-b", "main")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--project", str(legacy_repo), "init", "--name", "legacy-proj", "--wp-files"]), 0)
            legacy_cfg = json.loads((legacy_repo / ".orchestration" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(legacy_cfg["tracker"], "wp-files")
            self.assertEqual(legacy_cfg["delivery"], "auto")
            self.assertEqual(legacy_cfg["runs_dir"], ".orchestration/runs")

            inrepo_gh = base / "inrepo-gh"
            inrepo_gh.mkdir()
            git(inrepo_gh, "init", "-q", "-b", "main")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--project", str(inrepo_gh), "init", "--name", "inrepo-gh", "--keep-repo-ledger"]), 0)
            inrepo_cfg = json.loads((inrepo_gh / ".orchestration" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(inrepo_cfg["tracker"], "github")
            self.assertEqual(inrepo_cfg["runs_dir"], ".orchestration/runs")

            machine_path = base / "machine.json"
            machine_path.write_text(json.dumps({
                "execution_mode": "standalone",
                "data_dir": str(base / "data"),
                "routes": {},
                "tiers": {},
            }), encoding="utf-8")
            machine = MachineConfig.load(machine_path)
            context = Context.load(repo, machine, gh_client=github.GhClient("gh", cwd=repo, runner=FakeGh("octo/proj")))

            with (
                mock.patch("wpo.machine.MachineConfig.load", return_value=machine),
                mock.patch("wpo.context.Context.load", return_value=context),
                mock.patch("wpo.dashboard.autostart"),
            ):
                out_path = io.StringIO()
                with redirect_stdout(out_path):
                    self.assertEqual(cli.main(["--project", str(repo), "ledger", "path"]), 0)
                self.assertIn(str(context.project.runs_dir), out_path.getvalue())

                out_ov = io.StringIO()
                with redirect_stdout(out_ov):
                    self.assertEqual(
                        cli.main([
                            "--project", str(repo), "ledger", "override", "#5",
                            "--builder", "code-fast", "--owns", "src/a.py,src/b.py",
                        ]),
                        0,
                    )
                st = run_state.load(context.project.wp_run_dir("GH-5"))
                self.assertEqual(st["overrides"]["builder"], "code-fast")
                self.assertEqual(st["overrides"]["owns"], ["src/a.py", "src/b.py"])

                out_stat = io.StringIO()
                with redirect_stdout(out_stat):
                    self.assertEqual(cli.main(["--project", str(repo), "ledger", "status", "--json"]), 0)
                summary = json.loads(out_stat.getvalue())
                self.assertTrue(summary["external"])
                self.assertEqual(len(summary["items"]), 1)
                self.assertEqual(summary["items"][0]["id"], "GH-5")


class LedgerPruneTests(unittest.TestCase):
    def test_prune_indexes_usage_before_removing_old_transcripts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-ledger-") as tmp:
            base = Path(tmp)
            config = {"name": "demo", "tracker": "github", "runs_dir": "external", "github": {"repo": "octo/demo"}}
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i_done = fake_gh.add_issue("Done item", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            i_open = fake_gh.add_issue("Open item", "Goal.\n\n**Done when:**\n- Open.\n", state="OPEN")
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)

            old_time = time.time() - 40 * 86400
            done_dir = context.project.wp_run_dir(f"GH-{i_done}")
            open_dir = context.project.wp_run_dir(f"GH-{i_open}")
            old_log = done_dir / "attempt-1-builder.claude-sonnet.log"
            old_log.write_text(
                json.dumps({"type": "result", "total_cost_usd": 0.12, "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n",
                encoding="utf-8",
            )
            old_prompt = done_dir / "attempt-1-builder.claude-sonnet.log.prompt.md"
            old_prompt.write_text("secret prompt text", encoding="utf-8")
            (done_dir / "attempts.json").write_text(json.dumps({"rounds": 1}), encoding="utf-8")
            os.utime(old_log, (old_time, old_time))
            os.utime(old_prompt, (old_time, old_time))

            open_log = open_dir / "attempt-1-builder.claude-sonnet.log"
            open_log.write_text("still active\n", encoding="utf-8")
            os.utime(open_log, (old_time, old_time))

            dry = run_state.prune_transcripts(context, older_than_days=30, apply=False)
            self.assertEqual(dry["candidate_files"], 2)
            self.assertTrue(old_log.exists())
            self.assertIn(f"GH-{i_open}", dry["skipped_open"])

            applied = run_state.prune_transcripts(context, older_than_days=30, apply=True)
            self.assertEqual(applied["removed_files"], 2)
            self.assertFalse(old_log.exists())
            self.assertFalse(old_prompt.exists())
            # Structured ledgers, open item logs, and longitudinal usage metrics all survive!
            self.assertTrue((done_dir / "attempts.json").exists())
            self.assertTrue(open_log.exists())
            self.assertTrue((context.project.runs_dir / "usage" / "index.json").exists())
            from wpo.usage import load_sessions
            records = load_sessions(context.project)
            self.assertTrue(any(rec.wp == f"GH-{i_done}" and rec.tokens.get("input_tokens") == 100 for rec in records))


class DeterministicMigrationTests(unittest.TestCase):
    WP_TEMPLATE = """---
id: {id}
title: {title}
milestone: {milestone}
lane: {lane}
status: {status}
depends_on: [{depends}]
builder: {builder}
reviewer: {reviewer}
owns: [{owns}]
---

## Goal
{goal}

## Context
{context}

## Deliverables
- {deliverable}

## Steps
- [x] Step one done
- [ ] Step two pending

## Acceptance criteria
1. Works as expected.

## Verify
```
pytest -q
```

## Out of scope
- Nothing else.

## Progress log
- 2026-11-20 10:00 [builder claude-sonnet] Finished step one

## Review
### Review 1, round 1 (gpt-sol for code-hard, 2026-11-20 11:00)
VERDICT: PASS

## Try it
```sh
pytest -q
```
Expect 1 passed.

## Handoff
Private local handoff note.
"""

    def _setup_wp_repo(self, base: Path, *, inject_secret: bool = False) -> tuple[Path, Path, MachineConfig]:
        config = {"name": "legacy-app", "verify_round": ["pytest -q"]}
        repo, bare, machine = make_repo_with_remote(base, config)
        wp_dir = repo / "docs" / "plan" / "wp"
        wp_dir.mkdir(parents=True)
        (wp_dir / "WP-0001-base.md").write_text(self.WP_TEMPLATE.format(
            id="WP-0001", title="Base layer", milestone="M1", lane="core", status="done",
            depends="", builder="code", reviewer="code-hard", owns="src/app.py",
            goal="Build base layer.", context="Foundational work.", deliverable="src/app.py",
        ), encoding="utf-8")
        (wp_dir / "WP-0002-mid.md").write_text(self.WP_TEMPLATE.format(
            id="WP-0002", title="Middle feature", milestone="M1", lane="core", status="in_progress",
            depends="WP-0001", builder="pin:local-deepseek-max", reviewer="code-hard", owns="src/mid.py",
            goal="Build middle feature.",
            context="Builds on WP-0001 and prepares for WP-0003." + (" Secret: ghp_1234567890abcdefghij1234567890" if inject_secret else ""),
            deliverable="src/mid.py",
        ), encoding="utf-8")
        (wp_dir / "WP-0003-top.md").write_text(self.WP_TEMPLATE.format(
            id="WP-0003", title="Top feature", milestone="M2", lane="ui", status="blocked",
            depends="WP-0002", builder="code", reviewer="code-hard", owns="src/top.py",
            goal="Build top UI.", context="Depends on WP-0001 and WP-0002.", deliverable="src/top.py",
        ), encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "add legacy WPs")
        git(repo, "push", "-q", "origin", "main")

        # Create in-flight worktree & private transcript/findings for WP-0002!
        wt = repo.parent / "repo-worktrees" / "WP-0002"
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-q", "-b", "wp/WP-0002", str(wt), "main")
        (wt / "src" / "mid.py").write_text("# in-flight work\n", encoding="utf-8")
        wp2_run = repo / ".orchestration" / "runs" / "WP-0002"
        wp2_run.mkdir(parents=True)
        (wp2_run / "attempt-1-builder.local.log").write_text('{"type":"assistant","secret":"stay-local"}\n', encoding="utf-8")
        (wp2_run / "findings.json").write_text(json.dumps({"reviews": 1, "findings": []}), encoding="utf-8")
        return repo, bare, machine

    def test_privacy_preflight_blocks_secret_before_touching_github(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-mig-priv-") as tmp:
            repo, bare, machine = self._setup_wp_repo(Path(tmp), inject_secret=True)
            fake_gh = FakeGh("octo/legacy", bare_remote=bare)
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine)

            dry = migrate.migrate_to_github(context, migrate.MigrationOptions(apply=False, repo="octo/legacy"), gh_client=client)
            self.assertIn("WP-0002", dry["privacy_violations"])
            self.assertEqual(fake_gh.calls, [])

            with self.assertRaises(migrate.MigrationError):
                migrate.migrate_to_github(context, migrate.MigrationOptions(apply=True, repo="octo/legacy"), gh_client=client)
            self.assertEqual(fake_gh.calls, [])

    def test_full_deterministic_migration_and_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpo-mig-ok-") as tmp:
            base = Path(tmp)
            repo, bare, machine = self._setup_wp_repo(base)
            fake_gh = FakeGh("octo/legacy", allow_auto_merge=False, bare_remote=bare)
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine)

            # Dry run makes zero GitHub calls and reports full plan.
            plan = migrate.migrate_to_github(context, migrate.MigrationOptions(apply=False, repo="octo/legacy"), gh_client=client)
            self.assertFalse(plan["applied"])
            self.assertEqual(plan["total_wps"], 3)
            self.assertEqual(plan["milestones"], ["M1", "M2"])
            self.assertEqual(plan["lanes"], ["core", "ui"])
            self.assertEqual(plan["in_flight_worktrees"], ["WP-0002"])
            self.assertEqual(fake_gh.calls, [])

            # Execute migration with external ledger & auto-merge enablement.
            report = migrate.migrate_to_github(
                context,
                migrate.MigrationOptions(apply=True, repo="octo/legacy", external_ledger=True, enable_auto_merge=True),
                gh_client=client,
            )
            self.assertTrue(report["applied"])
            self.assertTrue(report["allow_auto_merge"])
            self.assertEqual(report["id_map"], {"WP-0001": "GH-1", "WP-0002": "GH-2", "WP-0003": "GH-3"})
            self.assertEqual(report["dependencies_wired"], 2)
            self.assertEqual(report["closed_issues"], [1])

            # Check GitHub issues: clean public specs, forward+backward WP refs rewritten, zero private leaks!
            self.assertEqual(fake_gh.issues[1]["state"], "CLOSED")
            self.assertEqual(fake_gh.issues[2]["blocked_by"], [1])
            self.assertEqual(fake_gh.issues[3]["blocked_by"], [2])
            self.assertIn("blocked", fake_gh.issues[3]["labels"])
            self.assertIn("Builds on #1 and prepares for #3.", fake_gh.issues[2]["body"])
            self.assertNotIn("Private local handoff note", fake_gh.issues[2]["body"])
            self.assertNotIn("Progress log", fake_gh.issues[2]["body"])
            self.assertNotIn("local-deepseek-max", fake_gh.issues[2]["body"])
            self.assertNotIn("stay-local", fake_gh.issues[2]["body"])

            # Check updated project config & external private ledger!
            migrated_ctx = Context.load(repo, machine, gh_client=client)
            self.assertTrue(migrated_ctx.project.is_github)
            self.assertEqual(migrated_ctx.project.delivery_mode, "pr-automerge")
            self.assertTrue(migrated_ctx.project.is_external_ledger)

            gh2_dir = migrated_ctx.project.runs_dir / "GH-2"
            self.assertTrue((gh2_dir / "attempt-1-builder.local.log").is_file())
            self.assertTrue((gh2_dir / "findings.json").is_file())
            gh2_state = run_state.load(gh2_dir)
            self.assertEqual(gh2_state["legacy_wp_id"], "WP-0002")
            self.assertEqual(gh2_state["status"], "in_progress")
            self.assertEqual(gh2_state["steps_done"], [1])
            self.assertEqual(gh2_state["overrides"]["builder"], "pin:local-deepseek-max")
            self.assertIn("Private local handoff note.", gh2_state["handoff"])
            self.assertIn("pytest -q", gh2_state["try_it"])
            # In-flight worktree binding preserved seamlessly!
            self.assertEqual(migrated_ctx.project.worktree("GH-2").resolve(), (base / "repo-worktrees" / "WP-0002").resolve())
            self.assertEqual(migrated_ctx.project.branch("GH-2"), "wp/WP-0002")

            # Verify migrated GH-2 loads cleanly through Context & preserves private builder override!
            gh2_wp = migrated_ctx.wp_in_tree("GH-2", migrated_ctx.project.worktree("GH-2"))
            self.assertEqual(gh2_wp.builder, "pin:local-deepseek-max")
            self.assertEqual(gh2_wp.depends_on, ["GH-1"])
            self.assertIn("Finished step one", gh2_wp.latest_progress())

            # Idempotent re-run even after wiping migration-state.json creates ZERO duplicate issues!
            (migrated_ctx.project.runs_dir / migrate.MIGRATION_STATE_FILE).unlink()
            rerun = migrate.migrate_to_github(
                migrated_ctx,
                migrate.MigrationOptions(apply=True, repo="octo/legacy", external_ledger=True),
                gh_client=client,
            )
            self.assertEqual(rerun["created_issues"], {})
            self.assertEqual(len(fake_gh.issues), 3)


class ReviewHardeningRegressionTests(unittest.TestCase):
    def test_privacy_blocks_tilde_home_paths_and_ref_rewrite_preserves_filenames(self) -> None:
        # R9: ~/... home-relative paths are blocked by outbound privacy guard.
        findings = privacy.check_public_text("Read ~/.ssh/id_ed25519 or ~/.local/share/wp-orchestration/secret")
        self.assertTrue(any("Home-relative path" in f for f in findings))
        with self.assertRaises(privacy.PrivacyError):
            privacy.require_public_safe("Inspect ~/private/notes.md")

        # R8: WP-XXXX ref rewriting updates references and sentence-end refs but NEVER filenames.
        rewritten = github.rewrite_wp_references(
            "Builds on WP-0001 and prepares for WP-0003. Keep docs/plan/wp/WP-0001-base.md and WP-0002.md intact.",
            {"WP-0001": 11, "WP-0002": 12, "WP-0003": 13},
        )
        self.assertEqual(
            rewritten,
            "Builds on #11 and prepares for #13. Keep docs/plan/wp/WP-0001-base.md and WP-0002.md intact.",
        )

    def test_corrupt_state_json_raises_without_erasing_file(self) -> None:
        # R7: load() and update() must raise StateError rather than overwriting corrupt state.json.
        with tempfile.TemporaryDirectory(prefix="wpo-corrupt-") as tmp:
            run_dir = Path(tmp) / "GH-9"
            run_dir.mkdir()
            state_file = run_state.state_path(run_dir)
            state_file.write_text("{broken json", encoding="utf-8")
            with self.assertRaises(run_state.StateError):
                run_state.load(run_dir)
            with self.assertRaises(run_state.StateError):
                run_state.set_status(run_dir, "in_progress")
            self.assertEqual(state_file.read_text(encoding="utf-8"), "{broken json")

    def test_closed_not_planned_issue_does_not_unblock_dependents(self) -> None:
        # R5: An issue closed without a merged PR (or NOT_PLANNED) is not merged and never unblocks dependents.
        with tempfile.TemporaryDirectory(prefix="wpo-closed-np-") as tmp:
            base = Path(tmp)
            config = {"name": "demo", "tracker": "github", "runs_dir": "external", "github": {"repo": "octo/demo"}}
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i1 = fake_gh.add_issue("Abandoned task", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED", state_reason="NOT_PLANNED")
            i2 = fake_gh.add_issue("Dependent task", "Goal.\n\n**Done when:**\n- Done.\n", blocked_by=[i1])
            i3 = fake_gh.add_issue(
                "Migrated done task",
                '## Goal\nDone before.\n\n<!-- wpo {"legacy_wp_id": "WP-0001", "migrated_merged": true} -->\n',
                state="CLOSED",
                state_reason="COMPLETED",
            )
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            wps = {wp.id: wp for wp in context.effective_wps(refresh=True)}

            self.assertFalse(wps[f"GH-{i1}"].is_merged)
            self.assertEqual(wps[f"GH-{i1}"].status, "blocked")
            self.assertNotIn(f"GH-{i1}", context.merged_done_ids())
            self.assertTrue(wps[f"GH-{i3}"].is_merged)
            self.assertIn(f"GH-{i3}", context.merged_done_ids())
            self.assertEqual(wps[f"GH-{i2}"].depends_on, [f"GH-{i1}"])

    def test_direct_merge_rejects_failing_check_list_and_unstable_merge_state(self) -> None:
        # R6: gh pr view returns statusCheckRollup as a list; UNSTABLE/UNKNOWN and failing checks must block merge.
        self.assertEqual(
            github._rollup_check_state([
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
            ]),
            "FAILURE",
        )
        self.assertEqual(
            github._rollup_check_state([{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]),
            "PENDING",
        )
        self.assertEqual(
            github._rollup_check_state([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]),
            "SUCCESS",
        )
        self.assertFalse(github._is_direct_merge_ready("UNSTABLE", "SUCCESS"))
        self.assertFalse(github._is_direct_merge_ready("UNKNOWN", "SUCCESS"))
        self.assertFalse(github._is_direct_merge_ready(None, "SUCCESS"))
        self.assertFalse(
            github._is_direct_merge_ready(
                "CLEAN",
                rollup=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}],
            )
        )
        self.assertTrue(
            github._is_direct_merge_ready(
                "CLEAN",
                rollup=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            )
        )

    def test_reconcile_retries_local_main_sync_hooks_and_standalone_worktree_cleanup(self) -> None:
        # R1 + R12: Dirty main checkout defers fast-forward, hooks, and standalone worktree cleanup until clean.
        with tempfile.TemporaryDirectory(prefix="wpo-retry-sync-") as tmp:
            base = Path(tmp)
            hook_marker = base / "hook-ran.txt"
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "verify_workspace": [f"{sys.executable} -c \"import pathlib; assert pathlib.Path('src/app.py').exists()\""],
                "post_merge_hooks": [{"when_changed": ["src/**"], "command": f"{sys.executable} -c \"import pathlib; pathlib.Path(r'{hook_marker}').write_text('ran')\""}],
                "github": {"repo": "octo/demo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i1 = fake_gh.add_issue("Feature X", "Goal.\n\n**Done when:**\n- Done.\n")
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)

            wt = context.project.worktree(f"GH-{i1}")
            branch = context.project.branch(f"GH-{i1}")
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "main")
            (wt / "src" / "app.py").write_text("VALUE = 55\n", encoding="utf-8")
            context.set_wp_try_it(context.wp_in_tree(f"GH-{i1}", wt), "Nothing to try by hand: test.")
            context.set_wp_status(context.wp_in_tree(f"GH-{i1}", wt), "done")
            self.assertEqual(integrate(context, f"GH-{i1}"), EXIT_PR_OPENED)

            # Merge on GitHub while local main checkout is dirty -> sync deferred, hook not run, worktree kept.
            fake_gh.merge_pr_in_remote(100)
            dirty_file = repo / "untracked-dirty.txt"
            dirty_file.write_text("wip\n", encoding="utf-8")
            rec1 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec1["merged"], [f"GH-{i1}"])
            self.assertEqual(rec1["pending_local_sync"], [f"GH-{i1}"])
            self.assertFalse(hook_marker.exists())
            self.assertTrue(wt.exists())
            self.assertFalse(run_state.load(context.project.wp_run_dir(f"GH-{i1}"))["pr"]["local_synced"])

            # Clean main checkout and reconcile again -> fast-forward, post-merge hook, and worktree/branch cleanup all finish!
            dirty_file.unlink()
            rec2 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec2["merged"], [])
            self.assertEqual(rec2["pending_local_sync"], [])
            self.assertIn("VALUE = 55", (repo / "src" / "app.py").read_text(encoding="utf-8"))
            self.assertTrue(hook_marker.exists())
            self.assertFalse(wt.exists())
            self.assertEqual(git(repo, "branch", "--list", branch).strip(), "")
            self.assertTrue(run_state.load(context.project.wp_run_dir(f"GH-{i1}"))["pr"]["local_synced"])

    def test_migration_resume_rewrites_forward_refs_preserves_state_paginates_and_rewrites_t3(self) -> None:
        # R2 + R3 + R4 + R10:
        # - Paginated GraphQL legacy discovery (page_size=2 across 3 issues)
        # - Checkpointed resume still edits forward WP-XXXX -> #N references
        # - Re-run preserves post-migration state.json updates instead of clobbering them
        # - In-flight T3 requests, sessions, and scope leases are re-keyed to GH-N and external runs_dir
        with tempfile.TemporaryDirectory(prefix="wpo-mig-hard-") as tmp:
            base = Path(tmp)
            helper = DeterministicMigrationTests()
            repo, bare, machine = helper._setup_wp_repo(base)

            # Add an in-flight T3 bridge request, session identity, scope lease, and t3-workspace.json for WP-0002.
            old_runs = repo / ".orchestration" / "runs"
            req_dir = old_runs / "t3" / "requests"
            sess_dir = old_runs / "t3" / "sessions"
            scope_dir = old_runs / "t3" / "scope"
            for d in (req_dir, sess_dir, scope_dir):
                d.mkdir(parents=True, exist_ok=True)
            (old_runs / "WP-0002" / "t3-workspace.json").write_text(json.dumps({"wp": "WP-0002", "thread_id": "th-2"}), encoding="utf-8")
            (req_dir / "req-1.json").write_text(json.dumps({
                "id": "req-1",
                "kind": "session",
                "state": "running",
                "payload": {
                    "wp": "WP-0002",
                    "title": "WP-0002: builder",
                    "log_path": str(old_runs / "WP-0002" / "attempt-1-builder.log"),
                    "heartbeat_file": str(old_runs / "WP-0002" / "heartbeat"),
                    "progress_file": str(repo / "docs" / "plan" / "wp" / "WP-0002-mid.md"),
                },
            }), encoding="utf-8")
            (sess_dir / "req-1.json").write_text(json.dumps({
                "version": 1,
                "env": {"WPO_WORK_PACKAGE": "WP-0002", "WPO_ROLE": "builder"},
                "project": str(repo),
            }), encoding="utf-8")
            (scope_dir / "abc.json").write_text(json.dumps({
                "request_id": "req-1",
                "log_path": str(old_runs / "WP-0002" / "attempt-1-builder.log"),
            }), encoding="utf-8")

            # Simulate an interrupted Pass-1 crash: WP-0001 and WP-0002 already created on GitHub with
            # unrewritten forward ref "prepares for WP-0003." in #2, before WP-0003 existed!
            fake_gh = FakeGh("octo/legacy", bare_remote=bare, issue_page_size=2)
            fake_gh.add_issue("Base layer", '## Goal\nBuild base layer.\n\n<!-- wpo {"legacy_wp_id": "WP-0001"} -->\n')
            fake_gh.add_issue(
                "Middle feature",
                '## Context\nBuilds on #1 and prepares for WP-0003.\n\n<!-- wpo {"legacy_wp_id": "WP-0002"} -->\n',
            )
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine)

            report = migrate.migrate_to_github(
                context,
                migrate.MigrationOptions(apply=True, repo="octo/legacy", external_ledger=True),
                gh_client=client,
            )
            # Only WP-0003 was created; WP-0002's stale forward reference WAS rewritten to #3 on resume!
            self.assertEqual(report["created_issues"], {"WP-0003": "GH-3"})
            self.assertIn("Builds on #1 and prepares for #3.", fake_gh.issues[2]["body"])

            # Verify T3 requests, sessions, scope leases, and t3-workspace.json all re-keyed to GH-2 & external runs_dir!
            migrated_ctx = Context.load(repo, machine, gh_client=client)
            dest_runs = migrated_ctx.project.runs_dir
            req_after = json.loads((dest_runs / "t3" / "requests" / "req-1.json").read_text(encoding="utf-8"))
            self.assertEqual(req_after["payload"]["wp"], "GH-2")
            self.assertEqual(req_after["payload"]["title"], "GH-2: builder")
            self.assertEqual(req_after["payload"]["log_path"], str(dest_runs / "GH-2" / "attempt-1-builder.log"))
            self.assertEqual(req_after["payload"]["heartbeat_file"], str(dest_runs / "GH-2" / "heartbeat"))
            self.assertEqual(req_after["payload"]["progress_file"], str(dest_runs / "GH-2" / "state.json"))
            sess_after = json.loads((dest_runs / "t3" / "sessions" / "req-1.json").read_text(encoding="utf-8"))
            self.assertEqual(sess_after["env"]["WPO_WORK_PACKAGE"], "GH-2")
            scope_after = json.loads((dest_runs / "t3" / "scope" / "abc.json").read_text(encoding="utf-8"))
            self.assertEqual(scope_after["log_path"], str(dest_runs / "GH-2" / "attempt-1-builder.log"))

            # Verify _remap_run_path resolves symlinked/alias prefixes (mirrors Windows 8.3 short paths & macOS /var).
            alias_parent = base / "alias-runs"
            try:
                alias_parent.symlink_to(old_runs)
            except OSError:
                alias_parent = None
            if alias_parent is not None:
                remapped = migrate._remap_run_path(
                    str(alias_parent / "WP-0002" / "attempt-1-builder.log"),
                    old_runs,
                    dest_runs,
                    {"WP-0002": "GH-2"},
                )
                self.assertEqual(remapped, str(dest_runs / "GH-2" / "attempt-1-builder.log"))

            # Post-migration local updates to GH-2 MUST survive an idempotent re-run (with paginated GraphQL)!
            run_state.set_status(dest_runs / "GH-2", "review")
            run_state.append_progress(dest_runs / "GH-2", "Post-migration work completed", "builder")
            run_state.set_try_it(dest_runs / "GH-2", "```sh\necho post-migration\n```")
            (dest_runs / migrate.MIGRATION_STATE_FILE).unlink()

            rerun = migrate.migrate_to_github(
                migrated_ctx,
                migrate.MigrationOptions(apply=True, repo="octo/legacy", external_ledger=True),
                gh_client=client,
            )
            self.assertEqual(rerun["created_issues"], {})
            self.assertEqual(len(fake_gh.issues), 3)
            gh2_state = run_state.load(dest_runs / "GH-2")
            self.assertEqual(gh2_state["status"], "review")
            self.assertIn("Post-migration work completed", gh2_state["progress"][-1]["text"])
            self.assertIn("post-migration", gh2_state["try_it"])

    def test_closed_pr_references_paging_and_manual_pull_hooks_and_fetch_failure_guard(self) -> None:
        # R13 + R14 + R15 + R16:
        # - GraphQL uses autoMergeAllowed & closedByPullRequestsReferences(includeClosedPrs: true) with pagination
        # - Manual pull before reconcile still executes post_merge_hooks once (via merge_commit^1)
        # - Failed git fetch with missing merge_commit never marks local_synced=True against stale origin/main
        # - Merging live PR with merge_commit=None never clobbers a previously recorded merge_commit OID
        with tempfile.TemporaryDirectory(prefix="wpo-r13-r16-") as tmp:
            base = Path(tmp)
            hook_log = base / "hook-count.txt"
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "verify_workspace": [f"{sys.executable} -c \"import pathlib; assert pathlib.Path('src/app.py').exists()\""],
                "post_merge_hooks": [{
                    "when_changed": ["src/**"],
                    "command": f"{sys.executable} -c \"import pathlib; p=pathlib.Path(r'{hook_log}'); p.write_text(str(int(p.read_text())+1 if p.exists() else 1))\"",
                }],
                "github": {"repo": "octo/demo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            # closed_pr_page_size=1 + omit_open_prs_query=True forces merged-PR discovery exclusively
            # via paginated closedByPullRequestsReferences(includeClosedPrs: true)!
            fake_gh = FakeGh("octo/demo", bare_remote=bare, closed_pr_page_size=1, omit_open_prs_query=True)
            i1 = fake_gh.add_issue("Paginated closed PRs", "Goal.\n\n**Done when:**\n- Done.\n")
            i2 = fake_gh.add_issue("Depends on #1", "Goal.\n\n**Done when:**\n- Done.\n", blocked_by=[i1])
            # Attach an earlier closed-unmerged PR on page 1 so the real merged PR lands on page 2!
            fake_gh.prs[99] = {
                "number": 99,
                "title": "Abandoned draft",
                "body": f"Fixes #{i1}",
                "url": "https://github.com/octo/demo/pull/99",
                "state": "CLOSED",
                "merged": False,
                "headRefName": "abandoned/GH-1",
                "headRefOid": "0000000",
                "baseRefName": "main",
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
                "checkState": "FAILURE",
            }
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)
            context.tracker.sync(force=True)
            self.assertTrue(context.tracker.status_report()["allow_auto_merge"])

            wt = context.project.worktree(f"GH-{i1}")
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-q", "-b", context.project.branch(f"GH-{i1}"), str(wt), "main")
            (wt / "src" / "app.py").write_text("VALUE = 77\n", encoding="utf-8")
            context.set_wp_try_it(context.wp_in_tree(f"GH-{i1}", wt), "Nothing to try by hand: test.")
            context.set_wp_status(context.wp_in_tree(f"GH-{i1}", wt), "done")
            self.assertEqual(integrate(context, f"GH-{i1}"), EXIT_PR_OPENED)

            # Merge PR #100 in remote AND manually pull local main BEFORE reconcile runs (simulates manual pull / post-ff crash).
            merge_sha = fake_gh.merge_pr_in_remote(100)
            git(repo, "pull", "--ff-only", "-q", "origin", "main")

            # Verify R14: fresh sync finds merged PR #100 on page 2 of closedByPullRequestsReferences!
            wps = {wp.id: wp for wp in context.effective_wps(refresh=True)}
            self.assertTrue(wps[f"GH-{i1}"].is_merged)
            self.assertIn(f"GH-{i1}", context.merged_done_ids())
            self.assertEqual(wps[f"GH-{i2}"].depends_on, [f"GH-{i1}"])

            # Verify R15: reconcile still runs post_merge_hooks ONCE (via merge_commit^1) even though moved==False!
            rec1 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec1["merged"], [f"GH-{i1}"])
            self.assertEqual(rec1["pending_local_sync"], [])
            self.assertEqual(hook_log.read_text(encoding="utf-8"), "1")
            self.assertFalse(wt.exists())
            st = run_state.load(context.project.wp_run_dir(f"GH-{i1}"))["pr"]
            self.assertTrue(st["hooks_ran"])
            self.assertTrue(st["local_synced"])
            # Second reconcile does NOT re-run hooks.
            github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(hook_log.read_text(encoding="utf-8"), "1")

            # Verify R16: (a) _merge_pr_records never overwrites a known merge_commit with None;
            # (b) failed git fetch with missing merge_commit NEVER marks local_synced=True against stale origin/main!
            merged_rec = github._merge_pr_records({"merge_commit": merge_sha, "hooks_ran": True}, {"merge_commit": None, "state": "MERGED"})
            self.assertEqual(merged_rec["merge_commit"], merge_sha)

            i3 = fake_gh.add_issue("No-oid fetch fail", "Goal.\n\n**Done when:**\n- Done.\n")
            fake_gh.prs[101] = {
                "number": 101,
                "title": "No oid PR",
                "body": f"Fixes #{i3}",
                "url": "https://github.com/octo/demo/pull/101",
                "state": "MERGED",
                "merged": True,
                "mergedAt": "2026-11-21T12:00:00Z",
                "mergeCommit": None,
                "headRefName": f"wp/GH-{i3}",
                "headRefOid": "deadbeef",
                "baseRefName": "main",
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
                "checkState": "SUCCESS",
            }
            fake_gh.issues[i3]["state"] = "CLOSED"
            git(repo, "remote", "set-url", "origin", str(base / "nonexistent-remote.git"))
            rec_fail = github.reconcile_open_prs(context, force_sync=True)
            self.assertIn(f"GH-{i3}", rec_fail["pending_local_sync"])
            self.assertFalse(run_state.load(context.project.wp_run_dir(f"GH-{i3}"))["pr"]["local_synced"])

    def test_post_merge_hook_range_isolation_non_main_checkout_and_missing_hook_base(self) -> None:
        # R17 + R18 + R19 + R20:
        # - R17: missing merge_commit with moved==False and configured hooks does NOT mark hooks_ran or local_synced
        # - R18: two external squash merges on main diff each PR's exact merge_commit^1..merge_commit so hook runs once
        # - R19: checkout on a non-main branch defers hooks & local_synced even when main ref contains merge_commit
        # - R20: run_post_merge_hooks logs per-hook ok/failed and attributes integration-failed event to wp_id
        with tempfile.TemporaryDirectory(prefix="wpo-r17-r20-") as tmp:
            base = Path(tmp)
            hook_log = base / "hook-count.txt"
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "post_merge_hooks": [{
                    "when_changed": ["src/b.py"],
                    "command": f"{sys.executable} -c \"import pathlib; p=pathlib.Path(r'{hook_log}'); p.write_text(str(int(p.read_text())+1 if p.exists() else 1))\"",
                }],
                "github": {"repo": "octo/demo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)
            # Create a side branch BEFORE any merges so we can test R19 (non-main checkout).
            git(repo, "branch", "side-work")

            # Push two sequential commits onto main representing two merged PRs: PR 1 touches src/app.py, PR 2 touches src/b.py.
            (repo / "src" / "app.py").write_text("VALUE = 10\n", encoding="utf-8")
            git(repo, "add", "src/app.py")
            git(repo, "commit", "-q", "-m", "PR 1 squash")
            sha1 = git(repo, "rev-parse", "HEAD").strip()

            (repo / "src" / "b.py").write_text("B = 1\n", encoding="utf-8")
            git(repo, "add", "src/b.py")
            git(repo, "commit", "-q", "-m", "PR 2 squash")
            sha2 = git(repo, "rev-parse", "HEAD").strip()
            git(repo, "push", "-q", "origin", "main")

            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i1 = fake_gh.add_issue("Touch app.py", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            i2 = fake_gh.add_issue("Touch b.py", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            i3 = fake_gh.add_issue("Missing merge_commit", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            fake_gh.prs[101] = {
                "number": 101, "title": "PR 1", "body": f"Fixes #{i1}", "url": "https://github.com/octo/demo/pull/101",
                "state": "MERGED", "merged": True, "mergedAt": "2026-11-21T12:00:00Z", "mergeCommit": {"oid": sha1},
                "headRefName": "wp/GH-1", "headRefOid": sha1, "baseRefName": "main", "isDraft": False,
                "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "checkState": "SUCCESS",
            }
            fake_gh.prs[102] = {
                "number": 102, "title": "PR 2", "body": f"Fixes #{i2}", "url": "https://github.com/octo/demo/pull/102",
                "state": "MERGED", "merged": True, "mergedAt": "2026-11-21T12:01:00Z", "mergeCommit": {"oid": sha2},
                "headRefName": "wp/GH-2", "headRefOid": sha2, "baseRefName": "main", "isDraft": False,
                "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "checkState": "SUCCESS",
            }
            fake_gh.prs[103] = {
                "number": 103, "title": "PR 3", "body": f"Fixes #{i3}", "url": "https://github.com/octo/demo/pull/103",
                "state": "MERGED", "merged": True, "mergedAt": "2026-11-21T12:02:00Z", "mergeCommit": None,
                "headRefName": "wp/GH-3", "headRefOid": "deadbeef", "baseRefName": "main", "isDraft": False,
                "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "checkState": "SUCCESS",
            }
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)

            # R19: checkout on side-work (cut before merges) -> hooks must NOT run and none become local_synced!
            git(repo, "checkout", "-q", "side-work")
            rec_off = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(set(rec_off["pending_local_sync"]), {f"GH-{i1}", f"GH-{i2}", f"GH-{i3}"})
            self.assertFalse(hook_log.exists())
            for issue_id in (i1, i2, i3):
                pr_st = run_state.load(context.project.wp_run_dir(f"GH-{issue_id}"))["pr"]
                self.assertFalse(pr_st.get("hooks_ran"))
                self.assertFalse(pr_st.get("local_synced"))

            # Switch back to clean main -> R18: hook matches only src/b.py (sha2), so it runs ONCE (not twice!);
            # R17: GH-3 (missing merge_commit, moved==False) still stays pending with hooks_ran=False & local_synced=False!
            git(repo, "checkout", "-q", "main")
            rec_on = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec_on["pending_local_sync"], [f"GH-{i3}"])
            self.assertEqual(hook_log.read_text(encoding="utf-8"), "1")
            self.assertTrue(run_state.load(context.project.wp_run_dir(f"GH-{i1}"))["pr"]["local_synced"])
            self.assertTrue(run_state.load(context.project.wp_run_dir(f"GH-{i2}"))["pr"]["local_synced"])
            pr3_st = run_state.load(context.project.wp_run_dir(f"GH-{i3}"))["pr"]
            self.assertFalse(pr3_st.get("hooks_ran"))
            self.assertFalse(pr3_st.get("local_synced"))

            # R20: run_post_merge_hooks logs ok/failed via log_fn and emits integration-failed with wp field.
            from wpo.integrate import run_post_merge_hooks
            logged: list[str] = []
            run_post_merge_hooks(context, f"{sha2}^1", after=sha2, wp_id="GH-2", log_fn=logged.append)
            self.assertTrue(any("post-merge hook" in line and line.endswith(": ok") for line in logged))
            context.project.config["post_merge_hooks"] = [{"when_changed": ["src/b.py"], "command": f"{sys.executable} -c \"raise SystemExit(1)\""}]
            logged.clear()
            run_post_merge_hooks(context, f"{sha2}^1", after=sha2, wp_id="GH-2", log_fn=logged.append)
            self.assertTrue(any("post-merge hook" in line and line.endswith(": failed") for line in logged))
            failed_events = [e for e in context.journal.read(0) if e.get("kind") == "integration-failed"]
            self.assertTrue(any(e.get("wp") == "GH-2" for e in failed_events))

    def test_fast_forward_never_masks_earlier_or_later_pr_hook_ranges(self) -> None:
        # R21:
        # Case 1: local main already at PR1's squash (touches src/app.py), fast-forward adds PR2 (touches src/b.py),
        #         hook matches src/app.py -> PR1 STILL uses sha1^1..sha1 and runs the hook!
        # Case 2: unrelated fast-forward occurs while PR3's merge_commit is not yet on main -> PR3 is NOT stamped
        #         with the unrelated range; once PR3 lands, PR3 runs its hook cleanly!
        with tempfile.TemporaryDirectory(prefix="wpo-r21-") as tmp:
            base = Path(tmp)
            hook_log = base / "hook-count.txt"
            config = {
                "name": "demo",
                "tracker": "github",
                "delivery": "pr-automerge",
                "runs_dir": "external",
                "post_merge_hooks": [{
                    "when_changed": ["src/app.py", "src/c.py"],
                    "command": f"{sys.executable} -c \"import pathlib; p=pathlib.Path(r'{hook_log}'); p.write_text(str(int(p.read_text())+1 if p.exists() else 1))\"",
                }],
                "github": {"repo": "octo/demo"},
            }
            repo, bare, machine = make_repo_with_remote(base, config)

            # Commit PR1 (src/app.py) onto main and push; local main is now at sha1.
            (repo / "src" / "app.py").write_text("VALUE = 11\n", encoding="utf-8")
            git(repo, "add", "src/app.py")
            git(repo, "commit", "-q", "-m", "PR 1 squash")
            sha1 = git(repo, "rev-parse", "HEAD").strip()
            git(repo, "push", "-q", "origin", "main")

            # Push PR2 (src/b.py) to bare remote from a separate clone so local main stays at sha1 (not yet at sha2).
            other = base / "other-clone"
            git(base, "clone", "-q", str(bare), str(other))
            git(other, "config", "user.email", "test@example.com")
            git(other, "config", "user.name", "test")
            (other / "src" / "b.py").write_text("B = 2\n", encoding="utf-8")
            git(other, "add", "src/b.py")
            git(other, "commit", "-q", "-m", "PR 2 squash")
            sha2 = git(other, "rev-parse", "HEAD").strip()
            git(other, "push", "-q", "origin", "main")

            # Create PR3 commit (src/c.py) in other clone, NOT pushed to bare remote yet!
            (other / "src" / "c.py").write_text("C = 3\n", encoding="utf-8")
            git(other, "add", "src/c.py")
            git(other, "commit", "-q", "-m", "PR 3 squash")
            sha3 = git(other, "rev-parse", "HEAD").strip()

            fake_gh = FakeGh("octo/demo", bare_remote=bare)
            i1 = fake_gh.add_issue("PR1 app.py", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            i2 = fake_gh.add_issue("PR2 b.py", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            i3 = fake_gh.add_issue("PR3 c.py", "Goal.\n\n**Done when:**\n- Done.\n", state="CLOSED")
            fake_gh.prs[201] = {
                "number": 201, "title": "PR1", "body": f"Fixes #{i1}", "url": "https://github.com/octo/demo/pull/201",
                "state": "MERGED", "merged": True, "mergedAt": "2026-11-21T12:00:00Z", "mergeCommit": {"oid": sha1},
                "headRefName": "wp/GH-1", "headRefOid": sha1, "baseRefName": "main", "isDraft": False,
                "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "checkState": "SUCCESS",
            }
            fake_gh.prs[202] = {
                "number": 202, "title": "PR2", "body": f"Fixes #{i2}", "url": "https://github.com/octo/demo/pull/202",
                "state": "MERGED", "merged": True, "mergedAt": "2026-11-21T12:01:00Z", "mergeCommit": {"oid": sha2},
                "headRefName": "wp/GH-2", "headRefOid": sha2, "baseRefName": "main", "isDraft": False,
                "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "checkState": "SUCCESS",
            }
            fake_gh.prs[203] = {
                "number": 203, "title": "PR3", "body": f"Fixes #{i3}", "url": "https://github.com/octo/demo/pull/203",
                "state": "MERGED", "merged": True, "mergedAt": "2026-11-21T12:02:00Z", "mergeCommit": {"oid": sha3},
                "headRefName": "wp/GH-3", "headRefOid": sha3, "baseRefName": "main", "isDraft": False,
                "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "checkState": "SUCCESS",
            }
            client = github.GhClient("gh", cwd=repo, runner=fake_gh)
            context = Context.load(repo, machine, gh_client=client)

            # Reconcile fast-forwards local main sha1 -> sha2 (PR2 only):
            # - PR1 (already in sha1) MUST still run hook for src/app.py!
            # - PR3 (sha3 not yet pushed) MUST stay pending with NO stale pre_sync_sha stamped!
            rec1 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec1["pending_local_sync"], [f"GH-{i3}"])
            self.assertEqual(hook_log.read_text(encoding="utf-8"), "1")
            pr3_mid = run_state.load(context.project.wp_run_dir(f"GH-{i3}"))["pr"]
            self.assertIsNone(pr3_mid.get("pre_sync_sha"))
            self.assertFalse(pr3_mid.get("hooks_ran"))

            # Now push sha3 to remote and reconcile -> PR3 runs hook for src/c.py (count becomes 2)!
            git(other, "push", "-q", "origin", "main")
            rec2 = github.reconcile_open_prs(context, force_sync=True)
            self.assertEqual(rec2["pending_local_sync"], [])
            self.assertEqual(hook_log.read_text(encoding="utf-8"), "2")
            self.assertTrue(run_state.load(context.project.wp_run_dir(f"GH-{i3}"))["pr"]["local_synced"])


if __name__ == "__main__":
    unittest.main()
