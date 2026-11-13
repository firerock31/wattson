"""Generate the HTML dashboard from the tracker database.

The page's CSS and JavaScript live in static/dashboard.css
and static/dashboard.js, linked by the generated page. Demo
builds (WATTSON_DEMO set) inline both files instead, so
the demo stays a single self-contained page.

Visual language mimics the Rivian app: near-black background, huge display
type, big rounded cards, green/amber accents, donut charts. No emoji, no
icons.

Data semantics:
- Trip durations marked ~ are poll-bracketed upper bounds, not drive time.
- Energy math uses Rivian's rated 87.9 kWh usable capacity (the API's
  batteryCapacity=91.4 is stored raw for the pack-health trend only).
  Validated against the car's own trip meter; a ~4% over-read
  vs the car remains unexplained.
- Averages always state their sample size.
- Phantom drain is measured between fresh cloud-sync anchors (the car's
  last report after parking, its first on waking). Plugged-in periods
  are not measurable: the car tops itself off while plugged in.
"""
import base64
import html
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from analyze import activity_segments
from config import (HOME_LAT, HOME_LON,
                    ODOMETER_METERS_PER_UNIT,
                    USABLE_KWH, HOME_KWH_RATE, VEHICLE_NAME,
                    VEHICLE_MODEL,
                    REPORT_PATH, TOKENS_PATH, LOCAL_TZ)
from crypto import unseal
from db import charge_price_info, connect
from dashboard_auth import token_from_env

GREEN = "#7ed321"
AMBER = "#f5a623"
RED = "#ff5a52"

CHARGE_TARGET_PCT = 90.0  # daily charge limit (100 for trips)
# HOME_KWH_RATE lives in config.py (set RIVIAN_HOME_KWH_RATE in .env).
HOME_RADIUS_M = 250.0
# Fuel-savings comparison: equivalent gas SUV mpg, and gas-price
GAS_COMPARISON_MPG = float(os.environ.get(
    'WATTSON_GAS_COMPARISON_MPG',
    os.environ.get('RIVIAN_GAS_MPG', '25')))
GAS_PRICE_FALLBACK = float(os.environ.get(
    'WATTSON_GAS_PRICE',
    os.environ.get('RIVIAN_GAS_PRICE', '4.50')))

def _ldt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)


def _fmt_ts(ts: int) -> str:
    return _ldt(ts).strftime("%b %d, %I:%M %p")


def _fmt_time(ts: int) -> str:
    return _ldt(ts).strftime("%-I:%M %p").lower()


def _fmt_day(ts: int) -> str:
    return _ldt(ts).strftime("%a, %b %d")


def _day(ts: int) -> str:
    return _ldt(ts).strftime("%Y-%m-%d")


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else "-"


def _mi_per_kwh(wh_per_mi):
    return f"{1000 / wh_per_mi:.2f}" if wh_per_mi else "-"


# ---------------------------------------------------------------- SVG pieces

def _donut(frac: float, color: str, big: str, small: str) -> str:
    """Rivian-style thick ring donut. frac is 0..1 of the ring filled."""
    frac = max(0.0, min(1.0, frac))
    r = 80
    c = 2 * 3.14159265 * r
    filled = c * frac
    return (
        f'<svg viewBox="0 0 200 200" class="donut" role="img">'
        f'<circle cx="100" cy="100" r="{r}" fill="none" stroke="#2b2b2e"'
        f' stroke-width="26"/>'
        f'<circle cx="100" cy="100" r="{r}" fill="none" stroke="{color}"'
        f' stroke-width="26" stroke-linecap="round"'
        f' stroke-dasharray="{filled:.1f} {c:.1f}"'
        f' transform="rotate(-90 100 100)"/>'
        f'<text x="100" y="98" text-anchor="middle" class="donut-big">'
        f'{_esc(big)}</text>'
        f'<text x="100" y="126" text-anchor="middle" class="donut-small">'
        f'{_esc(small)}</text></svg>')


def _donut_segments(segments: list[tuple[float, str]],
                    big: str, small: str, tiny: str = "") -> str:
    """Thick ring donut with stacked arcs. segments: (fraction 0..1, color).

    Drawn over the dark-gray empty ring, so green + orange read as the
    current battery and gray as what is left to 100%.
    """
    r = 80
    c = 2 * 3.14159265 * r
    parts = ['<circle cx="100" cy="100" r="80" fill="none" '
             'stroke="#2b2b2e" stroke-width="26"/>']
    off = 0.0
    for frac, color in segments:
        frac = max(0.0, min(1.0, frac))
        if frac <= 0:
            continue
        parts.append(
            f'<circle cx="100" cy="100" r="{r}" fill="none" '
            f'stroke="{color}" stroke-width="26" stroke-linecap="round" '
            f'stroke-dasharray="{c * frac:.1f} {c:.1f}" '
            f'stroke-dashoffset="{-off:.1f}" '
            f'transform="rotate(-90 100 100)"/>')
        off += c * frac
    return (
        '<svg viewBox="0 0 200 200" class="donut" role="img">'
        + "".join(parts) +
        f'<text x="100" y="98" text-anchor="middle" class="donut-big">'
        f'{_esc(big)}</text>'
        f'<text x="100" y="126" text-anchor="middle" class="donut-small">'
        f'{_esc(small)}</text>'
        f'<text x="100" y="150" text-anchor="middle" class="donut-tiny">'
        f'{_esc(tiny)}</text></svg>')


def _nice_ticks(vmax: float, target: int = 4):
    """Y-axis ticks on nice round steps. Returns (ticks, top)."""
    if vmax <= 0:
        return [0.0, 1.0], 1.0
    raw = vmax / target
    exp = math.floor(math.log10(raw))
    f = raw / 10 ** exp
    step = next(nf * 10 ** exp for nf in (1, 2, 2.5, 5, 10) if f <= nf)
    top = round(math.ceil(vmax / step - 1e-9) * step, 9)
    ticks = [i * step for i in range(int(round(top / step)) + 1)]
    return ticks, top


def _bars(items: list[tuple[str, float, str]], fmt: str = ".2f",
          labels: list[str] | None = None,
          sublabels: list[str] | None = None,
          thin: bool = False,
          y_unit: str | None = None,
          ref_value: float | None = None,
          ref_label: str = "median",
          stubs: set[int] | None = None) -> str:
    """items: (label, value, color). Simple rounded vertical bars.
    fmt controls the value label formatting (".2f" for kWh, ".0f" for mi);
    labels overrides the value text per bar (raw HTML, e.g. "&lt;0.1").
    sublabels adds a smaller second line under the value (e.g. miles).
    thin: for long ranges, value labels appear only on red (abnormal)
    bars and x labels thin to ~12.
    y_unit: when set (e.g. "kWh"), draws a y-axis with gridlines on nice
    round ticks and a true zero baseline, so bar heights read as absolute
    values instead of fractions of the tallest visible bar.
    ref_value/ref_label: dashed reference line, e.g. the median that the
    abnormal-night rule is measured against. The label sits in the axis
    gutter at the line's height (never over the bars, whose value labels
    live at the same height at the plot's right end).
    stubs: bar indexes drawn as short hollow outlines instead of filled
    bars, for periods that exist but are below the measurement floor."""
    if not items:
        return ""
    raw_max = max(v for _, v, _ in items)
    w, h, pad = 560, 150, 8
    n = len(items)
    axis = y_unit is not None
    x0 = 50 if axis else pad
    x1 = w - pad
    if axis:
        ticks, top = _nice_ticks(raw_max)
    else:
        ticks, top = [], (raw_max or 1.0)
    # Headroom above the tallest bar for the two-line value labels.
    plot_h = h - (64 if sublabels else 34)
    base_y = h - 22
    def Y(v): return base_y - (v / top) * plot_h
    gap = (x1 - x0) / max(n, 1)
    bw = min(46, gap * 0.55)
    lstep = max(1, math.ceil(n / 12)) if thin else 1
    parts = []
    if axis:
        for t in ticks:
            y = Y(t)
            if t > 0:
                parts.append(
                    f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"'
                    f' class="grid"/>')
            parts.append(
                f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end"'
                f' class="axis">{t:g}</text>')
        # Zero baseline, slightly stronger than the gridlines.
        parts.append(
            f'<line x1="{x0}" y1="{base_y}" x2="{x1}" y2="{base_y}"'
            f' stroke="#8e8e93" stroke-width="1.5"/>')
        if ref_value and ref_value <= top:
            ry = Y(ref_value)
            parts.append(
                f'<line x1="{x0}" y1="{ry:.1f}" x2="{x1}" y2="{ry:.1f}"'
                f' stroke="#98989f" stroke-width="1"'
                f' stroke-dasharray="5 4"/>')
            # Label in the axis gutter at the line's height; nudged
            # below the line when a tick label already sits there.
            ly = ry - 2
            if any(abs(ly - (Y(t) + 4)) < 12 for t in ticks):
                ly = ry + 15
            parts.append(
                f'<text x="{x0 - 4}" y="{ly:.1f}" text-anchor="end"'
                f' class="axis">{_esc(ref_label)}</text>')
    rects = []
    for i, (label, val, color) in enumerate(items):
        frac = val / top
        # With a real axis a zero night draws no bar (honest); without an
        # axis keep the old 4px minimum so tiny values stay visible.
        # Stub bars override the height: a short hollow outline marks a
        # period that exists but is below the measurement floor.
        stub = bool(stubs) and i in stubs
        bh = 9.0 if stub else (
            frac * plot_h if axis else max(4.0, frac * plot_h))
        x = x0 + gap * i + (gap - bw) / 2
        y = base_y - bh
        if thin and color != RED and not stub:
            vlab, slab = "", ""
        else:
            vlab = labels[i] if labels else f"{val:{fmt}}"
            slab = sublabels[i] if sublabels else ""
        xlab = _esc(label) if (i % lstep == 0 or i == n - 1) else ""
        vtxt = (f'<text x="{x + bw / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle"'
                f' class="bar-val">{vlab}</text>')
        if slab:
            vtxt = (
                f'<text x="{x + bw / 2:.1f}" y="{y - 24:.1f}" text-anchor="middle"'
                f' class="bar-val">{vlab}</text>'
                f'<text x="{x + bw / 2:.1f}" y="{y - 10:.1f}" text-anchor="middle"'
                f' class="bar-sub">{slab}</text>')
        if stub:
            rect = (f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                    f'height="{bh:.1f}" rx="4" fill="none" '
                    f'stroke="{color}" stroke-width="1.5"/>')
        else:
            rect = (f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                    f'height="{bh:.1f}" rx="7" fill="{color}"/>')
        rects.append(
            rect
            + f'<text x="{x + bw / 2:.1f}" y="{h - 6}" text-anchor="middle"'
            f' class="bar-label">{xlab}</text>' + vtxt)
    return (f'<svg viewBox="0 0 {w} {h}" class="bars" role="img">'
            + "".join(parts) + "".join(rects) + "</svg>")


