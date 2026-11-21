"""Outbound privacy guard for GitHub writes.

WPO keeps transcripts, raw logs, findings back-and-forth, local paths and account telemetry in the
private local ledger. Before any issue body, PR body or title is sent to GitHub, this module checks
for secret patterns, personal home paths and raw ledger markers so private state cannot leak.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("OpenAI / generic sk token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Bearer credential", re.compile(r"(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("inline secret assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9._/+~=-]{16,}['\"]?")),
)

LOCAL_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Unix home path", re.compile(r"(?<![A-Za-z0-9._~-])(?<![A-Za-z0-9._~-]/)/(?:home|Users)/[A-Za-z0-9._-]+(?:/[^\s`'\")>]+)?")),
    ("Home-relative path", re.compile(r"(?<![A-Za-z0-9._~/-])~/(?:[^\s`'\")>]+)")),
    ("Windows user path", re.compile(r"(?<![A-Za-z0-9._\\])[A-Za-z]:\\Users\\[A-Za-z0-9._-]+(?:\\[^\s`'\")>]+)?")),
)

LEDGER_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal WPO session variable", re.compile(r"\bWPO_T3_REQUEST_ID\b")),
    ("local runs directory reference", re.compile(r"(?:\.orchestration/runs/|/wp-orchestration/projects/)")),
    ("private routing pin", re.compile(r"(?<![A-Za-z0-9._-])pin:[A-Za-z0-9._-]+\b")),
)

SESSION_JSONL_TYPES = frozenset({"user", "assistant", "system", "result", "tool_use", "tool_result"})
SESSION_JSONL_FIELDS = frozenset({
    "message", "session_id", "uuid", "parent_tool_use_id", "request_id", "usage",
    "total_cost_usd", "modelUsage", "subtype", "content", "role", "timestamp", "tool_use_id",
    "backend", "is_error",
})
AGY_JSONL_EVENTS = frozenset({"user", "result", "step_update", "init", "error"})
AGY_JSONL_FIELDS = frozenset({"message", "result", "step_update", "session_id", "error", "tools"})
OPENCODE_JSONL_TYPES = frozenset({"text", "step_start", "step_finish", "tool_use", "error"})
TRUNCATED_SESSION_TYPE_RE = re.compile(r'"(?:type|event)"\s*:\s*"(?:user|assistant|system|result|step_update)"')
TRUNCATED_SESSION_FIELD_RE = re.compile(
    r'"(?:message|result|step_update|session_id|sessionID|uuid|parent_tool_use_id|total_cost_usd|modelUsage|tool_use_id|request_id|subtype|part)"\s*:'
)


def _extract_route_ids(section: object) -> list[str]:
    found: list[str] = []
    if isinstance(section, dict):
        for route_id in section:
            text = str(route_id).strip()
            if text:
                found.append(text)
    elif isinstance(section, list):
        for entry in section:
            if isinstance(entry, dict) and entry.get("id"):
                text = str(entry["id"]).strip()
                if text:
                    found.append(text)
    return found


def private_route_identifiers(machine: object | None, project: object | None = None,
                              router: object | None = None) -> tuple[str, ...]:
    """Extracts configured private route identifiers from machine/project routing policy
    (`routing.routes`, `routing.routes_extra`, and legacy top-level `routes`)."""
    del router
    identifiers: list[str] = []
    raw = getattr(machine, "raw", None)
    if isinstance(raw, dict):
        identifiers.extend(_extract_route_ids(raw.get("routes")))
        routing_cfg = raw.get("routing")
        if isinstance(routing_cfg, dict):
            identifiers.extend(_extract_route_ids(routing_cfg.get("routes")))
            identifiers.extend(_extract_route_ids(routing_cfg.get("routes_extra")))
    proj_cfg = getattr(project, "config", None)
    if isinstance(proj_cfg, dict):
        proj_routing = proj_cfg.get("routing")
        if isinstance(proj_routing, dict):
            identifiers.extend(_extract_route_ids(proj_routing.get("routes")))
            identifiers.extend(_extract_route_ids(proj_routing.get("routes_extra")))
    return tuple(sorted(set(identifiers)))


def _detect_session_jsonl(text: str) -> bool:
    """Detects serialized session transcript JSONL lines (Claude CLI, OpenCode, agy) regardless of key order."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "{" not in line:
            continue
        start = line.find("{")
        end = line.rfind("}")
        if end > start:
            candidate = line[start:end + 1]
            try:
                parsed = json.loads(candidate)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                keys = set(parsed.keys())
                kind = parsed.get("type")
                if isinstance(kind, str) and kind in SESSION_JSONL_TYPES and (SESSION_JSONL_FIELDS & keys):
                    return True
                event = parsed.get("event")
                if isinstance(event, str) and event in AGY_JSONL_EVENTS and (AGY_JSONL_FIELDS & keys):
                    return True
                if "sessionID" in parsed and ("part" in parsed or "type" in parsed):
                    return True
                if isinstance(kind, str) and kind in OPENCODE_JSONL_TYPES and isinstance(parsed.get("part"), dict):
                    return True
        if TRUNCATED_SESSION_TYPE_RE.search(line) and TRUNCATED_SESSION_FIELD_RE.search(line):
            return True
    return False


class PrivacyError(ValueError):
    """Refused an outbound GitHub write because it contained secrets or private local state."""

    def __init__(self, findings: list[str]) -> None:
        self.findings = list(findings)
        summary = "; ".join(self.findings)
        super().__init__(f"blocked outbound GitHub text for privacy safety: {summary}")


def sanitize_for_github(text: str, project_root: Path | None = None) -> str:
    """Rewrites repository-root paths to repo-relative paths while leaving secrets untouched so
    `check_public_text` catches and refuses them rather than silently masking credentials."""
    cleaned = text
    if project_root is not None:
        resolved = project_root.resolve()
        variants = {str(resolved), resolved.as_posix(), str(project_root), project_root.as_posix()}
        for root_text in sorted(variants, key=len, reverse=True):
            if not root_text:
                continue
            cleaned = cleaned.replace(root_text + "/", "").replace(root_text + "\\", "").replace(root_text, ".")
    return cleaned


def check_public_text(text: str, project_root: Path | None = None, *, allow_local_paths: bool = False,
                      extra_forbidden: Iterable[str] = ()) -> list[str]:
    """Returns human-readable privacy violations found in `text` (empty when safe for GitHub)."""
    if not text:
        return []
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"suspected {label}")
    if _detect_session_jsonl(text):
        findings.append("internal ledger content (raw session JSONL)")
    for label, pattern in LEDGER_LEAK_PATTERNS:
        if pattern.search(text):
            findings.append(f"internal ledger content ({label})")
    for ident in extra_forbidden:
        token = str(ident).strip()
        if not token:
            continue
        if re.search(rf"(?<![A-Za-z0-9._-]){re.escape(token)}(?![A-Za-z0-9._-])", text):
            findings.append(f"private routing identifier ({token})")
    if not allow_local_paths:
        checked = sanitize_for_github(text, project_root)
        for label, pattern in LOCAL_PATH_PATTERNS:
            if match := pattern.search(checked):
                findings.append(f"personal filesystem path ({label}: {match.group(0)[:48]})")
    return findings


