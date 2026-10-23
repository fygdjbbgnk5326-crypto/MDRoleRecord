"""用词典把曲名对上官包 music_{key}_assets_all.bundle。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

_MAP_SUFFIX = re.compile(r"_map(\d)?$", re.IGNORECASE)


@dataclass
class ChartHit:
    path: Path
    map_index: int
    label: str


def _norm(value: str) -> str:
    text = (value or "").strip().lower()
    for ch in " \t\r\n_'\"-—–·・.!?！？/\\()（）[]【】":
        text = text.replace(ch, "")
    return text


def load_dictionary(path: Path) -> List[tuple]:
    if not path.is_file():
        raise FileNotFoundError(f"词典不存在: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("\t")]
        key = parts[0]
        if not key:
            continue
        title = parts[1] if len(parts) > 1 and parts[1] else key
        rows.append((key, title))
    if not rows:
        raise FileNotFoundError(f"词典是空的: {path}")
    return rows


def _song_key(value: str) -> str:
    return _MAP_SUFFIX.sub("", (value or "").strip())


def _bundle_path(bundle_dir: Path, key: str) -> Path:
    return bundle_dir / f"music_{key}_assets_all.bundle"


def _has_map(path: Path, difficulty: int) -> bool:
    """官包里对象名的首字母有时不连续，只认 _mapN。"""
    needle = f"_map{difficulty}".encode("ascii")
    try:
        data = path.read_bytes()
    except OSError:
        return False
    start = 0
    while True:
        index = data.find(needle, start)
        if index < 0:
            return False
        after = index + len(needle)
        if after >= len(data) or data[after] not in b"0123456789":
            return True
        start = index + 1


_HIDDEN_LEVEL = {"?", "？", "¿", "‽", "里"}


def _with_hidden(path: Path, difficulty: int, level: str) -> int:
    """倒问号是里谱。按钮序号常停在 3，优先选现有的里谱。"""
    if (level or "").strip() not in _HIDDEN_LEVEL:
        return difficulty
    if difficulty >= 4 and _has_map(path, difficulty):
        return difficulty
    if _has_map(path, 4):
        return 4
    if _has_map(path, 5):
        return 5
    if _has_map(path, 6):
        return 6
    return difficulty


def find_chart(
    bundle_dir: Path,
    dictionary: Path,
    music_uid: str,
    difficulty: int,
    music_name: str = "",
    level: str = "",
) -> ChartHit:
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"官包目录不存在: {bundle_dir}")
    if not 1 <= difficulty <= 6:
        raise FileNotFoundError(f"难度号无效: {difficulty}")

    # The game supplies an exact UID in normal use. A title dictionary is
    # optional and only helps when resolving an ambiguous external name.
    rows = load_dictionary(dictionary) if dictionary.is_file() else []
    by_key: Dict[str, str] = {}
    by_title: Dict[str, List[str]] = {}
    for key, title in rows:
        by_key.setdefault(key.lower(), key)
        by_title.setdefault(_norm(title), []).append(key)
        by_title.setdefault(_norm(key), []).append(key)

    raw_uid = (music_uid or "").strip()
    numbered = re.search(r"_map(\d)$", raw_uid, re.IGNORECASE)
    if numbered:
        named = int(numbered.group(1))
        if 4 <= named <= 6:
            difficulty = named
    want_uid = _song_key(raw_uid)
    want_name = (music_name or "").strip()
    name_keys = []
    for key in by_title.get(_norm(want_name), []):
        if key not in name_keys:
            name_keys.append(key)
    # 曲名和 uid 不是同一首时，以屏幕上的曲名为准。
    title_wins = bool(name_keys) and want_uid.lower() not in {key.lower() for key in name_keys}
    ordered: List[str] = []

    def add(key: str) -> None:
        if key and key not in ordered:
            ordered.append(key)

    if title_wins:
        for key in name_keys:
            add(key)
    elif want_uid.lower() in by_key:
        add(by_key[want_uid.lower()])
    if not title_wins:
        for token in (want_name, want_uid):
            for key in by_title.get(_norm(token), []):
                add(key)
    if want_uid and not title_wins:
        add(want_uid)

    found: List[tuple] = []
    for key in ordered:
        path = _bundle_path(bundle_dir, key)
        if path.is_file():
            found.append((key, path))

    shown = want_name or want_uid or "?"
    if not found:
        raise FileNotFoundError(
            f"词典里没有 {shown} 对应的官包（难度 {difficulty}）: {bundle_dir}"
        )

    def label_of(key: str) -> str:
        return next((title for k, title in rows if k == key), key)

    if difficulty >= 4:
        for key, path in found:
            if _has_map(path, difficulty):
                return ChartHit(path, difficulty, label_of(key))
        raise FileNotFoundError(f"{shown} 没有里谱 map{difficulty}")

    key, path = found[0]
    difficulty = _with_hidden(path, difficulty, level)
    return ChartHit(path, difficulty, label_of(key))
