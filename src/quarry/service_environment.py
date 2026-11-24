"""Keep the managed daemon environment consistent across service managers."""

from __future__ import annotations

from typing import final
from xml.sax.saxutils import escape

from quarry.env_file import EnvFile
from quarry.home_paths import QuarryHome
from quarry.model_cache import ModelCache
from quarry.shutdown_policy import ShutdownPolicy

MALLOC_CONF = "dirty_decay_ms:1000,muzzy_decay_ms:0,narenas:1,tcache:false"


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

    @staticmethod
    def _values(api_key: str, policy: ShutdownPolicy) -> dict[str, str]:
        values = {"MALLOC_CONF": MALLOC_CONF, policy.ENV_NAME: f"{policy.total_s:g}"}
        if api_key:
            values["QUARRY_API_KEY"] = api_key
        if ModelCache.override() is not None:
            values["HF_HUB_CACHE"] = str(ModelCache.root())
        return values

    @staticmethod
    def _quote(key: str, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
