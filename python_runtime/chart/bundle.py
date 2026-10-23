"""
官包 music_*.bundle → ChartIR。

规则对齐 MusePSona《mdm物件转化词典》：
  - 可玩键 / 事件都是原样抄 NoteConfigData.ibms_id
  - type 8 必须保留 0G / 16 / 17，禁止一律写成 0G
  - 点长 0F（0 < length ≤ 0.02，约 0.00625s）是星：长条内走副键，空地走主键
  - length=0 的 0F 是长按中间点，丢掉
  - 长按/连打只留头上的 length；条内其它可玩键（0H、23、0C…）全留
  - 不要把 speed 写成 0O–0W，不要把 MusicInfo BPM 区间写成通道 08
"""
from __future__ import annotations

import json
import logging
import re
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .parser import (
    BMSParser,
    ChartEvent,
    ChartIR,
    EventKind,
    Lane,
    MDMInfo,
    NoteType,
)

logger = logging.getLogger(__name__)

MIN_HOLD_SEC = 0.02
UID_KEY = "note_uid".encode("utf-16le")
LEN_KEY = "length".encode("utf-16le")
PATH_KEY = "pathway".encode("utf-16le")
TICK_KEY = "tick".encode("utf-16le") + b"\x00\x00"
DELAY_KEY = "delay".encode("utf-16le") + b"\x00\x00"
UID_RE = re.compile(rb"(?:[\x30-\x39]\x00){6,8}")
MAP_NAME_RE = re.compile(r"_map(\d)$", re.I)

MID_IBMS = {"0G", "11", "12", "16", "17"}
MASH_IBMS = {"0G", "16", "17"}


def _rd_dec(buf: bytes, pos: int) -> float:
    flags = struct.unpack_from("<I", buf, pos)[0]
    hi = struct.unpack_from("<I", buf, pos + 4)[0]
    lo = struct.unpack_from("<I", buf, pos + 8)[0]
    mid = struct.unpack_from("<I", buf, pos + 12)[0]
    scale = (flags >> 16) & 0xFF
    value = ((hi << 64) | (mid << 32) | lo) / (10 ** scale)
    return -value if (flags >> 31) & 1 else value


def _utf16_digits(blob: bytes) -> str:
    match = UID_RE.search(blob)
    return match.group().decode("utf-16le") if match else ""


def _serialized_bytes(mb) -> bytes:
    sd = getattr(mb, "serializationData", None)
    if sd is None:
        return b""
    for attr in ("SerializedBytes", "serializedBytes"):
        value = getattr(sd, attr, None)
        if value is not None:
            return bytes(value)
    return b""


def _norm_ibms(raw) -> str:
    ibms = str(raw or "").strip().upper()
    if len(ibms) == 1:
        ibms = "0" + ibms
    return ibms if len(ibms) == 2 else ""


def _mash_ibms(ibms: str, des: str) -> str:
    ibms = _norm_ibms(ibms)
    if ibms in MASH_IBMS:
        return ibms
    if "后退场" in des:
        return "17"
    if "连击_平移" in des or "连击_下落" in des or "连击_上升" in des:
        return "0G"
    if "连击" in des:
        return "16"
    return "0G"


def parse_music_data(serialized: bytes) -> List[dict]:
    anchors = [m.start() for m in re.finditer(re.escape(TICK_KEY), serialized)]
    notes: List[dict] = []
    for i, start in enumerate(anchors):
        window = serialized[start : anchors[i + 1] if i + 1 < len(anchors) else len(serialized)]
        tick = _rd_dec(window, 8)
        uid = ""
        p = window.find(UID_KEY)
        if p >= 0:
            uid = _utf16_digits(window[p + len(UID_KEY) : p + len(UID_KEY) + 48])
        length = 0.0
        p = window.find(LEN_KEY)
        if p >= 0:
            for off in (len(LEN_KEY), len(LEN_KEY) + 2, len(LEN_KEY) + 4):
                if p + off + 16 <= len(window):
                    value = _rd_dec(window, p + off)
                    if 0 <= value < 600:
                        length = value
                        break
        pathway = None
        p = window.find(PATH_KEY)
        if p >= 0:
            for off in range(len(PATH_KEY), len(PATH_KEY) + 8):
                if p + off + 4 > len(window):
                    break
                value = struct.unpack_from("<i", window, p + off)[0]
                if value in (0, 1):
                    pathway = value
                    break
        if tick <= 0 and not uid:
            continue
        notes.append({"tick": tick, "note_uid": uid, "length": length, "pathway": pathway})
    notes.sort(key=lambda n: n["tick"])
    return notes


