"""Prepare a verified virtual-input action table for one selected song.

This process is launched while the game is on the song-selection screen.  It
never injects input into the game; the mod only opens the finished cache file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chart.bundle import _load_bundle_maps, find_notedata_dir
from chart.library import ChartHit, find_chart
from chart.parser import Lane, MDMParser
from config import Config
from scheduler.scheduler import EventScheduler, SchedulerConfig

CACHE_VERSION = 1  # Keep in sync with ChartActionCache.cs.
KINDS = {"tap", "hold", "dodge", "collect", "mash", "bar_star"}


def load_config(config_path: str) -> Config:
    if not config_path:
        return Config()
    return Config.from_dict(json.loads(Path(config_path).read_text(encoding="utf-8-sig")))


class VirtualLaneMapper:
    """Only map chart lanes while exporting; never initialize a keyboard backend."""

    @staticmethod
    def _lanes(lane: Lane) -> tuple[Lane, ...]:
        if lane == Lane.BOTH:
            return (Lane.AIR, Lane.GROUND)
        if lane == Lane.MID:
            return (Lane.AIR,)
        if lane in (Lane.AIR, Lane.GROUND):
            return (lane,)
        return (Lane.AIR,)


def validate_actions(path: Path, uid: str, map_index: int, stage_map: str = "") -> int:
    header: dict[str, str] = {}
    opened: set[tuple[str, int]] = set()
    previous = -1.0
    count = 0
    for source in path.read_text(encoding="utf-8-sig").splitlines():
        line = source.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "=" in line:
                key, value = line[1:].split("=", 1)
                header[key.strip()] = value.strip()
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError(f"invalid action row {count + 1}")
        t_text, edge, lane, kind, event_text = parts
        t = float(t_text)
        event_id = int(event_text)
        if not (0 <= t < 36000 and t >= previous and edge in {"down", "up"}
                and lane in {"air", "ground"} and kind in KINDS and event_id > 0):
            raise ValueError(f"invalid action row {count + 1}")
        previous = t
        key = (lane, event_id)
        if edge == "down":
            if key in opened:
                raise ValueError(f"duplicate down {key}")
            opened.add(key)
            if len(opened) > 32:
                raise ValueError("more than 32 fingers")
        elif key not in opened:
            raise ValueError(f"up without down {key}")
        else:
            opened.remove(key)
        count += 1
    if opened or count == 0:
        raise ValueError("unreleased fingers or empty table")
    if (header.get("mdr_virtual_format") != "1"
            or header.get("chart_uid") != uid
            or header.get("map_index") != str(map_index)
            or (header.get("stage_map", stage_map if stage_map == f"{uid}_map{map_index}" else "")
                != stage_map)
            or header.get("action_count") != str(count)):
        raise ValueError("action metadata does not match selection")
    return count


def signature(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {"path": str(path.resolve()), "length": stat.st_size,
            "mtime_ms": stat.st_mtime_ns // 1_000_000}


def rule_hash(config) -> str:
    digest = hashlib.sha256()
    for relative in ("action_cache_generator.py", "config.py",
                     "scheduler/scheduler.py", "chart/parser.py", "chart/bundle.py",
                     "chart/dodge.py", "chart/hold_hits.py", "chart/library.py"):
        digest.update(relative.encode("ascii"))
        digest.update((ROOT / relative).read_bytes())
    digest.update(json.dumps(config.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def generate(uid: str, map_index: int, name: str, level: str,
             cache_root: Path, config_path: str = "", stage_map: str = "") -> Path:
    uid = uid.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", uid):
        raise ValueError("invalid song UID")
    if not 1 <= map_index <= 6:
        raise ValueError("invalid map index")
    stage_map = (stage_map or f"{uid}_map{map_index}").strip().lower()
    if (not re.fullmatch(r"[a-z0-9_-]{1,120}_map[1-6]", stage_map)
            or not stage_map.endswith(f"_map{map_index}")
            or (stage_map != f"{uid}_map{map_index}"
                and not stage_map.startswith(uid + "_"))):
        raise ValueError("invalid stage map for selected song")
    config = load_config(config_path)
    # MDChartClock usually supplies the bundle's exact UID.  This path needs no
    # external title dictionary, which may be absent on a player's machine.
    direct = Path(config.bundle_dir) / f"music_{uid}_assets_all.bundle"
    hit = (ChartHit(direct, map_index, name or uid) if direct.is_file() else
           find_chart(Path(config.bundle_dir), Path(config.dictionary),
                      uid, map_index, name, level))
    bundle_uid = hit.path.stem.lower().removeprefix("music_").removesuffix("_assets_all")
    if bundle_uid != uid or hit.map_index != map_index:
        raise ValueError(f"selection changed or chart mismatch: {uid}/map{map_index} -> "
                         f"{bundle_uid}/map{hit.map_index}")
    notedata = sorted(find_notedata_dir(hit.path).glob("config_others_assets_notedata_*.bundle"))[0]
    sources = [signature(hit.path), signature(notedata)]
    rules = rule_hash(config)
    directory = cache_root / uid / f"map{map_index}"
    if stage_map != f"{uid}_map{map_index}":
        directory = directory / "variants" / stage_map
    directory.mkdir(parents=True, exist_ok=True)
    actions = directory / "virtual_actions.txt"
    manifest = directory / "manifest.json"
    # A pinned, hand-tuned table survives rule edits, but still requires the
    # same official chart files and a valid content hash and action pairing.
    if manifest.exists():
        try:
            old = json.loads(manifest.read_text(encoding="utf-8"))
            if (old.get("cache_version") == CACHE_VERSION
                    and (old.get("rule_hash") == rules
                         or old.get("manual_override") is True)
                    and old.get("sources") == sources
                    and old.get("stage_map", stage_map if stage_map == f"{uid}_map{map_index}" else "") == stage_map
                    and actions.is_file()
                    and old.get("sha256") == hashlib.sha256(actions.read_bytes()).hexdigest()
                    and validate_actions(actions, uid, map_index, stage_map) == old.get("action_count")):
                return actions
        except (OSError, ValueError, TypeError, KeyError):
            pass
        manifest.unlink(missing_ok=True)

    temporary = directory / f"virtual_actions.{os.getpid()}.tmp"
    manifest_tmp = directory / f"manifest.{os.getpid()}.tmp"
    try:
        chart = MDMParser().parse(str(hit.path), map_index, stage_map=stage_map)
        scheduler = EventScheduler(
            chart, VirtualLaneMapper(),
            SchedulerConfig(
                device_delay_sec=config.timing.device_delay_sec,
                inject_delay_sec=config.timing.inject_delay_sec,
                tick_interval=config.timing.tick_interval,
                pulse_duration=config.timing.pulse_duration,
                mash_frequency=config.timing.mash_frequency,
                grace_tail_sec=config.session.grace_tail_sec,
            ), virtual_input=True,
        )
        scheduler.export_virtual_actions(str(temporary), uid, map_index, stage_map=stage_map)
        count = validate_actions(temporary, uid, map_index, stage_map)
        if sources != [signature(hit.path), signature(notedata)]:
            raise RuntimeError("chart files changed during generation")
        info = {"cache_version": CACHE_VERSION, "chart_uid": uid, "stage_map": stage_map,
                "map_index": map_index, "action_count": count,
                "sources": sources, "rule_hash": rules,
                "sha256": hashlib.sha256(temporary.read_bytes()).hexdigest()}
        manifest_tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, actions)
        os.replace(manifest_tmp, manifest)  # The ready marker is published last.
        return actions
    finally:
        temporary.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)


def generate_hidden(uid: str, name: str, level: str, cache_root: Path,
                    config_path: str = "", stage_map: str = "") -> tuple[Path, list[int], list[str]]:
    """Prepare every hidden chart in one selected official bundle."""
    uid = uid.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", uid):
        raise ValueError("invalid song UID")
    config = load_config(config_path)
    bundle = Path(config.bundle_dir) / f"music_{uid}_assets_all.bundle"
    if not bundle.is_file():
        raise FileNotFoundError(f"找不到里谱官包: {bundle}")
    maps, *_ = _load_bundle_maps(str(bundle), stage_map)
    indices = sorted(index for index in maps if 4 <= index <= 6)
    if not indices:
        raise FileNotFoundError(f"{uid} 没有 map4–6 里谱")
    first: Path | None = None
    keys: list[str] = []
    for index in indices:
        chosen = maps[index]["name"].lower()
        path = generate(uid, index, name, level, cache_root, config_path, stage_map=chosen)
        key = f"{uid}/map{index}"
        if chosen != f"{uid}_map{index}":
            key += f"/variants/{chosen}"
        keys.append(key)
        if first is None:
            first = path
    assert first is not None
    return first, indices, keys


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one song's cached virtual actions")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--map", type=int, required=True)
    parser.add_argument("--stage-map", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--level", default="")
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--all-hidden", action="store_true")
    args = parser.parse_args()
    args.status.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.all_hidden:
            path, maps, keys = generate_hidden(args.uid, args.name, args.level,
                                               args.cache_root, args.config, args.stage_map)
        else:
            path = generate(args.uid, args.map, args.name, args.level,
                            args.cache_root, args.config, args.stage_map)
            maps = [args.map]
            keys = [f"{args.uid}/map{args.map}"]
            if args.stage_map and args.stage_map.lower() != f"{args.uid.lower()}_map{args.map}":
                keys[0] += f"/variants/{args.stage_map.lower()}"
        status = {"ok": True, "path": str(path), "maps": maps, "keys": keys}
    except Exception as exc:
        status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    temp = args.status.with_suffix(".tmp")
    temp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, args.status)
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
