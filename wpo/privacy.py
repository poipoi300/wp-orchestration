"""Outbound privacy guard for GitHub writes.

WPO keeps transcripts, raw logs, findings back-and-forth, local paths and account telemetry in the
private local ledger. Before any issue body, PR body or title is sent to GitHub, this module checks
for secret patterns, personal home paths and raw ledger markers so private state cannot leak.
"""

from __future__ import annotations

import re
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
    ("Unix home path", re.compile(r"(?<![A-Za-z0-9._~/-])/(?:home|Users)/[A-Za-z0-9._-]+(?:/[^\s`'\")>]+)?")),
    ("Home-relative path", re.compile(r"(?<![A-Za-z0-9._~/-])~/(?:[^\s`'\")>]+)")),
    ("Windows user path", re.compile(r"(?<![A-Za-z0-9._\\])[A-Za-z]:\\Users\\[A-Za-z0-9._-]+(?:\\[^\s`'\")>]+)?")),
)

LEDGER_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("raw session JSONL", re.compile(r'\{\s*"type"\s*:\s*"(?:assistant|system|result)"')),
    ("internal WPO session variable", re.compile(r"\bWPO_T3_REQUEST_ID\b")),
    ("local runs directory reference", re.compile(r"(?:\.orchestration/runs/|/wp-orchestration/projects/)")),
)


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
        variants = {str(resolved), resolved.as_posix()}
        for root_text in sorted(variants, key=len, reverse=True):
            if not root_text:
                continue
            cleaned = cleaned.replace(root_text + "/", "").replace(root_text + "\\", "").replace(root_text, ".")
    return cleaned


def check_public_text(text: str, project_root: Path | None = None, *, allow_local_paths: bool = False) -> list[str]:
    """Returns human-readable privacy violations found in `text` (empty when safe for GitHub)."""
    if not text:
        return []
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"suspected {label}")
    for label, pattern in LEDGER_LEAK_PATTERNS:
        if pattern.search(text):
            findings.append(f"internal ledger content ({label})")
    if not allow_local_paths:
        checked = sanitize_for_github(text, project_root)
        for label, pattern in LOCAL_PATH_PATTERNS:
            if match := pattern.search(checked):
                findings.append(f"personal filesystem path ({label}: {match.group(0)[:48]})")
    return findings


def require_public_safe(text: str, project_root: Path | None = None, *, enabled: bool = True, allow_local_paths: bool = False) -> str:
    """Sanitizes repo-root paths and raises PrivacyError if any secret or personal path remains."""
    cleaned = sanitize_for_github(text, project_root)
    if not enabled:
        return cleaned
    violations = check_public_text(cleaned, project_root, allow_local_paths=allow_local_paths)
    if violations:
        raise PrivacyError(violations)
    return cleaned
