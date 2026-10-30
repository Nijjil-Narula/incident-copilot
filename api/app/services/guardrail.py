"""
api/app/services/guardrail.py

Whole-command verification and destructive command detection guardrail for Incident Copilot.

Enforces:
  1. Known executables set.
  2. Extraction of complete runbook commands from chunks where doc_type == 'runbook'
     (fenced code blocks and inline code spans).
  3. Candidate command extraction from generated text:
     (a) Fenced code blocks (joining backslash-continued lines, stripping comments and $ prefixes).
     (b) Prose lines starting with known executables or runbook command first tokens.
     (c) Inline code spans where first token is a known executable or matches runbook command first token.
  4. Whole-command verification: Candidate is verified ONLY if it equals a complete runbook command
     or equals a complete runbook command with exactly ONE leading "sudo " removed. Dropping other prefixes
     (e.g. docker exec) or adding sudo to a command with no sudo is strictly rejected.
  5. Redaction of unverified commands with [UNVERIFIED COMMAND REMOVED].
  6. Detection of destructive commands (rm -rf, kubectl delete, drop table, truncate, etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple

KNOWN_EXECUTABLES: Set[str] = {
    "kubectl", "docker", "helm", "systemctl", "journalctl", "psql", "redis-cli",
    "kafka-topics", "kafka-console-producer", "kafka-console-consumer",
    "aws", "gcloud", "az", "curl", "grep", "cat", "rm", "ls", "kill",
    "chmod", "chown", "apt", "yum", "dnf", "git", "netstat", "ip", "ping",
    "top", "htop", "free", "df", "du", "sed", "awk", "tar", "gzip",
    "pg_dump", "pg_restore",
}

DESTRUCTIVE_PATTERN = re.compile(
    r"\b(rm\s+-rf|kubectl\s+delete|drop\s+(?:table|database)|truncate|mkfs|dd\s+of=|shutdown|reboot|flushall|flushdb)\b",
    re.IGNORECASE,
)


@dataclass
class GuardrailResult:
    """Result of running guardrail verification on generated LLM answer."""

    text: str
    guardrail: str  # "pass" | "redacted" | "no_context"
    unverified_commands: List[str] = field(default_factory=list)
    raw_answer: Optional[str] = None
    destructive_commands: List[str] = field(default_factory=list)


def parse_block_commands(code_text: str) -> List[str]:
    """
    Parses executable command lines from a fenced code block.

    Handles:
      - Multi-line commands with backslash continuations.
      - Stripping leading '$ ' shell prompts.
      - Ignoring comment lines (#...) and blank lines.
      - Collapsing internal whitespace.
    """
    lines = code_text.splitlines()
    commands: List[str] = []
    accumulator: List[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            if accumulator:
                joined = " ".join(accumulator)
                cleaned = re.sub(r"^\$\s*", "", joined).strip()
                cleaned = re.sub(r"\s+", " ", cleaned)
                if cleaned and not cleaned.startswith("#"):
                    commands.append(cleaned)
                accumulator = []
            continue

        if stripped.startswith("#") and not accumulator:
            continue

        if stripped.endswith("\\"):
            accumulator.append(stripped[:-1].strip())
        else:
            accumulator.append(stripped)
            joined = " ".join(accumulator)
            cleaned = re.sub(r"^\$\s*", "", joined).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and not cleaned.startswith("#"):
                commands.append(cleaned)
            accumulator = []

    if accumulator:
        joined = " ".join(accumulator)
        cleaned = re.sub(r"^\$\s*", "", joined).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned and not cleaned.startswith("#"):
            commands.append(cleaned)

    return commands


def extract_runbook_commands(chunks: List[Any]) -> Set[str]:
    """
    Extracts all valid shell and CLI commands from chunks where doc_type == 'runbook'.

    Commands are extracted from:
      1. Fenced code blocks (``` or ~~~).
      2. Inline code spans (`...`).
    """
    runbook_commands: Set[str] = set()
    fenced_re = re.compile(r"(?:```|~~~)(?:[a-zA-Z0-9_-]*\n)?(.*?)(?:```|~~~)", re.DOTALL)
    inline_re = re.compile(r"`([^`]+)`")

    for chunk in chunks:
        doc_type = getattr(chunk, "doc_type", "")
        if hasattr(doc_type, "value"):
            doc_type = doc_type.value
        if str(doc_type).lower() != "runbook":
            continue

        chunk_text = getattr(chunk, "text", "")
        if not chunk_text:
            continue

        # Extract from fenced code blocks
        for match in fenced_re.finditer(chunk_text):
            block_code = match.group(1)
            for cmd in parse_block_commands(block_code):
                runbook_commands.add(cmd)

        # Extract from inline code spans outside code fences
        text_without_fences = fenced_re.sub("", chunk_text)
        for match in inline_re.finditer(text_without_fences):
            span = match.group(1).strip()
            cleaned = re.sub(r"^\$\s*", "", span).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and not cleaned.startswith("#"):
                runbook_commands.add(cleaned)

    return runbook_commands


def is_verified_command(candidate: str, runbook_commands: Set[str]) -> bool:
    """
    Verifies a candidate command against the set of complete runbook commands.

    Rule:
      A candidate is verified only if, after whitespace normalization:
        1. It equals a complete runbook command.
        2. OR it equals a complete runbook command with exactly ONE leading "sudo "
           removed (e.g. runbook "sudo systemctl restart app", candidate "systemctl restart app").
      Nothing else is tolerated (e.g. dropping "docker exec app" or adding "sudo" to a command
      that has none in the runbook is NOT verified).
    """
    c_norm = " ".join(candidate.strip().split())
    if not c_norm:
        return False

    for r_cmd in runbook_commands:
        r_norm = " ".join(r_cmd.strip().split())
        if c_norm == r_norm:
            return True

        if r_norm.startswith("sudo "):
            r_without_sudo = r_norm[5:].lstrip()
            if c_norm == r_without_sudo:
                return True

    return False


def detect_destructive_commands(text: str) -> List[str]:
    """Detects dangerous or destructive CLI commands in text using regex."""
    matches = set()
    for m in DESTRUCTIVE_PATTERN.finditer(text):
        matches.add(m.group(0).lower().strip())
    return sorted(list(matches))


def _redact_code_block(
    block_code: str,
    runbook_commands: Set[str],
    unverified: List[str],
) -> str:
    """Verifies and redacts unverified commands inside a fenced code block."""
    raw_lines = block_code.splitlines()
    new_lines: List[str] = []
    i = 0

    while i < len(raw_lines):
        line = raw_lines[i]
        stripped = line.strip()
        if not stripped:
            new_lines.append(line)
            i += 1
            continue

        if stripped.startswith("#"):
            new_lines.append(line)
            i += 1
            continue

        # Collect multi-line command if continued with \
        cmd_parts = [stripped]
        orig_lines = [line]
        while stripped.endswith("\\") and i + 1 < len(raw_lines):
            i += 1
            line = raw_lines[i]
            stripped = line.strip()
            orig_lines.append(line)
            cmd_parts.append(stripped)

        # Build candidate command
        cleaned_parts = []
        for part in cmd_parts:
            if part.endswith("\\"):
                cleaned_parts.append(part[:-1].strip())
            else:
                cleaned_parts.append(part)
        raw_cmd = " ".join(cleaned_parts)
        candidate = re.sub(r"^\$\s*", "", raw_cmd).strip()
        candidate = re.sub(r"\s+", " ", candidate)

        if not candidate:
            new_lines.extend(orig_lines)
        elif is_verified_command(candidate, runbook_commands):
            new_lines.extend(orig_lines)
        else:
            unverified.append(candidate)
            new_lines.append("[UNVERIFIED COMMAND REMOVED]")

        i += 1

    return "\n".join(new_lines)


def _process_prose_line(
    line: str,
    command_triggers: Set[str],
    runbook_commands: Set[str],
    unverified: List[str],
) -> str:
    """
    Scans and processes a single prose line outside code fences.

    Strips list markers (-, *, "1.", "1)"), treats any line starting with a known
    executable token (or runbook command trigger) as a candidate command, and
    replaces unverified commands with [UNVERIFIED COMMAND REMOVED]. Also scans
    inline code spans for executable candidate commands.
    """
    # (c) Inline code spans
    inline_re = re.compile(r"`([^`]+)`")

    def replace_inline(m: re.Match) -> str:
        span_raw = m.group(1).strip()
        cleaned = re.sub(r"^\$\s*", "", span_raw).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            return m.group(0)

        first_token = cleaned.split()[0]
        if first_token in command_triggers:
            if is_verified_command(cleaned, runbook_commands):
                return m.group(0)
            unverified.append(cleaned)
            return "[UNVERIFIED COMMAND REMOVED]"
        return m.group(0)

    line = inline_re.sub(replace_inline, line)

    # (b) Prose line starting with known executable or runbook trigger
    # Strip list markers like "- ", "* ", "1. ", "1) "
    marker_match = re.match(r"^\s*(?:[-*+]\s+|\d+[\.\)]\s+)?(.*)$", line)
    if marker_match:
        content = marker_match.group(1)
        stripped_content = content.strip()
        # If line starts with shell prompt $
        norm_content = re.sub(r"^\$\s*", "", stripped_content).strip()
        if norm_content and not norm_content.startswith("`") and not norm_content.startswith("[UNVERIFIED"):
            first_token = norm_content.split()[0]
            if first_token in command_triggers:
                candidate = re.sub(r"\s+", " ", norm_content)
                if not is_verified_command(candidate, runbook_commands):
                    unverified.append(candidate)
                    return "[UNVERIFIED COMMAND REMOVED]"

    return line


def verify_guardrail(answer: str, chunks: List[Any]) -> GuardrailResult:
    """
    Executes whole-command verification on an LLM answer against retrieved runbook chunks.

    Returns a GuardrailResult with:
      - text: verified or redacted answer.
      - guardrail: "pass" or "redacted".
      - unverified_commands: list of detected commands that failed verification.
      - raw_answer: original answer text if redacted, otherwise None.
      - destructive_commands: detected dangerous commands for caution badges.
    """
    runbook_commands = extract_runbook_commands(chunks)

    # Command triggers = known executables + first tokens of all runbook commands
    runbook_first_tokens = {
        cmd.split()[0] for cmd in runbook_commands if cmd.split()
    }
    command_triggers = KNOWN_EXECUTABLES | runbook_first_tokens

    unverified: List[str] = []

    # Segment answer into code blocks and prose
    fence_pattern = re.compile(
        r"((?:```|~~~)[a-zA-Z0-9_-]*\n)(.*?)(```|~~~)",
        re.DOTALL,
    )

    last_end = 0
    result_parts: List[str] = []

    for match in fence_pattern.finditer(answer):
        # Process prose before the fence
        prose_before = answer[last_end:match.start()]
        prose_lines = prose_before.splitlines(keepends=True)
        for pline in prose_lines:
            has_newline = pline.endswith("\n")
            cleaned_line = pline[:-1] if has_newline else pline
            processed = _process_prose_line(cleaned_line, command_triggers, runbook_commands, unverified)
            result_parts.append(processed + ("\n" if has_newline else ""))

        # Process the fenced code block
        opening_fence = match.group(1)
        block_content = match.group(2)
        closing_fence = match.group(3)

        redacted_content = _redact_code_block(block_content, runbook_commands, unverified)
        if not redacted_content.endswith("\n"):
            redacted_content += "\n"
        result_parts.append(f"{opening_fence}{redacted_content}{closing_fence}")

        last_end = match.end()

    # Process remaining prose
    prose_after = answer[last_end:]
    if prose_after:
        prose_lines = prose_after.splitlines(keepends=True)
        for pline in prose_lines:
            has_newline = pline.endswith("\n")
            cleaned_line = pline[:-1] if has_newline else pline
            processed = _process_prose_line(cleaned_line, command_triggers, runbook_commands, unverified)
            result_parts.append(processed + ("\n" if has_newline else ""))

    final_text = "".join(result_parts)
    destructive = detect_destructive_commands(answer)

    if unverified:
        return GuardrailResult(
            text=final_text,
            guardrail="redacted",
            unverified_commands=unverified,
            raw_answer=answer,
            destructive_commands=destructive,
        )

    return GuardrailResult(
        text=final_text,
        guardrail="pass",
        unverified_commands=[],
        raw_answer=None,
        destructive_commands=destructive,
    )


__all__ = [
    "KNOWN_EXECUTABLES",
    "DESTRUCTIVE_PATTERN",
    "GuardrailResult",
    "parse_block_commands",
    "extract_runbook_commands",
    "is_verified_command",
    "detect_destructive_commands",
    "verify_guardrail",
]
