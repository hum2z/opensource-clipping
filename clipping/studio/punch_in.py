"""
clipping.studio.punch_in — Punch-in cut rhythm for single-camera clips.

A talking-head clip cut from a podcast is one continuous locked-off shot for
its whole duration. Short-form video that performs well changes what the
viewer is looking at every couple of seconds. With only one camera angle
there is no second shot to cut to, so the cut is manufactured by stepping the
crop between framings — wide, medium, tight — on the beat of the speech.

Reference measurements taken from a high-performing 59s Short (11.9M views):

    29 shots in 58.5s      median shot 1.77s      55% of shots under 2s

and, importantly, its cuts were **not** synchronised to the music (29% landed
within 100ms of a beat against a 52% random baseline — anti-correlated). They
tracked the narration instead: 64% fell within 0.5s of a clause boundary
against a 41% baseline. So this module places cuts on speech boundaries, never
on a fixed metronome.
"""

import os

# Zoom levels cycled through as the clip progresses. 1.0 is the framing the
# face tracker chose; the rest crop tighter around the same centre.
#
# The ceiling is deliberately low. A 9:16 crop out of 1080p source is already
# being upscaled ~1.78x to reach 1080x1920, so every extra 0.1 of zoom costs
# real detail. Past ~1.25 the softening becomes visible on a phone.
DEFAULT_LEVELS = (1.0, 1.15, 1.08, 1.22)

# Target seconds between cuts, and the hard floor on how short a shot may be.
DEFAULT_CADENCE = 1.8
MIN_SHOT = 0.9

# How far the crop travels across a single shot. Small: the point is continuous
# movement, not a visible zoom.
DEFAULT_DRIFT = 0.06


def _clause_boundaries(data_segmen, start_clip, end_clip):
    """Times (absolute seconds) where a spoken clause ends, within the clip.

    A boundary is the moment just after a word carrying sentence or clause
    punctuation. Falls back to segment ends when the transcript has no
    punctuation at all (some caption sources strip it).
    """
    bounds = []
    has_punct = False
    for seg in data_segmen or []:
        words = seg.get("words") or []
        for w in words:
            txt = str(w.get("word", "")).strip()
            end = w.get("end")
            if end is None:
                continue
            if txt.endswith((".", ",", "!", "?", ";", ":")):
                has_punct = True
                if start_clip < end < end_clip:
                    bounds.append(float(end))
    if not has_punct:
        for seg in data_segmen or []:
            end = seg.get("end")
            if end is not None and start_clip < float(end) < end_clip:
                bounds.append(float(end))
    return sorted(set(bounds))


def build_punch_plan(
    data_segmen,
    start_clip,
    end_clip,
    cadence=DEFAULT_CADENCE,
    levels=DEFAULT_LEVELS,
    min_shot=MIN_SHOT,
    drift=DEFAULT_DRIFT,
):
    """Build ``(t_start, t_end, zoom_start, zoom_end)`` spans in absolute seconds.

    Walks forward from *start_clip*, and at each step takes the clause
    boundary nearest to ``cadence`` seconds ahead — so cuts land on speech,
    not on a metronome. Falls back to a plain *cadence* step when no boundary
    is available in range (long uninterrupted delivery).

    Consecutive spans never share a zoom level, otherwise the "cut" would be
    invisible.
    """
    if end_clip <= start_clip:
        return []
    levels = tuple(levels) or (1.0,)

    bounds = _clause_boundaries(data_segmen, start_clip, end_clip)
    cuts = [float(start_clip)]
    t = float(start_clip)
    while t < end_clip - min_shot:
        target = t + cadence
        # Boundaries far enough ahead to respect min_shot, and not past the end.
        usable = [b for b in bounds if b >= t + min_shot and b <= end_clip - min_shot]
        if usable:
            nxt = min(usable, key=lambda b: abs(b - target))
            # Ignore a boundary that would drag the shot far past the cadence.
            if nxt > t + cadence * 2.0:
                nxt = target
        else:
            nxt = target
        if nxt >= end_clip - min_shot:
            break
        cuts.append(nxt)
        t = nxt
    cuts.append(float(end_clip))

    plan = []
    prev_zoom = None
    li = 0
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if b - a <= 0:
            continue
        zoom = levels[li % len(levels)]
        li += 1
        if zoom == prev_zoom and len(levels) > 1:
            zoom = levels[li % len(levels)]
            li += 1
        prev_zoom = zoom
        # Each shot also drifts across its own duration rather than holding a
        # fixed crop. A locked-off frame between cuts measured ~0.06 px/frame of
        # global motion against ~2.7 for a high-performing reference — the
        # reference's camera is never still. Direction alternates so the framing
        # oscillates instead of creeping ever tighter.
        direction = 1.0 if (len(plan) % 2 == 0) else -1.0
        z0 = float(zoom)
        z1 = float(zoom) + direction * float(drift)
        z1 = max(1.0, z1)
        plan.append((a, b, z0, z1))
    return plan


def get_zoom_at(plan, t_abs, default=1.0):
    """Zoom factor at absolute time *t_abs*, interpolated within its shot.

    Linear across the shot: the movement should be steady and barely
    perceptible frame to frame, not an eased move that calls attention to
    itself.
    """
    if not plan:
        return default
    for a, b, z0, z1 in plan:
        if a <= t_abs < b:
            span = b - a
            if span <= 0:
                return z0
            f = (t_abs - a) / span
            return z0 + (z1 - z0) * f
    return plan[-1][3] if t_abs >= plan[-1][0] else default


def describe_plan(plan):
    """One-line human summary, for the render log."""
    if not plan:
        return "punch-in: (none)"
    lens = [b - a for a, b, _z0, _z1 in plan]
    lens_sorted = sorted(lens)
    median = lens_sorted[len(lens_sorted) // 2]
    return (
        f"punch-in: {len(plan)} shots | median {median:.2f}s | "
        f"{sum(1 for x in lens if x < 2.0)}/{len(lens)} under 2s"
    )
