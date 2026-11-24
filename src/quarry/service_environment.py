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
        """Read the cache override from the installed service definition."""
        try:
            value = cls._installed_cache_value()
        except (HomeUnresolvedError, OSError, ValueError, ExpatError):
            return None
        if isinstance(value, str) and (path := Path(value)).is_absolute():
            return path
        return None

    @staticmethod
    def _installed_cache_value() -> object:
        system = platform.system()
        if system == "Darwin":
            plist: object = plistlib.loads(
                QuarryHome.launchd_plist(SERVICE_LABEL).read_bytes()
            )
            if isinstance(plist, dict) and isinstance(
                env := plist.get("EnvironmentVariables"), dict
            ):
                return env.get(_CACHE_KEY)
        if system == "Linux" and QuarryHome.systemd_unit().is_file():
            for entry in shlex.split(QuarryHome.env_file().read_text(), comments=True):
                key, _, value = entry.partition("=")
                if key == _CACHE_KEY:
                    return value
        return None

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
