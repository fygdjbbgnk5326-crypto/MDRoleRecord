"""
高精度事件调度：把谱面事件展开成 down/up，睡到下一事件前 1ms 再自旋。
"""
from __future__ import annotations

import ctypes
import heapq
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

try:
    from chart.parser import ChartEvent, ChartIR, EventKind, Lane, NoteType
except ImportError:
    from ..chart.parser import ChartEvent, ChartIR, EventKind, Lane, NoteType

try:
    from chart.dodge import JUMP_SEC
except ImportError:
    try:
        from ..chart.dodge import JUMP_SEC
    except ImportError:
        JUMP_SEC = 0.47

# 收集物提前量从 18ms 增加到 25ms，避免打晚
COLLECT_LEAD_SEC = 0.025
COLLECT_PULSE_SEC = 0.025
COLLECT_HOLD_SEC = 0.020
# Normal InputManager playback needs the jump rise before the gear reaches the
# player. The chart-only mode's 5 ms hop lead is too late for the dense 0H run.
VIRTUAL_DODGE_LEAD_SEC = 0.050
# A ground 18 beside an air note is judged Great when its jump starts 55 ms
# before the chart position. The measured 25 ms lead leaves time to rise.
VIRTUAL_COTIMED_18_LEAD_SEC = 0.020
# A normal air tap immediately after a co-timed ground 18 can receive Great
# even when the preceding dodge is Perfect. In verified rapid runs, moving
# each tap that has another matching air tap 70-120 ms later by 60 ms fixes it.
VIRTUAL_COTIMED_18_RAPID_TAP_DELAY_SEC = 0.060
VIRTUAL_COTIMED_18_SINGLE_TAP_HOLD_SEC = 0.240

# A rapid second air tap must release before the following two-hit 0E chord.
# Rush-Hour map4's 106.071 and 109.286 s chords both became Perfect at 40 ms.
VIRTUAL_PRE_DOUBLE_AIR_TAP_HOLD_SEC = 0.040
VIRTUAL_DODGE_HOLD_SEC = 0.550
VIRTUAL_COLLECT_LEAD_SEC = 0.005
# Measured with normal InputManager input: air starts about 40 ms after down
# and ends about 450 ms after down. AutoPlay's jump duration is longer.
VIRTUAL_AIR_END_SEC = 0.442
VIRTUAL_RISE_SEC = 0.045
VIRTUAL_GEAR_CONTACT_SEC = 0.100
VIRTUAL_GEAR_CLEAR_SEC = 0.160
VIRTUAL_GEAR_MARGIN_SEC = 0.010
VIRTUAL_MIN_REHOP_SEC = 0.520
VIRTUAL_COLLECT_AFTER_GEAR_SEC = 0.175
# Ground 18 gears can still hit a ground collect after their chart time. The
# observed 122.225 s gear clears at 122.380 s with normal virtual input.
VIRTUAL_COLLECT_AFTER_GROUND_18_SEC = 0.155
VIRTUAL_MAX_COLLECT_SHIFT_SEC = 0.060
VIRTUAL_REPHASE_GAP_SEC = 0.505
# Measured normal-input arc for an isolated pair of ground 0H gears. The
# 17.545/17.946 s Rush-Hour pair has only a ~3 ms feasible press window: the
# first contact must see the rise, and the second must happen before landing.
VIRTUAL_PAIR_CONTACT_SEC = 0.113
VIRTUAL_PAIR_RISE_SEC = 0.045
VIRTUAL_PAIR_AIR_SEC = 0.455
VIRTUAL_PAIR_MARGIN_SEC = 0.003


@dataclass
class SchedulerConfig:
    device_delay_sec: float = 0.0
    inject_delay_sec: float = 0.012
    tick_interval: float = 0.001
    pulse_duration: float = 0.04
    mash_frequency: float = 12.0
    grace_tail_sec: float = 2.0


@dataclass(order=True)
class TimedAction:
    t_fire: float
    seq: int
    down: bool = field(compare=False)
    lane: Lane = field(compare=False)
    alt: bool = field(default=False, compare=False)
    star: bool = field(default=False, compare=False)
    drop: bool = field(default=False, compare=False)
    collect: bool = field(default=False, compare=False)
    action_kind: str = field(default="tap", compare=False)
    event_id: int = field(default=0, compare=False)


