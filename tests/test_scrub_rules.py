"""Tests for quarry.scrub_rules — the secret-detection rule catalog."""

from __future__ import annotations

import re

from quarry.scrub_rules import BLOCK_RULES, LINE_RULES, SecretRule


def test_replacement_defaults_to_category_marker() -> None:
    rule = SecretRule(category="gh-pat", pattern=re.compile("x"))
    assert rule.replacement() == "[REDACTED:gh-pat]"


def test_replacement_uses_explicit_replace_when_set() -> None:
    rule = SecretRule(
        category="env-secret",
        pattern=re.compile("x"),
        replace=r"\1[REDACTED:env-secret]",
    )
    assert rule.replacement() == r"\1[REDACTED:env-secret]"


def test_catalog_tuples_are_populated() -> None:
    assert BLOCK_RULES
    assert LINE_RULES
    assert all(isinstance(r, SecretRule) for r in BLOCK_RULES + LINE_RULES)


def test_every_category_is_unique() -> None:
    categories = [r.category for r in BLOCK_RULES + LINE_RULES]
    assert len(categories) == len(set(categories))


def test_anthropic_rule_precedes_openai_rule() -> None:
    """anthropic-key must be matched before the broader openai-key rule."""
    order = [r.category for r in LINE_RULES]
    assert order.index("anthropic-key") < order.index("openai-key")