def _heat_strip(nights: list[dict], bad: set) -> str:
    """Idle-period heat strip for long ranges: one cell per period, weeks as
    columns, Monday..Sunday rows. Cell color intensity = watts draw;
    abnormal periods (>1.6x median) stay red; no-data periods are flat
    dark gray. Hover a cell for that period's numbers."""
    dates = [datetime.strptime(o["day"], "%Y-%m-%d").date()
             for o in nights]
    first_mon = dates[0] - timedelta(days=dates[0].weekday())
    cells = []
    for o, d in zip(nights, dates):
        idx = (d - first_mon).days
        cells.append((idx // 7, idx % 7, o))
    ncols = max(c for c, _, _ in cells) + 1
    cell = min(14, max(8, int(520 / max(ncols, 1))))
    gap = max(2, cell // 5)
    step = cell + gap
    pad_l, pad_t = 8, 20
    grid_h = 7 * step - gap
    leg_y = pad_t + grid_h + 22
    grid_w = pad_l * 2 + ncols * step - gap
    # Legend row can run wider than the grid when the abnormal key is on.
    leg_end = pad_l + 36 + 5 * 16 + 10 + 40
    if bad:
        leg_end += 46 + 19 + int(len("abnormal (>1.6x median)") * 6.5)
    W = max(grid_w, leg_end + pad_l)
    H = leg_y + 10
    vmax = max(o["watts"] for o in nights) or 1.0

    def amber(f: float) -> str:
        lo, hi = (61, 47, 26), (245, 166, 35)
        r = round(lo[0] + (hi[0] - lo[0]) * f)
        g = round(lo[1] + (hi[1] - lo[1]) * f)
        b = round(lo[2] + (hi[2] - lo[2]) * f)
        return f"#{r:02x}{g:02x}{b:02x}"

    parts = []
    prev_month = None
    for c in range(ncols):
        monday = first_mon + timedelta(days=7 * c)
        if monday.month != prev_month:
            parts.append(
                f'<text x="{pad_l + c * step}" y="13" class="axis">'
                f'{monday.strftime("%b")}</text>')
            prev_month = monday.month
    for c, r, o in cells:
        x, y = pad_l + c * step, pad_t + r * step
        if o["id"] in bad:
            fill = RED
        elif o["no_data"] or o.get("substep") or o["watts"] <= 0:
            fill = "#2b2b2d"
        else:
            fill = amber(o["watts"] / vmax)
        if o["no_data"]:
            tip = f'{o["label"]}: no data (car was asleep)'
        elif o.get("substep"):
            tip = (f'{o["label"]}: below one gauge step '
                   f'over {o["hours"]:.1f} h')
        else:
            tip = (f'{o["label"]}: {o["watts"]:.0f} W, '
                   f'{o["kwh"]:.2f} kWh over {o["hours"]:.1f} h')
        if o["id"] in bad:
            tip += " (abnormal)"
        parts.append(
            f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3"'
            f' fill="{fill}"><title>{_esc(tip)}</title></rect>')
    parts.append(f'<text x="{pad_l}" y="{leg_y}" class="axis">less</text>')
    sx = pad_l + 36
    for i in range(5):
        parts.append(
            f'<rect x="{sx + i * 16}" y="{leg_y - 11}" width="13"'
            f' height="13" rx="3" fill="{amber(i / 4)}"/>')
    ex = sx + 5 * 16 + 10
    parts.append(f'<text x="{ex}" y="{leg_y}" class="axis">more</text>')
    if bad:
        rx = ex + 46
        parts.append(
            f'<rect x="{rx}" y="{leg_y - 11}" width="13" height="13" rx="3"'
            f' fill="{RED}"/>')
        parts.append(
            f'<text x="{rx + 19}" y="{leg_y}" class="axis">'
            "abnormal (&gt;1.6&times; median)</text>")
    return (f'<svg viewBox="0 0 {W} {H}" class="heat" role="img">'
            + "".join(parts) + "</svg>")


def _timeline_strip(periods: list[dict], bad: set) -> str:
    """One-row timeline for short ranges: real time on the x-axis.

    Each idle period is a segment on a single track, positioned by its
    real start/end (t1/t2), length proportional to duration, color by
    watts: amber fill, red when abnormal (>1.6x median), hollow outline
    when the drop fits inside one gauge step (present but unmeasurable),
    dark gray when plugged in or no data. Gaps between segments are
    driving time, so a multi-day park reads as the long stretch it is
    instead of one bar as wide as a 6-hour park.

    Value labels are all in watts: a substep period prints an upper
    bound ("<4 W" = one gauge step, 88 Wh, over its hours) instead of
    switching to percent, so every label is comparable. Labels sit on
    two alternating rows above the track and are skipped when they
    would collide with the previous label in the same row, so dense
    ranges (1M) stay readable. Below the track, each segment gets a
    short hours label ("38h"); the dates live on a proper day axis at
    the bottom spanning whole calendar days, one tick per midnight with
    a date label, thinned on long ranges by pixel width. Every segment
    carries a <title> with the full numbers.
    """
    if not periods:
        return ""
    w, h = 560, 144
    x0, x1 = 8, w - 8
    t0 = min(o["t1"] for o in periods)
    t_end = max(o["t2"] for o in periods)
    # The axis spans whole calendar days, midnight to midnight, so its
    # ticks land on evenly spaced day boundaries and every day in the
    # range gets a marker. Segments keep their real start/end inside
    # that span.
    day0 = _ldt(t0).date()
    day1 = _ldt(t_end).date()
    axis_start = datetime(day0.year, day0.month, day0.day,
                          tzinfo=LOCAL_TZ).timestamp()
    day_end = day1 + timedelta(days=1)
    axis_end = datetime(day_end.year, day_end.month, day_end.day,
                        tzinfo=LOCAL_TZ).timestamp()
    span = max(axis_end - axis_start, 3600)

    def X(t: int) -> float:
        return x0 + (t - axis_start) / span * (x1 - x0)

    # Day boundaries inside the span: gridlines across the track and
    # ticks on the day axis below, at the same x positions.
    midnights = []
    d = day0 + timedelta(days=1)
    while d <= day1:
        midnights.append(datetime(d.year, d.month, d.day,
                                  tzinfo=LOCAL_TZ).timestamp())
        d += timedelta(days=1)

    track_y, track_h = 52, 26
    parts = [
        f'<rect x="{x0}" y="{track_y}" width="{x1 - x0}" height="{track_h}"'
        f' rx="7" fill="#1e1e20"/>'
    ]
    for mid in midnights:
        gx = X(mid)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{track_y}" x2="{gx:.1f}"'
            f' y2="{track_y + track_h}" class="grid"/>')
    # Value labels on two alternating rows; skip on collision.
    # Hours sublabels on one row below the track; same skip rule.
    row_y = [44, 26]
    row_end = [-1e9, -1e9]
    sub_end = -1e9
    for o in periods:
        sx = X(o["t1"])
        seg_w = max(4.0, X(o["t2"]) - sx)
        ex = sx + seg_w
        cx = (sx + ex) / 2
        if o["plugged"]:
            fill = "#2b2b2d"
            rect = (f'<rect x="{sx:.1f}" y="{track_y}" width="{seg_w:.1f}"'
                    f' height="{track_h}" rx="6" fill="{fill}">')
            vlab = "plugged in"
            tip = f'{o["label"]}: plugged in, not measured'
        elif o["no_data"]:
            rect = (f'<rect x="{sx:.1f}" y="{track_y}" width="{seg_w:.1f}"'
                    f' height="{track_h}" rx="6" fill="none"'
                    f' stroke="#555558" stroke-width="1.5">')
            vlab = "no data"
            tip = f'{o["label"]}: no data (car was asleep)'
        elif o.get("substep"):
            rect = (f'<rect x="{sx:.1f}" y="{track_y}" width="{seg_w:.1f}"'
                    f' height="{track_h}" rx="6" fill="none"'
                    f' stroke="{AMBER}" stroke-width="1.5">')
            # One gauge step is 0.1% of the usable pack: 87.9 Wh, which
            # is numerically USABLE_KWH in watts over one hour.
            bound = USABLE_KWH / max(o["hours"], 0.1)
            vlab = f"<{bound:.0f} W"
            tip = (f'{o["label"]}: below one gauge step '
                   f'over {o["hours"]:.1f} h')
        else:
            fill = RED if o["id"] in bad else AMBER
            rect = (f'<rect x="{sx:.1f}" y="{track_y}" width="{seg_w:.1f}"'
                    f' height="{track_h}" rx="6" fill="{fill}">')
            vlab = f'{o["watts"]:.0f} W'
            tip = (f'{o["label"]}: {o["watts"]:.0f} W, '
                   f'{o["kwh"]:.2f} kWh over {o["hours"]:.1f} h')
        if o["id"] in bad:
            tip += " (abnormal)"
        parts.append(rect + f"<title>{_esc(tip)}</title></rect>")
        lab_w = len(vlab) * 6.8
        # Clamp the text center so edge labels stay inside the viewBox.
        tx = min(max(cx, lab_w / 2), w - lab_w / 2)
        for r in (0, 1):
            if tx - lab_w / 2 > row_end[r] + 4:
                parts.append(
                    f'<text x="{tx:.1f}" y="{row_y[r]}" text-anchor="middle"'
                    f' class="bar-val">{_esc(vlab)}</text>')
                row_end[r] = tx + lab_w / 2
                break
        slab = f'{o["hours"]:.0f}h'
        slab_w = len(slab) * 6.5
        sx_txt = min(max(cx, slab_w / 2), w - slab_w / 2)
        if sx_txt - slab_w / 2 > sub_end + 4:
            parts.append(
                f'<text x="{sx_txt:.1f}" y="94" text-anchor="middle"'
                f' class="bar-sub">{_esc(slab)}</text>')
            sub_end = sx_txt + slab_w / 2
    # Day axis: baseline, one tick per midnight, date labels thinned
    # by pixel width (not by count) so a 7-day view shows every day.
    # Any label that would collide with the previous one is skipped.
    axis_y = 112
    parts.append(
        f'<line x1="{x0}" y1="{axis_y}" x2="{x1}" y2="{axis_y}"'
        f' stroke="#8e8e93" stroke-width="1.5"/>')
    axis_labels = [(axis_start, _ldt(axis_start).strftime("%b %d"))]
    axis_labels += [(m, _ldt(m).strftime("%b %d")) for m in midnights]
    axis_labels.append((axis_end, _ldt(axis_end).strftime("%b %d")))
    n_lab = len(axis_labels)
    lab_w_est = 6 * 6.5
    spacing = (x1 - x0) / max(n_lab - 1, 1)
    step = max(1, math.ceil((lab_w_est + 8) / spacing))
    ax_end = -1e9
    for i, (t, txt) in enumerate(axis_labels):
        if i % step and i != n_lab - 1:
            continue
        lx = X(t)
        lab_w = len(txt) * 6.5
        tx = min(max(lx, lab_w / 2), w - lab_w / 2)
        if tx - lab_w / 2 < ax_end + 2:
            continue
        parts.append(
            f'<line x1="{lx:.1f}" y1="{axis_y - 4}" x2="{lx:.1f}"'
            f' y2="{axis_y + 4}" stroke="#8e8e93" stroke-width="1.5"/>')
        parts.append(
            f'<text x="{tx:.1f}" y="{axis_y + 20}" text-anchor="middle"'
            f' class="axis">{_esc(txt)}</text>')
        ax_end = tx + lab_w / 2
    return (f'<svg viewBox="0 0 {w} {h}" class="timeline" role="img">'
            + "".join(parts) + "</svg>")

def _battery_svg(series: list[tuple[str, float]]) -> str:
    """End-of-day SoC line chart. series: (day_label, soc)."""
    w, h = 600, 200
    top, bot = 18, 34
    lo, hi = 0, 100
    def X(i): return 34 + (w - 52) * (i / max(len(series) - 1, 1))
    def Y(v): return top + (h - top - bot) * (1 - (v - lo) / (hi - lo))
    grid = []
    for gv in (25, 50, 75):
        y = Y(gv)
        grid.append(
            f'<line x1="34" y1="{y:.1f}" x2="{w - 18}" y2="{y:.1f}"'
            f' class="grid"/>'
            f'<text x="28" y="{y + 4:.1f}" text-anchor="end"'
            f' class="axis">{gv}%</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(series))
    dots = "".join(
        f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="5" fill="{GREEN}"/>'
        f'<text x="{X(i):.1f}" y="{Y(v) - 12:.1f}" text-anchor="middle"'
        f' class="axis">{v:.0f}%</text>'
        f'<text x="{X(i):.1f}" y="{h - 10}" text-anchor="middle"'
        f' class="axis">{_esc(lbl)}</text>'
        for i, (lbl, v) in enumerate(series))
    return (
        f'<svg viewBox="0 0 {w} {h}" class="linechart" role="img">'
        + "".join(grid) +
        f'<polyline points="{pts}" fill="none" stroke="{GREEN}"'
        f' stroke-width="3.5" stroke-linejoin="round"'
        f' stroke-linecap="round"/>' + dots + "</svg>")


def _pack_svg(series: list[tuple[str, float, float]],
              baseline_label: str = "") -> str:
    """Dual indexed trend: pack capacity vs implied full range, each = 100
    at the fixed baseline (first week of data). series: (label, cap_idx,
    range_idx).

    The y-axis is anchored at 100 on top and extends only downward (floor
    90 unless the data dips lower): for a degradation chart the only
    direction that matters is down, so headroom above 100 would waste
    space and exaggerate BMS noise. The top extends only if a reading
    ever lands above 100 (BMS recalibration)."""
    w, h = 600, 210
    top, bot = 18, 30
    vals = [v for _, c, r in series for v in (c, r)]
    lo = min(90.0, math.floor(min(vals) - 1.0))
    hi = 100.0 if max(vals) <= 100.0 else math.ceil(max(vals) + 0.5)
    def X(i): return 40 + (w - 58) * (i / max(len(series) - 1, 1))
    def Y(v): return top + (h - top - bot) * (1 - (v - lo) / (hi - lo))
    step = 2 if (hi - lo) <= 12 else 5
    grid = []
    gv = math.ceil(lo / step) * step
    while gv <= hi:
        y = Y(gv)
        grid.append(
            f'<line x1="40" y1="{y:.1f}" x2="{w - 18}" y2="{y:.1f}"'
            f' class="grid"/>'
            f'<text x="34" y="{y + 4:.1f}" text-anchor="end"'
            f' class="axis">{gv:g}</text>')
        gv += step
    def line(idxs, color):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}"
                       for i, v in enumerate(idxs))
        dots = "".join(
            f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="4.5"'
            f' fill="{color}"/>' for i, v in enumerate(idxs))
        return (f'<polyline points="{pts}" fill="none" stroke="{color}"'
                f' stroke-width="3" stroke-linejoin="round"'
                f' stroke-linecap="round"/>' + dots)
    cap = [c for _, c, _ in series]
    rng = [r for _, _, r in series]
    n = len(series)
    lstep = max(1, math.ceil(n / 6))  # ~6 x labels no matter the span
    labels = "".join(
        f'<text x="{X(i):.1f}" y="{h - 8}" text-anchor="middle"'
        f' class="axis">{_esc(lbl)}</text>'
        for i, (lbl, _, _) in enumerate(series)
        if i % lstep == 0 or i == n - 1)
    legend = (
        f'<div class="legend-row">'
        f'<span><span class="swatch dot-g"></span>'
        f'Pack capacity</span>'
        f'<span><span class="swatch dot-a"></span>'
        f'Implied full range</span>'
        + (f'<span class="faint">{_esc(baseline_label)}</span>'
           if baseline_label else "")
        + '</div>')
    return (legend +
        f'<svg viewBox="0 0 {w} {h}" class="linechart" role="img">'
        + "".join(grid) + line(cap, GREEN) + line(rng, AMBER)
        + labels + "</svg>")


# ---------------------------------------------------------------- HTML

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__FAVICON__
__STYLES__</head>
<body>
<nav class="sitenav"><div class="nav-inner">
  <a class="nav-brand" href="#top">__BRAND__</a>
  <div class="nav-links">
    <a href="#daily">Daily</a>
    <a href="#battery">Battery</a>
    <a href="#charging">Charging</a>
    <a href="#savings">Savings</a>
  </div>
  <div class="nav-updated">__DEMOPILL__<span>Updated __UPDATED____POLLAGE__</span></div>
</div></nav>
<div class="wrap">
__GOVERNOR__
<div class="hero" id="top">
  <div class="hero-top">
    <div class="hero-num">
      <p class="hero-range">__RANGE__</p>
      <div class="hero-sub"><b>__SOC__</b></div>
    </div>
    <div class="hero-side">
      __CARTAG__
      <div class="hero-odo">__ODO__</div>
    </div>
  </div>
  <div class="battbar__BATTCHG__"><div style="--p:__SOCPCT__%">__BOLT__</div></div>
<div class="heroloc row-between"><span class="flex1">__LOCATION__</span>__NAVBTN__</div>
  <div class="grid4">
    <a class="stat" href="#savings"><div class="k">Lifetime savings vs gas</div>
      <div class="v green">__GASSAVE__</div>
      <div class="s">__GASSAVESUB__</div></a>
    __IDLESTAT__
    <a class="stat" href="#daily"><div class="k">Lifetime efficiency</div>
      <div class="v green">__LIFEMI__<small> mi/kWh</small></div>
      <div class="s">__LIFESUB__</div></a>
    __TIRETILE__
  </div>
</div>

__CHARGING_TOP__
<div class="sec-row" id="daily"><h2 class="sec">Daily activity</h2>__TRIPMONTH__</div>
<div class="alegend"><span><i class="st-moving"></i>driving</span><span><i class="st-charging"></i>charging</span><span><i class="st-parked"></i>awake</span><span><i class="st-climate"></i>climate</span><span><i class="st-sleep"></i>deep sleep</span><span><i class="st-unknown"></i>no data</span></div>
__TRIPS__

<h2 class="sec" id="battery">Battery</h2>
<div class="card">
  <h3>End of day</h3>
  <div class="sub">Remaining battery at 11:55 PM &middot; __BATTERYNOTE__</div>
  <div class="seg" id="battseg">
    <button data-n="7" class="on">7 days</button>
    <button data-n="14">14 days</button>
    <button data-n="30">30 days</button>
  </div>
  <div id="batt7">__BATT7__</div>
  <div id="batt14" hidden>__BATT14__</div>
  <div id="batt30" hidden>__BATT30__</div>
</div>

<div class="card">
  <h3 class="row"><span>Phantom drain</span>__TRENDTOGGLE__</h3>
  __TRENDRANGES__
  <p class="note">__TRENDNOTE__</p>
</div>

__PACKHEALTH__

__CHARGING_BOTTOM__

<div class="foot">
<p>Trip energy is measured from battery % drop, which usually reads a little
higher than the car's own trip screen.</p>
<p>Durations marked ~ are approximate: the drive started sometime inside the
bracket, between polls.</p>
</div>
</div>
<button id="toTop" aria-label="Back to top"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
<form id="costform" method="POST" action="/api/charge-cost-form" class="offscreen"><input type="hidden" name="charge_id" id="form_charge_id"><input type="hidden" name="cost_per_kwh" id="form_cost_per_kwh"><input type="hidden" name="start_ts" id="form_start_ts"></form><div id="costmodal" class="modal-overlay hidden"><div class="cost-card"><h3 class="m0012">DC charging cost</h3><p class="sub m0012">Enter what you paid per kWh:</p><form onsubmit="saveCost();return false;" class="m0"><input id="costinput" type="number" inputmode="decimal" step="0.01" min="0" placeholder="0.48" class="field"><div class="btn-row"><button type="submit" class="btn-primary">Save</button><button type="button" onclick="closeCostModal()" class="btn-ghost">Cancel</button></div><div class="mt12 centered"><a class="cost-action tiny-dim" href="#" onclick="clearCost();return false;">clear entered cost</a></div></form></div></div><div id="navmodal" role="dialog" aria-modal="true" aria-label="Send destination" class="nav-overlay hidden">
<div class="nav-card">
<div class="row-between">
<h3 class="m0">Send destination</h3>
<button type="button" onclick="closeNavModal()" aria-label="Close Send destination" class="modal-close">×</button>
</div>
<p class="sub m0412">The car wakes up and starts navigating.</p>
<div class="rel">
<input id="navq" type="text" placeholder="Search a place or address, press Enter" autocomplete="off" aria-label="Search a place or address, press Enter" class="field field-nav">
<button type="button" id="navclear" onclick="navClearSearch()" aria-label="Clear search" class="clear-btn hidden">×</button>
</div>
<p class="sub fnote">You can also paste a Google Maps link.</p>
<div id="navtabs" class="navtabs hidden">
<button type="button" id="navtabrec" onclick="navSetTab('rec')" class="npill npill-on">Recents</button>
<button type="button" id="navtabfav" onclick="navSetTab('fav')" class="npill">Favorites</button>
</div>
<div id="navlist" class="navlist"></div>
<div id="navselhome"></div>
<div id="navsel" onclick="event.stopPropagation()" class="navsel hidden">
<a id="navmaplink" target="_blank" rel="noopener noreferrer" title="Open in Google Maps"><img id="navmap" alt="Map of the selected place" class="navmap hidden"></a>
<p id="navenergy" class="sub m1000"></p>
<div id="navtight" class="navtight hidden">
<div class="tight-row">
<span class="sub">Trip:</span>
<button type="button" id="navow" onclick="navSetRound(false)" class="npill npill-sm npill-on">One way</button>
<button type="button" id="navrt" onclick="navSetRound(true)" class="npill npill-sm">Round trip</button>
</div>
<p id="navtightmsg" class="sub tight-msg"></p>
<button type="button" id="navplanbtn" onclick="navPlan()" class="plan-btn hidden">Let the car plan charging</button>
<div id="navplan" class="sub mt10"></div>
</div>
<div id="navstatus" class="sub mt14"></div>
<div class="btn-row">
<button type="button" id="navsendbtn" onclick="navSend()" disabled class="btn-primary">Send</button>
<button type="button" onclick="navDeselect()" class="btn-ghost">Cancel</button>
</div>
</div>
</div></div>
__SCRIPTS__
</body></html>"""

_BOLT_SVG = ('<svg class="bolt" viewBox="0 0 24 24" aria-hidden="true">'
            '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'
            '</svg>')



# ---------------------------------------------------------------- static maps

_MAP_W, _MAP_H = 880, 440
# Esri World Dark Gray Canvas basemap (free tier, no API key; note the
# {z}/{y}/{x} tile order). Minimal streets-and-labels style, already dark
# so no CSS invert filter is needed. (CARTO's free tiles now watermark
# "API KEY REQUIRED"; OSM standard was too busy for the trip map.)
_TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
             "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}")
_MAP_UA = "Wattson/1.0"
# Disk cache for map tiles: the dashboard rebuilds often (every poll during
# a burst), and re-fetching tiles each time would hammer the tile servers
# and make every build depend on live network. Tiles change slowly, so a
# 7-day TTL is plenty. Cache misses still fetch with retries as before.
_TILE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "tile_cache_esri")
_TILE_CACHE_TTL = 7 * 24 * 3600


def _tile_cache_get(z: int, x: int, y: int):
    """Return cached tile bytes, or None on miss/expiry."""
    import time as _time
    p = os.path.join(_TILE_CACHE_DIR, str(z), str(x), f"{y}.png")
    try:
        if (os.path.exists(p)
                and _time.time() - os.path.getmtime(p) < _TILE_CACHE_TTL):
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


def _tile_cache_put(z: int, x: int, y: int, data: bytes) -> None:
    try:
        d = os.path.join(_TILE_CACHE_DIR, str(z), str(x))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{y}.png"), "wb") as f:
            f.write(data)
    except Exception:
        pass


def _merc(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Slippy-map pixel coords for lon/lat at zoom z."""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * 256 * n
    r = math.radians(max(min(lat, 85.0511), -85.0511))
    y = ((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi)
         / 2.0 * 256 * n)
    return x, y


def _trip_map_parts(trips: list[dict]):
    """Compute map raster tiles + SVG route overlay for a set of trips.

    Returns (jpeg_bytes|None, svg_html, cw, ch), or None when the day has
    no GPS paths at all. jpeg_bytes is None when tiles can't be fetched:
    the caller then ships the schematic fallback (SVG routes on the dark
    card), as before.
    """
    import io
    import urllib.request

    paths = []
    for t in trips or []:
        try:
            pts = json.loads(t.get("path") or "[]")
        except Exception:
            pts = []
        pts = [(p[0], p[1]) for p in pts
               if p and p[0] is not None and p[1] is not None]
        if len(pts) >= 2:
            paths.append((t, pts))
    if not paths:
        return None
    allp = [p for _, pp in paths for p in pp]
    lat0 = min(p[0] for p in allp)
    lat1 = max(p[0] for p in allp)
    lon0 = min(p[1] for p in allp)
    lon1 = max(p[1] for p in allp)

    z = 0
    # Esri's Dark Gray Canvas maxes out at z16; higher zooms return
    # placeholder tiles. Wide routes must also zoom out below z10 so
    # long drives or GPS outliers cannot allocate an enormous raster.
    for cand in range(16, -1, -1):
        x0, y0 = _merc(lon0, lat0, cand)
        x1, y1 = _merc(lon1, lat1, cand)
        if abs(x1 - x0) <= _MAP_W - 96 and abs(y1 - y0) <= _MAP_H - 96:
            z = cand
            break

    pad = 48
    gx0, gy0 = _merc(lon0, lat0, z)
    gx1, gy1 = _merc(lon1, lat1, z)
    # Small trips would stitch a tiny, blurry tile: pad out to a minimum
    # canvas so short hops still show street context crisply.
    padx = max(pad, (620 - abs(gx1 - gx0)) / 2)
    pady = max(pad, (420 - abs(gy1 - gy0)) / 2)
    ox = math.floor(min(gx0, gx1) - padx)
    oy = math.floor(min(gy0, gy1) - pady)
    ex = math.ceil(max(gx0, gx1) + padx)
    ey = math.ceil(max(gy0, gy1) + pady)
    cw, ch = ex - ox, ey - oy

    def pt(p):
        x, y = _merc(p[1], p[0], z)
        return x - ox, y - oy

    jpeg_bytes = None
    try:
        from PIL import Image
        import time as _time
        tiles = {}
        subs = "abc"
        i = 0
        for tx in range(ox // 256, ex // 256 + 1):
            for ty in range(oy // 256, ey // 256 + 1):
                cached = _tile_cache_get(z, tx, ty)
                if cached is not None:
                    tiles[(tx, ty)] = Image.open(
                        io.BytesIO(cached)).convert("RGB")
                    continue
                url = _TILE_URL.format(s=subs[i % len(subs)], z=z, x=tx, y=ty)
                i += 1
                req = urllib.request.Request(
                    url, headers={"User-Agent": _MAP_UA})
                # Tiles fail silently into the schematic fallback, so retry
                # transient blips instead of shipping a map-less dashboard.
                last_err = None
                for attempt in range(3):
                    try:
                        with urllib.request.urlopen(req, timeout=15) as r:
                            data = r.read()
                        _tile_cache_put(z, tx, ty, data)
                        tiles[(tx, ty)] = Image.open(
                            io.BytesIO(data)).convert("RGB")
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        _time.sleep(1.5 * (attempt + 1))
                if last_err is not None:
                    raise last_err
        canvas = Image.new("RGB", (cw, ch), (11, 13, 16))
        for (tx, ty), tile in tiles.items():
            canvas.paste(tile, (tx * 256 - ox, ty * 256 - oy))
        buf = io.BytesIO()
        # JPEG: the stitched street map is photographic content; at q75 it
        # is ~6x smaller than PNG with no visible difference.
        canvas.save(buf, "JPEG", quality=75, optimize=True)
        jpeg_bytes = buf.getvalue()
    except Exception as e:
        import sys as _sys
        print(f"WARNING: trip-map tiles failed ({e}); "
              f"dashboard ships schematic fallback", file=_sys.stderr)

    svg_parts = []
    for t, pts in paths:
        xy = [pt(p) for p in pts]
        pl = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
        s_lbl = _esc(t.get("start_label") or "start")
        e_lbl = _esc(t.get("end_label") or "end")
        tip = (f"{s_lbl} to {e_lbl}, {t['miles']:.1f} mi, "
               f"{t['kwh_used']:.2f} kWh")
        svg_parts.append(
            f'<polyline points="{pl}" fill="none" stroke="#7ed321" '
            f'stroke-width="3" stroke-opacity="0.9" stroke-linejoin="round" '
            f'stroke-linecap="round"><title>{_esc(tip)}</title></polyline>')
        sx, sy = xy[0]
        exx, eyy = xy[-1]
        svg_parts.append(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="6" fill="#7ed321" '
            f'stroke="#0b0d10" stroke-width="2">'
            f"<title>Start: {_esc(tip)}</title></circle>")
        svg_parts.append(
            f'<circle cx="{exx:.1f}" cy="{eyy:.1f}" r="6" fill="#7ed321" '
            f'stroke="#0b0d10" stroke-width="2">'
            f"<title>End: {_esc(tip)}</title></circle>")
        if t.get("via_lat") is not None:
            vx, vy = pt((t["via_lat"], t["via_lon"]))
            svg_parts.append(
                f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="6" fill="#f5a623" '
                f'stroke="#0b0d10" stroke-width="2">'
                f"<title>Stop: {_esc(t.get('via_label') or '')}</title>"
                "</circle>")
    svg = (f'<svg viewBox="0 0 {cw} {ch}" preserveAspectRatio="none">'
           + "".join(svg_parts) + "</svg>")
    return (jpeg_bytes, svg, cw, ch)


def _day_map_html(day: str, dtrips: list[dict]) -> str:
    """One day's trip map: lazy <img> to /maps/day-<day>.jpg + inline SVG.

    The raster tiles are written once per day (today regenerates every
    build since trips are still arriving, with a cache-busting query
    param so browsers never serve a stale today-map); the SVG route
    overlay is tiny and stays inline. Days without GPS get no map at
    all, never a placeholder.
    """
    parts = _trip_map_parts(dtrips)
    if parts is None:
        return ""
    jpeg_bytes, svg, cw, ch = parts
    demo = bool(os.environ.get("WATTSON_DEMO"))
    img = ""
    if jpeg_bytes is not None:
        if demo:
            # Single-file demo: embed the raster map, no sidecar files.
            import base64 as _b64
            url = ("data:image/jpeg;base64,"
                   + _b64.b64encode(jpeg_bytes).decode())
        else:
            _mp = "maps/" if demo else "/maps/"
            url = f"{_mp}day-{day}.jpg"
            maps_dir = REPORT_PATH.parent / "maps"
            maps_dir.mkdir(parents=True, exist_ok=True)
            png_path = maps_dir / f"day-{day}.jpg"
            today = _day(int(time.time()))
            if day == today or not png_path.exists():
                tmp = png_path.with_suffix(".tmp")
                tmp.write_bytes(jpeg_bytes)
                os.replace(tmp, png_path)
            if day == today:
                url += f"?v={int(time.time())}"
        img = (f'<img src="{url}" alt="Trip map" loading="lazy" '
               f'width="{cw}" height="{ch}" '
               f'class="mapimg" '
               f'onload="this.classList.add(\'ld\')" '
               f'onerror="this.classList.add(\'hidden\')">')
    # Schematic fallback when tiles failed: SVG routes on the dark card.
    return (f'<div class="mapwrap">{img}{svg}'
            f'<div class="mapattr">&copy; Esri &amp; contributors</div></div>')
# ---------------------------------------------------------------- sections

# ---------------------------------------------------------------- daily activity

_ACT_CLASS = {"moving": "st-moving", "charging": "st-charging",
              "parked": "st-parked", "sleep": "st-sleep",
              "climate": "st-climate", "unknown": "st-unknown"}
_ACT_LABEL = {"moving": "driving", "charging": "charging",
              "parked": "awake", "sleep": "deep sleep",
              "climate": "climate", "unknown": "no data"}


def _activity_bar(day_segs: list[dict], day_start: float,
                  now_min: float | None) -> str:
    """Thin 24h state strip: absolutely-positioned segments on a 1440-minute
    track. `now_min` draws the now-tick on the current (partial) day."""
    parts = []
    for s in day_segs:
        a = max(0.0, (s["start_ts"] - day_start) / 60)
        b = min(1440.0, (s["end_ts"] - day_start) / 60)
        if b <= a:
            continue
        tip = (f'{_fmt_time(s["start_ts"])}-{_fmt_time(s["end_ts"])} '
               f'{_ACT_LABEL.get(s["state"], s["state"])}')
        parts.append(
            f'<div class="aseg {_ACT_CLASS.get(s["state"], "st-unknown")}" '
            f'style="--l:{a / 14.4:.2f}%;--w:{(b - a) / 14.4:.2f}%" '
            f'title="{_esc(tip)}"></div>')
    if now_min is not None:
        parts.append(f'<div class="anow" style="--l:{now_min / 14.4:.2f}%">'
                     "</div>")
    return f'<div class="abar">{"".join(parts)}</div>'


def _activity_day_data(trips: list[dict], errands: list[dict],
                       segs: list[dict]) -> list[dict]:
    """Day groups for the Daily activity section as JSON-serializable dicts,
    newest first. Every day with tracker data gets a row (not just drive
    days) so the 24h bar can show a car that simply slept. Trip rows and the
    day map render inside the expanded day, as before."""
    now_ts = time.time()
    today = _day(int(now_ts))
    seg_by_day: dict[str, list[dict]] = defaultdict(list)
    for s in segs:
        seg_by_day[s["day"]].append(s)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in trips:
        by_day[_day(t["start_ts"])].append(t)
    err_by_day = {e["day"]: e for e in errands}
    days = sorted(set(list(by_day) + list(err_by_day) + list(seg_by_day)),
                  reverse=True)
    if not days:
        return []

    out = []
    demo = bool(os.environ.get("WATTSON_DEMO"))
    for day in days:
        dtrips = sorted(by_day.get(day, []), key=lambda t: t["start_ts"])
        err = err_by_day.get(day)
        mi = sum(t["miles"] for t in dtrips) + (err["miles"] if err else 0)
        kwh = (sum(t["kwh_used"] for t in dtrips)
               + (err["kwh_used"] if err else 0))
        n = len(dtrips) + (err["drives"] if err else 0)
        # Demo data ends yesterday; skip the empty "today" card so the
        # demo doesn't open on a "No drives" blank.
        if demo and day == today and n == 0:
            continue
        d = datetime.strptime(day, "%Y-%m-%d").date()
        day_start = datetime.combine(d, datetime.min.time(),
                                     tzinfo=LOCAL_TZ).timestamp()
        title = d.strftime("%a, %b %d")
        if day == today:
            title += ' <span class="dim">· today</span>'
            now_min = min(1440.0, (now_ts - day_start) / 60)
        else:
            now_min = None
        # Day-level efficiency is total miles over total kWh (same totals
        # as the summary), not an average of per-drive efficiencies.
        day_eff = f" · {mi / kwh:.2f} mi/kWh" if kwh > 0 else ""
        # Day temperature range across the day's trips (departure temps).
        dtemps = [t["temp_f"] for t in dtrips if t.get("temp_f") is not None]
        temp_range = ""
        if dtemps:
            lo, hi = min(dtemps), max(dtemps)
            temp_range = (f" · {lo:.0f}°F - {hi:.0f}°F" if hi > lo
                          else f" · {lo:.0f}°F")
        summary = ("No drives" if n == 0
                   else f"{n} drive{'s' if n != 1 else ''} · "
                        f"{mi:.1f} mi · {kwh:.2f} kWh{day_eff}{temp_range}")
        rows = []
        for t in dtrips:
            dur_min = (t["end_ts"] - t["start_ts"]) / 60
            dur = (f"~{dur_min:.0f} min" if t.get("bracketed")
                   else f"{dur_min:.0f} min")
            avg = t.get("avg_mph")
            avg_num = (f"~{avg:.0f}" if t.get("bracketed") and avg
                       else f"{avg:.0f}" if avg else "-")
            s_lbl, e_lbl = t["start_label"] or "?", t["end_label"] or "?"
            rt_tag = ('<span class="tag">round trip</span>'
                      if s_lbl == e_lbl and t["miles"] > 1 else "")
            via_bit = (f'<div class="trip-via">via {_esc(t["via_label"])}</div>'
                       if t.get("via_label") else "")
            rows.append(
                f'<div class="trip"><div class="trip-time">'
                f'{_fmt_time(t["start_ts"])} &rarr; {_fmt_time(t["end_ts"])}'
                f' <span class="dim">&middot; {dur}</span></div>'
                f'<div class="trip-route">{_esc(s_lbl)} &rarr; {_esc(e_lbl)}'
                f'{rt_tag}</div>{via_bit}'
                f'<div class="trip-stats"><span><b>{t["miles"]:.1f}</b> mi'
                f'</span><span><b>{t["kwh_used"]:.2f}</b> kWh</span>'
                f'<span><b>{_mi_per_kwh(t["wh_per_mi"])}</b> mi/kWh</span>'
                f'<span><b>{avg_num}</b> mph</span>'
                + (f'<span title="ambient temperature at departure">'
                   f'<b>{t["temp_f"]:.0f}</b> &deg;F</span>'
                   if t.get("temp_f") is not None else "")
                + (f'<span title="drive mode"><b>{_esc(t["drive_mode"])}</b></span>'
                   if t.get("drive_mode") else "")
                + f'</div></div>')
        if err:
            rows.append(
                f'<div class="trip"><div class="trip-time">Errands'
                f' <span class="dim">&middot; {err["drives"]} short drives'
                f'</span></div>'
                f'<div class="trip-stats"><span><b>{err["miles"]:.1f}</b> mi'
                f'</span><span><b>{err["kwh_used"]:.2f}</b> kWh</span>'
                f'</div></div>')
        if not rows:
            rows.append('<div class="trip"><div class="dim">'
                        "No drives recorded.</div></div>")
        # One street map per day, inside the expanded day: the map and the
        # trip rows are the same story. Days without GPS get no map at all,
        # never a placeholder.
        day_map = _day_map_html(day, dtrips)
        out.append({"day": day, "month": day[:7], "title": title,
                    "summary": summary,
                    "abar": _activity_bar(seg_by_day.get(day, []), day_start,
                                          now_min),
                    "body": f'<div class="abody">' + day_map + "".join(rows) + '</div>'})
    return out


def _activity_html(trips: list[dict], errands: list[dict],
                   segs: list[dict]) -> tuple[str, str]:
    """(month_select_html, activity_html) for the Daily activity section.
    Day groups ship as JSON; the page renders them client-side (latest 7
    first, "Show earlier" appends 7 more, month dropdown jumps)."""
    days = _activity_day_data(trips, errands, segs)
    if not days:
        return ("", '<div class="empty">No activity recorded yet.</div>')
    months: list[tuple[str, str]] = []
    for d in days:
        if not months or months[-1][0] != d["month"]:
            months.append(
                (d["month"],
                 datetime.strptime(d["month"], "%Y-%m").strftime("%B %Y")))
    opts = ('<option value="latest">Latest</option>' + "".join(
        f'<option value="{m}">{lbl}</option>' for m, lbl in months))
    select = (f'<select id="actmonth" class="monthsel" '
              f'aria-label="Jump to month">{opts}</select>')
    payload = json.dumps({"days": days}, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    html = ('<div id="actdays"></div>'
            '<button id="actshowmore" class="showmore">Show earlier</button>'
            f'<script type="application/json" id="actdata">{payload}'
            '</script>')
    return select, html


def _is_home(label: str | None, lat: float | None = None,
             lon: float | None = None) -> bool:
    """True when a charge location is home.

    The API's saved-location label ("HOME") wins; otherwise fall back to
    coords within HOME_RADIUS_M of the known home point.
    """
    if label and label.strip().lower() == "home":
        return True
    if lat is not None and lon is not None and not math.isnan(HOME_LAT):
        dx = (lon - HOME_LON) * 111320.0 * math.cos(math.radians(HOME_LAT))
        dy = (lat - HOME_LAT) * 110540.0
        return math.hypot(dx, dy) <= HOME_RADIUS_M
    return False


def _home_cost(kwh: float | None) -> float | None:
    """Session cost at the home off-peak rate; None when kWh unknown."""
    return round(kwh * HOME_KWH_RATE, 2) if kwh is not None else None


def _live_charge(snapshots: list[dict], now: int) -> dict | None:
    """Live charging stats while the car is actively charging, else None.

    Triggered by the API charger_state (charging_active), an explicit
    signal rather than an inference. Power comes from the SoC slope over
    recent charging polls; the first minutes are noisy while the BMS
    settles after wake, so the rate needs a few minutes of data.
    """
    if not snapshots or snapshots[-1].get("charger_state") != "charging_active":
        return None
    end_i = len(snapshots) - 1
    start_i = end_i
    while (start_i > 0
           and snapshots[start_i - 1].get("charger_state") == "charging_active"):
        start_i -= 1
    win = [s for s in snapshots[start_i:] if s.get("soc") is not None]
    if len(win) < 2:
        return None
    sess_start = win[0]["ts"]
    # Rate over the trailing ~15 minutes of charging.
    w = [s for s in win if s["ts"] >= max(now - 15 * 60, sess_start)]
    if len(w) < 2:
        w = win[-2:]
    a, b = w[0], w[-1]
    hours = (b["ts"] - a["ts"]) / 3600.0
    kw = None
    mihr = None
    if hours >= 3.0 / 60.0:
        dsoc = (b["soc"] or 0) - (a["soc"] or 0)
        if dsoc > 0:
            kw = dsoc / 100.0 * USABLE_KWH / hours
        ra, rb = a.get("range_mi"), b.get("range_mi")
        if ra is not None and rb is not None and rb > ra:
            mihr = (rb - ra) / hours
    soc_now = snapshots[-1].get("soc")
    rng_now = snapshots[-1].get("range_mi")
    kwh_added = None
    mi_added = None
    if soc_now is not None:
        kwh_added = max(0.0, (soc_now - win[0]["soc"]) / 100.0 * USABLE_KWH)
    r0, r1 = win[0].get("range_mi"), rng_now
    if r0 is not None and r1 is not None and r1 > r0:
        mi_added = r1 - r0
    end_txt = None
    if kw and kw > 0.5 and soc_now is not None and soc_now < CHARGE_TARGET_PCT:
        rem = (CHARGE_TARGET_PCT - soc_now) / 100.0 * USABLE_KWH
        end_txt = _fmt_time(int(now + rem / kw * 3600))
    return {
        "session_start": sess_start,
        "elapsed_min": (now - sess_start) / 60.0,
        "soc": soc_now, "soc_start": win[0]["soc"],
        "range_mi": rng_now,
        "kw": kw, "mihr": mihr,
        "kwh_added": kwh_added, "mi_added": mi_added,
        "end_txt": end_txt,
    }


def _live_charge_html(live: dict, is_home: bool = False) -> str:
    soc = live["soc"]
    base = live.get("soc_start") or 0.0
    tgt = CHARGE_TARGET_PCT
    # Stacked ring: green = battery at plug-in, orange = added this
    # session, dark gray = empty to 100%.
    added_frac = max(0.0, ((soc or 0.0) - base) / 100.0)
    small = (f'+{live["kwh_added"]:.1f} kWh'
             if live.get("kwh_added") is not None else "")
    tiny = (f'+{live["mi_added"]:.0f} mi'
            if live.get("mi_added") is not None else "")
    donut = _donut_segments(
        [(base / 100.0, GREEN), (added_frac, AMBER)],
        f"{soc:.0f}%" if soc is not None else "-", small, tiny)
    legend = (
        '<div class="alegend alegend-c">'
        f'<span><i class="dot-g"></i>plug-in {base:.0f}%</span>'
        f'<span><i class="dot-a"></i>added</span>'
        f'<span><i class="dot-n"></i>empty</span></div>')
    kw_v = f'{live["kw"]:.1f}<small> kW</small>' if live.get("kw") else "-"
    kw_s = f'{live["mihr"]:.0f} mi/hr' if live.get("mihr") else "warming up"
    sess_s = f'since {_fmt_time(int(live["session_start"]))}'
    note_txt = (f'{tgt:.0f}% by about {live["end_txt"]} at this rate'
                if live.get("end_txt") else "")
    sub_html = (f'<div class="sub centered">{note_txt}</div>'
                if note_txt else "")
    cost_stat = ""
    if is_home:
        cost = _home_cost(live.get("kwh_added"))
        if cost is not None:
            cost_stat = (
                f'<div class="stat"><div class="k">Cost</div>'
                f'<div class="v">${cost:.2f}</div>'
                f'<div class="s">so far &middot; home rate</div></div>')
    return (
        f'<div class="card">'
        f'<div class="chlive"><div class="chlive-main">{donut}{legend}</div>'
        f'<div class="chlive-side">'
        f'<div class="stat"><div class="k">Power</div>'
        f'<div class="v">{kw_v}</div><div class="s">{kw_s}</div></div>'
        f'<div class="stat"><div class="k">This session</div>'
        f'<div class="v">{live["elapsed_min"]:.0f}<small> min</small></div>'
        f'<div class="s">{sess_s}</div></div>'
        f'{cost_stat}'
        f'</div></div>'
        f'{sub_html}</div>')


def _charge_month_summary(hist: list[dict]) -> str:
    """One-line rollup for the latest month that has sessions."""
    groups: dict[str, list[dict]] = {}
    for c in hist:
        groups.setdefault(_ldt(c["start_ts"]).strftime("%Y-%m"), []).append(c)
    if not groups:
        return ""
    chs = groups[max(groups)]
    month = _ldt(chs[0]["start_ts"]).strftime("%B")
    kwh = sum(c["kwh_added"] for c in chs)
    mi = sum(c.get("miles_added") or 0 for c in chs)
    n = len(chs)
    return (
        f'<div class="sub m0204">{month} &middot; '
        f'{n} session{"s" if n != 1 else ""} &middot; '
        f'{kwh:.1f} kWh &middot; {mi:.0f} mi</div>')


def _get_tile(z: int, tx: int, ty: int):
    """One map tile as a PIL RGB image: disk cache first, else network.

    Same Esri dark tiles and cache the trip map uses. Returns None when
    the tile cannot be fetched after retries.
    """
    import io
    import urllib.request
    import time as _time
    from PIL import Image
    cached = _tile_cache_get(z, tx, ty)
    if cached is not None:
        return Image.open(io.BytesIO(cached)).convert("RGB")
    url = _TILE_URL.format(z=z, y=ty, x=tx)
    req = urllib.request.Request(url, headers={"User-Agent": _MAP_UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            _tile_cache_put(z, tx, ty, data)
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            _time.sleep(1.5 * (attempt + 1))
    return None


def _render_point_map(lat: float, lon: float, z: int = 16):
    """Stitch the charge-location tile window. Returns JPEG bytes, or None
    when any tile can't be fetched."""
    import io
    from PIL import Image, ImageDraw
    W, H = 512, 256
    gx, gy = _merc(lon, lat, z)
    # Tile range covering the centered window, with one tile of slack
    # so float rounding can never leave an edge uncovered.
    tx0 = math.floor((gx - W / 2) / 256) - 1
    ty0 = math.floor((gy - H / 2) / 256) - 1
    ntx = math.ceil(W / 256) + 2
    nty = math.ceil(H / 256) + 2
    canvas = Image.new("RGB", (ntx * 256, nty * 256), (11, 13, 16))
    for tx in range(tx0, tx0 + ntx):
        for ty in range(ty0, ty0 + nty):
            tile = _get_tile(z, tx, ty)
            if tile is None:
                return None
            canvas.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
    left = int(round(gx - W / 2 - tx0 * 256))
    top = int(round(gy - H / 2 - ty0 * 256))
    img = canvas.crop((left, top, left + W, top + H))
    d = ImageDraw.Draw(img)
    r = 11
    cx, cy = W / 2, H / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              fill=(245, 166, 35), outline=(11, 13, 16), width=3)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=75, optimize=True)
    return buf.getvalue()


def _point_map_html(lat: float | None, lon: float | None,
                    label: str, name: str) -> str:
    """Charge-location map as a lazy <img> to /maps/<name>.jpg.

    Written once (charge history is immutable); any tile failure ships
    no map at all, as before.
    """
    if lat is None or lon is None:
        return ""
    maps_dir = REPORT_PATH.parent / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    png_path = maps_dir / f"{name}.jpg"
    demo = bool(os.environ.get("WATTSON_DEMO"))
    jpeg_bytes = None
    if png_path.exists():
        jpeg_bytes = png_path.read_bytes()
    else:
        try:
            jpeg_bytes = _render_point_map(lat, lon)
        except Exception:
            jpeg_bytes = None
        if jpeg_bytes is None:
            return ""
        if not demo:
            tmp = png_path.with_suffix(".tmp")
            tmp.write_bytes(jpeg_bytes)
            os.replace(tmp, png_path)
    if demo:
        import base64 as _b64
        src = "data:image/jpeg;base64," + _b64.b64encode(jpeg_bytes).decode()
    else:
        src = f"/maps/{name}.jpg"
    return (
        f'<div class="chargemap"><img src="{src}" '
        f'alt="Charging location: {_esc(label)}" loading="lazy" '
        f'width="512" height="256" '
        f'onload="this.classList.add(\'ld\')" '
        f'onerror="this.closest(\'.chargemap\').classList.add(\'hidden\')">'
        f'</div>')
def _session_miles(c: dict, eff: float) -> float:
    """Miles for the gas comparison: prefer BMS miles_added, else kwh*eff."""
    m = c.get("miles_added") or 0
    if m > 0:
        return float(m)
    return float(c.get("kwh_added") or 0) * eff


def _charge_row_html(c: dict, scale: float, open_: bool,
                     eff: float = 3.0) -> str:
    """One expandable charging-log row: summary plus detail."""
    ma = (f"{c['miles_added']:.0f} mi"
          if c.get("miles_added") is not None else "-")
    tag = (f'<span class="tag">{_esc(c["charge_type"])}</span>'
           if c.get("charge_type") else "")
    donut = _donut(c["kwh_added"] / scale, AMBER,
                   f"{c['kwh_added']:.1f}", "kWh")
    cost_bit = ""
    if _is_home(c.get("label"), c.get("lat"), c.get("lon")):
        cost = _home_cost(c.get("kwh_added"))
        if cost is not None:
            cost_bit = f'<span><b>${cost:.2f}</b> cost</span>'
    savings_bit = ""
    _ctype = (c.get("charge_type") or "").lower()
    # DC-first, matching generate(): a DC session at home still counts as DC.
    _row_home = _is_home(c.get("label"), c.get("lat"), c.get("lon")) \
        and "dc" not in _ctype
    try:
        import gasprice as _gp2
        _gp_val = _gp2.get_gas_price(_day(c["start_ts"]))
    except Exception:
        _gp_val = GAS_PRICE_FALLBACK
    if _row_home:
        _kwh2 = c.get("kwh_added") or 0
        _miles2 = _session_miles(c, eff)
        if _kwh2 > 0 and _miles2 > 0:
            _ev_c = _kwh2 * HOME_KWH_RATE
            _gas_c = _miles2 / GAS_COMPARISON_MPG * _gp_val
            _save = _gas_c - _ev_c
            _cls = "green" if _save >= 0 else "amber"
            _verb = "saved" if _save >= 0 else "lost"
            savings_bit = ('<span><b class="%s">$%.2f</b> %s '
                           '<span class="dim">($%.2f/gal)</span></span>'
                           % (_cls, abs(_save), _verb, _gp_val))
    elif "dc" in _ctype:
        _dc_rate = c.get("user_cost_per_kwh")
        _kwh3 = c.get("kwh_added") or 0
        _miles3 = _session_miles(c, eff)
        if _dc_rate is not None and _kwh3 > 0 and _miles3 > 0:
            _ev_c3 = _kwh3 * _dc_rate
            _gas_c3 = _miles3 / GAS_COMPARISON_MPG * _gp_val
            _save3 = _gas_c3 - _ev_c3
            _cls3 = "green" if _save3 >= 0 else "amber"
            _verb3 = "saved" if _save3 >= 0 else "lost"
            savings_bit = ('<span><b class="%s">$%.2f</b> %s '
                           '<span class="dim">($%.2f paid &middot; $%.2f/gal, '
                           '$%.2f/kWh)</span> '
                           '<a href="#" onclick="event.stopPropagation();'
                           'openCostModal(%d, %.2f, %d);return false;" '
                           'class="cost-action green tiny">edit</a></span>'
                           % (_cls3, abs(_save3), _verb3, _ev_c3, _gp_val,
                              _dc_rate, c["id"], _dc_rate, c["start_ts"]))
        else:
            savings_bit = ('<span><a href="#" onclick="event.stopPropagation();'
                           'openCostModal(%d, 0, %d);return false;" '
                           'class="cost-action green">? add cost</a></span>'
                           % (c["id"], c["start_ts"]))
    cap = (f'{c["soc_start"]:.0f}% &rarr; {c["soc_end"]:.0f}% &middot; '
           f'{c["avg_kw"] or "-"} kW avg')
    mmap = _point_map_html(c.get("lat"), c.get("lon"), c.get("label") or "", f"charge-{c['start_ts']}")
    open_attr = " open" if open_ else ""
    return (
        f'<details class="chargerow sacc"{open_attr}><summary>'
        f'<div class="trip-time">{_fmt_ts(c["start_ts"])}'
        f' <span class="dim">&middot; {c["minutes"]:.0f} min</span>{tag}</div>'
        f'<div class="trip-route">{_esc(c["label"] or "Unknown location")}</div>'
        f'<div class="trip-stats">'
        f'<span><b>{c["kwh_added"]:.1f}</b> kWh</span>'
        f'{cost_bit}'
        f'{savings_bit}'
        f'<span><b>{ma}</b> gained</span>'
        f'<span><b>{c["avg_kw"] or "-"}</b> kW avg</span>'
        f'<span><b>{c["soc_start"]:.0f}%</b> &rarr; '
        f'<b>{c["soc_end"]:.0f}%</b></span>'
        f'</div></summary>'
        f'<div class="chargedetail"><div class="cdin">{donut}'
        f'<div class="donut-cap">{cap}</div>'
        f'{mmap}'
        f'<div class="sub">{_fmt_time(c["start_ts"])} &rarr; '
        f'{_fmt_time(c["end_ts"])}</div>'
        f'</div></div></details>')


def _charges_html(charges: list[dict], live: dict | None = None,
                  live_is_home: bool = False, eff: float = 3.0,
                  orphans=(), ambiguous=()) -> str:
    # The in-progress session is shown in the live card; keep it out of
    # the history list so it is not shown twice.
    hist = [ch for ch in charges
            if not (live and ch["end_ts"] >= live["session_start"])]
    orphan_bit = _orphan_note_html(charges, orphans, ambiguous)
    if not hist and not live:
        return ('<div class="empty">No charging sessions yet.<br>Ping me when '
                'you plug in and I will track the session.</div>'
                + orphan_bit)
    parts = []
    if live:
        parts.append(_live_charge_html(live, live_is_home))
    if hist:
        scale = max(ch["kwh_added"] for ch in hist) * 1.15 or 1.0
        # Every row starts collapsed, including the latest: the open
        # first row made the dashboard too long.
        rows = "".join(_charge_row_html(c, scale, open_=False, eff=eff)
                       for c in hist)
        export = ("" if os.environ.get("WATTSON_DEMO") else
                  '<a href="/api/charges.csv" '
                  'class="dim14">Export CSV</a>')
        parts.append(
            f'<div class="card"><h3 class="row"><span>Charging log</span>'
            f'{export}</h3>'
            f'{_charge_month_summary(hist)}'
            f'{rows}</div>')
    footnote = ('<p class="note">Home charging costed at $%.2f/kWh '
                '(set RIVIAN_HOME_KWH_RATE in .env). Away sessions show no '
                'cost; public rates vary. Tap "? add cost" on a DC fast '
                'charge to record what you paid.</p>' % HOME_KWH_RATE)
    return "".join(parts) + orphan_bit + footnote


def _orphan_note_html(charges, orphans, ambiguous) -> str:
    """Orphan/ambiguity note plus the small reassign form, or "".

    Kept separate so the note also renders when no sessions exist at
    all (otherwise orphaned prices would be invisible exactly when
    every session is gone).
    """
    if not orphans and not ambiguous:
        return ""
    lines = []
    for ts, cost in orphans:
        lines.append(f"{_day(ts)} &middot; ${cost:.2f}/kWh has no "
                     f"matching session any more.")
    for ts in sorted(ambiguous):
        lines.append(f"{_day(ts)} is shared by several sessions, so "
                     f"its saved price is held back from all of them.")
    form = ""
    if orphans:
        # Move targets: sessions shown here with no price yet. The
        # server revalidates everything, so this list is convenience
        # only, never authority.
        priced = {c["start_ts"] for c in charges
                  if c.get("user_cost_per_kwh") is not None}
        target_opts = "".join(
            f'<option value="{c["start_ts"]}">'
            f'{_day(c["start_ts"])} &middot; '
            f'{_esc(c.get("label") or c.get("charge_type") or "")} '
            f'&middot; {c.get("kwh_added") or 0:.1f} kWh</option>'
            for c in charges
            if c["start_ts"] not in priced
            and c["start_ts"] not in ambiguous)
        orphan_opts = "".join(
            f'<option value="{ts}">{_day(ts)} &middot; '
            f'${cost:.2f}/kWh</option>' for ts, cost in orphans)
        move_bit = ""
        if target_opts:
            move_bit = (
                f'<select name="target_start_ts">{target_opts}</select>'
                '<button type="submit" name="action" value="move" '
                'class="btn-primary">Move price</button>')
        form = (
            '<form method="POST" action="/api/charge-price-reassign" '
            'class="m0">'
            f'<select name="orphan_start_ts">{orphan_opts}</select>'
            + move_bit +
            '<button type="submit" name="action" value="remove" '
            'class="btn-ghost">Remove orphan</button></form>')
    return ('<div class="card"><p class="note">' +
            "<br>".join(lines) + "</p>" + form + "</div>")


def _fmt_money(v: float, decimals: int = 0) -> str:
    """$12 / -$12 (never $-12)."""
    s = ("$%d" if decimals == 0 else "$%." + str(decimals) + "f") % abs(v)
    return ("-" if v < 0 else "") + s


def _gas_subtitle(rows: list[dict], unpriced_dc: int = 0) -> str:
    """Included home/DC sessions and any unpriced DC exclusions."""
    n_home = sum(1 for r in rows if not r.get("is_dc"))
    n_dc = sum(1 for r in rows if r.get("is_dc"))
    parts = []
    if n_home:
        parts.append("%d home session%s" % (n_home, "" if n_home == 1 else "s"))
    if n_dc:
        parts.append("%d DC session%s" % (n_dc, "" if n_dc == 1 else "s"))
    if unpriced_dc:
        parts.append("%d unpriced DC session%s excluded" %
                     (unpriced_dc, "" if unpriced_dc == 1 else "s"))
    return "<br>".join(parts)


def _idle_table(periods: list[dict], bad: set) -> str:
    """One row per idle period, newest first.

    Mirrors the chart bars 1:1. Rates are measured between fresh
    cloud-sync anchors (see _idle_history); plugged-in periods show no
    rate because the car can top itself off. The * marker uses the same
    1.6x-median baseline as the chart's red bars. A ~ marks periods
    whose battery drop is close to what the gauge can resolve; periods
    at or under one gauge step (0.1%) print no rate at all.
    """
    if not periods:
        return '<p class="dim">No idle periods measured yet.</p>'
    rows = []
    for o in reversed(periods):
        warn = ' <span class="warn">*</span>' if o["id"] in bad else ""
        label = o["label"] + warn
        if o["plugged"]:
            label += ' <span class="dim">(plugged in)</span>'
        batt = (f"{o['soc_start']:.1f}% &rarr; {o['soc_end']:.1f}%"
                if o["soc_start"] is not None and o["soc_end"] is not None
                else "-")
        if o["measured"]:
            tilde = "~" if o["approx"] else ""
            drop_txt = (f"{o['soc_drop']:.1f}%"
                        if o["soc_drop"] is not None else "-")
            lost = f"{o['kwh']:.2f} kWh &bull; {drop_txt}"
            if o.get("substep"):
                watts_txt = "below one gauge step"
                pct_txt = "-"
            else:
                watts_txt = f"{tilde}{o['watts']:.0f} W"
                pct_txt = f"{tilde}{o['pct_day']:.1f}%"
        elif o["plugged"]:
            lost = watts_txt = pct_txt = "-"
        else:
            lost = watts_txt = pct_txt = "no data"
        dur = f"{o['hours']:.1f} h"
        if o.get("reports"):
            dur += (f" &middot; {o['reports']} "
                    f"report{'s' if o['reports'] != 1 else ''}")
        sl = o.get("sleep") or {}
        on_h, un_h = sl.get("online_h"), sl.get("unknown_h")
        if on_h is not None and on_h >= 0.5:
            dur += f" &middot; awake ~{on_h:.1f} h"
        elif on_h is not None and un_h is not None:
            tot_h = (sl.get("quiet_h") or 0) + on_h + un_h
            if tot_h and un_h <= 0.25 * tot_h:
                dur += " &middot; asleep throughout"
        rows.append(
            f"<tr><td>{label}</td>"
            f"<td>{dur}</td>"
            f"<td>{batt}</td>"
            f"<td>{lost}</td>"
            f"<td>{watts_txt}</td>"
            f"<td>{pct_txt}</td></tr>")
    note = ('<p class="note"><span class="warn">*</span> unusually high vs '
            'baseline.</p>' if bad else '')
    return ('<div class="xscroll"><table class="drain">'
            '<tr><th>Idle period</th><th>Duration</th>'
            '<th>Battery</th><th>Lost</th><th>Avg draw</th>'
            '<th>%/day</th></tr>'
            + "".join(rows) + "</table></div>" + note)



def _open_park_estimate(d: dict, snapshots) -> dict:
    """Running estimate for the still-open park (car parked right now).

    The open span has a start anchor (the car's last report after
    parking) but no end anchor, so it can never be measured the way a
    closed span is. What it can honestly show: how long the car has
    been parked, how much charge has gone so far (start anchor vs the
    latest reading), and the daily rate that implies if it holds.
    Rates are withheld while plugged in (top-offs mask drain) and
    until the drop clears a few gauge steps; small drops wear a ~,
    matching the closed-span convention. A real gain while parked
    means it charged, whatever the polls caught, so no drop is shown.
    """
    since = d["sleep_ts"]
    soc0 = d.get("soc_start")
    win = [s for s in snapshots if s["ts"] >= since]
    last = max(win, key=lambda s: s["ts"]) if win else None
    soc_now = last.get("soc") if last else None
    last_ts = last["ts"] if last else since
    elapsed_h = max(0.0, (last_ts - since) / 3600.0)
    plugged = any((s.get("charger_state") or "") in
                  ("charging_active", "charging_complete",
                   "charging_scheduled", "charging_user_stopped")
                  for s in win)
    drop = None
    approx = False
    if soc0 is not None and soc_now is not None:
        drop = soc0 - soc_now
        if drop < -0.15:
            plugged = True
            drop = None
        elif drop < 0:
            drop = 0.0
            approx = True
        elif drop <= 0.300001:
            approx = True
    pct_day = None
    if drop is not None and not plugged and elapsed_h >= 2 and drop > 0.300001:
        pct_day = round(drop / (elapsed_h / 24.0), 2)
    return {
        "since_ts": since,
        "soc": soc0,
        "soc_now": soc_now,
        "elapsed_h": round(elapsed_h, 1),
        "drop": (round(drop, 1) if drop is not None else None),
        "approx": approx,
        "plugged": plugged,
        "pct_day": pct_day,
        "sleep": _sleep_share(snapshots, since, last_ts, elapsed_h),
    }


def _idle_history(drains: list[dict], snapshots) -> tuple[list[dict], dict | None]:
    """Idle periods between drives, oldest first, plus the open park.

    Drain rows come from analyze.py, measured between fresh cloud-sync
    anchors (the car's last report after parking, its first report on
    waking). Periods that were plugged in, or that lack both anchors,
    carry no rate. The still-open park (car parked right now) is
    returned separately with a running estimate: it has no end anchor
    yet, so it cannot be measured, only tracked so far. Each period
    also carries a sleep share (how much of the park the car spent
    awake, from the cloud-online signal) so a high drain with a mostly
    awake car reads differently from the same drain asleep.
    """
    out = []
    open_park = None
    from bisect import bisect_left, bisect_right
    all_syncs = sorted({s["cloud_sync_ts"] for s in snapshots
                        if s.get("cloud_sync_ts")})
    for d in drains:
        if d.get("open_span"):
            open_park = _open_park_estimate(d, snapshots)
            continue
        t1, t2 = d["sleep_ts"], d["wake_ts"]
        measured = bool(d.get("measured"))
        plugged = bool(d.get("plugged_in"))
        a1, a2 = d.get("anchor_start_ts"), d.get("anchor_end_ts")
        w1, w2 = (a1, a2) if (measured and a1 and a2) else (t1, t2)
        d1, d2 = _ldt(t1), _ldt(t2)
        if d1.date() == d2.date():
            label = d1.strftime("%b %d")
        else:
            label = f"{d1.strftime('%b %d')}-{d2.strftime('%d')}"
        s0, s1 = d.get("soc_start"), d.get("soc_end")
        # A span whose whole drop fits inside one battery-gauge step
        # (0.1%) cannot support a rate: present, but unmeasurable.
        substep = bool(measured and s0 is not None and s1 is not None
                       and (s0 - s1) <= 0.100001)
        # Fresh car reports (distinct cloud syncs) between the anchors:
        # how many readings the span's number actually rests on.
        reports = None
        if measured and a1 and a2 and all_syncs:
            n = bisect_right(all_syncs, a2) - bisect_left(all_syncs, a1)
            reports = n or None
        out.append({
            "id": t1,  # unique key for abnormal-period flagging
            "day": d2.strftime("%Y-%m-%d"),  # for heat-strip positioning
            "label": label,
            "hours": round(d.get("hours") or 0, 1),
            "kwh": round(d.get("kwh_lost") or 0, 3),
            "watts": round(d.get("watts_avg") or 0, 1),
            "pct_day": d.get("pct_per_day"),
            "soc_start": s0, "soc_end": s1,
            # analyze clamps a parked gain (wake recalibration) to zero
            # in its stored numbers; the displayed drop agrees with it.
            "soc_drop": (round(max(0.0, s0 - s1), 1)
                         if s0 is not None and s1 is not None else None),
            "measured": measured,
            "plugged": plugged,
            "approx": bool(d.get("approx")),
            "substep": substep,
            "reports": reports,
            "no_data": not measured,
            "window": f"{_fmt_ts(w1)} \u2192 {_fmt_ts(w2)}",
            "t1": t1, "t2": t2,
            "wake_count": 0,
            "sleep": _sleep_share(snapshots, t1, t2, (t2 - t1) / 3600.0),
        })
    return out, open_park




def _sleep_share(snapshots, t1: int, t2: int, hrs: float) -> dict:
    """Share of a parked window the car spent quiet (cloud offline, our best
    deep-sleep proxy) vs online vs unknown. cloud_online is null on polls
    from before we started storing it."""
    win = [s for s in snapshots if t1 <= s["ts"] <= t2]
    if not win or not hrs:
        return {"quiet_h": None, "online_h": None, "unknown_h": None}
    nq = sum(1 for s in win if s.get("cloud_online") == 0)
    no = sum(1 for s in win if s.get("cloud_online") == 1)
    nu = len(win) - nq - no
    return {"quiet_h": round(hrs * nq / len(win), 1),
            "online_h": round(hrs * no / len(win), 1),
            "unknown_h": round(hrs * nu / len(win), 1)}


def _tire_tile_html(con, now):
    """Tires tile for the hero 2x2 grid. Footprint layout, quiet when fine."""
    try:
        import tires as _tires
        _tires.init_tire_tables(con)
        row = con.execute(
            "SELECT ts, fl, fr, rl, rr FROM tire_readings"
            " ORDER BY ts DESC LIMIT 1").fetchone()
        flags = dict(con.execute(
            "SELECT tire, low FROM tire_alert_state"))
    except Exception:
        return ""
    if row is None:
        ts = None
        bar = {"fl": None, "fr": None, "rl": None, "rr": None}
    else:
        try:
            ts, fl, fr, rl, rr = row
            bar = {"fl": fl, "fr": fr, "rl": rl, "rr": rr}
        except Exception:
            return ""
    psi = {p: (None if bar[p] is None else bar[p] * 14.5038)
           for p in ("fl", "fr", "rl", "rr")}
    stale = ts is not None and (now - ts) > 24 * 3600

    labels = {"fl": "FL", "fr": "FR", "rl": "RL", "rr": "RR"}
    cells = []
    for p in ("fl", "fr", "rl", "rr"):
        v = psi[p]
        txt = "%.1f" % v if v is not None else "--"
        lvl = int(flags.get(p) or 0)
        cls = "tc lowv" if lvl >= 2 else ("tc warnv" if lvl == 1 else "tc")
        cells.append('<span class="%s"><i>%s</i><b>%s</b></span>'
                     % (cls, labels[p], txt))
    if ts is None:
        sub = "no reading yet"
    elif stale:
        days = (now - ts) // 86400
        sub = "%dd ago" % days if days >= 1 else "yesterday"
    else:
        sub = ""
    sub_html = '<div class="s">%s</div>' % sub if sub else ""
    return ('<div class="stat"><div class="k">Tires</div>'
            '<div class="tire4">%s</div>%s</div>'
            % ("".join(cells), sub_html))


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_text(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def _styles_markup() -> str:
    """Dashboard CSS as it goes into the page: a <link> to
    static/dashboard.css for the served dashboard. Demo builds
    inline the file instead, so the demo stays one self-contained
    page."""
    if os.environ.get("WATTSON_DEMO"):
        return "<style>\n" + _asset_text("dashboard.css") + "</style>"
    return '<link rel="stylesheet" href="/dashboard.css">'


def _scripts_markup() -> str:
    """Dashboard JS as it goes into the page: a <script src> to
    static/dashboard.js for the served dashboard; inlined by demo
    builds, like the CSS."""
    if os.environ.get("WATTSON_DEMO"):
        # The flag tells the nav modal to run in demo mode:
        # no server behind the page, so places and search show
        # explanatory notes instead of calling the API.
        return ("<script>\nwindow.WATTSON_DEMO = true;\n"
                + _asset_text("dashboard.js") + "</script>")
    return '<script src="/dashboard.js"></script>'


def generate() -> None:
    try:
        tokens = unseal(TOKENS_PATH.read_bytes())
        vehicle_name = (tokens.get("vehicle_name") or VEHICLE_NAME
                        or "My Rivian")
    except Exception:
        vehicle_name = VEHICLE_NAME or "My Rivian"
    brand = f"{vehicle_name} - {VEHICLE_MODEL}" if VEHICLE_MODEL else vehicle_name
    _is_demo = bool(os.environ.get("WATTSON_DEMO"))
    demopill = ('<span class="demo-pill">Demo data</span>'
                if _is_demo else "")

    now = int(time.time())

    # Hero car cutout, embedded as a data URI so the page stays fully
    # self-contained. Missing asset -> no img tag, layout holds.
    car_tag = ""
    car_path = Path(__file__).resolve().parent / "assets" / "hero.webp"
    if car_path.exists():
        car_img = ("data:image/webp;base64,"
                   + base64.b64encode(car_path.read_bytes()).decode("ascii"))
        car_tag = (f'<img class="hero-car" src="{car_img}" '
                   f'alt="{vehicle_name}">')

    with connect() as con:
        try:
            tiretile = _tire_tile_html(con, now)
        except Exception:
            tiretile = ""

        latest = con.execute(
            "SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        snapshots = [dict(r) for r in con.execute(
            "SELECT ts, soc, capacity_kwh, range_mi, cloud_online, charger_state"
            ", cloud_sync_ts FROM snapshots ORDER BY ts")]
        trips = [dict(r) for r in con.execute(
            "SELECT * FROM trips ORDER BY start_ts DESC LIMIT 60")]
        errands = [dict(r) for r in con.execute(
            "SELECT * FROM errands ORDER BY day DESC LIMIT 30")]
        charges = [dict(r) for r in con.execute(
            "SELECT * FROM charges ORDER BY start_ts DESC LIMIT 20")]
        # Idle-drain events between drives (from analyze.py _drain_from_run)
        drains = [dict(r) for r in con.execute(
            "SELECT * FROM drains ORDER BY sleep_ts")]
        # All charges for lifetime savings (no LIMIT: the log shows 20).
        all_charges = [dict(r) for r in con.execute(
            "SELECT * FROM charges ORDER BY start_ts DESC")]
        # Prices resolve once, here, from the annotation table: the
        # charge dicts below (rows, per-session savings, lifetime
        # savings) all read the same key, and the CSV export resolves
        # through the same helper, so no surface can disagree.
        _prices, _orphans, _ambiguous = charge_price_info(
            con, [c["start_ts"] for c in all_charges])
        for _c in charges:
            _c["user_cost_per_kwh"] = _prices.get(_c["start_ts"])
        for _c in all_charges:
            _c["user_cost_per_kwh"] = _prices.get(_c["start_ts"])
        agg = con.execute(
            "SELECT SUM(miles) m, SUM(kwh_used) k FROM trips").fetchone()

    latest = dict(latest) if latest else {}
    soc = latest.get("soc")
    rng = latest.get("range_mi")
    odo_raw = latest.get("odometer")
    odo_mi = (odo_raw * ODOMETER_METERS_PER_UNIT / 1609.344
              if odo_raw is not None else None)

    # Location: API geo label wins (HOME), else cached street, else coords.
    loc = latest.get("geo_label")
    if not loc and latest.get("lat") is not None:
        import geocode
        loc = geocode.label(latest.get("lat"), latest.get("lon")) \
            or "Unknown location"

    # Lifetime efficiency
    lifetime_mpk = ((agg["m"] / agg["k"])
                    if agg and agg["m"] and agg["k"] else None)

    # Per-session fuel savings vs a gas SUV (home + priced DC sessions).
    # Uses each charge day's gas price and the car's lifetime efficiency
    # for the miles conversion, so rows and hero always agree.
    try:
        import gasprice as _gp
    except Exception:
        _gp = None
    _savings_rows = []
    _savings_total = 0.0
    _unpriced_dc = 0
    _eff = lifetime_mpk if lifetime_mpk else 3.0
    for ch in all_charges:
        ctype = (ch.get("charge_type") or "").lower()
        is_home = _is_home(ch.get("label"), ch.get("lat"), ch.get("lon")) \
            and "dc" not in ctype
        is_dc = "dc" in ctype
        if not is_home and not is_dc:
            continue
        if is_home:
            rate = HOME_KWH_RATE
        else:
            rate = ch.get("user_cost_per_kwh")
            if rate is None:
                _unpriced_dc += 1
                continue  # DC without an entered cost: excluded from total
        kwh = ch.get("kwh_added") or 0
        if kwh <= 0:
            continue
        ch_date = _day(ch["start_ts"])
        try:
            gp = _gp.get_gas_price(ch_date) if _gp else GAS_PRICE_FALLBACK
        except Exception:
            gp = GAS_PRICE_FALLBACK
        ev_cost = kwh * rate
        miles = _session_miles(ch, _eff)
        gas_cost = miles / GAS_COMPARISON_MPG * gp
        save = gas_cost - ev_cost
        _savings_total += save
        _savings_rows.append({
            "date": ch_date, "kwh": kwh, "ev_cost": ev_cost,
            "gas_price": gp, "gas_cost": gas_cost, "save": save,
            "is_dc": is_dc,
        })
    if _savings_rows:
        _srows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%.1f</td><td>$%.2f</td>"
            "<td>$%.2f</td><td>$%.2f</td>"
            '<td class="%s">%s</td></tr>' % (
                r["date"], "DC" if r["is_dc"] else "AC", r["kwh"], r["ev_cost"],
                r["gas_price"], r["gas_cost"],
                "green" if r["save"] >= 0 else "amber",
                _fmt_money(r["save"], 2))
            for r in _savings_rows[:10]
        )
        savings_table = (
            '<div class="card mt16">'
            "<h3>Fuel savings by session</h3>"
            '<div class="sub">Per charging session, priced at each '
            'day&apos;s gas price</div>'
            '<div class="xscroll"><table class="ctab"><thead><tr>'
            "<th>Date</th><th>Type</th><th>kWh</th><th>EV cost</th>"
            "<th>Gas $/gal</th><th>Gas cost</th><th>Saved</th>"
            "</tr></thead><tbody>" + _srows + "</tbody></table></div></div>"
        )
    else:
        savings_table = (
            '<div class="card mt16">'
            "<h3>Fuel savings by session</h3>"
            '<p class="note">'
            + ("Enter a DC price in the charging log to compare those sessions."
               if _unpriced_dc else
               "No charges recorded yet to compare against gas.")
            + "</p>"
            "</div>"
        )
        _savings_total = 0.0
    savings_table += (
        '<p class="note">Comparison assumes %g MPG; $%.2f/gal is used '
        'when a gas price is unavailable.</p>' %
        (GAS_COMPARISON_MPG, GAS_PRICE_FALLBACK))

    # Battery trend: end-of-day SoC series
    eod_series = []
    seen = {}
    for s in snapshots:
        seen[_day(s["ts"])] = s["soc"]
    for day in sorted(seen):
        eod_series.append((_ldt_str(day), seen[day]))
    n_all = len(eod_series)
    batt_note = (f"{n_all} day{'s' if n_all != 1 else ''} so far"
                 if n_all else "no data yet")
    batt_charts = {}
    for n in (7, 14, 30):
        batt_charts[n] = (_battery_svg(eod_series[-n:])
                          if eod_series else "")

    # Pack health: pack capacity (the BMS's kWh estimate) vs implied full
    # range (the car's range estimate / SoC). Both lines are indexed to a
    # FIXED baseline (first week of data = 100), so every range toggle
    # answers the same question: degradation since day one. The toggle
    # only changes the window, never the meaning. Downsampled by range
    # (daily for 1M, weekly for 6M/1Y, weekly-or-monthly for All) because
    # pack degradation is a slow trend; fine granularity would only add
    # noise. Both drifting down together = real degradation; only the
    # range line falling = the car changed its efficiency guess, not the
    # pack.
    from statistics import median, mean
    cap_by_day, rng_by_day = {}, {}
    for s in snapshots:
        d = _day(s["ts"])
        if s.get("capacity_kwh"):
            cap_by_day.setdefault(d, []).append(s["capacity_kwh"])
        if (s.get("range_mi") and s.get("soc") and s["soc"] > 5
                and s.get("capacity_kwh")):
            rng_by_day.setdefault(d, []).append(
                s["range_mi"] / s["soc"] * 100)
    pack_days = sorted(set(cap_by_day) & set(rng_by_day))
    pack_daily = [(d, median(cap_by_day[d]), median(rng_by_day[d]))
                  for d in pack_days]
    # Fixed baseline: first week of daily medians (or whatever exists yet).
    base_days = pack_daily[:7]
    if base_days:
        base_c = mean(c for _, c, _ in base_days)
        base_r = mean(r for _, _, r in base_days)
        n0 = len(base_days)
        base_lbl = (f"100 = {base_c:.1f} kWh baseline (first week)"
                    if n0 >= 7 else
                    f"100 = {base_c:.1f} kWh baseline (first {n0} days)")
        pack_idx = [(d, 100 * c / base_c, 100 * r / base_r)
                    for d, c, r in pack_daily]
    else:
        base_lbl, pack_idx = "", []

    def _pack_downsample(pts, freq):
        """Average indexed daily points into weekly ("W") or monthly ("M")
        buckets. Returns (label, cap, rng), label = bucket's first day."""
        buckets: dict = {}
        for d, c, r in pts:
            dt = datetime.strptime(d, "%Y-%m-%d")
            key = ((dt.isocalendar()[0], dt.isocalendar()[1]) if freq == "W"
                   else (dt.year, dt.month))
            buckets.setdefault(key, []).append((dt, c, r))
        out = []
        for key in sorted(buckets):
            ps = buckets[key]
            dt0 = min(p[0] for p in ps)
            lbl = (dt0.strftime("%b '%y") if freq == "M"
                   else dt0.strftime("%b %d"))
            out.append((lbl,
                        sum(p[1] for p in ps) / len(ps),
                        sum(p[2] for p in ps) / len(ps)))
        return out

    def _pack_daily_lbl(pts):
        return [(_ldt_str_fmt(d, "%b %d"), c, r) for d, c, r in pts]

    pack_charts = {}
    if pack_idx:
        span_days = (datetime.strptime(pack_idx[-1][0], "%Y-%m-%d")
                     - datetime.strptime(pack_idx[0][0], "%Y-%m-%d")).days
        # All downsamples by span: daily while short, weekly up to 2
        # years, monthly beyond. Fixed ranges always use their own grain.
        all_freq = ("D" if span_days < 62
                    else "W" if span_days < 730 else "M")
        ranges = (("1M", 30, "D"), ("6M", 182, "W"), ("1Y", 365, "W"),
                  ("All", None, all_freq))
        for key, trailing, freq in ranges:
            window = pack_idx[-trailing:] if trailing else pack_idx
            if freq == "D":
                series = _pack_daily_lbl(window)
            else:
                series = _pack_downsample(window, freq)
                if len(series) < 2:
                    # Too little data to bucket; stay daily.
                    series = _pack_daily_lbl(window)
            if len(series) < (2 if key == "All" else 7):
                continue
            pack_charts[key] = _pack_svg(series, base_lbl)
    if len(pack_charts) > 1:
        pack_seg = ('<div class="seg" id="packseg">' + "".join(
            f'<button data-n="{k}"'
            f"{' class=\"on\"' if k == 'All' else ''}>{k}</button>"
            for k in pack_charts) + '</div>')
        pack_chart = pack_seg + "".join(
            f'<div id="pack{k}"{" hidden" if k != "All" else ""}>'
            f'{svg}</div>'
            for k, svg in pack_charts.items())
    elif pack_charts:
        pack_chart = next(iter(pack_charts.values()))
    else:
        pack_chart = ""
    cap_now = latest.get("capacity_kwh")
    impl_now = (latest["range_mi"] / latest["soc"] * 100
                if latest.get("range_mi") and latest.get("soc") else None)
    cap_v = f"{cap_now:.1f}" if cap_now is not None else "-"
    impl_v = f"{impl_now:.0f}" if impl_now is not None else "-"
    packhealth = (
        '<div class="card"><h3>Pack health</h3>'
        '<div class="grid2">'
        '<div class="stat"><div class="k">Pack capacity</div>'
        f'<div class="v">{cap_v}<small> kWh</small></div>'
        '<div class="s">API estimate, not usable</div></div>'
        '<div class="stat"><div class="k">Implied full range</div>'
        f'<div class="v">{impl_v}<small> mi</small></div>'
        '<div class="s">range \u00f7 battery %</div></div>'
        '</div>' + pack_chart + '</div>')

    # Phantom drain hero: the most recent COMPLETED idle period between
    # drives, measured between fresh cloud-sync anchors. The tile never
    # headlines the still-open park (no end anchor yet); when the car is
    # parked, that shows as a quiet "parked since" line instead.
    idles, open_park = _idle_history(drains, snapshots)
    measured_list = [o for o in idles if o["measured"]]
    with_drop = [o for o in measured_list if o["soc_drop"] is not None]
    if with_drop:
        ov = with_drop[-1]
        tilde = "~" if ov["approx"] else ""
        idle_stat = (
            '<a class="stat" href="#battery"><div class="k">Phantom drain</div>'
            f'<div class="v amber">{tilde}{ov["soc_drop"]:.1f}'
            '<small> %</small></div>'
            f'<div class="s">{_esc(ov["window"])}</div></a>')
    elif idles or open_park:
        idle_stat = (
            '<a class="stat" href="#battery"><div class="k">Phantom drain</div>'
            '<div class="v amber">-<small> %</small></div>'
            '<div class="s">no measured period yet</div></a>')
    else:
        idle_stat = (
            '<a class="stat" href="#battery"><div class="k">Phantom drain</div>'
            '<div class="v amber">-<small> %</small></div>'
            '<div class="s">no data yet</div></a>')

    # Phantom drain trend: period-by-period timeline, so "typical" has a
    # visual anchor. A period over 1.6x the median watts (needs 3+
    # measured periods) flags red. Gets the same 1M / 6M / 1Y / All range
    # control as Pack health: each range recomputes its own average and
    # abnormal flags. 7N and 1M draw a timeline strip (one row, real time
    # on the x-axis, segment length = duration, color = watts); 6M, 1Y
    # and All draw a heat strip (one cell per period, weeks as columns,
    # color intensity = watts draw, abnormal periods stay red) instead of
    # an unreadable year of thin bars. Averaging into weekly bars was
    # rejected: it could hide a single abnormal period.
    def _trend_range(periods: list[dict], style: str = "bars") -> str:
        measured = [o for o in periods if not o["no_data"]]
        tot_hrs = sum(o["hours"] for o in measured)
        # Hour-weighted average watts across measured periods.
        avg_w = (sum(o["watts"] * o["hours"] for o in measured) / tot_hrs
                 if tot_hrs else 0)
        tot_kwh = sum(o["kwh"] for o in measured)
        wvals = sorted(o["watts"] for o in measured)
        med = wvals[len(wvals) // 2] if wvals else 0
        bad = {o["id"] for o in measured
               if len(measured) >= 3 and med > 0 and o["watts"] > 1.6 * med
               and not o.get("substep")}
        n_plugged = sum(1 for o in periods if o["plugged"])
        n_nodata = len(periods) - len(measured) - n_plugged
        tot_drop = sum(o["soc_drop"] or 0 for o in measured)
        avg_pct = tot_drop / (tot_hrs / 24.0) if tot_hrs else 0
        # Miles per parked day at the car's lifetime efficiency, so the
        # watts average reads as range lost, not just power. Same basis
        # as the %/day figure (parked time only, not calendar days).
        mi_day = avg_w * 24 / 1000 * (lifetime_mpk or 3.0)
        avg = (f"Average <b class='amber'>{avg_w:.0f} W</b> "
               f"(~{mi_day:.1f} mi/day) "
               f"({avg_pct:.1f}%/day, {tot_kwh:.2f} kWh total) "
               f"across {len(measured)} idle period"
               f"{'s' if len(measured) != 1 else ''}")
        if n_plugged:
            avg += f" &bull; {n_plugged} plugged in (not measured)"
        if n_nodata:
            avg += f" &bull; {n_nodata} with no data"
        avg += "."
        if len(measured) < 3:
            avg += (" Early data: the average firms up "
                    "after a few idle periods.")
        if style == "strip":
            chart = _heat_strip(periods, bad)
        else:
            chart = _timeline_strip(periods, bad)
        table = _idle_table(periods, bad)
        details = (
            '<details class="sacc"><summary class="dim lbl14">'
            'Period-by-period breakdown</summary>'
            f'<div class="abody">{table}</div></details>')
        return f'<div class="sub">{avg}</div>' + chart + details

    trend_ranges = {}
    trend_labels = {"7N": "Last 7"}
    for key, span in (("7N", 7), ("1M", 30), ("6M", 182), ("1Y", 365),
                      ("All", None)):
        periods = idles[-span:] if span else idles
        if len(periods) >= (2 if key in ("7N", "All") else 7):
            trend_ranges[key] = _trend_range(
                periods,
                style="strip" if key in ("6M", "1Y", "All") else "bars")
    first_key = next(iter(trend_ranges), None)
    if len(trend_ranges) > 1:
        trend_bars = (
            '<div class="seg" id="trendseg">' + "".join(
                f'<button data-n="{k}"'
                f'{" class=\"on\"" if k == first_key else ""}>'
                f'{trend_labels.get(k, k)}</button>'
                for k in trend_ranges) + '</div>' + "".join(
                f'<div id="trend{k}"{" hidden" if k != first_key else ""}>'
                f"{html_}</div>"
                for k, html_ in trend_ranges.items()))
    elif trend_ranges:
        trend_bars = trend_ranges[first_key]
    else:
        trend_bars = ("No idle periods measured yet. "
                      "Drive the car and park it to fill this in.")
    trend_toggle = ""
    if trend_ranges:
        trend_note = ("Each segment is one park between drives, measured "
                      "from the car's last report after parking to its "
                      "first report on waking. The car only reports when "
                      "it wakes or checks in, so a long park may rest on "
                      "just a few readings, and a short park can read "
                      "below one gauge step: the battery gauge moves in "
                      "0.1% steps, about 88 Wh each. Plugged-in periods "
                      "are not measured: the car tops itself off.")
    else:
        trend_note = ""
    if open_park:
        bits = [f"Parked now &bull; since {_fmt_ts(open_park['since_ts'])}"]
        if open_park.get("soc_now") is not None:
            bits.append(f"battery {open_park['soc_now']:.0f}%")
        drop = open_park.get("drop")
        elapsed = open_park.get("elapsed_h") or 0
        if drop is not None and elapsed >= 1 and not open_park.get("plugged"):
            if drop > 0:
                tilde = "~" if open_park.get("approx") else ""
                bit = f"down {tilde}{drop:.1f}% in {elapsed:.1f} h so far"
                if open_park.get("pct_day") is not None:
                    bit += f" (~{open_park['pct_day']:.1f}%/day if it holds)"
                bits.append(bit)
            else:
                bits.append("no drop so far")
        sl = open_park.get("sleep") or {}
        if sl.get("online_h") is not None and sl["online_h"] >= 0.5:
            bits.append(f"awake ~{sl['online_h']:.1f} h of it")
        trend_bars = (f'<div class="sub">{" &bull; ".join(bits)}</div>'
                      + trend_bars)
    trip_month_html, trips_html = _activity_html(
        trips, errands, activity_segments())

    doc = HTML_TEMPLATE
    live = _live_charge(snapshots, now)
    if live:
        eta_bit = (f" &middot; {CHARGE_TARGET_PCT:.0f}% by about "
                   f"{live['end_txt']}" if live.get("end_txt") else "")
        location_html = (f"Charging at <b>{_esc(loc)}</b>{eta_bit}" if loc
                         else f"Charging{eta_bit}")
    else:
        location_html = (f"Parked at <b>{_esc(loc)}</b>" if loc
                         else "Location unknown")
    live_is_home = bool(live) and _is_home(
        loc, latest.get("lat"), latest.get("lon"))
    charging_section = (
        '<h2 class="sec" id="charging">Charging</h2>'
        + _charges_html(charges, live, live_is_home,
                        eff=(lifetime_mpk if lifetime_mpk else 3.0),
                        orphans=_orphans, ambiguous=_ambiguous))
    savings_section = (
        '<h2 class="sec" id="savings">Fuel savings vs gas</h2>'
        + savings_table
    ) if savings_table else ""
    if live:
        charging_top, charging_bottom = charging_section, savings_section
    else:
        charging_top, charging_bottom = "", charging_section + savings_section
    gov_note = ""
    try:
        import governor as _governor
        _cd_until = _governor.cooldown_until()
        if _cd_until:
            gov_note = (
                '<div class="gov-note">'
                "Rivian cloud asked us to slow down, so polling is "
                f"paused until {_fmt_time(int(_cd_until))}.</div>")
    except Exception:
        gov_note = ""
    repl = {
        "__GOVERNOR__": gov_note,
        "__FAVICON__": (
        '<link rel="icon" href="data:image/x-icon;base64,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AOwAAAD/AAAA/wAAANIAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAA0gAAAP8AAAD/AAAA7gAAADYAAABRAAAA+QAAAG4AAABtAAAA+QAAAFIAAAA1AAAA7QAAAP8AAAD/AAAA0gAAABUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAANIAAAD/AAAA/wAAAPEAAAA/AAAARwAAAF0AAABdAAAASQAAAD4AAADxAAAA/wAAAP8AAADTAAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAADSAAAA/wAAAP8AAAD2AAAATgAAAAAAAAAAAAAATgAAAPYAAAD/AAAA/wAAANMAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAA0gAAAP8AAAD/AAAA/AAAAJUAAACVAAAA/AAAAP8AAAD/AAAA0gAAABUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAM4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADOAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAADFAAAA/wAAAP8AAAD/AAAA/wAAAMYAAAARAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKAAAAlwAAAPQAAAD0AAAAmAAAAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAIAAAAEAAAAABACAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALwAAALgAAAD4AAAA9wAAALgAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAD1AAAA/wAAAP8AAAD/AAAA/wAAAPYAAABRAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABiAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPwAAABjAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbgAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABvAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHcAAAD/AAAA/wAAAP8AAAD/AAAA9wAAAJoAAACaAAAA9wAAAP8AAAD/AAAA/wAAAP8AAAB5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB/AAAA/wAAAP8AAAD/AAAA/wAAAOgAAAA1AAAAAAAAAAAAAAA1AAAA5wAAAP8AAAD/AAAA/wAAAP8AAAB/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhAAAAP8AAAD/AAAA/wAAAP8AAADbAAAAIgAAABUAAAASAAAAEgAA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AGMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFEAAAD2AAAA/wAAAP8AAAD/AAAA/wAAAPYAAABSAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAAAAC4AAAA+AAAAPkAAAC5AAAAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACgAAAAwAAAAYAAAAAEAIAAAAAAAACQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAB5AAAA1QAAAPsAAAD6AAAA1QAAAHkAAAALAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJwAAANgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADZAAAAJwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3AAAA7AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA7AAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEIAAADyAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPMAAABDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASQAAAPYAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD2AAAASgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABNAAAA+AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+AAAAE4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFEAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPUAAAD1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADzAAAAYAAAAAUAAAAFAAAAYAAAAPMAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD6AAAAVQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAAA+gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOcAAAAxAAAAAAAAAAAAAAAAAAAAAAAAADEAAADmAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA2wAAACIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhAAAA2gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADRAAAAGAAAAAAAAAAYAAAAkQAAAAAAAAAAAAAAkQAAABgAAAAAAAAAFwAA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AAAAAABCAAAA9gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANEAAAARAAAAAAAAAB8AAADjAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAqAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAApwAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOQAAAAgAAAAAAAAABEAAADQAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9gAAAEMAAADMAAAA5AAAAOQAAADkAAAA5AAAAOQAAADhAAAAvQAAABcAAAAAAAAABAAAAM8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAACsAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAKsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADRAAAABAAAAAAAAAAWAAAAvQAAAOEAAADkAAAA5AAAAOQAAADkAAAA5AAAAM0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPQAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOUAAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAADjAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOIAAAAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAADgAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAQQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADJAAAA4wAAAOMAAADkAAAA4wAAAOMAAADgAAAAvAAAABYAAAAAAAAABAAAANUAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAACmAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAKUAAAD/AAAA/wAA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AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABVAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAMgAAAASAAAAAAAAACMAAADfAAAA3QAAAAAAAAAAAAAA2QAAAOEAAAAmAAAAAAAAABEAAADGAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVQAAAPsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADRAAAAGAAAAAAAAAAbAAAAmAAAAAAAAAAAAAAAmgAAAB4AAAAAAAAAFwAAANAAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAVgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA2wAAACIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhAAAA2gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABWAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABWAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOYAAAAxAAAAAAAAAAAAAAAAAAAAAAAAADAAAADmAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVQAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADzAAAAXgAAAAUAAAAFAAAAXQAAAPMAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAVgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPUAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABPAAAA+AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASgAAAPYAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD2AAAASwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEIAAADzAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPMAAABDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAA7AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA7QAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKAAAANkAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADaAAAAKQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAB6AAAA1gAAAPwAAAD8AAAA1gAAAHoAAAAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAQAAAAIAAAAABACAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA7AAAAowAAAOUAAAD8AAAA+gAAAOQAAACiAAAAOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAClAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAACoAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB4AAADSAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANMAAAAeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACoAAADjAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA5AAAACsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADUAAADrAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADsAAAANgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADwAAADwAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPEAAAA+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEMAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9QAAAEUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEgAAAD2AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD3AAAASQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE0AAAD3AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPgAAABPAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA8wAAAIIAAAA4AAAAOAAAAIIAAADzAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAFIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA3AAAACkAAAAAAAAAAAAAAAAAAAAAAAAAKgAAANsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD6AAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAyQAAABYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAAyAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABWAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFYAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAtwAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAC1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAApwAAAAYAAAAAAAAAAAAAACkAAAAuAAAAAAAAAAAAAAAuAAAAKgAAAAAAAAAAAAAABQAAAKQAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAlwAAAAIAAAAAAAAAAAAAADsAAADuAAAAeQAAAAAAAAAAAAAAdgAAAO4AAAA8AAAAAAAAAAAAAAACAAAAlgAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAiQAAAAEAAAAAAAAAAAAAAEYAAAD0AAAA/wAAAHsAAAAAAAAAAAAAAHcAAAD/AAAA9AAAAEgAAAAAAAAAAAAAAAEAAACIAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAfQAAAAAAAAAAAAAAAAAAAFAAAAD4AAAA/wAAAP8AAAB8AAAAAAAAAAAAAAB4AAAA/wAAAP8AAAD4AAAAUgAAAAAAAAAAAAAAAAAAAHsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD8AAAAWwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFoAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAdAAAAAAAAAAAAAAAAAAAAFoAAAD6AAAA/wAAAP8AAAD/AAAAewAAAAAAAAAAAAAAeQAAAP8AAAD/AAAA/wAAAPsAAABcAAAAAAAAAAAAAAAAAAAAcwAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPwAAABcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAbAAAAAAAAAAAAAAAAAAAAGAAAAD8AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAHoAAAD/AAAA/wAAAP8AAAD/AAAA/AAAAGIAAAAAAAAAAAAAAAAAAABrAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAZgAAAAAAAAAAAAAAAAAAAGcAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB6AAAAAAAAAAAAAAB6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAagAAAAAAAAAAAAAAAAAAAGUAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD8AAAAWwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAYwAAAAAAAAAAAAAAAAAAAG0AAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAegAAAAAAAAAAAAAAegAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABvAAAAAAAAAAAAAAAAAAAAYgAAAP0AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAYgAAAAAAAAAAAAAAAAAAAHAAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHkAAAAAAAAAAAAAAHgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHEAAAAAAAAAAAAAAAAAAABgAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAYgAAAAAAAAAAAAAAAAAAAHIAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB1AAAAAAAAAAAAAABvAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAcgAAAAAAAAAAAAAAAAAAAGEAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAZAAAAAAAAAAAAAAAAAAAAHIAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADqAAAAIAAAAAAAAAAAAAAAHwAAAOkAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABzAAAAAAAAAAAAAAAAAAAAYwAAAP0AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAaQAAAAAAAAAAAAAAAAAAAHAAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADlAAAALAAAAAAAAAAAAAAAAAAAAAAAAAAqAAAA5AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHAAAAAAAAAAAAAAAAAAAABoAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAcwAAAAAAAAAAAAAAAAAAAGsAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADdAAAAIgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAADcAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAbAAAAAAAAAAAAAAAAAAAAHEAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAVQAAAAAAAAAAAAAAAAAAAFAAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAfgAAAAAAAAAAAAAAAAAAAGEAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADWAAAAGwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGgAAANUAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABjAAAAAAAAAAAAAAAAAAAAfQAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABRAAAAAAAAAEwAAAD4AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAjAAAAAAAAAAAAAAAAAAAAFAAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADTAAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWAAAA0gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFQAAAAAAAAAAAAAAAAAAACLAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAE0AAADoAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD5AAAAiwAAAAEAAAAAAAAAAAAAACYAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADWAAAAFgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABYAAADVAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD2AAAAJwAAAAAAAAAAAAAAAQAAAIoAAAD4AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADqAAAAAgAAAAcAAAAJAAAACQAAAAkAAAAJAAAACQAAAAgAAAAGAAAAAQAAAAAAAAAAAAAAAAAAAAAAAACTAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADwAAAAHwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHwAAAPAAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAJcAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAYAAAAIAAAACQAAAAkAAAAJAAAACQAAAAkAAAAHAAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAugAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAvwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAC9AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAABgAAAAgAAAAIAAAACAAAAAgAAAAIAAAABwAAAAQAAAABAAAAAAAAAAAAAAAAAAAAAAAAAJgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAO0AAAAaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAXAAAA7AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAmQAAAAAAAAAAAAAAAAAAAAAAAAABAAAABAAAAAcAAAAIAAAACAAAAAgAAAAIAAAACAAAAAYAAAABAAAA5QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9wAAAIoAAAABAAAAAAAAAAAAAAAqAAAA+AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA0AAAABIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASAAAAzwAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+AAAAC0AAAAAAAAAAAAAAAEAAACJAAAA9wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA5gAAAEwAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAiwAAAAAAAAAAAAAAAAAAAFsAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADNAAAAEwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASAAAAzAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/QAAAFwAAAAAAAAAAAAAAAAAAACKAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAE0AAAAAAAAAUQAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB+AAAAAAAAAAAAAAAAAAAAawAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANAAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAAzwAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAG0AAAAAAAAAAAAAAAAAAAB7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+gAAAFMAAAAAAAAAAAAAAAAAAABVAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHIAAAAAAAAAAAAAAAAAAAB1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA1QAAABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbAAAA1QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHUAAAAAAAAAAAAAAAAAAABvAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAZwAAAAAAAAAAAAAAAAAAAH0AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADeAAAAJAAAAAAAAAAAAAAAAAAAAAAAAAAkAAAA3gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAAAAAABmAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWQAAAPsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP0AAABhAAAAAAAAAAAAAAAAAAAAgAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOUAAAAeAAAAAAAAAAAAAAAaAAAA5AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAH4AAAAAAAAAAAAAAAAAAABhAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABaAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAF0AAAAAAAAAAAAAAAAAAACAAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAbQAAAAAAAAAAAAAAbgAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAH4AAAAAAAAAAAAAAAAAAABfAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFsAAAD8AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD8AAAAXQAAAAAAAAAAAAAAAAAAAH4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHYAAAAAAAAAAAAAAHgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAH0AAAAAAAAAAAAAAAAAAABeAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWwAAAPwAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPwAAABfAAAAAAAAAAAAAAAAAAAAeQAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB5AAAAAAAAAAAAAAB6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHkAAAAAAAAAAAAAAAAAAABfAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAF0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABbAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAGIAAAAAAAAAAAAAAAAAAABzAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAegAAAAAAAAAAAAAAegAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHQAAAAAAAAAAAAAAAAAAABiAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAF0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFsAAAD8AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAaAAAAAAAAAAAAAAAAAAAAG0AAAD+AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAHoAAAD/AAAA/wAAAP8AAAD/AAAA/gAAAG8AAAAAAAAAAAAAAAAAAABnAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWwAAAPwAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAAByAAAAAAAAAAAAAAAAAAAAZAAAAPwAAAD/AAAA/wAAAP8AAAB7AAAAAAAAAAAAAAB5AAAA/wAAAP8AAAD/AAAA/QAAAGcAAAAAAAAAAAAAAAAAAABwAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABbAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAAAAAABcAAAA+wAAAP8AAAD/AAAAfAAAAAAAAAAAAAAAeAAAAP8AAAD/AAAA/AAAAF8AAAAAAAAAAAAAAAAAAAB5AAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFoAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAiAAAAAEAAAAAAAAAAAAAAFAAAAD4AAAA/wAAAHwAAAAAAAAAAAAAAHcAAAD/AAAA+QAAAFQAAAAAAAAAAAAAAAEAAACGAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWQAAAPsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAACXAAAAAgAAAAAAAAAAAAAAQwAAAPIAAAB7AAAAAAAAAAAAAAB2AAAA9AAAAEgAAAAAAAAAAAAAAAIAAACUAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABYAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAKYAAAAGAAAAAAAAAAAAAAAvAAAAMwAAAAAAAAAAAAAANAAAADMAAAAAAAAAAAAAAAUAAACkAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAtgAAAAsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAC1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVgAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADIAAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAADHAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABVAAAA+gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANsAAAAoAAAAAAAAAAAAAAAAAAAAAAAAACgAAADaAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA8gAAAH8AAAA3AAAANwAAAH8AAADxAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+gAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUQAAAPgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAFIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABKAAAA9wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9wAAAEwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEQAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9QAAAEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPQAAAPAAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA8QAAAD4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1AAAA6wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA7AAAADYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACsAAADkAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA5QAAACwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHwAAANQAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA1QAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPAAAAqAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAqQAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8AAAAowAAAOcAAAD9AAAA/QAAAOcAAACkAAAAPQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==">'
        '<link rel="apple-touch-icon" href="data:image/png;base64,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">'
        if os.environ.get("WATTSON_DEMO") else
        '<link rel="icon" href="/favicon.ico?v=4" type="image/x-icon">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=4">'),
        "__DEMOBANNER__": ('<div class="demobanner">Demo data: fictional '
                           'vehicle, fictional drives</div>'
                           if os.environ.get("WATTSON_DEMO") else ""),
        "__TITLE__": f"{_esc(vehicle_name)} - Energy Tracker",
        "__VEHICLE__": _esc(vehicle_name),
        "__BRAND__": _esc(brand),
        "__DEMOPILL__": demopill,
        "__UPDATED__": _fmt_ts(latest.get("ts", now)),
        "__UPDATED_TS__": str(latest.get("ts", "")),
        "__POLLAGE__": ('<span id="poll-age" data-ts="' + str(latest.get("ts", "")) + '"></span>'
                        if not os.environ.get("WATTSON_DEMO") else ""),
        "__RANGE__": f"{rng:.0f} mi" if rng else "-",
        "__CARTAG__": car_tag,
        "__SOC__": f"{soc:.0f}%" if soc is not None else "-",
        "__ODO__": f"{odo_mi:,.0f} mi odometer" if odo_mi else "",
        "__SOCPCT__": f"{soc:.1f}" if soc is not None else "0",
        "__BATTCHG__": " charging" if live else "",
        "__BOLT__": (_BOLT_SVG if live and soc is not None and soc >= 8
                     else ""),
        "__LOCATION__": location_html,
        # The demo shows the button too: its modal explains
        # itself (see the WATTSON_DEMO flag in _scripts_markup).
        # A served dashboard without a password set hides it,
        # because the nav endpoints are off there.
        "__NAVBTN__": ("" if (not os.environ.get("WATTSON_DEMO")
                            and not token_from_env())
                       else '<button type="button" id="navopen" '
                            'class="navbtn" onclick="openNavModal()">'
                            'Send destination</button>'),
        "__IDLESTAT__": idle_stat,
        "__TIRETILE__": tiretile,
        "__LIFEMI__": f"{lifetime_mpk:.2f}" if lifetime_mpk else "-",
        "__GASSAVE__": (_fmt_money(int(round(_savings_total)))
                        if _savings_rows else "-"),
        "__GASSAVESUB__": _gas_subtitle(_savings_rows, _unpriced_dc),
        "__LIFESUB__": (f"{float(os.environ.get('WATTSON_DEMO_LIFETIME_MI', agg['m'])):.1f} mi tracked" if agg["m"] else ""),
        "__TRIPMONTH__": trip_month_html,
        "__TRIPS__": trips_html,
        "__BATTERYNOTE__": batt_note,
        "__BATT7__": batt_charts[7], "__BATT14__": batt_charts[14],
        "__BATT30__": batt_charts[30],
        "__TRENDTOGGLE__": trend_toggle,
        "__TRENDRANGES__": trend_bars,
        "__PACKHEALTH__": packhealth,
        "__TRENDNOTE__": trend_note,
        "__CHARGING_TOP__": charging_top,
        "__CHARGING_BOTTOM__": charging_bottom,
        "__STYLES__": _styles_markup(),
        "__SCRIPTS__": _scripts_markup(),
    }
    for k, v in repl.items():
        doc = doc.replace(k, v)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(".tmp")
    tmp.write_text(doc)
    os.replace(tmp, REPORT_PATH)      # visitors never see a half-written page
    print(f"report -> {REPORT_PATH}")


def _ldt_str(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%b %d")


def _ldt_str_fmt(day: str, fmt: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime(fmt)


if __name__ == "__main__":
    generate()


