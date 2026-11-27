"""The app's own settings, kept in the configuration directory."""

from __future__ import annotations

import json
import os
import re
import threading
import zoneinfo
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from triage.policy import DEFAULT_CONFIG_DIR


@dataclass(frozen=True, slots=True)
class Settings:
    apply_policy_automatically: bool = False
    """Whether the policy is applied to new mail as it arrives, without the user asking."""
    time_zone: str = ""
    """The user's time zone (such as ``Europe/London``), as their browser reports it. Used
    for the date in the line that introduces a quotation in a reply. Empty if not known."""


def _valid(name: str, value: object) -> bool:
    if name == "time_zone":
        if not isinstance(value, str) or len(value) > 64:
            return False
        if value == "":
            return True
        if not re.fullmatch(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}", value):
            return False
        try:
            zoneinfo.ZoneInfo(value)
        except (OSError, ValueError, KeyError):  # an unknown name is a KeyError
            return False
        return True
    return isinstance(value, bool)


class SettingsStore:
    def __init__(self, config_dir: str | os.PathLike[str] | None = None):
        if config_dir is None:
            config_dir = os.environ.get("TRIAGE_CONFIG_DIR", DEFAULT_CONFIG_DIR)
        directory = Path(config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / "settings.json"
        self._lock = threading.Lock()

    def get(self) -> Settings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return Settings()
        if not isinstance(raw, dict):
            return Settings()
        known: dict[str, Any] = {}
        for field in fields(Settings):
            value = raw.get(field.name)
            if _valid(field.name, value):
                known[field.name] = value
        return Settings(**known)

    def update(self, changes: dict[str, Any]) -> Settings:
        """Change some settings. Unknown names and values of the wrong type are rejected."""
        names = {field.name for field in fields(Settings)}
        for name, value in changes.items():
            if name not in names or not _valid(name, value):
                raise ValueError(f"invalid setting {name!r}")
        with self._lock:
            settings = replace(self.get(), **changes)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(settings)), encoding="utf-8")
            temporary.replace(self._path)
        return settings
