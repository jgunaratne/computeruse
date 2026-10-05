"""Guardrail rules and per-session policy composition."""

from computeruse.agent.guardrails import (
    Decision,
    GuardrailContext,
    GuardrailPolicy,
    extract_domains,
)
from computeruse.computer.actions import parse_action
from computeruse.config import Settings
from computeruse.server.orchestrator import Orchestrator


def ctx(isolated=True, backend="browser"):
    return GuardrailContext(backend=backend, task="t", step=0, isolated=isolated)


def policy(**kw):
    base = dict(blocked_keys=["ctrl+alt+Delete"], desktop_blocked_keys=["alt+F4", "super+l"], allowed_domains=[],
                blocked_domains=[], max_type_length=100)
    base.update(kw)
    return GuardrailPolicy.default(**base)


def test_extract_domains():
    assert extract_domains("https://en.wikipedia.org/wiki/Python\n") == ["en.wikipedia.org"]
    assert extract_domains("go to www.example.com please") == ["www.example.com"]
    assert extract_domains("hello world") == []


def test_blocked_keys_global_and_desktop_only():
    p = policy()
    assert p.evaluate(parse_action({"action": "key", "text": "ctrl+alt+Delete"}), ctx()).decision == Decision.BLOCK
    assert p.evaluate(parse_action({"action": "key", "text": "CTRL+ALT+delete"}), ctx()).decision == Decision.BLOCK
    alt_f4 = parse_action({"action": "key", "text": "alt+F4"})
    assert p.evaluate(alt_f4, ctx(isolated=True)).decision == Decision.ALLOW, "fine inside a sandbox"
    assert p.evaluate(alt_f4, ctx(isolated=False, backend="desktop")).decision == Decision.BLOCK, "not on the real desktop"


def test_domain_allowlist_and_blocklist():
    p = policy(allowed_domains=["example.com"], blocked_domains=["evil.test"])
    ok = parse_action({"action": "type", "text": "https://www.example.com/path\n"})
    bad = parse_action({"action": "type", "text": "https://other.org\n"})
    evil = parse_action({"action": "type", "text": "evil.test/login"})
    assert p.evaluate(ok, ctx()).decision == Decision.ALLOW
    assert p.evaluate(bad, ctx()).decision == Decision.BLOCK
    assert p.evaluate(evil, ctx()).decision == Decision.BLOCK
    assert p.evaluate(parse_action({"action": "type", "text": "just some words"}), ctx()).decision == Decision.ALLOW


def test_sensitive_text_requires_approval_and_long_text_blocked():
    p = policy(max_type_length=20)
    secret = parse_action({"action": "type", "text": "password=hunter22"})
    v = p.evaluate(secret, ctx())
    assert v.decision == Decision.REQUIRE_APPROVAL and "credential" in v.reason
    card = parse_action({"action": "type", "text": "4111 1111 1111 1111"})
    assert p.evaluate(card, ctx()).decision == Decision.REQUIRE_APPROVAL
    rmrf = parse_action({"action": "type", "text": "rm -rf ~/\n"})
    assert p.evaluate(rmrf, ctx()).decision == Decision.REQUIRE_APPROVAL
    long = parse_action({"action": "type", "text": "x" * 21})
    assert p.evaluate(long, ctx()).decision == Decision.BLOCK


def test_per_session_overrides_only_tighten(settings: Settings):
    settings.allowed_domains = ["example.com", "wikipedia.org"]
    settings.blocked_domains = ["evil.test"]
    orch = Orchestrator(settings)
    try:
        # Narrowing the allowlist works.
        p = orch.make_policy({"allowed_domains": ["wikipedia.org"], "blocked_domains": ["ads.test"], "blocked_keys": ["ctrl+w"]})
        assert p.evaluate(parse_action({"action": "type", "text": "https://example.com\n"}), ctx()).decision == Decision.BLOCK
        assert p.evaluate(parse_action({"action": "type", "text": "https://wikipedia.org\n"}), ctx()).decision == Decision.ALLOW
        assert p.evaluate(parse_action({"action": "type", "text": "https://ads.test\n"}), ctx()).decision == Decision.BLOCK
        assert p.evaluate(parse_action({"action": "key", "text": "ctrl+w"}), ctx()).decision == Decision.BLOCK
        # Attempting to widen the global allowlist is ignored.
        p2 = orch.make_policy({"allowed_domains": ["anything.goes"]})
        assert p2.evaluate(parse_action({"action": "type", "text": "https://anything.goes\n"}), ctx()).decision == Decision.BLOCK
        assert p2.evaluate(parse_action({"action": "type", "text": "https://example.com\n"}), ctx()).decision == Decision.ALLOW
    finally:
        orch.store.close()
