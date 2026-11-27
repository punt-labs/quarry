"""Tests for the daemon-registry collection resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.registrar import Registrar
from tests.conftest import FakeRegistryClient


def test_registers_new_directory() -> None:
    client = FakeRegistryClient()
    res = Registrar(client).resolve(Path("/proj"), "")
    assert res.created is True
    assert res.collection  # a fresh unique name was dispatched
    assert res.covered_by == ""
    assert [r.directory for r in client._registered] == ["/proj"]


def test_reuses_covering_same_directory() -> None:
    client = FakeRegistryClient([("proj", Path("/proj"))])
    res = Registrar(client).resolve(Path("/proj"), "")
    assert res.created is False
    assert res.collection == "proj"
    assert res.covered_by == ""
    assert client._registered == []


def test_collection_override_is_used() -> None:
    client = FakeRegistryClient()
    res = Registrar(client).resolve(Path("/proj"), "custom")
    assert res.created is True
    assert res.collection == "custom"


def test_child_of_registered_parent_reuses_parent_collection() -> None:
    client = FakeRegistryClient([("proj", Path("/proj"))])
    res = Registrar(client).resolve(Path("/proj/src"), "")
    assert res.created is False
    assert res.collection == "proj"
    assert res.covered_by == "/proj"
    assert client._registered == []


def test_child_with_matching_override_is_accepted() -> None:
    client = FakeRegistryClient([("proj", Path("/proj"))])
    res = Registrar(client).resolve(Path("/proj/src"), "proj")
    assert res.collection == "proj"


def test_child_with_conflicting_override_raises() -> None:
    client = FakeRegistryClient([("proj", Path("/proj"))])
    with pytest.raises(ValueError, match="already covered by the registration"):
        Registrar(client).resolve(Path("/proj/src"), "other")
