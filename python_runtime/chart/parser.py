"""
Muse Dash 谱面解析：MDM 包（info.json + mapN.bms）→ ChartIR
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


BMS_LINE_RE = re.compile(r"#(\d{3})(\d{2}):([0-9A-Za-z]+)")
SLOTS_PER_MEASURE = 192
AUDIO_NAMES = ("music.ogg", "music.mp3", "music.wav")


class NoteType(Enum):
    TAP = 1
    DODGE = 2
    HOLD = 3
    GHOST = 4
    BOSS_MELEE = 5
    COLLECTIBLE = 6
    NOTE_STRIP = 7
    MASH = 8
    SCRIPT = 0


class Lane(Enum):
    AIR = "air"
    GROUND = "ground"
    MID = "mid"
    BOTH = "both"


class EventKind(Enum):
    TAP = "tap"
    HOLD_ON = "hold_on"
    HOLD_OFF = "hold_off"
    MASH_ON = "mash_on"
    MASH_OFF = "mash_off"
    DODGE = "dodge"
    CH15 = "ch15"
    IGNORE = "ignore"


SKIP_KINDS = {EventKind.IGNORE, EventKind.CH15}


@dataclass
class ChartEvent:
    t_sec: float
    t_end_sec: Optional[float]
    lane: Lane
    kind: EventKind
    ibms_id: str
    note_type: NoteType
    bms_channel: int
    map_n: int
    use_alt: bool = False
    skip: bool = False
    hold_tail_repress_at_sec: Optional[float] = None


@dataclass
class MDMInfo:
    bpm: float
    delay: float = 0.0
    name: str = ""
    author: str = ""
    difficulty: List[int] = field(default_factory=lambda: [0, 0, 0, 0, 0, 0])


@dataclass
class ChartIR:
    events: List[ChartEvent]
    info: MDMInfo
    map_index: int
    t_audio_sec: float
    t_last_action_sec: float
    raw_gears: List[ChartEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.events.sort(key=lambda e: (e.t_sec, e.kind.value))


def info_from_dict(data: dict) -> MDMInfo:
    return MDMInfo(
        bpm=float(data.get("bpm", 120) or 120),
        delay=float(data.get("delay", 0.0) or 0.0),
        name=str(data.get("name", "")),
        author=str(data.get("author", "")),
        difficulty=[int(data.get(f"difficulty{i}", 0) or 0) for i in range(1, 7)],
    )


def _read_audio_length(source: Union[str, Path, io.BytesIO]) -> Optional[float]:
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        MutagenFile = None  # type: ignore[misc, assignment]
    if MutagenFile is not None:
        try:
            if isinstance(source, io.BytesIO):
                source.seek(0)
            audio = MutagenFile(source)
            if audio is not None and getattr(audio, "info", None) is not None:
                return float(audio.info.length)
        except Exception:
            pass
    if isinstance(source, io.BytesIO):
        try:
            from mutagen.oggvorbis import OggVorbis

            source.seek(0)
            return float(OggVorbis(source).info.length)
        except Exception:
            return None
    return None


class BMSParser:
    """BMS 解析。ibms_id 为 36 进制风格两字符（含 G–Z），不能当十六进制字面量。"""

    IBMS_TYPE_MAP: Dict[str, NoteType] = {
        **{f"{i:02X}": NoteType.TAP for i in range(0x01, 0x0F)},
        "0C": NoteType.TAP,
        "0D": NoteType.TAP,
        "0E": NoteType.TAP,
        "0F": NoteType.HOLD,
        "0G": NoteType.MASH,
        "0H": NoteType.DODGE,
        "0I": NoteType.TAP,
        "0J": NoteType.TAP,
        "11": NoteType.BOSS_MELEE,
        "12": NoteType.BOSS_MELEE,
        "13": NoteType.TAP,
        "14": NoteType.TAP,
        "15": NoteType.TAP,
        "16": NoteType.MASH,
        "17": NoteType.MASH,
        "18": NoteType.DODGE,
        "20": NoteType.COLLECTIBLE,
        "21": NoteType.GHOST,
        "22": NoteType.COLLECTIBLE,
        "23": NoteType.NOTE_STRIP,
        "40": NoteType.TAP,
        "41": NoteType.TAP,
        "42": NoteType.TAP,
        **{f"3{d}": NoteType.DODGE for d in "0123456789ABCDEFGHI"},
        "M0": NoteType.TAP,
        "M1": NoteType.HOLD,
        "M2": NoteType.TAP,
        "M5": NoteType.TAP,
        "M6": NoteType.TAP,
        "M7": NoteType.HOLD,
    }

    TAP_CHANNELS = {13: Lane.AIR, 14: Lane.GROUND}
    HOLD_CHANNELS = {53: Lane.AIR, 54: Lane.GROUND}
    BPM_CHANNEL = 8
    EVENT_CHANNEL = 15
    MIN_HOLD_SEC = 0.02

    def __init__(self, bpm: float) -> None:
        self.bpm = bpm if bpm > 0 else 120.0
        self._bpm_changes: List[Tuple[float, float]] = []
        self._bpm_timeline: List[Tuple[float, float, float]] = []
        self._paired_start: Dict[int, ChartEvent] = {}

    def parse(self, bms_content: str, map_n: int) -> List[ChartEvent]:
        events: List[ChartEvent] = []
        ticks: Dict[int, float] = {}
        end_ticks: Dict[int, float] = {}
        paired_start: Dict[int, ChartEvent] = {}
        hold_pairs: Dict[Tuple[str, Lane], ChartEvent] = {}
        bpm_raw: List[Tuple[float, float]] = []

        for match in BMS_LINE_RE.finditer(bms_content):
            measure = int(match.group(1))
            channel = int(match.group(2))
            data = match.group(3)
            num_objs = len(data) // 2
            if num_objs == 0:
                continue

            for i in range(num_objs):
                obj_id = data[i * 2 : i * 2 + 2].upper()
                if obj_id == "00":
                    continue
                tick = measure * SLOTS_PER_MEASURE + (i * SLOTS_PER_MEASURE) / num_objs

                if channel == self.BPM_CHANNEL:
                    try:
                        new_bpm = int(obj_id, 16)
                    except ValueError:
                        continue
                    if new_bpm > 0:
                        bpm_raw.append((tick, float(new_bpm)))
                    continue

                if channel in self.TAP_CHANNELS:
                    event = self._create_tap_event(
                        obj_id, self.TAP_CHANNELS[channel], channel, map_n
                    )
                    ticks[id(event)] = tick
                    events.append(event)
                elif channel in self.HOLD_CHANNELS:
                    self._process_hold_pair(
                        obj_id,
                        tick,
                        self.HOLD_CHANNELS[channel],
                        channel,
                        map_n,
                        hold_pairs,
                        events,
                        ticks,
                        end_ticks,
                        paired_start,
                    )
                elif channel == self.EVENT_CHANNEL:
                    event = ChartEvent(
                        t_sec=0.0,
                        t_end_sec=None,
                        lane=Lane.MID,
                        kind=EventKind.CH15,
                        ibms_id=obj_id,
                        note_type=NoteType.SCRIPT,
                        bms_channel=channel,
                        map_n=map_n,
                    )
                    ticks[id(event)] = tick
                    events.append(event)

        bpm_raw.sort(key=lambda x: x[0])
        self._bpm_changes = bpm_raw
        self._build_bpm_timeline()

        for event in events:
            event.t_sec = self._tick_to_sec(ticks.get(id(event), 0.0))
            if id(event) in end_ticks:
                event.t_end_sec = self._tick_to_sec(end_ticks[id(event)])

        self._paired_start = paired_start
        return events

    def _build_bpm_timeline(self) -> None:
        """(tick, bpm, cumulative_sec_at_tick)"""
        timeline: List[Tuple[float, float, float]] = [(0.0, self.bpm, 0.0)]
        last_tick = 0.0
        last_bpm = self.bpm
        acc = 0.0
        for change_tick, change_bpm in self._bpm_changes:
            if change_tick <= last_tick:
                last_bpm = change_bpm
                if timeline:
                    timeline[-1] = (timeline[-1][0], change_bpm, timeline[-1][2])
                continue
            acc += (change_tick - last_tick) * (240.0 / last_bpm / SLOTS_PER_MEASURE)
            timeline.append((change_tick, change_bpm, acc))
            last_tick = change_tick
            last_bpm = change_bpm
        self._bpm_timeline = timeline

    def _tick_to_sec(self, tick: float) -> float:
        if not self._bpm_timeline:
            return tick * (240.0 / self.bpm / SLOTS_PER_MEASURE)

        start_tick, bpm, acc = self._bpm_timeline[0]
        for t, b, a in self._bpm_timeline:
            if t > tick:
                break
            start_tick, bpm, acc = t, b, a
        return acc + (tick - start_tick) * (240.0 / bpm / SLOTS_PER_MEASURE)

    def _create_tap_event(
        self, ibms_id: str, lane: Lane, channel: int, map_n: int
    ) -> ChartEvent:
        note_type = self._infer_note_type(ibms_id)
        if ibms_id == "0E":
            lane = Lane.BOTH
        if note_type == NoteType.BOSS_MELEE:
            lane = Lane.MID
        kind = EventKind.DODGE if note_type == NoteType.DODGE else EventKind.TAP
        return ChartEvent(
            t_sec=0.0,
            t_end_sec=None,
            lane=lane,
            kind=kind,
            ibms_id=ibms_id,
            note_type=note_type,
            bms_channel=channel,
            map_n=map_n,
        )

    def _process_hold_pair(
        self,
        ibms_id: str,
        tick: float,
        lane: Lane,
        channel: int,
        map_n: int,
        hold_pairs: Dict[Tuple[str, Lane], ChartEvent],
        events: List[ChartEvent],
        ticks: Dict[int, float],
        end_ticks: Dict[int, float],
        paired_start: Dict[int, ChartEvent],
    ) -> None:
        note_type = self._infer_note_type(ibms_id)
        key = (ibms_id, lane)
        if key in hold_pairs:
            start_event = hold_pairs.pop(key)
            end_ticks[id(start_event)] = tick
            kind = EventKind.HOLD_OFF if note_type == NoteType.HOLD else EventKind.MASH_OFF
            off = ChartEvent(
                t_sec=0.0,
                t_end_sec=None,
                lane=lane,
                kind=kind,
                ibms_id=ibms_id,
                note_type=note_type,
                bms_channel=channel,
                map_n=map_n,
            )
            ticks[id(off)] = tick
            paired_start[id(off)] = start_event
            events.append(off)
            return

        kind = EventKind.HOLD_ON if note_type == NoteType.HOLD else EventKind.MASH_ON
        event = ChartEvent(
            t_sec=0.0,
            t_end_sec=None,
            lane=lane,
            kind=kind,
            ibms_id=ibms_id,
            note_type=note_type,
            bms_channel=channel,
            map_n=map_n,
        )
        ticks[id(event)] = tick
        events.append(event)
        hold_pairs[key] = event

    @classmethod
    def _infer_note_type(cls, ibms_id: str) -> NoteType:
        return cls.IBMS_TYPE_MAP.get(ibms_id.upper(), NoteType.TAP)


class MDMParser:
    def __init__(self) -> None:
        self.info: Optional[MDMInfo] = None

    def parse(self, mdm_path: str, map_index: int = 1, stage_map: str = "") -> ChartIR:
        path = Path(mdm_path)
        if path.is_dir():
            return self._parse_folder(path, map_index)
        if path.suffix.lower() in {".mdm", ".zip"}:
            return self._parse_zip(path, map_index)
        if path.suffix.lower() == ".bundle":
            from .bundle import parse_music_bundle

            ir = parse_music_bundle(str(path), map_index, stage_map)
            self.info = ir.info
            return ir
        raise ValueError(f"不支持的谱面格式: {path}")

    def _parse_zip(self, zip_path: Path, map_index: int) -> ChartIR:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            info_name = next((n for n in names if n.replace("\\", "/").endswith("info.json")), None)
            if not info_name:
                raise ValueError("MDM 包中缺少 info.json")
            self.info = info_from_dict(json.loads(zf.read(info_name)))
            bms_key = self._find_member(names, f"map{map_index}.bms")
            if not bms_key:
                raise ValueError(f"MDM 包中缺少 map{map_index}.bms")
            bms_content = zf.read(bms_key).decode("utf-8-sig")
            t_audio = self._audio_from_zip(zf, names)
        return self._parse_bms(bms_content, map_index, t_audio)

    def _parse_folder(self, folder_path: Path, map_index: int) -> ChartIR:
        info_path = folder_path / "info.json"
        if not info_path.exists():
            raise ValueError(f"缺少 info.json: {info_path}")
        with open(info_path, "r", encoding="utf-8-sig") as f:
            self.info = info_from_dict(json.load(f))
        bms_path = folder_path / f"map{map_index}.bms"
        if not bms_path.exists():
            raise ValueError(f"缺少 map{map_index}.bms: {bms_path}")
        bms_content = bms_path.read_text(encoding="utf-8-sig")
        t_audio = 120.0
        for name in AUDIO_NAMES:
            candidate = folder_path / name
            if candidate.exists():
                length = _read_audio_length(candidate)
                if length:
                    t_audio = length
                    break
        return self._parse_bms(bms_content, map_index, t_audio)

    def _parse_bms(self, bms_content: str, map_index: int, t_audio_sec: float) -> ChartIR:
        assert self.info is not None
        bms = BMSParser(self.info.bpm)
        events = bms.parse(bms_content, map_index)
        return self._build_ir(events, map_index, t_audio_sec, getattr(bms, "_paired_start", {}))

    @staticmethod
    def _find_member(names: List[str], filename: str) -> Optional[str]:
        target = filename.lower()
        for name in names:
            if name.replace("\\", "/").split("/")[-1].lower() == target:
                return name
        return None

    @staticmethod
    def _audio_from_zip(zf: zipfile.ZipFile, names: List[str]) -> float:
        for name in names:
            base = name.replace("\\", "/").split("/")[-1].lower()
            if base in AUDIO_NAMES:
                data = zf.read(name)
                length = _read_audio_length(io.BytesIO(data))
                if length:
                    return length
        return 120.0

    def _build_ir(
        self,
        events: List[ChartEvent],
        map_index: int,
        t_audio_sec: float,
        paired_start: Optional[Dict[int, ChartEvent]] = None,
    ) -> ChartIR:
        ir_events: List[ChartEvent] = []
        starts_to_drop = set()
        paired_start = paired_start or {}

        for event in events:
            paired = paired_start.get(id(event))
            if paired is not None and event.kind in (EventKind.HOLD_OFF, EventKind.MASH_OFF):
                length = event.t_sec - paired.t_sec
                if length <= BMSParser.MIN_HOLD_SEC and paired.ibms_id != "0F":
                    starts_to_drop.add(id(paired))
                    continue
                if length > BMSParser.MIN_HOLD_SEC:
                    paired.t_end_sec = event.t_sec

        for event in events:
            if id(event) in starts_to_drop:
                continue
            paired = paired_start.get(id(event))
            if paired is not None and id(paired) in starts_to_drop:
                continue

            kind, lane = self._map_note_to_action(event)
            if kind in (EventKind.HOLD_OFF, EventKind.MASH_OFF):
                continue
            if kind == EventKind.HOLD_ON and event.t_end_sec is None:
                kind = EventKind.TAP
            if kind == EventKind.MASH_ON and event.t_end_sec is None:
                kind = EventKind.TAP
                lane = Lane.MID
            if kind in SKIP_KINDS:
                continue
            event.kind = kind
            event.lane = lane
            ir_events.append(event)

        from .dodge import plan_gear_dodges
        from .hold_hits import plan_hold_hits

        raw_gears = [e for e in ir_events if e.kind == EventKind.DODGE]
        ir_events = plan_hold_hits(plan_gear_dodges(ir_events))
        t_last_action = 0.0
        for event in ir_events:
            end_t = event.t_end_sec if event.t_end_sec is not None else event.t_sec
            t_last_action = max(t_last_action, end_t)

        assert self.info is not None
        return ChartIR(
            events=ir_events,
            info=self.info,
            map_index=map_index,
            t_audio_sec=t_audio_sec,
            t_last_action_sec=t_last_action,
            raw_gears=raw_gears,
        )

    @staticmethod
    def _map_note_to_action(event: ChartEvent) -> Tuple[EventKind, Lane]:
        if event.kind in (EventKind.HOLD_ON, EventKind.HOLD_OFF):
            return event.kind, event.lane
        if event.kind in (EventKind.MASH_ON, EventKind.MASH_OFF):
            return event.kind, Lane.MID
        if event.kind == EventKind.CH15:
            return EventKind.CH15, event.lane

        note_type = event.note_type
        if note_type == NoteType.DODGE:
            return EventKind.DODGE, event.lane
        if note_type == NoteType.MASH:
            return EventKind.TAP, Lane.MID
        if note_type == NoteType.HOLD:
            return EventKind.TAP, event.lane
        if note_type == NoteType.BOSS_MELEE:
            return EventKind.TAP, Lane.MID
        if note_type == NoteType.TAP and event.ibms_id == "0E":
            return EventKind.TAP, Lane.BOTH
        if note_type in (
            NoteType.TAP,
            NoteType.GHOST,
            NoteType.COLLECTIBLE,
            NoteType.NOTE_STRIP,
        ):
            return EventKind.TAP, event.lane
        return EventKind.IGNORE, event.lane
