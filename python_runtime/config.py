"""
配置模块
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Type, TypeVar

T = TypeVar("T")


def _default_bundle_dir() -> str:
    explicit = os.environ.get("MUSE_DASH_BUNDLE_DIR", "").strip()
    if explicit:
        return explicit
    game_path = os.environ.get("MUSE_DASH_PATH", "").strip()
    candidates = []
    if game_path:
        candidates.append(Path(game_path))
    candidates.extend((
        Path(r"D:\steam\steamapps\common\Muse Dash"),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Steam" / "steamapps" / "common" / "Muse Dash",
    ))
    for game in candidates:
        bundles = game / "MuseDash_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64"
        if bundles.is_dir():
            return str(bundles)
    return ""


def _merge_dataclass(instance: T, data: Dict[str, Any]) -> T:
    if not data:
        return instance
    for f in fields(instance):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(instance, f.name)
        if is_dataclass(current) and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, f.name, value)
    return instance


@dataclass
class KeyMapping:
    air: str = "d"
    ground: str = "j"
    air_alt: str = "f"
    ground_alt: str = "k"


@dataclass
class TimingConfig:
    device_delay_sec: float = 0.0
    inject_delay_sec: float = 0.012
    tick_interval: float = 0.001
    pulse_duration: float = 0.04
    mash_frequency: float = 12.0


@dataclass
class SessionConfig:
    grace_tail_sec: float = 2.0
    watchdog_sec: float = 10.0


@dataclass
class Config:
    mdm_path: str = ""
    bundle_dir: str = field(default_factory=_default_bundle_dir)
    dictionary: str = ""
    map_index: int = 3
    keys: KeyMapping = field(default_factory=KeyMapping)
    timing: TimingConfig = field(default_factory=TimingConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    debug: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls: Type["Config"], data: dict) -> "Config":
        return _merge_dataclass(cls(), data or {})

    def to_dict(self) -> dict:
        return asdict(self)