def parse_stage_delay(serialized: bytes) -> float:
    if not serialized:
        return 0.0
    p = serialized.rfind(DELAY_KEY)
    if p < 0:
        return 0.0
    for off in (len(DELAY_KEY), len(DELAY_KEY) + 2):
        if p + off + 16 <= len(serialized):
            value = _rd_dec(serialized, p + off)
            if abs(value) < 60:
                return float(value)
    return 0.0


_notedata_cache: Dict[str, Dict[str, dict]] = {}
_bundle_cache_key = ""
_bundle_cache: Optional[tuple] = None


def load_notedata(bundle_dir: Path) -> Dict[str, dict]:
    key = str(bundle_dir.resolve())
    cached = _notedata_cache.get(key)
    if cached is not None:
        return cached
    hits = sorted(bundle_dir.glob("config_others_assets_notedata_*.bundle"))
    if not hits:
        raise FileNotFoundError(f"同目录缺少 notedata：{bundle_dir}")
    unitypy = __import__("UnityPy")
    env = unitypy.load(str(hits[0]))
    text = next(obj.read() for obj in env.objects if obj.type.name == "TextAsset")
    raw = text.m_Script if isinstance(text.m_Script, str) else bytes(text.m_Script).decode("utf-8-sig")
    table = json.loads(raw)
    loaded = {str(entry.get("uid")): entry for entry in table}
    _notedata_cache[key] = loaded
    return loaded


def _load_bundle_maps(path: str, stage_map: str = ""):
    global _bundle_cache_key, _bundle_cache
    bundle = Path(path)
    requested = (stage_map or "").strip().lower()
    key = (str(bundle.resolve()), requested)
    if _bundle_cache is not None and _bundle_cache_key == key:
        return _bundle_cache
    unitypy = __import__("UnityPy")
    if not bundle.is_file():
        raise FileNotFoundError(bundle)
    templates = load_notedata(find_notedata_dir(bundle))
    env = unitypy.load(str(bundle))
    maps: Dict[int, dict] = {}
    t_audio = 120.0
    song_name = bundle.stem.replace("_assets_all", "").replace("music_", "")
    for obj in env.objects:
        if obj.type.name == "AudioClip":
            clip = obj.read()
            length = _clip_length(clip)
            if length > 0:
                t_audio = length
            continue
        if obj.type.name != "MonoBehaviour":
            continue
        data = obj.read()
        name = getattr(data, "m_Name", "") or ""
        if "_map" not in name:
            continue
        serialized = _serialized_bytes(data)
        raw_notes = parse_music_data(serialized) if serialized else []
        joined = [join_template(n, templates) for n in raw_notes]
        try:
            map_bpm = float(data.bpm)
        except Exception:
            map_bpm = 120.0
        try:
            diff = int(data.difficulty)
        except Exception:
            diff = 1
        idx = _map_index_of(name, diff)
        canonical = f"{song_name}_map{idx}".lower()
        current = maps.get(idx)
        if current is not None:
            current_name = current["name"].lower()
            if current_name == requested:
                continue
            if name.lower() != requested and current_name == canonical:
                continue
            if name.lower() != requested and name.lower() != canonical:
                continue
        maps[idx] = {
            "name": name,
            "bpm": map_bpm,
            "delay": parse_stage_delay(serialized) if serialized else 0.0,
            "notes": joined,
            "music": getattr(data, "music", "") or song_name,
        }
    _bundle_cache_key = key
    _bundle_cache = (maps, t_audio, song_name, bundle.name)
    return _bundle_cache


