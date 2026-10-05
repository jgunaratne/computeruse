"""Guardrails: policy checks applied to every proposed action before execution.

A rule returns ALLOW, BLOCK (the model receives an error tool_result and must
choose another approach) or REQUIRE_APPROVAL (the session pauses until a human
approves/rejects in the UI). Rules are pure functions of the action + context,
so they are trivially unit-testable and every decision is logged as telemetry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
from urllib.parse import urlparse

from computeruse.computer.actions import Action, KeyPress, TypeText


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class GuardrailResult:
    decision: Decision
    rule: str | None = None
    reason: str | None = None

    @classmethod
    def allow(cls) -> GuardrailResult:
        return cls(Decision.ALLOW)


@dataclass
class GuardrailContext:
    backend: str
    task: str
    step: int
    isolated: bool = True
    recent_actions: list[str] = field(default_factory=list)


class Guardrail(Protocol):
    name: str

    def check(self, action: Action, ctx: GuardrailContext) -> GuardrailResult: ...


def _normalise_chord(chord: str) -> str:
    aliases = {"control": "ctrl", "cmd": "super", "command": "super", "win": "super", "meta": "super",
               "option": "alt", "return": "enter", "del": "delete", "esc": "escape"}
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    parts = [aliases.get(p, p) for p in parts]
    mods = sorted(p for p in parts[:-1])
    return "+".join(mods + parts[-1:])


_URL_RE = re.compile(r"(?:(?:https?://)|(?:www\.))?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:[/:?#]|$)", re.I)


def extract_domains(text: str) -> list[str]:
    """Domains a `type` action is likely navigating to (URL-ish tokens)."""
    found: list[str] = []
    for token in re.split(r"\s+", text.strip()):
        if not token:
            continue
        if "://" in token or token.lower().startswith("www.") or ("." in token and "/" in token):
            parsed = urlparse(token if "://" in token else f"http://{token}")
            if parsed.hostname:
                found.append(parsed.hostname.lower())
                continue
        m = _URL_RE.match(token)
        if m and ("://" in token or token.lower().startswith("www.")):
            found.append(m.group(1).lower())
    # Bare "example.com\n" style navigation also counts.
    stripped = text.strip()
    if "\n" in text and " " not in stripped and "." in stripped and not found:
        parsed = urlparse(f"http://{stripped.splitlines()[0]}")
        if parsed.hostname and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", parsed.hostname, re.I):
            found.append(parsed.hostname.lower())
    return found


def _domain_matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower().lstrip("*.")
    return host == pattern or host.endswith("." + pattern)


class BlockedKeys:
    """Block key chords that could lock, kill or break out of the session."""

    name = "blocked_keys"

    def __init__(self, chords: list[str], only_when_not_isolated: bool = False) -> None:
        self.chords = {_normalise_chord(c) for c in chords}
        self.only_when_not_isolated = only_when_not_isolated

    def check(self, action: Action, ctx: GuardrailContext) -> GuardrailResult:
        if not isinstance(action, KeyPress):
            return GuardrailResult.allow()
        if self.only_when_not_isolated and ctx.isolated:
            return GuardrailResult.allow()
        if _normalise_chord(action.text) in self.chords:
            return GuardrailResult(Decision.BLOCK, self.name, f"key chord {action.text!r} is not permitted")
        return GuardrailResult.allow()


class DomainPolicy:
    """Allow/block navigation by typed URL (browser-control surface)."""

    name = "domain_policy"

    def __init__(self, allowed: list[str] | None = None, blocked: list[str] | None = None) -> None:
        self.allowed = allowed or []
        self.blocked = blocked or []

    def check(self, action: Action, ctx: GuardrailContext) -> GuardrailResult:
        if not isinstance(action, TypeText):
            return GuardrailResult.allow()
        for host in extract_domains(action.text):
            if any(_domain_matches(host, b) for b in self.blocked):
                return GuardrailResult(Decision.BLOCK, self.name, f"domain {host} is blocked by policy")
            if self.allowed and not any(_domain_matches(host, a) for a in self.allowed):
                return GuardrailResult(Decision.BLOCK, self.name,
                                       f"domain {host} is not in the allowlist ({', '.join(self.allowed)})")
        return GuardrailResult.allow()


_SECRET_PATTERNS = [
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "looks like a payment card number"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "looks like a US SSN"),
    (re.compile(r"(?i)\b(?:sk|pk|api|token|secret|password|passwd|pwd)[-_ ]?(?:key|live|test)?[-_ ]?[:=]\s*\S{6,}"),
     "looks like a credential"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "looks like an API key"),
]
_DESTRUCTIVE_PATTERNS = [
    (re.compile(r"(?i)(^|[;&|]\s*)(sudo\s+)?rm\s+-[a-z]*r[a-z]*f?\s"), "recursive delete"),
    (re.compile(r"(?i)(^|[;&|]\s*)(sudo\s+)?(mkfs|dd\s+if=|shutdown|reboot|poweroff|halt)\b"), "destructive system command"),
    (re.compile(r"(?i)(^|[;&|]\s*)git\s+push\s+.*--force"), "force push"),
    (re.compile(r"(?i)(^|[;&|]\s*)(drop\s+(table|database)|truncate\s+table)\b"), "destructive SQL"),
    (re.compile(r"(?i)(^|[;&|]\s*)(sudo|su)\s"), "privilege escalation"),
]


class SensitiveText:
    """Human approval before typing secrets or destructive commands."""

    name = "sensitive_text"

    def __init__(self, max_length: int = 4000) -> None:
        self.max_length = max_length

    def check(self, action: Action, ctx: GuardrailContext) -> GuardrailResult:
        if not isinstance(action, TypeText):
            return GuardrailResult.allow()
        if len(action.text) > self.max_length:
            return GuardrailResult(Decision.BLOCK, self.name,
                                   f"type action of {len(action.text)} chars exceeds limit {self.max_length}")
        for pattern, why in _DESTRUCTIVE_PATTERNS:
            if pattern.search(action.text):
                return GuardrailResult(Decision.REQUIRE_APPROVAL, self.name, f"typed text contains a {why}")
        for pattern, why in _SECRET_PATTERNS:
            if pattern.search(action.text):
                return GuardrailResult(Decision.REQUIRE_APPROVAL, self.name, f"typed text {why}")
        return GuardrailResult.allow()


class GuardrailPolicy:
    """Ordered rule set. BLOCK wins over REQUIRE_APPROVAL wins over ALLOW."""

    def __init__(self, rules: list[Guardrail] | None = None) -> None:
        self.rules: list[Guardrail] = rules or []

    def evaluate(self, action: Action, ctx: GuardrailContext) -> GuardrailResult:
        verdict = GuardrailResult.allow()
        for rule in self.rules:
            result = rule.check(action, ctx)
            if result.decision == Decision.BLOCK:
                return result
            if result.decision == Decision.REQUIRE_APPROVAL and verdict.decision == Decision.ALLOW:
                verdict = result
        return verdict

    @classmethod
    def default(cls, *, blocked_keys: list[str], desktop_blocked_keys: list[str],
                allowed_domains: list[str], blocked_domains: list[str], max_type_length: int) -> GuardrailPolicy:
        return cls([
            BlockedKeys(blocked_keys),
            BlockedKeys(desktop_blocked_keys, only_when_not_isolated=True),
            DomainPolicy(allowed_domains, blocked_domains),
            SensitiveText(max_type_length),
        ])
