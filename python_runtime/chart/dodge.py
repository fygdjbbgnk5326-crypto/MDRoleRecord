"""
根据谱面姿态插入躲避键。

官机自动游玩（195154）对 0.5s 地面锯海是「跳、跳、等整点」：
组内间隔约 0.50s，落地只跑约 33ms；0.375s 那颗（9.975）用上一跳的滞空盖住，
不在 10.12 再补第三跳，下一跳对准 10.475。

按「判定瞬间还在地上的锯」起跳，滞空约 0.47s 自然形成这套节奏。
长条里是 press。下一发上空 18 到来前补地面键落地。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence

from .parser import ChartEvent, EventKind, Lane, NoteType

logger = logging.getLogger(__name__)

# 实机 jump 段 0.465–0.468s。
JUMP_SEC = 0.468
STREAM_GAP_SEC = 0.55
SAMPLE_SEC = 0.02
# 官机双跳间隔 0.499s，落地跑约 32ms；9.975 用滞空盖住，不在 10.12 补第三跳。
# 对比官机轨迹：官机起跳约 9.096（9.1 前 4ms）、9.595（9.6 前 5ms）。
# 绕开 OS（mod 帧内直读虚拟按键）没有 OS 注入延迟，起跳=动作表时间，
# 所以提前量应回到 ~5ms，而不是 Python 注入时代补延迟的 16ms。
# 16ms 会让工具比官机早 ~11ms 起跳，子锯时刻(0.39s)角色更低 → 撞子锯。
HOP_LEAD_SEC = 0.005
LAND_GAP_SEC = 0.032
# 落地后 20ms 内的判定是 9.975 残留（10.076），不是下一颗要跳的锯。
POST_LAND_SKIP_SEC = 0.020
COLLECT_REHOP_SEC = 0.10
UPPER_LEAD_SEC = 0.10
UPPER_LINGER_SEC = 0.15
LAND_BEFORE_UPPER_SEC = 0.08
MIN_AIR_SEC = 0.08
# 长条头上的 0H（35.100）比 hold 早 2ms，提前 80ms 算按住。
HOLD_LEAD_SEC = 0.08
SAW_LINGER_SEC = 0.15
COLLECT_LAND_SEC = 0.04


@dataclass
class _Key:
    t: float
    t_end: float
    air: bool
    fly: float = 0.22
    dodge_hop: bool = False

    @property
    def is_hold(self) -> bool:
        return self.t_end > self.t + 1e-4


def _is_air_lane(lane: Lane) -> bool:
    return lane in (Lane.AIR, Lane.MID)


def _playable_keys(events: Sequence[ChartEvent]) -> List[_Key]:
    keys: List[_Key] = []
    for ev in events:
        if ev.kind == EventKind.DODGE:
            continue
        if ev.kind not in (
            EventKind.TAP,
            EventKind.HOLD_ON,
            EventKind.MASH_ON,
        ):
            continue
        t_end = ev.t_sec
        if ev.kind == EventKind.HOLD_ON and ev.t_end_sec is not None:
            t_end = ev.t_end_sec
        elif ev.kind == EventKind.MASH_ON and ev.t_end_sec is not None:
            t_end = ev.t_end_sec
        air = _is_air_lane(ev.lane)
        fly = 0.0
        if air:
            fly = JUMP_SEC if ev.note_type == NoteType.DODGE else 0.22
        if ev.lane == Lane.BOTH:
            keys.append(_Key(ev.t_sec, ev.t_sec, False, 0.0))
            keys.append(_Key(ev.t_sec, ev.t_sec, True, fly or 0.22))
        else:
            keys.append(_Key(ev.t_sec, t_end, air, fly))
    keys.sort(key=lambda k: (k.t, k.air))
    return keys


def _posture_at(keys: Sequence[_Key], t: float) -> bool:
    """True = 空中。按下才算一次跳跃，按住长条不会续航。"""
    last_t = None
    saw_air = False
    saw_ground = False
    for k in keys:
        if k.t > t + 1e-6:
            continue
        if last_t is None or k.t > last_t + 1e-6:
            last_t = k.t
            saw_air = False
            saw_ground = False
        if k.air:
            saw_air = True
        else:
            saw_ground = True
    if last_t is None or not saw_air or saw_ground:
        return False
    fly = 0.0
    dodge_hop = False
    for k in keys:
        if abs(k.t - last_t) <= 1e-6 and k.air:
            fly = max(fly, k.fly)
            dodge_hop = dodge_hop or k.dodge_hop
    # 0B 短空中音不算躲锯跳；18 截断后的 hop fly 可能只有 0.18s，要用 dodge_hop 区分。
    if not dodge_hop:
        return False
    return t <= last_t + fly + 1e-9


def _lane_held(keys: Sequence[_Key], t: float, air: bool) -> bool:
    return any(
        k.is_hold and k.air == air and k.t - 1e-6 <= t <= k.t_end + 1e-6
        for k in keys
    )


def _make_tap(src: ChartEvent, t: float, air: bool, alt: bool) -> ChartEvent:
    return replace(
        src,
        t_sec=t,
        t_end_sec=None,
        lane=Lane.AIR if air else Lane.GROUND,
        kind=EventKind.TAP,
        note_type=NoteType.DODGE,
        use_alt=alt,
    )


def _clusters(gears: Sequence[ChartEvent], max_gap: float) -> List[List[ChartEvent]]:
    if not gears:
        return []
    ordered = sorted(gears, key=lambda e: e.t_sec)
    groups: List[List[ChartEvent]] = [[ordered[0]]]
    for gear in ordered[1:]:
        if gear.t_sec - groups[-1][-1].t_sec <= max_gap + 1e-6:
            groups[-1].append(gear)
        else:
            groups.append([gear])
    return groups


def _jump_hits_upper(air_gears: Sequence[ChartEvent], t: float) -> bool:
    """起跳后 0.15s 上升期内，上空 18 的判定点落在这段里才会爆头。

    不要用 UPPER_LEAD 把 0.17s 后的 18 也算进上升期：官机 42.098 起跳后
    42.267 是跳着 down_hit，不是爆头。已经过去的上空齿轮更不能拦下一跳。
    """
    rise_end = t + 0.15
    for gear in air_gears:
        if t - 1e-6 < gear.t_sec <= rise_end + 1e-6:
            return True
    return False


def _split_on_upper(
    streams: List[List[ChartEvent]], air_gears: Sequence[ChartEvent]
) -> List[List[ChartEvent]]:
    """中间夹着上空 18 的地面锯拆开，避免 1.392 那串连跳进 1.725。"""
    out: List[List[ChartEvent]] = []
    for stream in streams:
        cur = [stream[0]]
        for gear in stream[1:]:
            if _next_upper(air_gears, cur[-1].t_sec, gear.t_sec) is not None:
                out.append(cur)
                cur = [gear]
            else:
                cur.append(gear)
        out.append(cur)
    return out


def _next_ground_collect(collects: Sequence[ChartEvent], t_from: float, t_until: float) -> ChartEvent | None:
    nxt = None
    for ev in collects:
        if t_from + 1e-6 < ev.t_sec <= t_until + 1e-6:
            if nxt is None or ev.t_sec < nxt.t_sec:
                nxt = ev
    return nxt


def _next_upper(air_gears: Sequence[ChartEvent], t_from: float, t_until: float) -> ChartEvent | None:
    nxt = None
    for gear in air_gears:
        if t_from + 1e-6 < gear.t_sec <= t_until + 1e-6:
            if nxt is None or gear.t_sec < nxt.t_sec:
                nxt = gear
    return nxt


def _has_ground_press(keys: Sequence[_Key], t: float, window: float = 0.10) -> bool:
    return any((not k.air) and abs(k.t - t) <= window for k in keys)


def _under_hold(keys: Sequence[_Key], t: float) -> bool:
    """锯落在长条里，或紧挨着长条头，都是 press。"""
    return any(k.is_hold and k.t - HOLD_LEAD_SEC <= t <= k.t_end + 1e-4 for k in keys)


def _is_18(gear: ChartEvent) -> bool:
    return (gear.ibms_id or "") == "18"


def _in_dodge_jump(keys: Sequence[_Key], t: float) -> bool:
    return any(
        k.dodge_hop and k.air and k.t - 1e-6 <= t <= k.t + k.fly + 1e-9
        for k in keys
    )


def _air_note_soon(keys: Sequence[_Key], t: float, ahead: float = 0.16) -> bool:
    """紧跟着上空短音（56.475）时不要起跳，官机是 atk 链，跳了会 Great。"""
    for k in keys:
        if k.is_hold or not k.air or k.dodge_hop:
            continue
        if t - 1e-6 <= k.t <= t + ahead + 1e-6:
            return True
    return False


def _jump_cut_by_18(hit_18s: Sequence[ChartEvent], hop_t: float) -> Optional[float]:
    """跳起后 0.15s 之外的 18 官机是 down_hit，滞空在那里结束，才能接 42.476。"""
    cut = None
    rise_end = hop_t + 0.15
    fly_end = hop_t + JUMP_SEC
    for gear in hit_18s:
        if rise_end < gear.t_sec <= fly_end + 1e-6:
            if cut is None or gear.t_sec < cut:
                cut = gear.t_sec
    return cut


def _try_hop(
    src: ChartEvent,
    keys: List[_Key],
    extra: List[ChartEvent],
    hop_t: float,
    air_gears: Sequence[ChartEvent],
    ground_collects: Sequence[ChartEvent],
    hit_18s: Sequence[ChartEvent],
) -> Optional[float]:
    """在 hop_t 起跳。成功则返回滞空结束时间。连跳不要再减 HOP_LEAD。

    满跳不要补地面键：官机是 jump→run 再 D，落地 J 会变成 down_hit，
    把 33ms 后的连跳 D 吞掉（13.596 / 42.098）。
    """
    t = max(0.0, hop_t)
    if _under_hold(keys, t):
        return None
    if _posture_at(keys, t):
        return None
    # 上升期正撞上空 18：开头 1.392→1.725 那种才放弃。
    # 对着 18 自己起跳不算爆头。42.098 起跳后 0.17s 的 18 是 down_hit。
    if not _is_18(src) and _jump_hits_upper(air_gears, t):
        return None
    collect = _next_ground_collect(ground_collects, t + MIN_AIR_SEC, t + JUMP_SEC)
    fly = JUMP_SEC
    if collect is not None:
        # 官机 15.6/17.3 是跳着 down_hit 接 23，滞空在收集物处结束。
        fly = max(MIN_AIR_SEC, collect.t_sec - COLLECT_LAND_SEC - t)
    # 0H 跳进 18：滞空在 down_hit 处结束，才能接 42.476。
    # 18 自己起跳不要截断，否则 0.25s 串会变成连跳而不是 jump/down_hit。
    if not _is_18(src):
        cut18 = _jump_cut_by_18(hit_18s, t)
        if cut18 is not None:
            fly = min(fly, max(MIN_AIR_SEC, cut18 - t))
    extra.append(_make_tap(src, t, True, _lane_held(keys, t, True)))
    keys.append(_Key(t, t, True, fly, dodge_hop=True))
    return t + fly


def plan_gear_dodges(events: List[ChartEvent]) -> List[ChartEvent]:
    dodges = [e for e in events if e.kind == EventKind.DODGE]
    playable = [e for e in events if e.kind != EventKind.DODGE]
    if not dodges:
        return playable

    keys = _playable_keys(playable)
    extra: List[ChartEvent] = []
    air_gears = [d for d in dodges if _is_air_lane(d.lane)]
    ground_gears = [
        d
        for d in dodges
        if not _is_air_lane(d.lane)
        and not _is_18(d)
        and not _under_hold(keys, d.t_sec)
    ]
    ground_collects = [
        e
        for e in playable
        if e.kind == EventKind.TAP
        and (
            e.ibms_id in {"23", "20", "22"}
            or e.note_type in (NoteType.NOTE_STRIP, NoteType.COLLECTIBLE)
        )
        and not _is_air_lane(e.lane)
    ]

    hops = 0
    hit_18s = [d for d in dodges if (d.ibms_id or "") == "18"]
    streams = _split_on_upper(_clusters(ground_gears, STREAM_GAP_SEC), air_gears)
    for stream in streams:
        last_land = -999.0
        for gear in stream:
            if _under_hold(keys, gear.t_sec):
                continue
            if _posture_at(keys, gear.t_sec) or _in_dodge_jump(keys, gear.t_sec):
                continue
            if last_land <= gear.t_sec <= last_land + POST_LAND_SKIP_SEC:
                continue
            if not _is_18(gear) and _air_note_soon(keys, gear.t_sec):
                continue
            hop_t = max(0.0, gear.t_sec - HOP_LEAD_SEC)
            earliest = last_land + LAND_GAP_SEC
            if hop_t < earliest:
                if earliest > gear.t_sec + 1e-4:
                    continue
                hop_t = earliest
            if _posture_at(keys, hop_t):
                continue
            air_end = _try_hop(
                gear, keys, extra, hop_t, air_gears, ground_collects, hit_18s
            )
            if air_end is not None:
                last_land = air_end
                hops += 1

    n_upper_hit = 0
    for gear in hit_18s:
        t = gear.t_sec
        if _under_hold(keys, t):
            continue
        # 先 0H 起跳。已在跳里的 18：上空打 J，地面锯被滞空盖住。
        # 还在地上的 18（含地面 1.975 / 66.100）才补跳。
        rising = any(
            k.air and k.dodge_hop and k.t - 1e-6 <= t <= k.t + 0.15 + 1e-6
            for k in keys
        )
        if _in_dodge_jump(keys, t):
            if _is_air_lane(gear.lane) and not rising:
                extra.append(_make_tap(gear, t, False, _lane_held(keys, t, False)))
                keys.append(_Key(t, t, False, 0.0))
                n_upper_hit += 1
            continue
        hop_t = max(0.0, t - HOP_LEAD_SEC)
        if _in_dodge_jump(keys, hop_t) or rising:
            if not rising:
                extra.append(_make_tap(gear, t, False, _lane_held(keys, t, False)))
                keys.append(_Key(t, t, False, 0.0))
                n_upper_hit += 1
            continue
        # 18 空中齿轮（030906，air 轨道）：待地面打 J，不能起跳，跳了就撞/漏。
        # 18 地面齿轮（030903）才走 _try_hop 起跳躲。
        if _is_air_lane(gear.lane):
            extra.append(_make_tap(gear, t, False, _lane_held(keys, t, False)))
            keys.append(_Key(t, t, False, 0.0))
            n_upper_hit += 1
            continue
        air_end = _try_hop(
            gear, keys, extra, hop_t, air_gears, ground_collects, hit_18s
        )
        if air_end is None:
            extra.append(_make_tap(gear, t, False, _lane_held(keys, t, False)))
            keys.append(_Key(t, t, False, 0.0))
            n_upper_hit += 1
        else:
            hops += 1

    logger.info(
        "齿轮躲避: %d 个齿轮 / %d 串地面锯 / %d 个补跳 / %d 个上空18打击",
        len(dodges),
        len(streams),
        hops,
        n_upper_hit,
    )

    out = playable + extra
    out.sort(key=lambda e: (e.t_sec, e.kind.value, e.lane.value))
    return out
