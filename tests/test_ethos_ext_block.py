"""Locate / classify / splice behaviour of the versioned memory guide block."""

from __future__ import annotations

import yaml

from quarry.ethos_ext_block import (
    MEMORY_GUIDE_HEADER,
    BlockState,
    SessionContextBlock,
)

# The v1 block exactly as ``quarry enable`` wrote it before the guide was
# versioned — the shape every existing identity carries today.
_V1_FILE = """\
memory_collection: memory-rmh

session_context: |
  ## Memory

  You have persistent memory stored in quarry, a local semantic
  search engine. Your memories survive across sessions and machines.

  ### Working Memory

  Collection: "memory-rmh"

  Memory types:
  - fact: objective, verifiable information ("the API rate limit is 100 req/s")
"""


class TestClassify:
    def test_absent(self) -> None:
        block = SessionContextBlock.locate("memory_collection: memory-rmh\n")
        assert block.state is BlockState.ABSENT
        assert block.needs_guide is True

    def test_v1_is_stale(self) -> None:
        block = SessionContextBlock.locate(_V1_FILE)
        assert block.state is BlockState.STALE
        assert block.needs_guide is True

    def test_v2_is_current(self) -> None:
        text = SessionContextBlock.locate("").with_guide("rmh", "memory-rmh")
        assert SessionContextBlock.locate(text).state is BlockState.CURRENT

    def test_hand_authored_literal_block_is_custom(self) -> None:
        raw = "session_context: |\n  You are the ops lead.\n  Be terse.\n"
        block = SessionContextBlock.locate(raw)
        assert block.state is BlockState.CUSTOM
        assert block.needs_guide is False

    def test_inline_scalar_is_custom(self) -> None:
        """A plain (non-literal) value is hand-authored, never rewritten."""
        raw = yaml.dump({"memory_collection": "m", "session_context": "keep me"})
        assert SessionContextBlock.locate(raw).state is BlockState.CUSTOM

    def test_empty_literal_block_is_custom(self) -> None:
        assert SessionContextBlock.locate("session_context: |\n").state is (
            BlockState.CUSTOM
        )


class TestRender:
    def test_header_is_the_first_body_line(self) -> None:
        fragment = SessionContextBlock.render("rmh", "memory-rmh")
        lines = fragment.splitlines()
        assert lines[0] == "session_context: |"
        assert lines[1] == f"  {MEMORY_GUIDE_HEADER}"

    def test_round_trips_as_a_yaml_literal(self) -> None:
        fragment = SessionContextBlock.render("rmh", "memory-rmh")
        data = yaml.safe_load(f"memory_collection: memory-rmh\n{fragment}")
        text = data["session_context"]
        assert text.startswith(MEMORY_GUIDE_HEADER)
        assert 'agent_handle="rmh"' in text
        assert 'Collection: "memory-rmh"' in text

    def test_names_the_five_moments_and_the_attribution_rule(self) -> None:
        fragment = SessionContextBlock.render("kpz", "memory-kpz")
        for moment in (
            "root cause",
            "ratified",
            "how-to",
            "judgement",
            "mission result",
        ):
            assert moment in fragment
        assert "cannot infer your identity" in fragment
        assert "30-day half-life" in fragment

    def test_blank_lines_carry_no_trailing_indent(self) -> None:
        fragment = SessionContextBlock.render("rmh", "memory-rmh")
        assert "\n  \n" not in fragment
        assert "\n\n" in fragment


class TestWithGuide:
    def test_append_when_absent_keeps_existing_text(self) -> None:
        raw = "# keep this comment\nmemory_collection: memory-rmh\n"
        text = SessionContextBlock.locate(raw).with_guide("rmh", "memory-rmh")
        assert text.startswith(raw)
        assert yaml.safe_load(text)["memory_collection"] == "memory-rmh"
        assert yaml.safe_load(text)["session_context"].startswith(MEMORY_GUIDE_HEADER)

    def test_append_terminates_an_unterminated_last_line(self) -> None:
        text = SessionContextBlock.locate("memory_collection: m").with_guide("h", "m")
        assert yaml.safe_load(text) == {
            "memory_collection": "m",
            "session_context": yaml.safe_load(text)["session_context"],
        }

    def test_splice_replaces_only_the_stale_block(self) -> None:
        raw = _V1_FILE + "trailing_key: after\n"
        text = SessionContextBlock.locate(raw).with_guide("rmh", "memory-rmh")
        data = yaml.safe_load(text)
        assert data["memory_collection"] == "memory-rmh"
        assert data["trailing_key"] == "after"
        assert data["session_context"].startswith(MEMORY_GUIDE_HEADER)
        assert "### Working Memory" not in text
        assert text.startswith("memory_collection: memory-rmh\n\n")

    def test_splice_preserves_crlf_outside_the_block(self) -> None:
        raw = "memory_collection: m\r\nsession_context: |\r\n  ## Memory\r\n  old\r\n"
        text = SessionContextBlock.locate(raw).with_guide("h", "m")
        assert text.startswith("memory_collection: m\r\n")
        assert "  old" not in text

    def test_refresh_is_idempotent(self) -> None:
        once = SessionContextBlock.locate(_V1_FILE).with_guide("rmh", "memory-rmh")
        block = SessionContextBlock.locate(once)
        assert block.state is BlockState.CURRENT
        assert block.with_guide("rmh", "memory-rmh") == once
