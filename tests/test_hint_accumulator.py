"""Tests for the hint accumulator data structure."""

from __future__ import annotations

from quarry.hint_accumulator import HintAccumulator, ToolEvent


class TestAddAndRecent:
    def test_add_and_retrieve(self) -> None:
        acc = HintAccumulator(clock=lambda: 100.0)
        acc.add(ToolEvent(ts=100.0, tool="Bash", command="ls"))
        assert len(acc.recent()) == 1
        assert acc.recent()[0].command == "ls"

    def test_recent_returns_last_n(self) -> None:
        acc = HintAccumulator(clock=lambda: 100.0)
        for i in range(5):
            acc.add(ToolEvent(ts=100.0, tool="Bash", command=f"cmd-{i}"))
        result = acc.recent(n=3)
        assert len(result) == 3
        assert result[0].command == "cmd-2"

    def test_recent_returns_all_when_fewer_than_n(self) -> None:
        acc = HintAccumulator(clock=lambda: 100.0)
        acc.add(ToolEvent(ts=100.0, tool="Bash", command="one"))
        assert len(acc.recent(n=10)) == 1


class TestExpiry:
    def test_expired_events_pruned_on_add(self) -> None:
        t = 100.0

        def clock() -> float:
            return t

        acc = HintAccumulator(ttl=60.0, clock=clock)
        acc.add(ToolEvent(ts=100.0, tool="Bash", command="old"))

        t = 200.0  # 100s later — old event is expired (ttl=60)
        acc.add(ToolEvent(ts=200.0, tool="Bash", command="new"))

        events = acc.recent()
        assert len(events) == 1
        assert events[0].command == "new"

    def test_expired_events_pruned_on_recent(self) -> None:
        t = 100.0

        def clock() -> float:
            return t

        acc = HintAccumulator(ttl=60.0, clock=clock)
        acc.add(ToolEvent(ts=100.0, tool="Bash", command="old"))

        t = 200.0
        assert len(acc.recent()) == 0


class TestMaxCap:
    def test_max_events_enforced(self) -> None:
        acc = HintAccumulator(max_events=3, clock=lambda: 100.0)
        for i in range(10):
            acc.add(ToolEvent(ts=100.0, tool="Bash", command=f"cmd-{i}"))
        events = acc.recent(n=100)
        assert len(events) == 3
        assert events[0].command == "cmd-7"


class TestJsonRoundtrip:
    def test_roundtrip(self) -> None:
        acc = HintAccumulator(clock=lambda: 100.0)
        acc.add(ToolEvent(ts=100.0, tool="Bash", command="ls"))
        acc.add(ToolEvent(ts=101.0, tool="Edit", command="edit foo"))

        data = acc.to_json()
        restored = HintAccumulator.from_json(data, clock=lambda: 102.0)
        events = restored.recent()
        assert len(events) == 2
        assert events[0].command == "ls"
        assert events[1].command == "edit foo"

    def test_corrupt_json_returns_empty(self) -> None:
        acc = HintAccumulator.from_json("not valid json{{{")
        assert len(acc.recent()) == 0

    def test_non_list_json_returns_empty(self) -> None:
        acc = HintAccumulator.from_json('{"key": "value"}')
        assert len(acc.recent()) == 0

    def test_partial_items_skipped(self) -> None:
        import json

        data = json.dumps(
            [
                {"ts": 100.0, "tool": "Bash", "command": "valid"},
                {"ts": "bad", "tool": "Bash", "command": "invalid_ts"},
                {"ts": 101.0, "tool": 42, "command": "invalid_tool"},
                {"not_an_event": True},
            ]
        )
        acc = HintAccumulator.from_json(data, clock=lambda: 102.0)
        events = acc.recent()
        assert len(events) == 1
        assert events[0].command == "valid"


class TestClockInjection:
    def test_custom_clock_used_for_expiry(self) -> None:
        t = 1000.0

        def clock() -> float:
            return t

        acc = HintAccumulator(ttl=10.0, clock=clock)
        acc.add(ToolEvent(ts=1000.0, tool="Bash", command="first"))

        t = 1005.0
        acc.add(ToolEvent(ts=1005.0, tool="Bash", command="second"))
        assert len(acc.recent()) == 2

        t = 1011.0  # first event now expired
        assert len(acc.recent()) == 1
        assert acc.recent()[0].command == "second"