def join_template(note: dict, templates: Dict[str, dict]) -> dict:
    entry = templates.get(note["note_uid"]) or {}
    typ = int(entry.get("type") or 0)
    pathway = note["pathway"]
    if pathway is None:
        pathway = int(entry.get("pathway") or 0)
    ibms = _norm_ibms(entry.get("ibms_id"))
    des = str(entry.get("des") or "")
    if typ == 8:
        ibms = _mash_ibms(ibms, des)
    elif len(ibms) != 2:
        if typ == 3:
            ibms = "0F"
        elif typ == 2:
            ibms = "0H"
        elif typ == 5:
            ibms = "11"
        elif typ == 8:
            ibms = "0G"
        else:
            ibms = "01" if typ == 1 else ibms
    return {
        "tick": float(note["tick"]),
        "length": float(note["length"] or 0),
        "air": pathway == 1,
        "type": typ,
        "bms_id": ibms,
        "note_uid": note["note_uid"],
        "des": des,
        "speed": int(entry.get("speed") or 1),
    }


def _is_event(typ: int, ibms: str) -> bool:
    return bool(ibms) and (typ == 0 or typ >= 9)


def _lane_of(ibms: str, air: bool) -> Lane:
    if ibms == "0E":
        return Lane.BOTH
    if ibms in MID_IBMS:
        return Lane.MID
    return Lane.AIR if air else Lane.GROUND


def _inside_hold(t: float, air: bool, occupied: List[Tuple[float, float, bool]]) -> bool:
    for start, end, hold_air in occupied:
        if hold_air == air and start - 0.001 <= t <= end + 0.001:
            return True
    return False


def notes_to_events(joined: List[dict], map_index: int) -> List[ChartEvent]:
    holds: List[Tuple[float, float, bool, str]] = []
    for note in joined:
        typ, ibms = note["type"], note["bms_id"]
        if not ibms or _is_event(typ, ibms):
            continue
        length = note["length"]
        if typ == 3 and length > MIN_HOLD_SEC:
            holds.append((note["tick"], note["tick"] + length, note["air"], ibms if ibms != "0F" else "0F"))
        elif typ == 8 and length > MIN_HOLD_SEC:
            holds.append((note["tick"], note["tick"] + length, note["air"], _mash_ibms(ibms, note.get("des") or "")))
    occupied = [(a, b, air) for a, b, air, _ in holds]

    events: List[ChartEvent] = []
    for start, end, air, ibms in holds:
        note_type = BMSParser._infer_note_type(ibms)
        kind = EventKind.MASH_ON if note_type == NoteType.MASH or ibms in MASH_IBMS else EventKind.HOLD_ON
        lane = Lane.MID if kind == EventKind.MASH_ON else _lane_of(ibms, air)
        events.append(
            ChartEvent(
                t_sec=start,
                t_end_sec=end,
                lane=lane,
                kind=kind,
                ibms_id=ibms,
                note_type=note_type if kind != EventKind.MASH_ON else NoteType.MASH,
                bms_channel=53 if air else 54,
                map_n=map_index,
            )
        )

    for note in joined:
        typ, ibms, t, air = note["type"], note["bms_id"], note["tick"], note["air"]
        if not ibms or _is_event(typ, ibms):
            continue
        if typ in (3, 8):
            if note["length"] > MIN_HOLD_SEC:
                continue
            if ibms == "0F":
                # length=0 是长按中间刻度。点长才是星：条内副键，空地主键。
                if note["length"] <= 0:
                    continue
                events.append(
                    ChartEvent(
                        t_sec=t,
                        t_end_sec=None,
                        lane=Lane.AIR if air else Lane.GROUND,
                        kind=EventKind.TAP,
                        ibms_id="0F",
                        note_type=NoteType.HOLD,
                        bms_channel=13 if air else 14,
                        map_n=map_index,
                        use_alt=_inside_hold(t, air, occupied),
                    )
                )
                continue
            if _inside_hold(t, air, occupied):
                continue
        note_type = BMSParser._infer_note_type(ibms)
        if note_type == NoteType.DODGE or typ == 2:
            events.append(
                ChartEvent(
                    t_sec=t,
                    t_end_sec=None,
                    lane=Lane.AIR if air else Lane.GROUND,
                    kind=EventKind.DODGE,
                    ibms_id=ibms or "0H",
                    note_type=NoteType.DODGE,
                    bms_channel=13 if air else 14,
                    map_n=map_index,
                )
            )
            continue
        if ibms in MID_IBMS:
            lane = Lane.MID
        else:
            lane = _lane_of(ibms, air)
        events.append(
            ChartEvent(
                t_sec=t,
                t_end_sec=None,
                lane=lane,
                kind=EventKind.TAP,
                ibms_id=ibms,
                note_type=note_type,
                bms_channel=13 if air else 14,
                map_n=map_index,
            )
        )
    return events