class EventScheduler:
    def __init__(
        self,
        chart_ir: ChartIR,
        injector: Any,
        config: Optional[SchedulerConfig] = None,
        on_complete: Optional[Callable[[], None]] = None,
        virtual_input: bool = False,
    ) -> None:
        self.chart_ir = chart_ir
        self.injector = injector
        self.config = config or SchedulerConfig()
        self.on_complete = on_complete
        self.virtual_input = virtual_input

        self._total_delay = self.config.device_delay_sec + self.config.inject_delay_sec
        self._heap: List[TimedAction] = []
        self._seq = 0
        self._build_heap()

        self._t_start: Optional[float] = None
        self._is_paused = False
        self._pause_time = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._timer_period_set = False
        self._late_up: List[tuple] = []
        self._star_down_wall: dict = {}
        self._collect_down_wall: dict = {}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _push(
        self,
        t_fire: float,
        down: bool,
        lane: Lane,
        alt: bool = False,
        star: bool = False,
        drop: bool = False,
        collect: bool = False,
        action_kind: str = "tap",
        event_id: int = 0,
    ) -> None:
        heapq.heappush(
            self._heap,
            TimedAction(
                t_fire=t_fire,
                seq=self._next_seq(),
                down=down,
                lane=lane,
                alt=alt,
                star=star,
                drop=drop,
                collect=collect,
                action_kind=action_kind,
                event_id=event_id,
            ),
        )

    def _build_heap(self) -> None:
        self._heap.clear()
        self._seq = 0
        pulse = max(self.config.pulse_duration, 0.008)
        mash_hz = max(self.config.mash_frequency, 1.0)
        mash_interval = 1.0 / mash_hz
        delay = self._total_delay

        # 检测齿轮海区域（密集齿轮，间隔 < 0.5s）
        gear_times = []
        for event in self.chart_ir.events:
            if event.kind == EventKind.DODGE:
                gear_times.append(event.t_sec)

        # 构建齿轮海时间区间
        gear_sea_zones = []
        if gear_times:
            zone_start = gear_times[0]
            zone_end = gear_times[0]
            for t in gear_times[1:]:
                if t - zone_end <= 0.5:  # 齿轮间隔小于0.5s，属于同一齿轮海
                    zone_end = t
                else:
                    if zone_end - zone_start >= 1.0:  # 齿轮海至少持续1秒
                        gear_sea_zones.append((zone_start, zone_end))
                    zone_start = t
                    zone_end = t
            if zone_end - zone_start >= 1.0:
                gear_sea_zones.append((zone_start, zone_end))

        def in_gear_sea(t):
            """检查时间 t 是否在齿轮海区域内"""
            for start, end in gear_sea_zones:
                if start - 0.5 <= t <= end + 0.5:  # 前后各扩展0.5s
                    return True
            return False

        for event_id, event in enumerate(self.chart_ir.events, 1):
            # Node AutoPlay can resolve a 0B tap and a 0F hold head together.
            # Virtual input needs a separate finger for the skipped 0B event.
            # A 17 tap at an air hold's tail is also a separate judgement: the
            # hold release at that instant does not hit it (Oshama 120.951 s).
            tail_mash = (
                self.virtual_input and event.kind == EventKind.TAP
                and event.ibms_id == "17"
                and any(
                    held.kind == EventKind.HOLD_ON
                    and held.t_end_sec is not None
                    and abs(held.t_end_sec - event.t_sec) <= 0.012
                    and Lane.AIR in self.injector._lanes(held.lane)
                    for held in self.chart_ir.events
                )
            )
            if getattr(event, "skip", False) and not (
                (self.virtual_input and event.kind == EventKind.TAP and event.ibms_id == "0B")
                or tail_mash
                or (self.virtual_input and event.hold_tail_repress_at_sec is not None)
            ):
                continue
            # 空中 D 也减 inject_delay（011448 不减会晚 27ms 吞掉 10.475）。
            # 组内间隔由 dodge.plan_gear_dodges 排好；这里不要再挤到 10.12。
            t0 = event.t_sec - delay
            if self.virtual_input and event.hold_tail_repress_at_sec is not None:
                t0 = max(t0, event.hold_tail_repress_at_sec - delay)
            kind = event.kind
            lane = event.lane
            if self.virtual_input and kind == EventKind.TAP and event.note_type == NoteType.DODGE and lane == Lane.AIR:
                lead = VIRTUAL_DODGE_LEAD_SEC
                if event.ibms_id == "18" and any(
                    g.ibms_id == "18" and g.lane == Lane.GROUND
                    and abs(g.t_sec - event.t_sec) <= 0.010
                    for g in self.chart_ir.raw_gears
                ) and any(
                    other.kind == EventKind.TAP
                    and other.note_type != NoteType.DODGE
                    and other.lane in (Lane.AIR, Lane.MID, Lane.BOTH)
                    and abs(other.t_sec - event.t_sec) <= 0.010
                    for other in self.chart_ir.events
                ):
                    lead = VIRTUAL_COTIMED_18_LEAD_SEC
                t0 -= lead

            # 在齿轮海区域内，延迟长按事件，优先保证跳跃
            if not self.virtual_input and in_gear_sea(event.t_sec) and kind == EventKind.HOLD_ON:
                # 检查附近是否有跳跃事件
                has_jump_nearby = False
                for other_event in self.chart_ir.events:
                    if (other_event.kind == EventKind.DODGE and
                        abs(other_event.t_sec - event.t_sec) < 0.3):
                        has_jump_nearby = True
                        break

                if has_jump_nearby:
                    # 延迟长按到跳跃之后
                    t0 = event.t_sec + 0.3 - delay

            if kind == EventKind.TAP:
                alt = bool(getattr(event, "use_alt", False))
                is_bar_star = event.ibms_id == "0F"
                is_collect = event.ibms_id in {"23", "20", "22"} or event.note_type in (
                    NoteType.NOTE_STRIP,
                    NoteType.COLLECTIBLE,
                )
                # 空中 23 必须按（官机 down_hit）。跑动里不能全靠自动吃，
                # 否则个别 15.1 / 7.9 会漏。只在起跳键 ±60ms 内让 D 优先。
                if (
                    is_collect
                    and event.lane == Lane.GROUND
                    and not self._airborne_at(event.t_sec)
                    and self._air_dodge_near(event.t_sec, 0.06)
                ):
                    continue
                if is_bar_star:
                    self._push(t0, True, lane, alt=alt, star=True, action_kind="bar_star", event_id=event_id)
                    self._push(t0 + 0.06, False, lane, alt=alt, star=True, action_kind="bar_star", event_id=event_id)
                elif is_collect:
                    c0 = t0 - (VIRTUAL_COLLECT_LEAD_SEC if self.virtual_input else COLLECT_LEAD_SEC)
                    self._push(c0, True, lane, alt=alt, collect=True, action_kind="collect", event_id=event_id)
                    self._push(c0 + COLLECT_PULSE_SEC, False, lane, alt=alt, collect=True, action_kind="collect", event_id=event_id)
                else:
                    action_kind = "dodge" if event.note_type == NoteType.DODGE else "tap"
                    self._push(t0, True, lane, alt=alt, action_kind=action_kind, event_id=event_id)
                    self._push(t0 + pulse, False, lane, alt=alt, action_kind=action_kind, event_id=event_id)
            elif kind == EventKind.HOLD_ON:
                alt = bool(getattr(event, "use_alt", False))
                self._push(t0, True, lane, alt=alt, action_kind="hold", event_id=event_id)
                if event.t_end_sec is not None:
                    self._push(event.t_end_sec - delay, False, lane, alt=alt, action_kind="hold", event_id=event_id)
            elif kind == EventKind.HOLD_OFF:
                alt = bool(getattr(event, "use_alt", False))
                self._push(t0, False, lane, alt=alt, action_kind="hold", event_id=event_id)
            elif kind == EventKind.MASH_ON:
                end = event.t_end_sec if event.t_end_sec is not None else event.t_sec
                t = t0
                t_end = end - delay
                while t <= t_end + 1e-9:
                    self._push(t, True, lane, action_kind="mash", event_id=event_id)
                    self._push(t + pulse, False, lane, action_kind="mash", event_id=event_id)
                    t += mash_interval
            elif kind == EventKind.MASH_OFF:
                continue

        heapq.heapify(self._heap)

    def export_actions(self, path: str, chart_uid: str = "", map_index: int | None = None) -> int:
        """导出动作表供游戏内注入器（MDRoleRecord.ActionInjector）使用。

        每行：`song_time down|up|tap lane kind event_id`，lane 为 air / ground。
        song_time 是歌曲时间（= t_fire + inject_delay，补回调度里扣掉的注入延迟），
        由 mod 在 LateUpdate 里按 timeFromMusicStart 对齐到帧后发键。返回动作数。
        """
        delay = self._total_delay
        actions = sorted(self._heap, key=lambda a: (a.t_fire, a.seq))
        lines: List[str] = []
        chart_uid = (chart_uid or "").strip().lower()
        export_map = self.chart_ir.map_index if map_index is None else int(map_index)
        down_times: dict[tuple[int, str], float] = {}
        for a in actions:
            song_t = a.t_fire + delay
            for ln in self.injector._lanes(a.lane):
                lane_name = "air" if ln == Lane.AIR else "ground"
                key = (a.event_id, lane_name)
                if a.alt or a.action_kind in {"collect", "bar_star"}:
                    if a.down:
                        lines.append(f"{song_t:.4f} tap {lane_name} {a.action_kind} {a.event_id}\n")
                    continue
                if a.down:
                    down_times[key] = song_t
                    lines.append(f"{song_t:.4f} down {lane_name} {a.action_kind} {a.event_id}\n")
                    continue
                if a.action_kind == "tap" and key in down_times:
                    song_t = down_times[key] + 0.280
                elif a.action_kind == "dodge" and key in down_times:
                    song_t = down_times[key] + JUMP_SEC
                lines.append(f"{song_t:.4f} up {lane_name} {a.action_kind} {a.event_id}\n")
        lines.sort(key=lambda line: float(line.split(" ", 1)[0]))
        with open(path, "w", encoding="utf-8") as f:
            f.write("# mdr_action_format=3\n")
            f.write(f"# chart_uid={chart_uid}\n")
            f.write(f"# map_index={export_map}\n")
            f.write(f"# action_count={len(lines)}\n")
            f.write("# song_time down|up|tap air|ground kind event_id\n")
            f.write("# kind=tap|hold|dodge|collect|mash|bar_star；event_id 用于重叠按住配对\n")
            f.writelines(lines)
        return len(lines)

    def export_virtual_actions(self, path: str, chart_uid: str, map_index: int | None = None,
                               stage_map: str = "") -> int:
        """Export paired event-level edges for MDRoleRecord's virtual touch backend.

        Every note gets its own finger in the mod. A collectible remains a real
        down/up pulse even while a long hold is active on the same lane.
        """
        if not self.virtual_input:
            raise ValueError("virtual_input=True is required when building the scheduler")
        chart_uid = (chart_uid or "").strip().lower()
        if not chart_uid:
            raise ValueError("virtual input requires a chart UID from an official bundle")
        export_map = self.chart_ir.map_index if map_index is None else int(map_index)
        delay = self._total_delay
        actions = sorted(self._heap, key=lambda a: (a.t_fire, a.seq))
        shifts = self._virtual_timing_shifts(actions, delay)
        rapid_tap_ids = self._virtual_cotimed_18_rapid_tap_ids()
        solo_rapid_tap_ids = {
            event_id for event_id in rapid_tap_ids
            if not any(other_id != event_id and
                       abs(self.chart_ir.events[other_id - 1].t_sec
                           - self.chart_ir.events[event_id - 1].t_sec) <= 0.120
                       for other_id in rapid_tap_ids)
        }
        for event_id in rapid_tap_ids:
            shifts[event_id] = max(
                shifts.get(event_id, 0.0), VIRTUAL_COTIMED_18_RAPID_TAP_DELAY_SEC
            )
        pre_double_air_tap_ids = self._virtual_pre_double_air_tap_ids(shifts)
        overlap_tap_ends = self._virtual_overlap_tap_ends()
        short_ground_pairs = self._virtual_ground_enemy_gear_pairs()
        lines: List[tuple[float, int, str]] = []
        down_times: dict[tuple[int, str], float] = {}
        active: set[tuple[int, str]] = set()
        dodge_downs: dict[str, List[float]] = {"air": [], "ground": []}
        for action in actions:
            if action.down and action.action_kind == "dodge":
                for lane in self.injector._lanes(action.lane):
                    dodge_downs["air" if lane == Lane.AIR else "ground"].append(
                        action.t_fire + delay + shifts.get(action.event_id, 0.0)
                    )
        for a in actions:
            # HOLD_OFF is already represented by HOLD_ON's t_end_sec and carries
            # a different chart event ID; it cannot release that finger.
            if a.action_kind == "hold" and not a.down and (a.event_id, "air") not in active and (a.event_id, "ground") not in active:
                continue
            song_t = max(0.0, a.t_fire + delay + shifts.get(a.event_id, 0.0))
            for ln in self.injector._lanes(a.lane):
                lane_name = "air" if ln == Lane.AIR else "ground"
                key = (a.event_id, lane_name)
                if a.down:
                    if key in active:
                        raise ValueError(f"duplicate down for {key}")
                    active.add(key)
                    down_times[key] = song_t
                    edge = "down"
                else:
                    if key not in active:
                        raise ValueError(f"up without down for {key}")
                    if a.action_kind == "tap":
                        tail_repress = self.chart_ir.events[a.event_id - 1].hold_tail_repress_at_sec
                        tap_hold = (0.120 if tail_repress is not None else
                                    0.040 if a.event_id in short_ground_pairs else
                                    VIRTUAL_PRE_DOUBLE_AIR_TAP_HOLD_SEC
                                    if lane_name == "air" and a.event_id in pre_double_air_tap_ids else
                                    VIRTUAL_COTIMED_18_SINGLE_TAP_HOLD_SEC
                                    if a.event_id in solo_rapid_tap_ids else 0.280)
                        song_t = down_times[key] + tap_hold
                        song_t = max(song_t, overlap_tap_ends.get(key, song_t))
                    elif a.action_kind == "dodge":
                        if lane_name == "ground" and a.event_id in short_ground_pairs:
                            song_t = down_times[key] + 0.040
                        else:
                        # Normal input takes time to rise. Keep the key through
                        # the observed late gear, but release early enough for
                        # the game's jump-action flag to reset before rehop.
                            next_down = next(
                                (t for t in dodge_downs[lane_name] if t > down_times[key] + 1e-6),
                                float("inf"),
                            )
                            song_t = min(down_times[key] + VIRTUAL_DODGE_HOLD_SEC, next_down - 0.120)
                            song_t = max(song_t, down_times[key] + JUMP_SEC)
                    elif a.action_kind in {"collect", "bar_star"}:
                        song_t = max(song_t, down_times[key] + 0.034)
                    active.remove(key)
                    edge = "up"
                lines.append((song_t, a.seq, f"{song_t:.4f} {edge} {lane_name} {a.action_kind} {a.event_id}\n"))
        if active:
            raise ValueError(f"unpaired virtual input events: {sorted(active)[:8]}")
        rephased = self._virtual_rephase_gear_streams(lines)
        if rephased:
            removed_ids = {event_id for event_id, _ in rephased[0]}
            preland_times = self._virtual_preland_pulses(lines, rephased[1])
            lines = [row for row in lines
                     if not (row[2].split()[4].strip() in removed_ids)]
            next_id = len(self.chart_ir.events) + 1
            next_seq = max((a.seq for a in actions), default=0) + 1
            for down in rephased[1]:
                event_id = next_id
                next_id += 1
                lines.append((down, next_seq,
                              f"{down:.4f} down air dodge {event_id}\n"))
                next_seq += 1
                up = down + JUMP_SEC
                lines.append((up, next_seq,
                              f"{up:.4f} up air dodge {event_id}\n"))
                next_seq += 1
            for down in preland_times:
                event_id = next_id
                next_id += 1
                lines.append((down, next_seq,
                              f"{down:.4f} down ground dodge {event_id}\n"))
                next_seq += 1
                up = down + 0.040
                lines.append((up, next_seq,
                              f"{up:.4f} up ground dodge {event_id}\n"))
                next_seq += 1
        lines = self._virtual_split_isolated_gear_pairs(lines)
        # Verified Rush-Hour map4 correction: the middle gear in this trio
        # needs a second rise while the ground notes prevent the generic
        # isolated-pair planner from splitting the first jump.
        if stage_map == "rush_hour_map4":
            gear_times = [g.t_sec for g in self.chart_ir.raw_gears
                          if g.ibms_id == "0H" and g.lane == Lane.GROUND]
            if all(any(abs(actual - expected) <= 0.002 for actual in gear_times)
                   for expected in (17.411, 17.813, 18.214)) and not any(
                       abs(t - 17.818) < 0.050 and line.split()[1:4] == ["down", "air", "dodge"]
                       for t, _, line in lines
                   ):
                event_id = max(int(row[2].split()[4]) for row in lines) + 1
                seq = max(row[1] for row in lines) + 1
                lines.extend((
                    (17.818, seq, f"17.8180 down air dodge {event_id}\n"),
                    (18.028, seq + 1, f"18.0280 up air dodge {event_id}\n"),
                ))
        dense_ids, last_dense_ids = self._virtual_dense_double_gear_ids()
        if dense_ids:
            updated = []
            for song_t, seq, line in lines:
                parts = line.split()
                event_id = int(parts[4])
                if event_id in dense_ids and parts[2] == "ground":
                    continue
                if event_id in last_dense_ids and parts[2:4] == ["air", "tap"]:
                    parts[3] = "dodge"
                    line = " ".join(parts) + "\n"
                updated.append((song_t, seq, line))
            lines = updated
        # A normal air tap can leave the character in its jump action when a
        # later 0H dodge key arrives. The dodge down is then consumed without a
        # new rise, and the gear hits as that first jump lands. Apply the same
        # measured pre-landing pulse used by rephased streams to isolated 0H
        # jumps, but only when the source event is an actual ground gear.
        ordinary_hops = []
        for song_t, _, line in lines:
            parts = line.split()
            if parts[1:4] != ["down", "air", "dodge"]:
                continue
            event_id = int(parts[4])
            if event_id > len(self.chart_ir.events):
                continue
            source = self.chart_ir.events[event_id - 1]
            if source.ibms_id == "0H" and source.note_type == NoteType.DODGE:
                ordinary_hops.append(song_t)
        preland_times = self._virtual_preland_pulses(lines, ordinary_hops)
        if preland_times:
            next_id = max(int(row[2].split()[4]) for row in lines) + 1
            next_seq = max((row[1] for row in lines), default=0) + 1
            for down in preland_times:
                event_id = next_id
                next_id += 1
                lines.append((down, next_seq,
                              f"{down:.4f} down ground dodge {event_id}\n"))
                next_seq += 1
                up = down + 0.040
                lines.append((up, next_seq,
                              f"{up:.4f} up ground dodge {event_id}\n"))
                next_seq += 1
            logging.getLogger(__name__).info(
                "虚拟齿轮预落地: 增加 %d 次普通跳前地轨触点", len(preland_times)
            )
        # Rush-Hour map4's 68 s air run overlaps a ground 18 gear. Repeated
        # full-rate taps score Great; removing the whole run misses a note.
        # These per-note edits were verified with virtual input: the two
        # redundant taps are omitted and the remaining taps keep their own
        # fingers at the measured judgement times.
        if stage_map == "rush_hour_map4":
            dense_tap_edits = {
                67.969: None,
                68.036: 0.020,
                68.103: 0.020,
                68.170: None,
                68.237: -0.060,
                68.304: -0.060,
                68.371: -0.060,
                68.438: -0.030,
            }
            matched: dict[float, int] = {}
            for event_id, event in enumerate(self.chart_ir.events, 1):
                if (event.kind != EventKind.TAP or event.ibms_id != "14"
                        or event.lane != Lane.AIR):
                    continue
                for target in dense_tap_edits:
                    if abs(event.t_sec - target) <= 0.002:
                        matched[target] = event_id
            if len(matched) == len(dense_tap_edits) and len(set(matched.values())) == len(matched):
                edits_by_id = {matched[t]: shift for t, shift in dense_tap_edits.items()}
                adjusted = []
                for song_t, seq, line in lines:
                    parts = line.split()
                    event_id = int(parts[4])
                    if event_id in edits_by_id:
                        shift = edits_by_id[event_id]
                        if shift is None:
                            continue
                        song_t += shift
                        parts[0] = f"{song_t:.4f}"
                        line = " ".join(parts) + "\n"
                    adjusted.append((song_t, seq, line))
                lines = adjusted
            # In the verified 105 s phrase the ground 23 is collected by the
            # adjacent input, while the second 0E at 105.134 repeats a hit
            # already supplied by the continuing first chord. Keep this
            # exception tied to the exact chart objects and event order;
            # other 0E pairs (including 106.071/106.205) need both hits.
            collect_ids = [event_id for event_id, event in enumerate(self.chart_ir.events, 1)
                           if event.kind == EventKind.TAP and event.ibms_id == "23"
                           and event.lane == Lane.GROUND
                           and abs(event.t_sec - 104.933) <= 0.002]
            second_chord_ids = [event_id for event_id, event in enumerate(self.chart_ir.events, 1)
                                if event.kind == EventKind.TAP and event.ibms_id == "0E"
                                and event.lane == Lane.BOTH
                                and abs(event.t_sec - 105.134) <= 0.002]
            if len(collect_ids) == 1 and len(second_chord_ids) == 2:
                omitted = {collect_ids[0], second_chord_ids[1]}
                lines = [row for row in lines if int(row[2].split()[4]) not in omitted]
        lines.sort(key=lambda row: (row[0], row[1]))
        # Check the final table after rephasing and synthetic landing pulses,
        # not only the original heap. The in-game loader enforces these same
        # pairing/order constraints and supports at most 32 live fingers.
        opened: set[tuple[str, str]] = set()
        previous_time = -1.0
        for _, _, line in lines:
            time_text, edge, lane, _, event_id = line.split()
            song_time = float(time_text)
            if song_time < previous_time:
                raise ValueError("virtual actions are out of order")
            previous_time = song_time
            key = (lane, event_id)
            if edge == "down":
                if key in opened:
                    raise ValueError(f"duplicate final virtual down: {key}")
                opened.add(key)
                if len(opened) > 32:
                    raise ValueError("more than 32 simultaneous virtual fingers")
            elif not (edge == "up" and key in opened):
                raise ValueError(f"unpaired final virtual up: {key}")
            else:
                opened.remove(key)
        if opened:
            raise ValueError(f"unreleased final virtual fingers: {sorted(opened)[:8]}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# mdr_virtual_format=1\n")
            f.write(f"# chart_uid={chart_uid}\n")
            f.write(f"# map_index={export_map}\n")
            if stage_map:
                f.write(f"# stage_map={stage_map}\n")
            f.write(f"# action_count={len(lines)}\n")
            f.write("# song_time down|up air|ground kind event_id\n")
            f.writelines(row[2] for row in lines)
        return len(lines)

    def _virtual_pre_double_air_tap_ids(self, shifts: dict[int, float]) -> set[int]:
        """Release the second of two quick air taps before a two-hit 0E chord.

        Both air taps are judged at their existing down times. Only the later
        finger's up edge is shortened, leaving the first finger and the chord
        inputs intact. Use scheduled times so other timing shifts cannot turn
        an unrelated chart pattern into this overlap.
        """
        air_taps: list[tuple[float, int]] = []
        air_notes: list[tuple[float, int]] = []
        double_hits: dict[float, list[int]] = {}
        for event_id, event in enumerate(self.chart_ir.events, 1):
            if event.kind != EventKind.TAP or event.skip:
                continue
            scheduled = event.t_sec + shifts.get(event_id, 0.0)
            if event.lane in (Lane.AIR, Lane.MID, Lane.BOTH):
                air_notes.append((scheduled, event_id))
            if (event.ibms_id == "14" and event.lane == Lane.AIR
                    and event.note_type == NoteType.TAP):
                air_taps.append((scheduled, event_id))
            elif event.ibms_id == "0E" and event.lane == Lane.BOTH:
                double_hits.setdefault(round(scheduled, 3), []).append(event_id)
        air_taps.sort()
        shortened: set[int] = set()
        for chord_time, chord_ids in double_hits.items():
            if len(chord_ids) != 2:
                continue
            preceding = [(t, event_id) for t, event_id in air_taps
                         if 0 < chord_time - t <= 0.280 + 1e-6]
            if len(preceding) < 2:
                continue
            (first, first_id), (second, second_id) = preceding[-2:]
            if (0.050 - 1e-6 <= second - first <= 0.150 + 1e-6
                    and 0.050 - 1e-6 <= chord_time - second <= 0.150 + 1e-6
                    and not any(first < t < chord_time and event_id not in (first_id, second_id)
                                for t, event_id in air_notes)):
                shortened.add(second_id)
        return shortened

    def _virtual_cotimed_18_rapid_tap_ids(self) -> set[int]:
        """Delay only the leading taps of an air run beside a ground 18 gear.

        This covers the observed first tap in Cryogenic's 71.586 s pair and
        the first two taps in INFiNiTE ENERZY's 95/100/105 s triplets, and
        Rush-Hour's 67 ms run. A lone co-timed tap is left at its chart position.
        """
        ground_gears = [g.t_sec for g in self.chart_ir.raw_gears
                        if g.ibms_id == "18" and g.lane == Lane.GROUND]
        air_dodges = [e.t_sec for e in self.chart_ir.events
                      if e.ibms_id == "18" and e.note_type == NoteType.DODGE
                      and e.lane == Lane.AIR]
        if not ground_gears or not air_dodges:
            return set()
        air_taps = [(event_id, event) for event_id, event
                    in enumerate(self.chart_ir.events, 1)
                    if event.kind == EventKind.TAP and event.note_type == NoteType.TAP
                    and event.lane == Lane.AIR and not event.skip]
        shifted: set[int] = set()
        for first_index, (_, first) in enumerate(air_taps[:-1]):
            # 13/14 and the 15 run in Re:End share this measured timing.
            # Other note types can have different judgement behavior.
            if first.ibms_id not in {"13", "14", "15"} or not (
                    any(abs(t - first.t_sec) <= 0.010 for t in ground_gears)
                    and any(abs(t - first.t_sec) <= 0.010 for t in air_dodges)):
                continue
            for i in range(first_index, len(air_taps) - 1):
                event_id, current = air_taps[i]
                _, following = air_taps[i + 1]
                gap = following.t_sec - current.t_sec
                # A charted 65/67 ms gap can lose precision in subtraction.
                if (current.ibms_id != following.ibms_id
                        or not 0.065 - 1e-6 <= gap <= 0.120 + 1e-6):
                    break
                shifted.add(event_id)
        return shifted

    def _virtual_dense_double_gear_ids(self) -> tuple[set[int], set[int]]:
        """Use the air touch for a dense 0E/ground-0H run.

        The simultaneous ground touch ends the jump before the gear clears.
        The air touch alone scored Perfect throughout Rush-Hour's 53-57 s run.
        Keep the final air touch retryable when the following ground note lands.
        """
        ground_gears = [g.t_sec for g in self.chart_ir.raw_gears
                        if g.ibms_id == "0H" and g.lane == Lane.GROUND]
        matched = [(event_id, event) for event_id, event in enumerate(self.chart_ir.events, 1)
                   if event.kind == EventKind.TAP and event.ibms_id == "0E"
                   and event.lane == Lane.BOTH
                   and any(abs(event.t_sec - t) <= 0.003 for t in ground_gears)]
        if not matched:
            return set(), set()
        streams: list[list[tuple[int, ChartEvent]]] = [[matched[0]]]
        for item in matched[1:]:
            if item[1].t_sec - streams[-1][-1][1].t_sec <= 0.300:
                streams[-1].append(item)
            else:
                streams.append([item])
        ids: set[int] = set()
        last_ids: set[int] = set()
        for stream in streams:
            if len(stream) < 6:
                continue
            first, last = stream[0][1].t_sec, stream[-1][1].t_sec
            if any(first - 1e-6 <= event.t_sec <= last + 1e-6
                   and event.kind in (EventKind.TAP, EventKind.HOLD_ON, EventKind.MASH_ON)
                   and event.ibms_id != "0E"
                   for event in self.chart_ir.events):
                continue
            if any(first <= gear.t_sec <= last and gear.lane != Lane.GROUND
                   for gear in self.chart_ir.raw_gears):
                continue
            ids.update(event_id for event_id, _ in stream)
            last_ids.add(stream[-1][0])
        return ids, last_ids

    def _virtual_split_isolated_gear_pairs(
        self, lines: list[tuple[float, int, str]]
    ) -> list[tuple[float, int, str]]:
        """Rehop across an isolated 0H pair too far apart for one normal jump."""
        gears = sorted(g.t_sec for g in self.chart_ir.raw_gears
                       if g.ibms_id == "0H" and g.lane == Lane.GROUND)
        if len(gears) < 2:
            return lines
        next_id = max((int(line.split()[4]) for _, _, line in lines), default=0) + 1
        next_seq = max((seq for _, seq, _ in lines), default=0) + 1
        removed: set[int] = set()
        added: list[tuple[float, int, str]] = []
        for first, second in zip(gears, gears[1:]):
            if not 0.380 <= second - first <= 0.420:
                continue
            if any(first - 0.500 < g.t_sec < second + 0.500
                   and g.t_sec not in (first, second)
                   for g in self.chart_ir.raw_gears):
                continue
            if any(first - 0.100 <= event.t_sec <= second + 0.200
                   and event.kind in (EventKind.TAP, EventKind.HOLD_ON, EventKind.MASH_ON)
                   and event.note_type != NoteType.DODGE
                   for event in self.chart_ir.events):
                continue
            sources = [event_id for event_id, event in enumerate(self.chart_ir.events, 1)
                       if event.kind == EventKind.TAP and event.note_type == NoteType.DODGE
                       and event.ibms_id == "0H" and event.lane == Lane.AIR
                       and abs(event.t_sec - first) <= 0.010]
            if len(sources) != 1:
                continue
            source_id = sources[0]
            source_lines = [line for _, _, line in lines
                            if line.split()[4] == str(source_id)]
            if len(source_lines) != 2 or not all(" air dodge " in line for line in source_lines):
                continue
            first_down = first + 0.045
            land_down = first_down + 0.210
            second_down = second + 0.004
            if second_down - (land_down + 0.040) < 0.100:
                continue
            removed.add(source_id)
            edges = [
                (first_down, "down", "air", source_id),
                (land_down, "down", "ground", next_id),
                (land_down + 0.040, "up", "ground", next_id),
                (first_down + 0.280, "up", "air", source_id),
                (second_down, "down", "air", next_id + 1),
                (second_down + 0.210, "up", "air", next_id + 1),
            ]
            next_id += 2
            for t, edge, lane, event_id in edges:
                added.append((t, next_seq,
                              f"{t:.4f} {edge} {lane} dodge {event_id}\n"))
                next_seq += 1
        if not removed:
            return lines
        return [row for row in lines if int(row[2].split()[4]) not in removed] + added

    def _virtual_rephase_gear_streams(
        self, current_lines: list[tuple[float, int, str]]
    ) -> tuple[list[tuple[str, float]], list[float]] | None:
        """Replace dense isolated 0H runs with playable normal-input jumps.

        Raw 0H positions are split at upper gears and long gaps. Nearby gears
        form one burst; each burst needs the character airborne from contact
        through clear. Forward and backward interval passes find a jump phase
        with at least 505 ms between downs. Infeasible or note-conflicted runs
        keep the existing actions.
        """
        ground = sorted(g.t_sec for g in self.chart_ir.raw_gears
                        if g.ibms_id == "0H" and g.lane == Lane.GROUND)
        upper = sorted(g.t_sec for g in self.chart_ir.raw_gears
                       if g.lane in (Lane.AIR, Lane.MID, Lane.BOTH))
        if not ground:
            return None
        existing_downs = sorted(
            t for t, _, line in current_lines
            if line.split()[1:4] == ["down", "air", "dodge"]
        )
        dodge_ups = sorted(
            t for t, _, line in current_lines
            if line.split()[1:4] == ["up", "air", "dodge"]
        )
        streams: list[list[float]] = [[ground[0]]]
        dense_start = ground[0]
        for t in ground[1:]:
            previous = streams[-1][-1]
            gap = t - previous
            long_dense_run_ended = gap > 0.200 and previous - dense_start > 0.300
            if gap > 0.650 or any(previous < u < t for u in upper) or long_dense_run_ended:
                streams.append([t])
                dense_start = t
            else:
                streams[-1].append(t)
                if gap > 0.200:
                    dense_start = t
        removed: list[tuple[str, float]] = []
        new_downs: list[float] = []
        rebuilt = 0
        for stream in streams:
            if len(stream) < 6:
                continue
            first, last = stream[0], stream[-1]
            # A dense run can already be safe with its original jump phase.
            # Rebuild only when a gear reaches the player after the measured
            # normal-input air window. This preserves the 41/50/106 s runs.
            late_risk = any(not any(
                VIRTUAL_RISE_SEC <= t + 0.130 - down <= 0.430
                for down in existing_downs
            ) for t in stream)
            tight_reset = any(
                first <= up < next_down <= last + 0.100
                and 0 <= next_down - up < 0.040
                for first_down, next_down in zip(existing_downs, existing_downs[1:])
                for up in dodge_ups
                if first_down - 1e-6 <= up <= next_down + 1e-6
            )
            if not (late_risk or tight_reset):
                continue
            nearby = [e for e in self.chart_ir.events
                      if first - 0.300 <= e.t_sec <= last + 0.200
                      and e.note_type != NoteType.DODGE]
            collects = [e for e in nearby if e.kind == EventKind.TAP
                        and e.lane == Lane.GROUND
                        and (e.ibms_id in {"23", "20", "22"}
                             or e.note_type in (NoteType.COLLECTIBLE, NoteType.NOTE_STRIP))]
            if len(collects) > 1 or len(collects) != len(nearby):
                continue
            if any(t + VIRTUAL_GEAR_CONTACT_SEC - 0.020 <= c.t_sec
                   <= t + VIRTUAL_GEAR_CLEAR_SEC + 0.020
                   for c in collects for t in stream):
                continue
            bursts: list[list[float]] = [[stream[0]]]
            for t in stream[1:]:
                if t - bursts[-1][-1] > 0.200:
                    bursts.append([t])
                else:
                    bursts[-1].append(t)
            if len(bursts) < 2:
                continue
            low = [b[-1] + VIRTUAL_GEAR_CLEAR_SEC + 0.005 - 0.460 for b in bursts]
            high = [b[0] + VIRTUAL_GEAR_CONTACT_SEC - VIRTUAL_RISE_SEC - 0.005
                    for b in bursts]
            earliest: list[float] = []
            for lo in low:
                earliest.append(max(lo, earliest[-1] + VIRTUAL_REPHASE_GAP_SEC)
                                if earliest else max(0.0, lo))
            latest = [0.0] * len(bursts)
            latest[-1] = high[-1]
            for j in range(len(bursts) - 2, -1, -1):
                latest[j] = min(high[j], latest[j + 1] - VIRTUAL_REPHASE_GAP_SEC)
            if any(earliest[j] > latest[j] + 1e-6 for j in range(len(bursts))):
                continue
            presses = [(earliest[j] + latest[j]) / 2 for j in range(len(bursts))]
            if any(not any(
                down + VIRTUAL_RISE_SEC + 0.005 <= t + VIRTUAL_GEAR_CONTACT_SEC
                and t + VIRTUAL_GEAR_CLEAR_SEC <= down + 0.460 + 1e-6
                for down in presses
            ) for t in stream):
                continue
            source_ids = [str(event_id) for event_id, e in enumerate(self.chart_ir.events, 1)
                          if e.kind == EventKind.TAP and e.note_type == NoteType.DODGE
                          and e.lane == Lane.AIR and e.ibms_id == "0H"
                          and first - 0.010 <= e.t_sec <= last + 0.010]
            if not source_ids:
                continue
            removed.extend((event_id, first) for event_id in source_ids)
            new_downs.extend(presses)
            rebuilt += 1
        if rebuilt:
            logging.getLogger(__name__).info(
                "虚拟齿轮连跳重排: %d 段，移除 %d 组旧跳跃，加入 %d 组可行跳跃",
                rebuilt, len(removed), len(new_downs),
            )
            return removed, new_downs
        return None

    def _virtual_preland_pulses(
        self, current_lines: list[tuple[float, int, str]], new_downs: list[float]
    ) -> list[float]:
        """End an earlier normal-input tap's flight before a rephased jump.

        A ground pulse 140 ms before the new jump was measured to end the
        8.475 s air tap's flight at 8.698 s, allowing the 8.820 s jump to
        dodge the 9.1 s gear. Keep this narrow: only use it for an otherwise
        isolated air tap whose flight would overlap the planned jump.
        """
        air_taps = sorted(
            t for t, _, line in current_lines
            if line.split()[1:4] == ["down", "air", "tap"]
        )
        ground_downs = sorted(
            t for t, _, line in current_lines
            if line.split()[1] == "down" and line.split()[2] == "ground"
        )
        ground_open: dict[str, float] = {}
        ground_held: list[tuple[float, float]] = []
        for t, _, line in sorted(current_lines, key=lambda row: (row[0], row[1])):
            parts = line.split()
            if parts[2] != "ground":
                continue
            if parts[1] == "down":
                ground_open[parts[4]] = t
            elif parts[4] in ground_open:
                ground_held.append((ground_open.pop(parts[4]), t))
        pulses: list[float] = []
        for jump in new_downs:
            preceding = [t for t in air_taps if t + 0.130 < jump < t + 0.460]
            if not preceding:
                continue
            tap = preceding[-1]
            pulse = jump - 0.140
            if pulse <= tap + 0.130:
                continue
            if any(tap < t < jump - 0.080 for t in ground_downs):
                continue
            if any(abs(t - pulse) < 0.100 for t in ground_downs):
                continue
            if any(start <= pulse < end for start, end in ground_held):
                continue
            if any(abs(g.t_sec - pulse) < 0.100 for g in self.chart_ir.raw_gears):
                continue
            pulses.append(pulse)
        return pulses

    def _virtual_overlap_tap_ends(self) -> dict[tuple[int, str], float]:
        """Keep a 0B head finger through a short same-lane 0F hold's tail.

        In the observed 3.1 s overlap, releasing the 0B finger at 3.38 s
        cleared isAirPressing even though the hold finger remained down. The
        following long-press tick was judged Great at 3.391 s.
        """
        result: dict[tuple[int, str], float] = {}
        holds = [e for e in self.chart_ir.events
                 if e.kind == EventKind.HOLD_ON and e.t_end_sec is not None]
        for event_id, tap in enumerate(self.chart_ir.events, 1):
            if tap.kind != EventKind.TAP or tap.ibms_id != "0B":
                continue
            for hold in holds:
                duration = hold.t_end_sec - hold.t_sec
                if not (0 < duration <= 0.700) or abs(hold.t_sec - tap.t_sec) > 0.005:
                    continue
                for lane in self.injector._lanes(tap.lane):
                    if lane in self.injector._lanes(hold.lane):
                        result[(event_id, "air" if lane == Lane.AIR else "ground")] = hold.t_end_sec + 0.020
        return result

    def _virtual_ground_enemy_gear_pairs(self) -> set[int]:
        """Release co-timed ground 21/18 touches before the next pair arrives."""
        events = self.chart_ir.events
        pairs: set[int] = set()
        for enemy_id, enemy in enumerate(events, 1):
            if not (enemy.kind == EventKind.TAP and enemy.ibms_id == "21"
                    and enemy.lane == Lane.GROUND):
                continue
            for gear_id, gear in enumerate(events, 1):
                if (gear.kind == EventKind.TAP and gear.note_type == NoteType.DODGE
                        and gear.ibms_id == "18" and gear.lane == Lane.GROUND
                        and abs(gear.t_sec - enemy.t_sec) <= 0.005):
                    pairs.update((enemy_id, gear_id))
        return pairs

    def _virtual_timing_shifts(self, actions: List[TimedAction], delay: float) -> dict[int, float]:
        """Keep normal-input jumps airborne at late 0H contact and collect after it.

        Chart-only dodge planning assumes the AutoPlay jump arc. Use the raw gear
        positions to find a later gear assigned to the same hop, then move that
        hop only as far as the measured InputManager arc requires. A ground
        collect pulse inside the gear's contact window is moved just beyond it.
        """
        gears = sorted(
            (g.t_sec for g in self.chart_ir.raw_gears
             if g.ibms_id == "0H" and g.lane == Lane.GROUND),
        )
        ground_18_gears = sorted(
            g.t_sec for g in self.chart_ir.raw_gears
            if g.ibms_id == "18" and g.lane == Lane.GROUND
        )
        if not gears and not ground_18_gears:
            return {}
        hops: List[tuple[int, float, float]] = []
        for a in actions:
            if not a.down or a.action_kind != "dodge" or a.lane != Lane.AIR:
                continue
            source = self.chart_ir.events[a.event_id - 1]
            if source.ibms_id == "0H":
                hops.append((a.event_id, source.t_sec, a.t_fire + delay))
        shifts: dict[int, float] = {}
        unresolved = 0
        narrow_pairs = 0
        desired_downs = [down for _, _, down in hops]
        latest_downs = [float("inf")] * len(hops)
        for i, (event_id, source_time, down_time) in enumerate(hops):
            next_source = hops[i + 1][1] if i + 1 < len(hops) else float("inf")
            covered = [t for t in gears
                       if source_time - 0.010 <= t < min(next_source - 0.010, source_time + 0.500)]
            if not covered:
                continue
            if (len(covered) == 2 and 0.350 <= covered[1] - covered[0] <= 0.420
                    and not any(source_time < e.t_sec <= covered[1] + VIRTUAL_PAIR_CONTACT_SEC
                                and e.note_type != NoteType.DODGE
                                and e.kind in (EventKind.TAP, EventKind.HOLD_ON)
                                for e in self.chart_ir.events)):
                earliest = (covered[1] + VIRTUAL_PAIR_CONTACT_SEC
                            + VIRTUAL_PAIR_MARGIN_SEC - VIRTUAL_PAIR_AIR_SEC)
                latest = (covered[0] + VIRTUAL_PAIR_CONTACT_SEC
                          - VIRTUAL_PAIR_RISE_SEC - VIRTUAL_PAIR_MARGIN_SEC)
                if earliest <= latest and down_time <= latest:
                    desired_downs[i] = max(down_time, (earliest + latest) / 2)
                    latest_downs[i] = latest
                    narrow_pairs += 1
                    continue
            last_contact = covered[-1] + VIRTUAL_GEAR_CONTACT_SEC
            required_down = last_contact + VIRTUAL_GEAR_MARGIN_SEC - VIRTUAL_AIR_END_SEC
            # When a complete collision interval fits in one jump, cover its
            # tail too. This catches the 52.35 s gear that hit on landing.
            clear_down = (covered[-1] + VIRTUAL_GEAR_CLEAR_SEC
                          + VIRTUAL_GEAR_MARGIN_SEC - 0.460)
            required_down = max(required_down, down_time)
            latest_down = (covered[0] + VIRTUAL_GEAR_CONTACT_SEC
                           - VIRTUAL_RISE_SEC - VIRTUAL_GEAR_MARGIN_SEC)
            latest_downs[i] = max(latest_down, down_time)
            if clear_down <= latest_down:
                required_down = max(required_down, clear_down)
            if required_down > latest_down:
                unresolved += 1
                required_down = max(down_time, latest_down)
            desired_downs[i] = required_down
        # If a delayed jump leaves too little reset time, move the following
        # jump as well when its first gear still allows that. Never pull a jump
        # earlier through an active jump-action flag.
        for i in range(len(hops) - 1):
            earliest_next = desired_downs[i] + VIRTUAL_MIN_REHOP_SEC
            if desired_downs[i + 1] < earliest_next:
                if earliest_next <= latest_downs[i + 1]:
                    desired_downs[i + 1] = earliest_next
                else:
                    unresolved += 1
                    allowed_current = desired_downs[i + 1] - VIRTUAL_MIN_REHOP_SEC
                    desired_downs[i] = max(hops[i][2], min(desired_downs[i], allowed_current))
        for (event_id, _, original_down), desired in zip(hops, desired_downs):
            if desired > original_down:
                shifts[event_id] = desired - original_down
        collect_count = 0
        ground_collects: List[tuple[float, float]] = []
        for a in actions:
            if not a.down or a.action_kind != "collect" or a.lane != Lane.GROUND:
                continue
            down_time = a.t_fire + delay
            safe_downs = [t + VIRTUAL_COLLECT_AFTER_GEAR_SEC for t in gears
                          if t + VIRTUAL_GEAR_CONTACT_SEC - 0.010 <= down_time
                          <= t + VIRTUAL_COLLECT_AFTER_GEAR_SEC]
            safe_downs.extend(
                t + VIRTUAL_COLLECT_AFTER_GROUND_18_SEC for t in ground_18_gears
                if t + VIRTUAL_GEAR_CONTACT_SEC - 0.010 <= down_time
                <= t + VIRTUAL_COLLECT_AFTER_GROUND_18_SEC
            )
            if not safe_downs:
                ground_collects.append((down_time, down_time + 0.034))
                continue
            safe_down = max(safe_downs)
            shift = safe_down - down_time
            if 0 < shift <= VIRTUAL_MAX_COLLECT_SHIFT_SEC:
                shifts[a.event_id] = shift
                collect_count += 1
                ground_collects.append((safe_down, safe_down + 0.034))
            else:
                ground_collects.append((down_time, down_time + 0.034))
        collect_conflicts = 0
        for event_id, source_time, original_down in hops:
            down = original_down + shifts.get(event_id, 0.0)
            collision = [end for start, end in ground_collects if start <= down < end + 0.020]
            if not collision:
                continue
            new_down = max(collision) + 0.020
            first_gear = next((t for t in gears if source_time - 0.010 <= t <= source_time + 0.500), None)
            latest_down = (first_gear + VIRTUAL_GEAR_CONTACT_SEC - VIRTUAL_RISE_SEC
                           - VIRTUAL_GEAR_MARGIN_SEC) if first_gear is not None else float("inf")
            if new_down <= latest_down:
                shifts[event_id] = new_down - original_down
                collect_conflicts += 1
        logging.getLogger(__name__).info(
            "虚拟躲避时序: 调整 %d 次跳跃（窄双齿轮 %d 组）、%d 次地轨收集、%d 次收集后起跳；%d 组齿轮超出单跳窗口",
            len(shifts) - collect_count, narrow_pairs,
            collect_count, collect_conflicts, unresolved,
        )
        return shifts

    def _airborne_at(self, t: float) -> bool:
        """t 是否落在一次空中 TAP / 躲锯跳的滞空里。"""
        for ev in self.chart_ir.events:
            if ev.kind not in (EventKind.TAP, EventKind.HOLD_ON):
                continue
            if ev.lane not in (Lane.AIR, Lane.MID, Lane.BOTH):
                continue
            fly = JUMP_SEC if ev.note_type == NoteType.DODGE else 0.22
            if ev.t_sec - 1e-6 <= t < ev.t_sec + fly - 1e-6:
                return True
        return False

    def _air_dodge_near(self, t: float, window: float) -> bool:
        for ev in self.chart_ir.events:
            if ev.kind != EventKind.TAP or ev.note_type != NoteType.DODGE:
                continue
            if ev.lane not in (Lane.AIR, Lane.MID, Lane.BOTH):
                continue
            if abs(ev.t_sec - t) <= window:
                return True
        return False

    def _need_settle(self, event) -> bool:
        """上一击还在另一条轨的跳跃里。先换到星星这条轨，再在判定点打一次。"""
        if getattr(event, "use_alt", False) or event.lane not in (Lane.AIR, Lane.GROUND):
            return False
        other = Lane.GROUND if event.lane == Lane.AIR else Lane.AIR
        t = event.t_sec
        for held in self.chart_ir.events:
            if held.kind != EventKind.HOLD_ON or held.t_end_sec is None:
                continue
            if held.lane == other and held.t_sec <= t <= held.t_end_sec:
                return False
        last_other = None
        last_same = None
        for prev in self.chart_ir.events:
            if prev.t_sec >= t - 1e-4:
                break
            if t - prev.t_sec > 0.40:
                continue
            if prev.kind not in (EventKind.TAP, EventKind.HOLD_ON):
                continue
            if prev.lane == event.lane:
                last_same = prev.t_sec
            elif prev.lane == other:
                last_other = prev.t_sec
        if last_other is None:
            return False
        return last_same is None or last_same < last_other

    @property
    def t_chart_end_sec(self) -> float:
        return max(self.chart_ir.t_last_action_sec, self.chart_ir.t_audio_sec) + self.config.grace_tail_sec

    def read_T_chart(self) -> float:
        if self._t_start is None:
            return 0.0
        if self._is_paused:
            return self._pause_time - self._t_start
        return time.perf_counter() - self._t_start

    def start(self, t0: Optional[float] = None, chart_time: float = 0.0) -> None:
        self._begin_period()
        now = t0 if t0 is not None else time.perf_counter()
        chart_time = max(0.0, float(chart_time))
        self._t_start = now - chart_time
        self._is_paused = False
        self._running = True
        self._drop_missed(chart_time)
        self._wake.clear()
        self._thread = threading.Thread(target=self._run_loop, name="chart-scheduler", daemon=True)
        self._thread.start()

    def sync_to(self, t_chart: float, drop_missed: bool = False) -> None:
        """把本地谱面钟对准游戏 timeFromMusicStart。"""
        t_chart = float(t_chart)
        self._t_start = time.perf_counter() - t_chart
        if drop_missed:
            self._drop_missed(t_chart)
        self._wake.set()

    def _drop_missed(self, t_chart: float, skew: float = 0.03) -> None:
        cutoff = t_chart - skew
        while self._heap and self._heap[0].t_fire < cutoff:
            heapq.heappop(self._heap)

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self.injector.release_all()
        self._end_period()

    def pause(self) -> None:
        if not self._is_paused:
            self._pause_time = time.perf_counter()
            self._is_paused = True
            self._wake.set()

    def resume(self) -> None:
        if self._is_paused:
            self._t_start = (self._t_start or 0.0) + (time.perf_counter() - self._pause_time)
            self._is_paused = False
            self._wake.clear()
            self._wake.set()

    def _run_loop(self) -> None:
        try:
            while self._running:
                if self._is_paused:
                    self._wake.wait(timeout=0.05)
                    self._wake.clear()
                    continue

                self._flush_late_up()
                if not self._heap:
                    if self._late_up:
                        self._wake.wait(timeout=0.01)
                        self._wake.clear()
                        continue
                    if self.on_complete:
                        self.on_complete()
                    break

                next_t = self._heap[0].t_fire
                now = self.read_T_chart()
                if next_t > now:
                    wait = next_t - now
                    if self._late_up:
                        wall_wait = self._late_up[0][0] - time.perf_counter()
                        self._wake.wait(timeout=max(0.001, min(wait, max(0.0, wall_wait))))
                        self._wake.clear()
                        continue
                    self._wait_until(next_t)
                    continue

                due: List[TimedAction] = []
                T = self.read_T_chart()
                while self._heap and self._heap[0].t_fire <= T:
                    due.append(heapq.heappop(self._heap))
                self._dispatch_batch(due)
        finally:
            self._running = False

    def _wait_until(self, t_chart: float) -> None:
        """等到谱面时钟 t_chart。长间隔用 sleep，最后约 1ms 自旋。"""
        while self._running and not self._is_paused:
            remaining = t_chart - self.read_T_chart()
            if remaining <= 0:
                return
            if remaining > 0.002:
                self._wake.wait(timeout=remaining - 0.001)
                self._wake.clear()
            else:
                deadline = time.perf_counter() + remaining
                while self._running and not self._is_paused and time.perf_counter() < deadline:
                    pass
                return

    def _flush_late_up(self) -> None:
        now = time.perf_counter()
        ready = [item for item in self._late_up if item[0] <= now]
        self._late_up = [item for item in self._late_up if item[0] > now]
        for _, action in ready:
            self.injector.hit_up(action.lane, alt=action.alt)

    def _dispatch_batch(self, actions: List[TimedAction]) -> None:
        saw_down = set()
        for action in actions:
            ident = (action.lane, bool(action.alt))
            if action.collect:
                if action.down:
                    self._collect_down_wall[ident] = time.perf_counter()
                    self.injector.hit_down(action.lane, alt=action.alt)
                else:
                    down_at = self._collect_down_wall.get(ident, 0.0)
                    if time.perf_counter() < down_at + COLLECT_HOLD_SEC:
                        self._late_up.append((down_at + COLLECT_HOLD_SEC, action))
                    else:
                        self.injector.hit_up(action.lane, alt=action.alt)
                continue
            if action.star and not action.down:
                down_at = self._star_down_wall.get(ident, 0.0)
                if ident in saw_down or time.perf_counter() < down_at + 0.07:
                    self._late_up.append((down_at + 0.08, action))
                    continue
                self.injector.hit_up(action.lane, alt=action.alt)
                continue
            if action.star and action.down:
                self._star_down_wall[ident] = time.perf_counter()
                saw_down.add(ident)
                self.injector.hit_down(action.lane, alt=action.alt)
                continue
            if action.down:
                self.injector.down(action.lane, alt=action.alt)
            else:
                self.injector.up(action.lane, alt=action.alt)

    def past_last_action(self, T: float) -> bool:
        return T >= self.chart_ir.t_last_action_sec - self._total_delay

    def has_pending_events(self) -> bool:
        return bool(self._heap)

    def reset(self) -> None:
        self.stop()
        self._build_heap()
        self._t_start = None
        self._is_paused = False

    def _begin_period(self) -> None:
        if sys.platform != "win32" or self._timer_period_set:
            return
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_period_set = True
        except Exception:
            self._timer_period_set = False

    def _end_period(self) -> None:
        if not self._timer_period_set:
            return
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        self._timer_period_set = False