def _machine_path_variants(path: Path | None) -> list[str]:
    if path is None:
        return []
    variants: set[str] = set()
    for candidate in (path, path.resolve()):
        for text in (str(candidate), candidate.as_posix()):
            cleaned = text.rstrip("/\\")
            if len(cleaned) >= 4:
                variants.add(cleaned)
    return sorted(variants, key=len, reverse=True)


def check_code_diff_text(
    text: str,
    project_root: Path | None = None,
    runs_dir: Path | None = None,
    *,
    extra_forbidden: Iterable[str] = (),
) -> list[str]:
    """Privacy scan tailored to committed code diffs and commit messages (A48): blocks secrets,
    this machine's real home/project/ledger paths, internal session markers, and configured
    private route IDs without false-positiving on generic doc examples (`~/.cargo`) or JSONL fixtures."""
    if not text:
        return []
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"suspected {label}")
    if re.search(r"\bWPO_T3_REQUEST_ID\b", text):
        findings.append("internal ledger content (internal WPO session variable)")
    if re.search(r"/wp-orchestration/projects/", text):
        findings.append("internal ledger content (external ledger path reference)")
    for ident in extra_forbidden:
        token = str(ident).strip()
        if not token:
            continue
        if re.search(rf"(?<![A-Za-z0-9._-]){re.escape(token)}(?![A-Za-z0-9._-])", text):
            findings.append(f"private routing identifier ({token})")

    # Check machine-specific absolute paths (real home dir, checkout root, ledger dir).
    try:
        home_path: Path | None = Path.home()
    except RuntimeError:
        home_path = None
    path_checks: list[tuple[str, Path | None]] = [
        ("local home path", home_path),
        ("local project path", project_root),
        ("local ledger path", runs_dir),
    ]
    for label, target in path_checks:
        for variant in _machine_path_variants(target):
            # Match `<variant>` (including inside `file:///`, `file://localhost`, `vscode://file`, and `//` URIs)
            # without matching `/srv/home/...` or `/home/Tommy2`.
            flags = re.IGNORECASE if "\\" in variant else 0
            pattern = re.compile(
                rf"(?:(?<![A-Za-z0-9._\\-])(?<![A-Za-z0-9._-]/)|[A-Za-z][A-Za-z0-9+.-]*://[^\s/]*){re.escape(variant)}(?:[/\\]|(?![A-Za-z0-9._-]))",
                flags,
            )
            if pattern.search(text):
                findings.append(f"personal filesystem path ({label}: {variant[:48]})")
                break
    return findings


def require_public_safe(text: str, project_root: Path | None = None, *, enabled: bool = True,
                        allow_local_paths: bool = False, extra_forbidden: Iterable[str] = ()) -> str:
    """Sanitizes repo-root paths and raises PrivacyError if any secret or personal path remains."""
    cleaned = sanitize_for_github(text, project_root)
    if not enabled:
        return cleaned
    violations = check_public_text(cleaned, project_root, allow_local_paths=allow_local_paths, extra_forbidden=extra_forbidden)
    if violations:
        raise PrivacyError(violations)
    return cleaned
