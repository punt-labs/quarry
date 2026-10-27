"""Behaviour of :mod:`quarry.own_checkout`."""

from __future__ import annotations

from pathlib import Path

from quarry.own_checkout import OWN_PACKAGE_NAME, OwnCheckout


def _write_pyproject(root: Path, package_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{package_name}"\n', encoding="utf-8"
    )


class TestOwnCheckout:
    def test_confirmed_for_the_real_package_name(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, OWN_PACKAGE_NAME)
        assert OwnCheckout(tmp_path).confirmed() is True

    def test_not_confirmed_for_a_different_package_name(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, "punt-lux")
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_when_pyproject_toml_is_missing(self, tmp_path: Path) -> None:
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_when_pyproject_toml_has_no_project_table(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.other]\nkey = 1\n", encoding="utf-8"
        )
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_when_name_is_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_when_name_is_not_a_string(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 1\n", encoding="utf-8"
        )
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_on_malformed_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("not valid toml {{{", encoding="utf-8")
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_on_invalid_utf8(self, tmp_path: Path) -> None:
        """``UnicodeDecodeError`` is a ``ValueError`` subclass -- must fail
        safe rather than propagate through a guard that gates a destructive
        or trust-sensitive operation."""
        (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = \xff\xfe\n")
        assert OwnCheckout(tmp_path).confirmed() is False

    def test_not_confirmed_when_pyproject_toml_is_a_directory(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").mkdir()
        assert OwnCheckout(tmp_path).confirmed() is False
