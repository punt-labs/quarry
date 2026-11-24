"""Keep the managed daemon environment consistent across service managers."""

from __future__ import annotations

import platform
import plistlib
import shlex
from pathlib import Path
from typing import final
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape

from quarry.env_file import EnvFile
from quarry.home_paths import HomeUnresolvedError, QuarryHome
from quarry.model_cache import ModelCache
from quarry.shutdown_policy import ShutdownPolicy

MALLOC_CONF = "dirty_decay_ms:1000,muzzy_decay_ms:0,narenas:1,tcache:false"
SERVICE_LABEL = "com.punt-labs.quarry"
_CACHE_KEY = "HF_HUB_CACHE"


@final
class ServiceEnvironment:
    """Render install-time settings for systemd and launchd."""

    __slots__ = ()

    @classmethod
    def write(cls, api_key: str = "") -> None:
        """Write the systemd EnvironmentFile atomically and owner-only."""
        values = cls._values(api_key, ShutdownPolicy.from_settings())
        lines = [cls._quote(key, value) for key, value in values.items()]
        EnvFile(QuarryHome.env_file()).write(lines)

    @classmethod
    def launchd_entries(cls, api_key: str, policy: ShutdownPolicy) -> str:
        """Render XML entries with the indentation expected by the plist."""
        return "".join(
            f"            <key>{key}</key>\n"
            f"            <string>{escape(value)}</string>\n"
            for key, value in cls._values(api_key, policy).items()
        )

    @classmethod
    def installed_cache(cls) -> Path | None:
        """Return the managed cache, or None when no service is installed."""
        try:
            return cls._read_installed_cache()
        except (HomeUnresolvedError, FileNotFoundError):
            return None
        except (OSError, ValueError, ExpatError) as exc:
            msg = f"cannot read installed service settings ({type(exc).__name__})"
            raise ValueError(msg) from None

    @classmethod
    def _read_installed_cache(cls) -> Path | None:
        system = platform.system()
        if system == "Darwin":
            return cls._cache_path(cls._launchd_cache())
        if system == "Linux" and QuarryHome.systemd_unit().is_file():
            return cls._cache_path(cls._systemd_cache())
        return None

    @staticmethod
    def _launchd_cache() -> object:
        plist: object = plistlib.loads(
            QuarryHome.launchd_plist(SERVICE_LABEL).read_bytes()
        )
        if isinstance(plist, dict) and isinstance(
            env := plist.get("EnvironmentVariables"), dict
        ):
            return env.get(_CACHE_KEY)
        return None

    @staticmethod
    def _systemd_cache() -> str | None:
        try:
            content = QuarryHome.env_file().read_text()
        except FileNotFoundError:
            return None
        lexer = shlex.shlex(content, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#;"
        prefix = f"{_CACHE_KEY}="
        return next(
            (
                entry.removeprefix(prefix)
                for entry in reversed(list(lexer))
                if entry.startswith(prefix)
            ),
            None,
        )

    @staticmethod
    def _cache_path(value: object) -> Path:
        if value is None:
            return QuarryHome.cache_root()
        if isinstance(value, str) and (path := Path(value)).is_absolute():
            return path
        msg = "expected an absolute cache path in installed service settings"
        raise ValueError(msg)

    @staticmethod
    def _values(api_key: str, policy: ShutdownPolicy) -> dict[str, str]:
        values = {"MALLOC_CONF": MALLOC_CONF, policy.ENV_NAME: f"{policy.total_s:g}"}
        if api_key:
            values["QUARRY_API_KEY"] = api_key
        if ModelCache.override() is not None:
            values[_CACHE_KEY] = str(ModelCache.root())
        return values

    @staticmethod
    def _quote(key: str, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