def _clip_length(clip) -> float:
    for attr in ("m_Length", "length", "duration"):
        value = getattr(clip, attr, None)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _map_index_of(name: str, difficulty: int) -> int:
    match = MAP_NAME_RE.search(name)
    if match:
        n = int(match.group(1))
        if 1 <= n <= 6:
            return n
    return difficulty if 1 <= difficulty <= 6 else 1


def find_notedata_dir(music_bundle: Path) -> Path:
    folder = music_bundle.parent
    if list(folder.glob("config_others_assets_notedata_*.bundle")):
        return folder
    raise FileNotFoundError(
        "找不到 config_others_assets_notedata_*.bundle，应与 music_*.bundle 同在 "
        r"MuseDash_Data\StreamingAssets\aa\StandaloneWindows64"
    )


def parse_music_bundle(path: str, map_index: int = 1, stage_map: str = "") -> ChartIR:
    maps, t_audio, song_name, bundle_name = _load_bundle_maps(path, stage_map)
    bpm = 120.0
    delay = 0.0

    if map_index not in maps:
        available = ", ".join(str(i) for i in sorted(maps)) or "无"
        raise ValueError(f"{bundle_name} 没有 map{map_index}（现有: {available}）")

    chosen = maps[map_index]
    if stage_map and chosen["name"].lower() != stage_map.strip().lower():
        raise ValueError(f"{bundle_name} 没有指定的谱面对象 {stage_map}")
    bpm = float(chosen["bpm"] or 120.0)
    delay = float(chosen["delay"] or 0.0)
    from .dodge import plan_gear_dodges
    from .hold_hits import plan_hold_hits

    notes = chosen["notes"]
    hide_spans = sum(1 for n in notes if n.get("bms_id") == "1J")
    ghosts = sum(1 for n in notes if n.get("bms_id") == "21")
    doubles = sum(1 for n in notes if n.get("bms_id") == "0E")
    if hide_spans or ghosts:
        logger.info(
            "特殊谱面: 谱面消失 %d 段，幽灵 %d，双押 %d。消失期间的键照常打",
            hide_spans,
            ghosts,
            doubles,
        )
    raw_events = notes_to_events(notes, map_index)
    raw_gears = [e for e in raw_events if e.kind == EventKind.DODGE]
    events = plan_hold_hits(plan_gear_dodges(raw_events))
    t_last = 0.0
    for event in events:
        end_t = event.t_end_sec if event.t_end_sec is not None else event.t_sec
        t_last = max(t_last, end_t)

    info = MDMInfo(
        bpm=bpm,
        delay=delay,
        name=str(chosen.get("music") or song_name),
        author="",
    )
    return ChartIR(
        events=events,
        info=info,
        map_index=map_index,
        t_audio_sec=t_audio,
        t_last_action_sec=t_last,
        raw_gears=raw_gears,
    )
