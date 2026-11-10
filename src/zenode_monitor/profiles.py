"""Saved connection profiles, as one JSON file in the user's config directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from zenode import TransportConfig, load_transport_config


@dataclass
class Profile:
    name: str = "Local"
    mode: str = "peer"
    connect: list[str] = field(default_factory=list)
    namespace: str = ""
    multicast_scouting: bool = True
    shared_memory: bool = False

    def to_transport(self) -> TransportConfig:
        return TransportConfig(
            mode=self.mode,  # type: ignore[arg-type]
            connect=list(self.connect),
            namespace=self.namespace.strip().strip("/"),
            multicast_scouting=self.multicast_scouting,
            shared_memory=self.shared_memory,
        )

    @classmethod
    def from_toml(cls, path: str) -> Profile:
        """Read the ``[transport]`` section of a ``zenode.toml``."""
        t = load_transport_config(path, env={})
        return cls(
            name=Path(path).resolve().parent.name or "Imported",
            mode=t.mode,
            connect=list(t.connect),
            namespace=t.namespace,
            multicast_scouting=t.multicast_scouting,
            shared_memory=t.shared_memory,
        )

    def summary(self) -> str:
        where = ", ".join(self.connect) if self.connect else "multicast" if self.multicast_scouting else "no endpoint"
        return f"{self.mode} · {where}"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "zenode-monitor" / "profiles.json"


@dataclass
class ProfileBook:
    profiles: list[Profile] = field(default_factory=list)
    last: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> ProfileBook:
        path = path or config_path()
        try:
            raw = json.loads(path.read_text())
            known = Profile.__dataclass_fields__
            profiles = [Profile(**{k: v for k, v in p.items() if k in known}) for p in raw["profiles"]]
            return cls(profiles, str(raw.get("last", "")))
        except (OSError, ValueError, KeyError, TypeError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"last": self.last, "profiles": [asdict(p) for p in self.profiles]}
        path.write_text(json.dumps(data, indent=2))

    def get(self, name: str) -> Profile | None:
        return next((p for p in self.profiles if p.name == name), None)

    def put(self, profile: Profile) -> None:
        """Insert or replace by name."""
        for i, p in enumerate(self.profiles):
            if p.name == profile.name:
                self.profiles[i] = profile
                return
        self.profiles.append(profile)

    def remove(self, name: str) -> None:
        self.profiles = [p for p in self.profiles if p.name != name]
        if self.last == name:
            self.last = ""
