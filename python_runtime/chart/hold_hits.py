"""音符条 23 / 红心 22 / P 点 20 都要一次新的按下。同轨长按改走另一个键，主键不松。空地走主键。"""
from __future__ import annotations

import logging
from typing import List, Tuple

from .parser import ChartEvent, EventKind, Lane, NoteType

logger = logging.getLogger(__name__)

HEAD_SEC = 0.008
COLLECT_IBMS = {"23", "20", "22"}
COLLECT_TYPES = {NoteType.NOTE_STRIP, NoteType.COLLECTIBLE}


def _phys_lane(lane: Lane) -> Tuple[Lane, ...]:
    if lane == Lane.BOTH:
        return (Lane.AIR, Lane.GROUND)
    if lane == Lane.MID:
        return (Lane.AIR,)
    if lane in (Lane.AIR, Lane.GROUND):
        return (lane,)
    return ()


def _inside_hold(ev: ChartEvent, occupies: List[Tuple[float, float, Lane]]) -> bool:
    lanes = _phys_lane(ev.lane)
    t = ev.t_sec
    for t0, t1, lane in occupies:
        # 起点同时刻的短音跟长按共用那一次 down，不 repress；只有严格在条内部的才 repress
        if lane in lanes and t0 < t <= t1 + 1e-4:
            return True
    return False


def _at_hold_start(ev: ChartEvent, occupies: List[Tuple[float, float, Lane]]) -> bool:
    lanes = _phys_lane(ev.lane)
    t = ev.t_sec
    for t0, t1, lane in occupies:
        if lane in lanes and abs(t - t0) <= 1e-4:
            return True
    return False


def _near_hold_end(ev: ChartEvent, occupies: List[Tuple[float, float, Lane]], win: float = 0.012) -> bool:
    lanes = _phys_lane(ev.lane)
    t = ev.t_sec
    for t0, t1, lane in occupies:
        if lane in lanes and abs(t - t1) <= win:
            return True
    return False


def _is_collect(ev: ChartEvent) -> bool:
    return ev.note_type in COLLECT_TYPES or ev.ibms_id in COLLECT_IBMS


def plan_hold_hits(events: List[ChartEvent]) -> List[ChartEvent]:
    occupies: List[Tuple[float, float, Lane]] = []
    for e in events:
        if e.t_end_sec is None or _is_collect(e):
            continue
        if e.kind not in (EventKind.HOLD_ON, EventKind.MASH_ON):
            continue
        for lane in _phys_lane(e.lane):
            occupies.append((e.t_sec, e.t_end_sec, lane))

    n_collect = 0
    n_collect_alt = 0
    n_star_alt = 0
    n_star_pri = 0
    n_alt = 0
    n_skip = 0
    for ev in events:
        if ev.kind != EventKind.TAP:
            continue
        if _at_hold_start(ev, occupies) or _near_hold_end(ev, occupies):
            # 起点跟长按共用 down；终点 12ms 内跟松键冲突，不再叠一次。
            ev.skip = True
            n_skip += 1
            continue
        if ev.ibms_id == "0F":
            if _inside_hold(ev, occupies):
                ev.use_alt = True
                n_star_alt += 1
            else:
                ev.use_alt = False
                n_star_pri += 1
            continue
        if _is_collect(ev):
            n_collect += 1
            if _inside_hold(ev, occupies):
                ev.use_alt = True
                n_collect_alt += 1
            else:
                ev.use_alt = False
            continue
        if _inside_hold(ev, occupies):
            ev.use_alt = True
            n_alt += 1
    logger.info(
        "点长星: 条上副键 %d，空地主键 %d；音符条: %d，长条上另一键 %d，空地主键 %d；其它条上短音走副键 %d；起点同时刻跳过 %d",
        n_star_alt,
        n_star_pri,
        n_collect,
        n_collect_alt,
        n_collect - n_collect_alt,
        n_alt,
        n_skip,
    )
    return events
