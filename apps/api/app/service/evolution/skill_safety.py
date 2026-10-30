"""Conservative, text-only skill patch validation. Never executes skill code."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from app.utils.redaction import redact_text

MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
_METADATA = {
    f"{stem}.{ext}" for stem in ("manifest", "skill") for ext in ("json", "yaml", "yml")
} | {"package.json"}
_SECRET_PATTERNS = (
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@",
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    r"""(?im)\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key)\s*[:=]\s*["'][^"'\n]{4,}["']""",
)
_DANGEROUS_PATTERNS = (
    r"\brm\s+(?:-[\w]*r[\w]*\s+|-[\w]*f[\w]*\s+)",
    r"\b(?:curl|wget)\b[^\n]*\|\s*(?:ba|z|da|k)?sh\b",
    r"\b(?:eval|exec)\s*\(",
    r"\b(?:os\.system|os\.popen|subprocess\.(?:run|call|Popen|check_call|check_output)|child_process|Runtime\.getRuntime)\b",
    r"\b(?:sudo|mkfs|shutdown|reboot)\b",
    r"\bchmod\s+(?:[0-7]*[46][0-7]{2}|[augo]*\+s)\b",
    r"/dev/(?:tcp|udp)/",
    r"\b(?:shutil\.rmtree|pickle\.loads?|marshal\.loads?)\s*\(",
    r"\b(?:bash|sh|zsh)\s+-[a-z]*c\b",
    r":\s*\(\s*\)\s*\{\s*:\s*\|",
)


def validate_path(path: str) -> None:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ValueError("Invalid skill path")
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("Unsafe skill path")
    parts = path.split("/")
    if path.startswith("/") or ":" in path or any(p in ("", ".", "..") for p in parts):
        raise ValueError("Unsafe skill path")
    if any(p.lower() == ".git" for p in parts) or str(PurePosixPath(path)) != path:
        raise ValueError("Unsafe skill path")


def _frontmatter(text: str) -> str:
    if text.startswith("\ufeff"):
        raise ValueError("BOM-prefixed SKILL.md is unsupported")
    lines = text.splitlines(keepends=True)
    if not lines or not re.fullmatch(r"---[ \t]*(?:#.*)?", lines[0].rstrip("\r\n")):
        return ""
    for i, line in enumerate(lines[1:], 1):
        if re.fullmatch(r"(?:---|\.\.\.)[ \t]*(?:#.*)?", line.rstrip("\r\n")):
            return "".join(lines[: i + 1])
    raise ValueError("Unterminated SKILL.md frontmatter")


def reject_secrets(text: str) -> None:
    """Reject likely credentials without echoing matched content into errors/logs.

    Callers should log only this generic error, never the rejected payload.
    Credential-looking examples and environment assignments are deliberately rejected too.
    This heuristic cannot guarantee detection of all credential formats.
    """
    if not isinstance(text, str):
        raise ValueError("Expected text")
    if redact_text(text) != text or any(re.search(pattern, text) for pattern in _SECRET_PATTERNS):
        raise ValueError("Potential secret in content")


def _check_files(files: dict[str, str]) -> None:
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError("Too many skill files or invalid file mapping")
    total = 0
    for path, content in files.items():
        validate_path(path)
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Skill files must be text")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("Invalid Unicode content") from exc
        if size > MAX_FILE_BYTES:
            raise ValueError("Skill file exceeds size limit")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Skill exceeds total size limit")
    if "SKILL.md" not in files:
        raise ValueError("SKILL.md is required")
    paths = set(files)
    for path in paths:
        if any("/".join(path.split("/")[:i]) in paths for i in range(1, len(path.split("/")))):
            raise ValueError("File and directory path collision")


def validate_patch(files: dict[str, str], changes: dict[str, str | None]) -> dict[str, str]:
    """Return a validated copy; deletion is represented by None.

    This denylist is defense in depth, not a sandbox or proof that code is safe.
    Consumers must never execute candidate scripts as part of validation.
    """
    _check_files(files)
    _frontmatter(files["SKILL.md"])
    for original_content in files.values():
        reject_secrets(original_content)
    if not isinstance(changes, dict) or len(changes) > MAX_FILES:
        raise ValueError("Invalid or oversized patch")
    result = dict(files)
    for path, content in changes.items():
        validate_path(path)
        if PurePosixPath(path).name.lower() in _METADATA:
            raise ValueError("Metadata files are immutable")
        if content is None:
            if path == "SKILL.md":
                raise ValueError("SKILL.md cannot be deleted")
            if path not in result:
                raise ValueError("Cannot delete a missing file")
            del result[path]
            continue
        if not isinstance(content, str):
            raise ValueError("Patch contents must be text or None")
        if path.lower().endswith(".md") and content != files.get(path):
            body = [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if body and all(line in {"...", "…", "……"} for line in body):
                raise ValueError("Markdown replacement is only an omission placeholder")
        if path == "SKILL.md" and _frontmatter(content) != _frontmatter(files[path]):
            raise ValueError("SKILL.md frontmatter is immutable")
        reject_secrets(content)
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                raise ValueError("Potentially dangerous code in patch")
        result[path] = content
    _check_files(result)
    return result


def reject_sensitive_payload(value: Any) -> None:
    """Inspect both structured credential fields and unescaped embedded text."""
    reject_secrets(json.dumps(value, allow_nan=False))
    if isinstance(value, dict):
        for item in value.values():
            reject_sensitive_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_sensitive_payload(item)
    elif isinstance(value, str):
        reject_secrets(value)
