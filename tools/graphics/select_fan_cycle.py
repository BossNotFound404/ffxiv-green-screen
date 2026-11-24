"""Select a bracketed cast from consecutive native observations and QPC ticks."""

from dataclasses import dataclass

import numpy as np

from pack_terrain_fan import ADD, COVERAGE, SCREEN, observable_coverage_alpha


# A short draw-free break can occur between stages of the same cast. Require
# both elapsed native time and multiple samples to recognize an idle interval.
IDLE_MILLISECONDS = 180
MIN_IDLE_SAMPLES = 3
PREROLL_SAMPLES = 4
EXPECTED_ROLES = {COVERAGE, ADD, SCREEN}


@dataclass(frozen=True)
class Activity:
    draw_roles: frozenset
    measured_roles: frozenset
    support_pixels: int
    touches_edge: bool


def observe(entries, crop):
    """Measure RGB change or coverage attenuation without a display fit."""
    draw_roles, measured_roles = set(), set()
    support = np.zeros((crop["height"], crop["width"]), dtype=bool)
    for entry in entries:
        before, after, role = entry["before"], entry["after"], entry["role"]
        draw_roles.add(role)
        changed = (before[..., :3] != after[..., :3]).any(axis=2)
        if role == COVERAGE:
            row = entry["row"]
            if (row["srcAlphaBlend"], row["dstAlphaBlend"], row["alphaBlendOp"]) != (1, 6, 1) or not row["writeMask"] & 8:
                raise ValueError("Coverage requires stored ZERO/INV_SRC_ALPHA/ADD alpha.")
            start_alpha, end_alpha = before[..., 3], after[..., 3]
            changed |= observable_coverage_alpha(start_alpha, end_alpha) & (end_alpha < start_alpha)
        if changed.any():
            measured_roles.add(role)
            support |= changed
    edge = support[0].any() or support[-1].any() or support[:, 0].any() or support[:, -1].any()
    return Activity(frozenset(draw_roles), frozenset(measured_roles), int(support.sum()), bool(edge))


def select_cycle(frames, activity, frequency):
    """Pick a measured cast with a draw-free lead, closure, and real tail timestamp."""
    if len(frames) != len(activity):
        raise ValueError("Auto-cycle activity must cover every published native frame.")
    times = [frame["qpc"] for frame in frames]

    def proves_idle(first, last):
        return (last - first + 1 >= MIN_IDLE_SAMPLES
                and (times[last] - times[first]) * 1000 >= IDLE_MILLISECONDS * frequency)

    intervals = []
    for index, frame in enumerate(activity):
        if not frame.draw_roles:
            continue
        if not intervals or proves_idle(intervals[-1][1] + 1, index - 1):
            intervals.append([index, index])
        else:
            intervals[-1][1] = index
    if not intervals:
        raise ValueError("Auto-cycle found no qualifying native fan draws in the published frames.")

    def point(index):
        return {"sourceIndex": index, "present": frames[index]["present"], "qpc": times[index]}

    candidates, rejected = [], []
    for position, (onset, end) in enumerate(intervals):
        opening = intervals[position - 1][1] + 1 if position else 0
        next_onset = intervals[position + 1][0] if position + 1 < len(intervals) else None
        closing = next_onset if next_onset is not None else len(frames)
        measured = set().union(*(row.measured_roles for row in activity[onset:end + 1]))
        missing = EXPECTED_ROLES - measured
        edge_frames = [i for i in range(onset, end + 1) if activity[i].touches_edge]

        # Close on an observed draw-free timestamp, reserving it as the QPC
        # following the last exported frame. This also applies to fault prefixes.
        guard_tail = next((i for i in range(end + 1 + MIN_IDLE_SAMPLES, closing)
                           if proves_idle(end + 1, i)), None)
        causes = []
        if not proves_idle(opening, onset - 1):
            causes.append("insufficient draw-free samples before onset")
        if guard_tail is None:
            causes.append("insufficient draw-free samples or missing following timestamp after the last draw")
        if missing:
            causes.append(f"missing measured roles {', '.join(sorted(missing))}")
        if edge_frames:
            causes.append(f"measured support touches the crop edge at present {frames[edge_frames[0]]['present']}")
        if causes:
            rejected.append(f"indices {onset}–{end}: " + ", ".join(causes))
            continue

        lead = min(PREROLL_SAMPLES, onset - opening)
        first = onset - lead
        repeat_tail = next_onset - lead if next_onset is not None else None
        # A following cast can provide the repeat timing even when the capture
        # stops in that cast. Require coverage at its onset before using it.
        repeated = (repeat_tail is not None and repeat_tail >= guard_tail
                    and COVERAGE in activity[next_onset].draw_roles)
        tail = repeat_tail if repeated else guard_tail
        last = tail - 1
        active_rows = activity[onset:end + 1]
        peak = max(row.support_pixels for row in active_rows)
        observed_duration = (times[end + 1] - times[onset]) / frequency
        record = {
            "method": "native_observed_fan_cast_v1",
            "reason": ("Bracketed cast with a subsequent observed coverage onset; matched idle preroll."
                       if repeated else "Bracketed cast trimmed after the observed draw-free closing guard."),
            "first": point(first), "last": point(last), "followingTimestamp": point(tail),
            "onset": point(onset), "lastDraw": point(end),
            "cleanPlate": point(onset - 1), "nextObservedOnset": point(next_onset) if next_onset is not None else None,
            "repeatTimingUsed": repeated,
            "openingIdleFirst": point(opening), "closingIdleFirst": point(end + 1),
            "closingGuardThrough": point(guard_tail),
            "minimumIdleMilliseconds": IDLE_MILLISECONDS, "minimumIdleSamples": MIN_IDLE_SAMPLES,
            "measuredRoles": sorted(measured), "peakMeasuredSupportPixels": peak,
            "drawFreeFramesWithinCast": sum(not row.draw_roles for row in active_rows),
            "drawFramesWithoutMeasuredChange": sum(bool(row.draw_roles) and not row.support_pixels for row in active_rows),
            "observedDrawWindowSeconds": observed_duration,
            "observedDrawWindowFps": len(active_rows) / observed_duration,
            "selectedAverageFps": (last - first + 1) * frequency / (times[tail] - times[first]),
            "scope": ("Brackets cover the recorded crop and qualified native draw signatures. Crop edges are checked "
                      "against measured support, and the selection cannot prove the complete authored lifecycle."),
        }
        candidates.append(((repeated, peak, -onset), record))
    if not candidates:
        details = "; ".join(rejected[:6])
        if len(rejected) > 6:
            details += f"; {len(rejected) - 6} more intervals rejected"
        raise ValueError(f"Auto-cycle found no bracketed, uncropped cast. {details}")
    selected = max(candidates, key=lambda candidate: candidate[0])[1]
    selected["candidateCount"] = len(candidates)
    selected["observedDrawIntervals"] = [{"onset": point(first), "lastDraw": point(last)} for first, last in intervals]
    return selected
