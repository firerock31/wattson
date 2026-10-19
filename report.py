"""Generate a self-contained HTML dashboard from the tracker database.

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
- Wake-up counts are derived from the SoC curve and only register at
  minute-level polling; at the current hourly cadence they read 0.
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
from db import connect

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
          ref_label: str = "median") -> str:
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
    abnormal-night rule is measured against."""
    if not items:
        return ""
    raw_max = max(v for _, v, _ in items)
    w, h, pad = 560, 150, 8
    n = len(items)
    axis = y_unit is not None
    x0 = 42 if axis else pad
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
            parts.append(
                f'<text x="{x1}" y="{ry - 6:.1f}" text-anchor="end"'
                f' class="axis">{_esc(ref_label)}</text>')
    rects = []
    for i, (label, val, color) in enumerate(items):
        frac = val / top
        # With a real axis a zero night draws no bar (honest); without an
        # axis keep the old 4px minimum so tiny values stay visible.
        bh = frac * plot_h if axis else max(4.0, frac * plot_h)
        x = x0 + gap * i + (gap - bw) / 2
        y = base_y - bh
        if thin and color != RED:
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
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}"'
            f' rx="7" fill="{color}"/>'
            f'<text x="{x + bw / 2:.1f}" y="{h - 6}" text-anchor="middle"'
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
        elif o["no_data"] or o["watts"] <= 0:
            fill = "#2b2b2d"
        else:
            fill = amber(o["watts"] / vmax)
        if o["no_data"]:
            tip = f'{o["label"]}: no data (car was asleep)'
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
        f'<div style="display:flex;flex-wrap:wrap;gap:6px 18px;'
        f'margin:2px 0 8px;font-size:13px;'
        f'color:var(--dim)">'
        f'<span><span style="display:inline-block;width:10px;height:10px;'
        f'border-radius:5px;background:{GREEN};margin-right:6px"></span>'
        f'Pack capacity</span>'
        f'<span><span style="display:inline-block;width:10px;height:10px;'
        f'border-radius:5px;background:{AMBER};margin-right:6px"></span>'
        f'Implied full range</span>'
        + (f'<span style="color:var(--faint)">{_esc(baseline_label)}</span>'
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
<style>
:root{
  --bg:#0f0f11; --card:#1c1c1e; --card2:#242426; --track:#2b2b2e;
  --text:#f5f5f7; --dim:#a1a1a6; --faint:#6e6e73;
  --green:#7ed321; --amber:#f5a623; --red:#ff5a52; --radius:24px;
}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,
  sans-serif;margin:0;padding:0 16px 48px;background:var(--bg);color:var(--text);
  -webkit-font-smoothing:antialiased}
.wrap{max-width:720px;margin:0 auto}
.sitenav{position:fixed;top:12px;left:50%;transform:translateX(-50%);width:min(720px,calc(100% - 24px));z-index:50;background:rgba(17,19,23,.88);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,.14);border-radius:18px;box-shadow:0 12px 40px rgba(0,0,0,.35),0 0 28px rgba(255,255,255,.10),inset 0 1px 0 rgba(255,255,255,.08)}.nav-inner{padding:0 20px;height:56px;display:flex;align-items:center;justify-content:space-between;gap:16px}.nav-brand{font-weight:700;font-size:17px;letter-spacing:.06em;color:var(--text);text-decoration:none;white-space:nowrap}.nav-links{display:flex;gap:22px}.nav-links a{color:var(--dim);text-decoration:none;font-size:14px;font-weight:500;white-space:nowrap;padding:4px 0 2px;border-bottom:2px solid transparent;transition:color .2s,border-color .2s}.nav-links a:hover{color:var(--text)}.nav-links a.active{color:#ff9f0a;border-bottom-color:#ff9f0a}.nav-updated{font-size:12px;color:var(--faint);white-space:nowrap;display:flex;align-items:center;gap:8px}@media(max-width:640px){.nav-links{display:none}}body{padding-top:80px}#daily,#battery,#charging,#savings,#top{scroll-margin-top:92px}.demo-pill{font-size:11px;font-weight:600;color:#b45309;background:rgba(180,83,9,.15);border:1px solid rgba(180,83,9,.4);padding:2px 8px;border-radius:20px;white-space:nowrap}.topbar{display:flex;justify-content:space-between;align-items:center;
  padding:20px 4px 8px}
.pill{background:var(--card2);border-radius:999px;padding:10px 18px;
  font-weight:700;font-size:15px}
.updated{color:var(--faint);font-size:13px}
@media(max-width:640px){
  .nav-inner{height:auto;flex-wrap:wrap;padding:8px 16px 0;gap:4px 12px}
  .nav-updated{margin-left:auto}
  .nav-links{display:flex;order:3;flex:0 0 100%;overflow-x:auto}
  .nav-links a{display:inline-flex;align-items:center;flex:none;min-height:44px}
  body{padding-top:112px}
  #daily,#battery,#charging,#savings,#top{scroll-margin-top:120px}
}
#poll-age{display:block;text-align:right}

.hero{padding:28px 4px 8px}
.hero-top{display:flex;justify-content:space-between;gap:10px;align-items:center}
.hero-num{min-width:0;flex:1 1 auto}
.hero-range{font-size:clamp(60px,17vw,120px);font-weight:650;
  letter-spacing:-3px;line-height:1;margin:0;white-space:nowrap}
.hero-side{width:38%;max-width:300px;min-width:130px;flex:0 0 auto;
  display:flex;flex-direction:column;align-items:center}
.hero-car{width:100%;height:auto}
.hero-odo{margin-top:10px;font-size:20px;font-weight:700;color:var(--text);
  text-align:center}
.hero-sub{display:flex;gap:10px;align-items:baseline;justify-content:center;
  margin-top:10px;color:var(--dim);font-size:17px}
.hero-sub b{color:var(--text);font-size:20px}
.battbar{height:26px;border-radius:999px;background:var(--track);
  margin:18px 0 10px;overflow:hidden}
.battbar>div{height:100%;border-radius:999px;background:var(--green);
  position:relative}
.battbar.charging .bolt{position:absolute;right:7px;top:50%;
  transform:translateY(-50%);width:18px;height:20px;fill:#ffffff;
  animation:boltfade 4s ease-in-out infinite}
@keyframes boltfade{0%,100%{opacity:0}
  50%{opacity:1}}
.heroloc{margin-top:14px;font-size:16px;color:var(--dim)}
.heroloc b{color:var(--text)}
h2.sec{font-size:32px;font-weight:750;letter-spacing:-0.8px;margin:38px 0 14px}
.card{background:var(--card);border-radius:var(--radius);padding:22px;
  margin-bottom:14px}
.card h3{margin:0 0 2px;font-size:21px;font-weight:700;letter-spacing:-0.3px}
.card h3.row{display:flex;justify-content:space-between;align-items:center;
  gap:10px}
.card .sub{color:var(--dim);font-size:14px;margin-bottom:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:18px}
@media(max-width:560px){.grid3{grid-template-columns:1fr}}
.chlive{display:flex;gap:18px;align-items:center;margin-top:6px}
.chlive-main{flex:1 1 0;min-width:0}
.chlive-side{flex:0 0 172px;display:flex;flex-direction:column;gap:12px}
.chlive-side .stat{padding:14px 16px}
@media (max-width:620px){
  .chlive{flex-direction:column;align-items:stretch}
  .chlive-side{flex:1 1 auto;display:grid;grid-template-columns:1fr 1fr;gap:14px}
}
.stat{background:var(--card);border-radius:var(--radius);padding:20px}a.stat{color:inherit;text-decoration:none;transition:background .2s,transform .2s;cursor:pointer}a.stat:hover{background:rgba(255,255,255,.07);transform:translateY(-1px)}
.stat .k{font-size:13px;color:var(--dim);margin-bottom:8px}
.stat .v{font-size:38px;font-weight:700;letter-spacing:-1px;line-height:1.05}
.stat .v small{font-size:18px;font-weight:600;color:var(--dim)}
.stat .s{font-size:13px;color:var(--dim);margin-top:8px}
.drain-k{display:flex;justify-content:space-between;align-items:center;gap:8px}
.useg{display:inline-flex;background:var(--card2);border-radius:999px;padding:2px;gap:2px;flex:none}
.useg button{border:0;background:transparent;color:var(--dim);font-size:11px;
  font-weight:700;padding:4px 10px;border-radius:999px;cursor:pointer}
.useg button.on{background:#f5f5f7;color:#0f0f11}
.amber{color:var(--amber)} .green{color:var(--green)} .dim{color:var(--dim)}
.donut{width:100%;max-width:300px;display:block;margin:6px auto}
.donut-big{fill:var(--text);font-size:44px;font-weight:700}
.donut-small{fill:var(--dim);font-size:19px}
.donut-tiny{fill:var(--dim);font-size:15px}
.donut-cap{text-align:center;color:var(--dim);font-size:14px;margin-top:6px}
.bars{width:100%;display:block}
.heat{width:100%;display:block}
.bar-label{fill:var(--faint);font-size:12px}
.bar-val{fill:var(--dim);font-size:12px;font-weight:600}
.bar-sub{fill:var(--faint);font-size:11px}
.linechart{width:100%;display:block}
.grid{stroke:#2b2b2e;stroke-width:1}
.axis{fill:var(--faint);font-size:12px}
.seg{display:flex;gap:8px;margin:12px 0 14px}
.seg button{flex:1;border:0;border-radius:14px;padding:11px 0;font-size:15px;
  font-weight:700;background:var(--card2);color:var(--dim);cursor:pointer}
.seg button.on{background:#f5f5f7;color:#0f0f11}
.sec-row{display:flex;justify-content:space-between;align-items:center;
  margin:38px 0 14px}
.sec-row h2.sec{margin:0}
.monthsel{background:var(--card2);color:var(--dim);border:0;border-radius:12px;
  padding:10px 12px;font-size:14px;font-weight:600;cursor:pointer;
  max-width:45%}
.showmore{display:block;width:100%;border:0;border-radius:14px;padding:13px 0;
  font-size:15px;font-weight:700;background:var(--card2);color:var(--dim);
  cursor:pointer;margin:4px 0 0}
.showmore[hidden]{display:none}
details.aday{background:var(--card);border-radius:var(--radius);
  margin-bottom:12px;padding:6px 20px}
details.aday summary{cursor:pointer;list-style:none;padding:14px 0;
  font-size:16px;font-weight:700}
details.aday summary::-webkit-details-marker{display:none}
details.aday summary .dim{font-weight:400;font-size:14px}
.abody{overflow:hidden}
.asum{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.abar{position:relative;height:12px;border-radius:999px;background:var(--track);
  margin-top:10px;overflow:hidden}
.aseg{position:absolute;top:0;bottom:0}
.anow{position:absolute;top:0;bottom:0;width:2px;background:#f5f5f7;opacity:.85}
.alegend{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 12px;color:var(--dim);
  font-size:13px}
.alegend i{display:inline-block;width:10px;height:10px;border-radius:3px;
  margin-right:6px}
.st-moving{background:#ff9f0a}.st-charging{background:#bf5af2}
.st-parked{background:#30d158}.st-sleep{background:#2b2b2d}
.st-unknown{background:repeating-linear-gradient(45deg,#48484a 0 4px,#333336 4px 8px)}
.trip{border-top:1px solid #2c2c2e;padding:14px 0}
.trip:first-of-type{margin-top:6px}
.trip-time{font-size:15px;font-weight:700}
.trip-time .dim{font-weight:400}
.trip-route{font-size:15px;margin-top:3px}
.tag{display:inline-block;font-size:12px;font-weight:600;color:var(--amber);
  border:1px solid #4a3a1c;border-radius:999px;padding:2px 9px;margin-left:8px;
  vertical-align:1px}
.trip-stats{display:flex;gap:16px;margin-top:8px;color:var(--dim);font-size:14px}
.trip-stats b{color:var(--text);font-weight:700}
.cost-action{display:inline-flex;align-items:center;justify-content:center;
  min-width:44px;min-height:44px;padding:6px 8px}
details.chargerow{border-top:1px solid #2c2c2e}
details.chargerow:first-of-type{margin-top:6px}
details.chargerow summary{cursor:pointer;list-style:none;padding:12px 0 2px}
details.chargerow summary::-webkit-details-marker{display:none}
.chargedetail{overflow:hidden}
.cdin{padding:10px 0 16px}
.chargemap{border-radius:var(--radius);overflow:hidden;margin:10px 0 2px}
.chargemap img{width:100%;height:auto;display:block;opacity:0;transition:opacity .45s ease}.chargemap img.ld{opacity:1}
.trip-via{color:var(--faint);font-size:13px;margin-top:2px}
#tripmap{height:300px;border-radius:var(--radius);margin-top:4px;
  background:#141416}
.mapwrap{position:relative;border-radius:var(--radius);overflow:hidden;
  background:#0b0d10;margin-top:4px}
.mapwrap svg{position:absolute;inset:0;width:100%;height:100%}
.mapwrap img{opacity:0;transition:opacity .45s ease}
.mapwrap img.ld{opacity:1}
.mapattr{position:absolute;right:8px;bottom:6px;font-size:10px;color:#8a8f98;
  background:rgba(10,12,15,.72);padding:2px 6px;border-radius:4px}
.empty{background:var(--card);border-radius:var(--radius);padding:34px 22px;
  color:var(--dim);font-size:15px;text-align:center;line-height:1.6}
table.drain,table.ctab{width:100%;border-collapse:collapse;font-size:14px;margin-top:6px}
table.drain th,table.ctab th{text-align:left;color:var(--faint);font-weight:600;
  font-size:12px;text-transform:uppercase;letter-spacing:0.6px;
  padding:10px 8px;border-bottom:1px solid #2c2c2e}
table.drain td,table.ctab td{padding:11px 8px;border-bottom:1px solid #232326}
table.drain tr:last-child td,table.ctab tr:last-child td{border-bottom:0}

.warn{color:var(--red);font-weight:700}
.foot{margin-top:34px;color:var(--faint);font-size:13px;line-height:1.7}
.foot p{margin:8px 0}
.note{font-size:13px;color:var(--faint);margin-top:10px;line-height:1.6}
#toTop{position:fixed;right:20px;bottom:calc(20px + env(safe-area-inset-bottom));z-index:50;width:46px;height:46px;border-radius:50%;border:1px solid var(--track);background:var(--card2);color:var(--text);cursor:pointer;display:flex;align-items:center;justify-content:center;opacity:0;visibility:hidden;transform:translateY(10px);transition:opacity .25s ease,transform .25s ease,visibility .25s}
#toTop.show{opacity:1;visibility:visible;transform:translateY(0)}
#toTop.show:active{transform:scale(.93)}
#toTop svg{width:22px;height:22px}
.wrap:focus{outline:none}
@media (prefers-reduced-motion: reduce){
  #toTop,#toTop.show{transition:none;transform:none}
  .battbar.charging .bolt{animation:none;opacity:1}
}
</style></head>
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
  <div class="battbar__BATTCHG__"><div style="width:__SOCPCT__%">__BOLT__</div></div>
  <div class="heroloc">__LOCATION__</div>
  <div class="grid3">
    <a class="stat" href="#savings"><div class="k">Lifetime savings vs gas</div>
      <div class="v green">__GASSAVE__</div>
      <div class="s">__GASSAVESUB__</div></a>
    __IDLESTAT__
    <a class="stat" href="#daily"><div class="k">Lifetime efficiency</div>
      <div class="v green">__LIFEMI__<small> mi/kWh</small></div>
      <div class="s">__LIFESUB__</div></a>
  </div>
</div>

__CHARGING_TOP__
<div class="sec-row" id="daily"><h2 class="sec">Daily activity</h2>__TRIPMONTH__</div>
<div class="alegend"><span><i class="st-moving"></i>driving</span><span><i class="st-charging"></i>charging</span><span><i class="st-parked"></i>awake</span><span><i class="st-sleep"></i>deep sleep</span><span><i class="st-unknown"></i>no data</span></div>
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
<script>
__SCRIPT__
</script><script>var scrollRaf=null;(function(){var ease=function(t){return t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2};document.querySelectorAll('a[href^="#"]').forEach(function(a){a.addEventListener('click',function(e){var el=document.querySelector(a.getAttribute('href'));if(!el)return;e.preventDefault();var y=el.getBoundingClientRect().top+window.scrollY-document.querySelector('.sitenav').getBoundingClientRect().bottom-12;cancelAnimationFrame(scrollRaf);var s=window.scrollY,d=y-s,dur=900,t0=performance.now();(function step(t){var p=Math.min((t-t0)/dur,1);window.scrollTo(0,s+d*ease(p));if(p<1)scrollRaf=requestAnimationFrame(step);})(t0);});});})();</script><script>(function(){var links={};document.querySelectorAll('.nav-links a[href^="#"]').forEach(function(a){links[a.getAttribute('href').slice(1)]=a});var ids=Object.keys(links);var secs=ids.map(function(id){return document.getElementById(id)}).filter(Boolean).sort(function(a,b){return a.getBoundingClientRect().top-b.getBoundingClientRect().top});if(!secs.length)return;var cur='',locked=null,lockTimer=null;function setActive(id){if(id===cur)return;cur=id;ids.forEach(function(k){links[k].classList.toggle('active',k===id)})}function update(){if(locked){setActive(locked);return}var line=120,id='';secs.forEach(function(s){if(s.getBoundingClientRect().top<=line){id=s.id}});var lastSec=secs[secs.length-1];var distToBottom=document.documentElement.scrollHeight-(window.innerHeight+window.scrollY);if(lastSec&&distToBottom<=300&&lastSec.getBoundingClientRect().top<window.innerHeight){id=lastSec.id}setActive(id)}document.querySelectorAll('.nav-links a[href^="#"]').forEach(function(a){a.addEventListener('click',function(){locked=a.getAttribute('href').slice(1);setActive(locked);clearTimeout(lockTimer);lockTimer=setTimeout(function(){locked=null;update()},1000)})});var ticking=false;window.addEventListener('scroll',function(){if(ticking)return;ticking=true;requestAnimationFrame(function(){ticking=false;update()})},{passive:true});update();})();</script><form id="costform" method="POST" action="/api/charge-cost-form" style="position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;opacity:0"><input type="hidden" name="charge_id" id="form_charge_id"><input type="hidden" name="cost_per_kwh" id="form_cost_per_kwh"><input type="hidden" name="start_ts" id="form_start_ts"></form><div id="costmodal" style="display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.7);align-items:center;justify-content:center"><div style="background:#1a1d21;border:1px solid #333;border-radius:16px;padding:24px;max-width:320px;width:90%"><h3 style="margin:0 0 12px">DC charging cost</h3><p class="sub" style="margin:0 0 12px">Enter what you paid per kWh:</p><form onsubmit="saveCost();return false;" style="margin:0"><input id="costinput" type="number" inputmode="decimal" step="0.01" min="0" placeholder="0.48" style="width:100%;padding:10px;font-size:16px;border-radius:8px;border:1px solid #444;background:#0b0d10;color:#fff;box-sizing:border-box"><div style="display:flex;gap:10px;margin-top:16px"><button type="submit" style="flex:1;padding:10px;border-radius:8px;border:none;background:#7ed321;color:#000;font-weight:600;cursor:pointer">Save</button><button type="button" onclick="closeCostModal()" style="flex:1;padding:10px;border-radius:8px;border:1px solid #444;background:transparent;color:#fff;cursor:pointer">Cancel</button></div><div style="margin-top:12px;text-align:center"><a class="cost-action" href="#" onclick="clearCost();return false;" style="color:#888;font-size:12px">clear entered cost</a></div></form></div></div><script>var _costChargeId=null;var _costStartTs=null;function openCostModal(chargeId,currentVal,startTs){_costChargeId=chargeId;_costStartTs=(startTs===undefined||startTs===null)?'':startTs;document.getElementById('costinput').value=((currentVal===null||currentVal===undefined)?'':currentVal);document.getElementById('costmodal').style.display='flex';document.getElementById('costinput').focus();}function closeCostModal(){document.getElementById('costmodal').style.display='none';_costChargeId=null;}function saveCost(){var v=parseFloat(document.getElementById('costinput').value);if(v===null||v===undefined||isNaN(v)||v<0){alert('Enter a valid price');return;}document.getElementById('form_charge_id').value=_costChargeId;document.getElementById('form_start_ts').value=_costStartTs;document.getElementById('form_cost_per_kwh').value=v;document.getElementById('costform').submit();}function clearCost(){if(!confirm('Clear the entered cost for this session?'))return;document.getElementById('form_charge_id').value=_costChargeId;document.getElementById('form_start_ts').value=_costStartTs;document.getElementById('form_cost_per_kwh').value='';document.getElementById('costform').submit();}document.getElementById('costmodal').addEventListener('click',function(e){if(e.target===this)closeCostModal();});document.addEventListener('keydown',function(e){if(e.key==='Escape'){var m=document.getElementById('costmodal');if(m&&m.style.display!=='none')closeCostModal();}});</script></body></html>"""

_BOLT_SVG = ('<svg class="bolt" viewBox="0 0 24 24" aria-hidden="true">'
            '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'
            '</svg>')

JS_TEMPLATE = """
(function() {
  var el = document.getElementById('poll-age');
  if (!el || !el.dataset.ts) return;
  var timestamp = Number(el.dataset.ts);
  if (!Number.isFinite(timestamp)) return;
  function updatePollAge() {
    var seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    var age = 'just now';
    if (seconds >= 86400) {
      var days = Math.floor(seconds / 86400);
      age = days + (days === 1 ? ' day ago' : ' days ago');
    } else if (seconds >= 3600) {
      age = Math.floor(seconds / 3600) + ' hr ago';
    } else if (seconds >= 60) {
      age = Math.floor(seconds / 60) + ' min ago';
    }
    el.textContent = 'Last poll ' + age;
  }
  updatePollAge();
  setInterval(updatePollAge, 60000);
})();
/* Evaluated at use time, not cached, so an OS-level change while the page is
   open takes effect on the next interaction. */
function prefersReducedMotion() {
  return !!(window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}
/* Segmented range controls (battery 7/14/30d, pack health and overnight
   trend 1M/6M/1Y/All): one helper, each button shows its own panel. */
function segToggle(segId, prefix) {
  var seg = document.getElementById(segId);
  if (!seg) return;
  seg.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => {
      seg.querySelectorAll('button')
        .forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      seg.querySelectorAll('button').forEach(x => {
        document.getElementById(prefix + x.dataset.n).hidden = (x !== b);
      });
    });
  });
}
segToggle('battseg', 'batt');
segToggle('packseg', 'pack');
segToggle('trendseg', 'trend');
/* Daily activity: progressive list. Day groups ship as JSON; the latest 7 render
   first, "Show earlier" appends 7 more, and the month dropdown jumps to a
   month (showing that whole month, then earlier from there). */
(function () {
  var dataEl = document.getElementById('actdata');
  var listEl = document.getElementById('actdays');
  var moreBtn = document.getElementById('actshowmore');
  var monthSel = document.getElementById('actmonth');
  if (!dataEl || !listEl || !moreBtn) return;
  var days = JSON.parse(dataEl.textContent).days;
  var PAGE = 7, start = 0, shown = PAGE;
  /* render() rebuilds the whole list; render(from) appends days[from..end)
     to what is already on screen, so rows the user has expanded stay open
     and the scroll position holds when "Show earlier" is pressed. */
  function render(from) {
    var end = Math.min(start + shown, days.length), html = '', i, d;
    if (from == null) { listEl.innerHTML = ''; from = start; }
    for (i = from; i < end; i++) {
      d = days[i];
      html += '<details class="aday"><summary><div class="asum"><span>' +
        d.title + '</span><span class="dim">' + d.summary + '</span></div>' +
        d.abar + '</summary>' + d.body + '</details>';
    }
    listEl.insertAdjacentHTML('beforeend', html);
    moreBtn.hidden = end >= days.length;
  }
  moreBtn.addEventListener('click', function () {
    var from = Math.min(start + shown, days.length);
    var prevCount = from - start;
    shown += PAGE;
    render(from);
    if (prefersReducedMotion()) return;
    /* Staggered entrance for the freshly appended rows. */
    var rows = listEl.querySelectorAll('details.aday');
    var fresh = rows.length - prevCount, i;
    for (i = 0; i < fresh; i++) {
      rows[prevCount + i].animate(
        [{opacity: 0, transform: 'translateY(14px)'},
         {opacity: 1, transform: 'translateY(0)'}],
        {duration: 340, delay: i * 60, easing: 'cubic-bezier(0.4,0,0.2,1)',
         fill: 'backwards'});
    }
  });
  if (monthSel) monthSel.addEventListener('change', function () {
    var m = monthSel.value, i;
    start = 0; shown = PAGE;
    if (m !== 'latest') {
      for (i = 0; i < days.length; i++)
        if (days[i].month === m) { start = i; break; }
      shown = 0;
      for (i = start; i < days.length && days[i].month === m; i++) shown++;
    }
    render();
  });
  render();
})();

/* Smooth accordions: intercept the native <details> toggle on day rows,
   charging rows, and the night-by-night breakdown, and animate the body
   height instead. Own IIFE (not inside the activity one, which returns early
   when there is no activity data). State is read from the live `open`
   attribute, so toggles done by the browser itself (find-in-page auto-open,
   #fragment navigation) can never desync it; data-closing marks only the
   in-flight collapse, during which `open` is still set. */
(function () {
  var EASE = 'cubic-bezier(0.4,0,0.2,1)';
  function isOpen(details) {
    return details.open && details.dataset.closing !== '1';
  }
  function setAccOpen(details, open) {
    var body = details.querySelector('.abody, .chargedetail');
    if (!body) { details.open = open; return; }
    /* Measure BEFORE cancelling: cancel() snaps the body back to its natural
       size, so reversing a half-finished animation would otherwise jump. */
    var from = details.open ? body.getBoundingClientRect().height : 0;
    var op = details.open ? parseFloat(getComputedStyle(body).opacity) : .3;
    body.getAnimations().forEach(function (a) { a.cancel(); });
    if (open) {
      delete details.dataset.closing;
      details.open = true;
      /* Lazy images reserve their box via width/height attributes, so
         scrollHeight is already final here. */
      var to = body.scrollHeight;
      body.animate([{height: from + 'px', opacity: op},
                    {height: to + 'px', opacity: 1}],
                   {duration: 320, easing: EASE});
    } else {
      details.dataset.closing = '1';
      var anim = body.animate([{height: from + 'px', opacity: op},
                               {height: '0px', opacity: .3}],
                              {duration: 260, easing: EASE, fill: 'forwards'});
      anim.onfinish = function () {
        /* fill:forwards holds the collapsed state until `open` is removed,
           so there is no frame at full height. Cancel afterwards so no stale
           fill can hide the body if the page later toggles natively. */
        details.open = false;
        delete details.dataset.closing;
        anim.cancel();
      };
    }
  }
  document.addEventListener('click', function (e) {
    var t = e.target;
    var summary = (t && t.closest) ? t.closest('summary') : null;
    if (!summary) return;
    var details = summary.parentElement;
    if (!details || details.tagName !== 'DETAILS') return;
    if (!details.classList.contains('aday') &&
        !details.classList.contains('sacc')) return;
    if (prefersReducedMotion() ||
        !details.querySelector('.abody, .chargedetail')) return;
    e.preventDefault();
    setAccOpen(details, !isOpen(details));
  });
})();

/* Back-to-top: appears past 600px of scroll. rAF-throttled scroll
   check: explicit and unambiguous beats clever. */
(function () {
  var btn = document.getElementById('toTop');
  if (!btn) return;
  var ticking = false;
  function update() {
    btn.classList.toggle('show',
      window.scrollY > 600);
    ticking = false;
  }
  window.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(update); }
  }, {passive: true});
  update();
  btn.addEventListener('click', function () {
    window.scrollTo({top: 0,
                     behavior: prefersReducedMotion() ? 'auto' : 'smooth'});
    /* The button hides itself once we are near the top, which would drop
       keyboard focus to <body>. Park focus on the page container instead so
       the next Tab continues from the top of the page. */
    var pageTop = document.querySelector('.wrap');
    if (pageTop) {
      pageTop.setAttribute('tabindex', '-1');
      pageTop.focus({preventScroll: true});
    }
  });
})();

/* Trip maps are static, tile-backed images rendered at build time (the
   viewer blocks external network), so no client-side map library is needed. */
"""


# ---------------------------------------------------------------- static maps

_MAP_W, _MAP_H = 880, 440
# Esri World Dark Gray Canvas basemap (free tier, no API key; note the
# {z}/{y}/{x} tile order). Minimal streets-and-labels style, already dark
# so no CSS invert filter is needed. (CARTO's free tiles now watermark
# "API KEY REQUIRED"; OSM standard was too busy for the trip map.)
_TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
             "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}")
_TILE_FILTER = ""
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

    z = 10
    # Esri's Dark Gray Canvas maxes out at z16; higher zooms return
    # "Map data not yet available" placeholder tiles.
    for cand in range(16, 9, -1):
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
               f'style="width:100%;height:auto;display:block;{_TILE_FILTER}" '
               f'onload="this.classList.add(\'ld\')" '
               f'onerror="this.style.display=\'none\'">')
    # Schematic fallback when tiles failed: SVG routes on the dark card.
    return (f'<div class="mapwrap">{img}{svg}'
            f'<div class="mapattr">&copy; Esri &amp; contributors</div></div>')
# ---------------------------------------------------------------- sections

# ---------------------------------------------------------------- daily activity

_ACT_CLASS = {"moving": "st-moving", "charging": "st-charging",
              "parked": "st-parked", "sleep": "st-sleep",
              "unknown": "st-unknown"}
_ACT_LABEL = {"moving": "driving", "charging": "charging",
              "parked": "awake", "sleep": "deep sleep",
              "unknown": "no data"}


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
               f'{_ACT_LABEL[s["state"]]}')
        parts.append(
            f'<div class="aseg {_ACT_CLASS[s["state"]]}" '
            f'style="left:{a / 14.4:.2f}%;width:{(b - a) / 14.4:.2f}%" '
            f'title="{_esc(tip)}"></div>')
    if now_min is not None:
        parts.append(f'<div class="anow" style="left:{now_min / 14.4:.2f}%">'
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
        '<div class="alegend" style="justify-content:center;margin-top:2px">'
        f'<span><i style="background:{GREEN}"></i>plug-in {base:.0f}%</span>'
        f'<span><i style="background:{AMBER}"></i>added</span>'
        f'<span><i style="background:#2b2b2e"></i>empty</span></div>')
    kw_v = f'{live["kw"]:.1f}<small> kW</small>' if live.get("kw") else "-"
    kw_s = f'{live["mihr"]:.0f} mi/hr' if live.get("mihr") else "warming up"
    sess_s = f'since {_fmt_time(int(live["session_start"]))}'
    note_txt = (f'{tgt:.0f}% by about {live["end_txt"]} at this rate'
                if live.get("end_txt") else "")
    sub_html = (f'<div class="sub" style="text-align:center">{note_txt}</div>'
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
        f'<div class="sub" style="margin:2px 0 4px">{month} &middot; '
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


def _render_point_map(lat: float, lon: float):
    """Stitch the charge-location tile window. Returns JPEG bytes, or None
    when any tile can't be fetched."""
    import io
    from PIL import Image, ImageDraw
    z = 16
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
        f'onerror="this.closest(\'.chargemap\').style.display=\'none\'">'
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
                           'class="cost-action" '
                           'style="color:#7ed321;font-size:12px">edit</a></span>'
                           % (_cls3, abs(_save3), _verb3, _ev_c3, _gp_val,
                              _dc_rate, c["id"], _dc_rate, c["start_ts"]))
        else:
            savings_bit = ('<span><a href="#" onclick="event.stopPropagation();'
                           'openCostModal(%d, 0, %d);return false;" '
                           'class="cost-action" '
                           'style="color:#7ed321">? add cost</a></span>'
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
                  live_is_home: bool = False, eff: float = 3.0) -> str:
    # The in-progress session is shown in the live card; keep it out of
    # the history list so it is not shown twice.
    hist = [ch for ch in charges
            if not (live and ch["end_ts"] >= live["session_start"])]
    if not hist and not live:
        return ('<div class="empty">No charging sessions yet.<br>Ping me when '
                'you plug in and I will track the session.</div>')
    parts = []
    if live:
        parts.append(_live_charge_html(live, live_is_home))
    if hist:
        scale = max(ch["kwh_added"] for ch in hist) * 1.15 or 1.0
        rows = "".join(_charge_row_html(c, scale, open_=(i == 0), eff=eff)
                       for i, c in enumerate(hist))
        export = ("" if os.environ.get("WATTSON_DEMO") else
                  '<a href="/api/charges.csv" '
                  'style="font-size:14px;color:var(--dim)">Export CSV</a>')
        parts.append(
            f'<div class="card"><h3 class="row"><span>Charging log</span>'
            f'{export}</h3>'
            f'{_charge_month_summary(hist)}'
            f'{rows}</div>')
    footnote = ('<p class="note">Home charging costed at $%.2f/kWh '
                '(set RIVIAN_HOME_KWH_RATE in .env). Away sessions show no '
                'cost; public rates vary. Tap "? add cost" on a DC fast '
                'charge to record what you paid.</p>' % HOME_KWH_RATE)
    return "".join(parts) + footnote


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

    Mirrors the chart bars 1:1. Watts is the primary metric; periods with
    no fresh data show "no data" instead of fake zeros. The * marker uses
    the same 1.6x-median baseline as the chart's red bars.
    """
    if not periods:
        return '<p class="dim">No idle periods measured yet.</p>'
    rows = []
    for o in reversed(periods):
        warn = ' <span class="warn">*</span>' if o["id"] in bad else ""
        if o["no_data"]:
            watts_txt, lost_txt = "no data", "no data"
        else:
            watts_txt = f"{o['watts']:.0f} W"
            lost_txt = f"{o['kwh']:.2f} kWh"
        rows.append(
            f"<tr><td>{o['label']}{warn}</td>"
            f"<td>{o['hours']:.1f} h</td>"
            f"<td>{watts_txt}</td>"
            f"<td>{lost_txt}</td>"
            f"<td>{int(o.get('wake_count') or 0)}</td></tr>")
    note = ('<p class="note"><span class="warn">*</span> unusually high vs '
            'baseline.</p>' if bad else '')
    return ('<div style="overflow-x:auto"><table class="drain">'
            '<tr><th>Idle period</th><th>Duration</th>'
            '<th>Avg draw</th><th>Lost</th><th>Wake-ups</th></tr>'
            + "".join(rows) + "</table></div>" + note)


# ---------------------------------------------------------------- generate


def _idle_no_data(snapshots, t1: int, t2: int) -> bool:
    """True if the car was cloud-offline for essentially the whole idle
    period, meaning the SoC readings are stale cache, not measurements.
    A zero SoC change under these conditions is "no data", not "no drain"."""
    win = [s for s in snapshots
           if t1 <= s["ts"] <= t2 and s.get("cloud_online") is not None]
    if not win:
        return True  # no cloud_online data at all (old polls)
    offline = sum(1 for s in win if s["cloud_online"] == 0)
    return offline / len(win) > 0.9


def _idle_history(drains: list[dict], snapshots) -> list[dict]:
    """Idle periods between drives, oldest first.

    Uses the drain events from analyze.py (each defined as last drive
    ended -> next drive started) instead of a fixed clock window. Watts
    is the primary metric. Periods where the API slept through (stale
    SoC, car cloud-offline) are marked no_data instead of showing
    fake zeros.
    """
    out = []
    for d in drains:
        t1, t2 = d["sleep_ts"], d["wake_ts"]
        hours = d.get("hours") or 0
        watts = d.get("steady_watts") or d.get("watts_avg") or 0
        kwh = d.get("kwh_lost") or 0
        no_data = kwh <= 0 and _idle_no_data(snapshots, t1, t2)
        # Label: "Oct 7" for same-day, "Oct 7-9" for multi-day spans.
        d1, d2 = _ldt(t1), _ldt(t2)
        if d1.date() == d2.date():
            label = d1.strftime("%b %d")
        else:
            label = f"{d1.strftime('%b %d')}-{d2.strftime('%d')}"
        out.append({
            "id": t1,  # unique key for abnormal-period flagging
            "day": d2.strftime("%Y-%m-%d"),  # for heat-strip positioning
            "label": label,
            "hours": round(hours, 1),
            "kwh": round(kwh, 3),
            "watts": round(watts, 1),
            "wake_count": int(d.get("wake_count") or 0),
            "no_data": no_data,
            "window": f"{_fmt_ts(t1)} \u2192 {_fmt_ts(t2)}",
            "t1": t1, "t2": t2,
            "soc_start": d.get("soc_start"), "soc_end": d.get("soc_end"),
        })
    return out


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
        latest = con.execute(
            "SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        snapshots = [dict(r) for r in con.execute(
            "SELECT ts, soc, capacity_kwh, range_mi, cloud_online, charger_state"
            " FROM snapshots ORDER BY ts")]
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
            '<div class="card" style="margin-top:16px">'
            "<h3>Fuel savings by session</h3>"
            '<div class="sub">Per charging session, priced at each '
            'day&apos;s gas price</div>'
            '<div style="overflow-x:auto"><table class="ctab"><thead><tr>'
            "<th>Date</th><th>Type</th><th>kWh</th><th>EV cost</th>"
            "<th>Gas $/gal</th><th>Gas cost</th><th>Saved</th>"
            "</tr></thead><tbody>" + _srows + "</tbody></table></div></div>"
        )
    else:
        savings_table = (
            '<div class="card" style="margin-top:16px">'
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

    # Phantom drain hero: the latest idle period between drives. Watts is the
    # primary metric. Periods with no fresh data (car asleep the whole
    # time) show "no data" instead of a fake zero.
    idles = _idle_history(drains, snapshots)
    ov = idles[-1] if idles else None
    if ov and not ov["no_data"]:
        window_bit = _esc(ov["window"])
        detail = (f"{ov['kwh']:.2f} kWh over {ov['hours']:.1f} h"
                  f" &bull; {ov['wake_count']} wake-up"
                  f"{'s' if ov['wake_count'] != 1 else ''}")
        idle_stat = (
            '<a class="stat" href="#battery"><div class="k">Phantom drain</div>'
            f'<div class="v amber">{ov["watts"]:.0f}<small> W</small></div>'
            f'<div class="s">{window_bit}</div>'
            f'<div class="s">{detail}</div></a>')
    elif ov:
        window_bit = _esc(ov["window"])
        idle_stat = (
            '<a class="stat" href="#battery"><div class="k">Phantom drain</div>'
            '<div class="v amber">-<small> W</small></div>'
            f'<div class="s">{window_bit}</div>'
            '<div class="s">no fresh data (car was asleep)</div></a>')
    else:
        idle_stat = (
            '<a class="stat" href="#battery"><div class="k">Phantom drain</div>'
            '<div class="v amber">-<small> W</small></div>'
            '<div class="s">no data yet</div></a>')
    # Phantom drain trend: period-by-period bars, so "typical" has a visual
    # anchor. A period over 1.6x the median watts (needs 3+ measured
    # periods) flags red. Gets the same 1M / 6M / 1Y / All range control
    # as Pack health: each range recomputes its own average and abnormal
    # flags. 7N and 1M draw bars with the real watts axis; 6M, 1Y and All
    # draw a heat strip (one cell per period, weeks as columns, color
    # intensity = watts draw, abnormal periods stay red) instead of an
    # unreadable year of thin bars. Averaging into weekly bars was
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
               if len(measured) >= 3 and med > 0 and o["watts"] > 1.6 * med}
        n_nodata = len(periods) - len(measured)
        avg = (f"Average <b class='amber'>{avg_w:.0f} W</b> "
               f"({tot_kwh:.2f} kWh total) "
               f"across {len(measured)} idle period"
               f"{'s' if len(measured) != 1 else ''}")
        if n_nodata:
            avg += f" &bull; {n_nodata} with no data"
        avg += "."
        if len(measured) < 3:
            avg += (" Early data: the average firms up "
                    "after a few idle periods.")
        cols = [RED if o["id"] in bad else AMBER for o in periods]
        thin = len(periods) > 16
        labels = ["no data" if o["no_data"] else f"{o['watts']:.0f} W"
                  for o in periods]
        sublabels = ["" if o["no_data"] else f"{o['hours']:.0f}h"
                     for o in periods]
        if style == "strip":
            chart = _heat_strip(periods, bad)
        else:
            bars = _bars(
                [(o["label"], o["watts"], c) for o, c in zip(periods, cols)],
                labels=labels, sublabels=sublabels, thin=thin,
                y_unit="W", ref_value=med if med > 0 else None)
            chart = bars
        table = _idle_table(periods, bad)
        details = (
            '<details class="sacc"><summary class="dim" '
            'style="font-size:14px;cursor:pointer;margin-top:14px">'
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
        trend_note = ("Each bar is one idle period between drives, however "
                      "long it lasted. Watts is the average draw; periods "
                      "with no fresh data (car asleep) show as no data.")
    else:
        trend_note = ""
    trip_month_html, trips_html = _activity_html(
        trips, errands, activity_segments())

    script = JS_TEMPLATE
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
                        eff=(lifetime_mpk if lifetime_mpk else 3.0)))
    savings_section = (
        '<h2 class="sec" id="savings">Fuel savings vs gas</h2>'
        + savings_table
    ) if savings_table else ""
    if live:
        charging_top, charging_bottom = charging_section, savings_section
    else:
        charging_top, charging_bottom = "", charging_section + savings_section
    repl = {
        "__FAVICON__": (
        '<link rel="icon" href="data:image/x-icon;base64,AAABAAUAEBAAAAEAIAAoBAAAVgAAABgYAAABACAAKAkAAH4EAAAgIAAAAQAgACgQAACmDQAAMDAAAAEAIAAoJAAAzh0AAEBAAAABACAAKEAAAPZBAAAoAAAAEAAAACAAAAABACAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAXQAAAOsAAADrAAAAXgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcwAAAP4AAAD/AAAA/wAAAP4AAAB0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAfQAAAP8AAAD5AAAAcgAAAHIAAAD5AAAA/wAAAH4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgwAAAP8AAADzAAAATQAAAFAAAABPAAAATQAAAPMAAAD/AAAAhAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAiAAAAP8AAADvAAAARAAAAL0AAACeAAAAngAAAL4AAABEAAAA7wAAAP8AAACIAAAAAAAAAAAAAAAAAAAAiwAAAP8AAADtAAAAQQAAAMUAAAD/AAAAkAAAAI8AAAD/AAAAxQAAAEEAAADtAAAA/wAAAIwAAAAAAAAAjQAAAP8AAADvAAAAPwAAAMYAAAD/AAAAtwAAAAoAAAAKAAAAtwAAAP8AAADGAAAAPwAAAO8AAAD/AAAAjgAAAEQAAABHAAAAJgAAAGIAAAD/AAAA+QAAAA8AAAAAAAAAAAAAAA8AAAD5AAAA/wAAAGMAAAAmAAAARwAAAEQAAACOAAAA/wAAAO8AAABAAAAAyAAAAP8AAAC1AAAACQAAAAkAAAC1AAAA/wAAAMgAAABAAAAA7wAAAP8AAACOAAAAAAAAAIwAAAD/AAAA7QAAAEEAAADHAAAA/wAAAI8AAACOAAAA/wAAAMcAAABBAAAA7QAAAP8AAACNAAAAAAAAAAAAAAAAAAAAiAAAAP8AAADuAAAARQAAAMAAAACeAAAAngAAAMAAAABFAAAA7gAAAP8AAACJAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACEAAAA/wAAAPMAAABOAAAAUQAAAFIAAABOAAAA8wAAAP8AAACEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH4AAAD/AAAA+QAAAHEAAABxAAAA+QAAAP8AAAB+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdAAAAP4AAAD/AAAA/wAAAP4AAAB0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABeAAAA7AAAAOwAAABeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACgAAAAYAAAAMAAAAAEAIAAAAAAAAAkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKAAAAlwAAAPQAAADzAAAAlwAAAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAADFAAAA/wAAAP8AAAD/AAAA/wAAAMYAAAARAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEwAAAM4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADOAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAA0QAAAP8AAAD/AAAA/AAAAJYAAACWAAAA/AAAAP8AAAD/AAAA0QAAABUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAADSAAAA/wAAAP8AAAD2AAAATwAAAAAAAAAAAAAATgAAAPYAAAD/AAAA/wAAANIAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAANIAAAD/AAAA/wAAAPEAAAA/AAAARAAAAFsAAABaAAAARAAAAD4AAADxAAAA/wAAAP8AAADSAAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAA0gAAAP8AAAD/AAAA7gAAADYAAABNAAAA9wAAAG4AAABtAAAA+AAAAE0AAAA2AAAA7gAAAP8AAAD/AAAA0gAAABUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAADRAAAA/wAAAP8AAADtAAAAMgAAAFIAAAD5AAAA/wAAAG4AAABtAAAA/wAAAPoAAABSAAAAMgAAAO0AAAD/AAAA/wAAANEAAAAUAAAAAAAAAAAAAAAAAAAAEwAAANAAAAD/AAAA/wAAAO0AAAAxAAAAUwAAAPoAAAD/AAAA/wAAAGEAAABfAAAA/wAAAP8AAAD6AAAAUwAAADEAAADtAAAA/wAAAP8AAADQAAAAEwAAAAAAAAASAAAAzgAAAP8AAAD/AAAA8QAAADUAAABQAAAA+gAAAP8AAAD/AAAAnAAAAAMAAAADAAAAmwAAAP8AAAD/AAAA+gAAAFAAAAA1AAAA8QAAAP8AAAD/AAAAzgAAABIAAAC6AAAA8gAAAPEAAADnAAAAPgAAADwAAAD4AAAA/wAAAP8AAACWAAAAAQAAAAAAAAAAAAAAAQAAAJUAAAD/AAAA/wAAAPgAAAA9AAAAPgAAAOcAAADxAAAA8gAAALoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJ8AAAD/AAAA/wAAAPEAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAMAAADwAAAA/wAAAP8AAACgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC5AAAA8QAAAPEAAADmAAAAPgAAAD8AAAD5AAAA/wAAAP8AAACSAAAAAQAAAAAAAAAAAAAAAQAAAJIAAAD/AAAA/wAAAPkAAABAAAAAPQAAAOYAAADxAAAA8QAAALoAAAASAAAAzwAAAP8AAAD/AAAA8QAAADUAAABTAAAA+wAAAP8AAAD/AAAAlwAAAAIAAAACAAAAlwAAAP8AAAD/AAAA+wAAAFMAAAA0AAAA8QAAAP8AAAD/AAAAzwAAABIAAAAAAAAAEwAAANAAAAD/AAAA/wAAAO0AAAAwAAAAWAAAAPsAAAD/AAAA/wAAAF8AAABeAAAA/wAAAP8AAAD7AAAAVwAAADEAAADtAAAA/wAAAP8AAADRAAAAFAAAAAAAAAAAAAAAAAAAABQAAADRAAAA/wAAAP8AAADsAAAAMQAAAFYAAAD6AAAA/wAAAG0AAABtAAAA/wAAAPsAAABWAAAAMQAAAOwAAAD/AAAA/wAAANIAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAA0gAAAP8AAAD/AAAA7gAAADYAAABRAAAA+QAAAG4AAABtAAAA+QAAAFIAAAA1AAAA7QAAAP8AAAD/AAAA0gAAABUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAANIAAAD/AAAA/wAAAPEAAAA/AAAARwAAAF0AAABdAAAASQAAAD4AAADxAAAA/wAAAP8AAADTAAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAADSAAAA/wAAAP8AAAD2AAAATgAAAAAAAAAAAAAATgAAAPYAAAD/AAAA/wAAANMAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAA0gAAAP8AAAD/AAAA/AAAAJUAAACVAAAA/AAAAP8AAAD/AAAA0gAAABUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAM4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADOAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAADFAAAA/wAAAP8AAAD/AAAA/wAAAMYAAAARAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKAAAAlwAAAPQAAAD0AAAAmAAAAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAIAAAAEAAAAABACAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALwAAALgAAAD4AAAA9wAAALgAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAD1AAAA/wAAAP8AAAD/AAAA/wAAAPYAAABRAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABiAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPwAAABjAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbgAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABvAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHcAAAD/AAAA/wAAAP8AAAD/AAAA9wAAAJoAAACaAAAA9wAAAP8AAAD/AAAA/wAAAP8AAAB5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB/AAAA/wAAAP8AAAD/AAAA/wAAAOgAAAA1AAAAAAAAAAAAAAA1AAAA5wAAAP8AAAD/AAAA/wAAAP8AAAB/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhAAAAP8AAAD/AAAA/wAAAP8AAADbAAAAIgAAABUAAAASAAAAEgAA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AGMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFEAAAD2AAAA/wAAAP8AAAD/AAAA/wAAAPYAAABSAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAAAAC4AAAA+AAAAPkAAAC5AAAAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACgAAAAwAAAAYAAAAAEAIAAAAAAAACQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAB5AAAA1QAAAPsAAAD6AAAA1QAAAHkAAAALAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJwAAANgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADZAAAAJwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3AAAA7AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA7AAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEIAAADyAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPMAAABDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASQAAAPYAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD2AAAASgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABNAAAA+AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+AAAAE4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFEAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPUAAAD1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADzAAAAYAAAAAUAAAAFAAAAYAAAAPMAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD6AAAAVQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAAA+gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOcAAAAxAAAAAAAAAAAAAAAAAAAAAAAAADEAAADmAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA2wAAACIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhAAAA2gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADRAAAAGAAAAAAAAAAYAAAAkQAAAAAAAAAAAAAAkQAAABgAAAAAAAAAFwAA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AAAAAABCAAAA9gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANEAAAARAAAAAAAAAB8AAADjAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAqAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAApwAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOQAAAAgAAAAAAAAABEAAADQAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9gAAAEMAAADMAAAA5AAAAOQAAADkAAAA5AAAAOQAAADhAAAAvQAAABcAAAAAAAAABAAAAM8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAACsAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAKsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADRAAAABAAAAAAAAAAWAAAAvQAAAOEAAADkAAAA5AAAAOQAAADkAAAA5AAAAM0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPQAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOUAAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAADjAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOIAAAAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAADgAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAQQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADJAAAA4wAAAOMAAADkAAAA4wAAAOMAAADgAAAAvAAAABYAAAAAAAAABAAAANUAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAACmAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAKUAAAD/AAAA/wAA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AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABVAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAMgAAAASAAAAAAAAACMAAADfAAAA3QAAAAAAAAAAAAAA2QAAAOEAAAAmAAAAAAAAABEAAADGAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVQAAAPsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADRAAAAGAAAAAAAAAAbAAAAmAAAAAAAAAAAAAAAmgAAAB4AAAAAAAAAFwAAANAAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAVgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA2wAAACIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhAAAA2gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABWAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABWAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOYAAAAxAAAAAAAAAAAAAAAAAAAAAAAAADAAAADmAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVQAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADzAAAAXgAAAAUAAAAFAAAAXQAAAPMAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAVgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFQAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPUAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABPAAAA+AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASgAAAPYAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD2AAAASwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEIAAADzAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPMAAABDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAA7AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA7QAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKAAAANkAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADaAAAAKQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAB6AAAA1gAAAPwAAAD8AAAA1gAAAHoAAAAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAQAAAAIAAAAABACAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA7AAAAowAAAOUAAAD8AAAA+gAAAOQAAACiAAAAOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAClAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAACoAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB4AAADSAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANMAAAAeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACoAAADjAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA5AAAACsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADUAAADrAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADsAAAANgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADwAAADwAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPEAAAA+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEMAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9QAAAEUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEgAAAD2AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD3AAAASQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE0AAAD3AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPgAAABPAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA8wAAAIIAAAA4AAAAOAAAAIIAAADzAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAFIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA3AAAACkAAAAAAAAAAAAAAAAAAAAAAAAAKgAAANsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD6AAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAyQAAABYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAAyAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABWAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFYAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAtwAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAC1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAApwAAAAYAAAAAAAAAAAAAACkAAAAuAAAAAAAAAAAAAAAuAAAAKgAAAAAAAAAAAAAABQAAAKQAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAlwAAAAIAAAAAAAAAAAAAADsAAADuAAAAeQAAAAAAAAAAAAAAdgAAAO4AAAA8AAAAAAAAAAAAAAACAAAAlgAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAiQAAAAEAAAAAAAAAAAAAAEYAAAD0AAAA/wAAAHsAAAAAAAAAAAAAAHcAAAD/AAAA9AAAAEgAAAAAAAAAAAAAAAEAAACIAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAfQAAAAAAAAAAAAAAAAAAAFAAAAD4AAAA/wAAAP8AAAB8AAAAAAAAAAAAAAB4AAAA/wAAAP8AAAD4AAAAUgAAAAAAAAAAAAAAAAAAAHsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD8AAAAWwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFoAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAdAAAAAAAAAAAAAAAAAAAAFoAAAD6AAAA/wAAAP8AAAD/AAAAewAAAAAAAAAAAAAAeQAAAP8AAAD/AAAA/wAAAPsAAABcAAAAAAAAAAAAAAAAAAAAcwAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPwAAABcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAbAAAAAAAAAAAAAAAAAAAAGAAAAD8AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAHoAAAD/AAAA/wAAAP8AAAD/AAAA/AAAAGIAAAAAAAAAAAAAAAAAAABrAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFkAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAZgAAAAAAAAAAAAAAAAAAAGcAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB6AAAAAAAAAAAAAAB6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAagAAAAAAAAAAAAAAAAAAAGUAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD8AAAAWwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAYwAAAAAAAAAAAAAAAAAAAG0AAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAegAAAAAAAAAAAAAAegAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABvAAAAAAAAAAAAAAAAAAAAYgAAAP0AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAYgAAAAAAAAAAAAAAAAAAAHAAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHkAAAAAAAAAAAAAAHgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHEAAAAAAAAAAAAAAAAAAABgAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAYgAAAAAAAAAAAAAAAAAAAHIAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB1AAAAAAAAAAAAAABvAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAcgAAAAAAAAAAAAAAAAAAAGEAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAZAAAAAAAAAAAAAAAAAAAAHIAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADqAAAAIAAAAAAAAAAAAAAAHwAAAOkAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABzAAAAAAAAAAAAAAAAAAAAYwAAAP0AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPsAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAaQAAAAAAAAAAAAAAAAAAAHAAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADlAAAALAAAAAAAAAAAAAAAAAAAAAAAAAAqAAAA5AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHAAAAAAAAAAAAAAAAAAAABoAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAcwAAAAAAAAAAAAAAAAAAAGsAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADdAAAAIgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAADcAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAbAAAAAAAAAAAAAAAAAAAAHEAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD7AAAAVQAAAAAAAAAAAAAAAAAAAFAAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAfgAAAAAAAAAAAAAAAAAAAGEAAAD+AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADWAAAAGwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGgAAANUAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAABjAAAAAAAAAAAAAAAAAAAAfQAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPoAAABRAAAAAAAAAEwAAAD4AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAjAAAAAAAAAAAAAAAAAAAAFAAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADTAAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWAAAA0gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFQAAAAAAAAAAAAAAAAAAACLAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAE0AAADoAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD5AAAAiwAAAAEAAAAAAAAAAAAAACYAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADWAAAAFgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABYAAADVAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD2AAAAJwAAAAAAAAAAAAAAAQAAAIoAAAD4AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADqAAAAAgAAAAcAAAAJAAAACQAAAAkAAAAJAAAACQAAAAgAAAAGAAAAAQAAAAAAAAAAAAAAAAAAAAAAAACTAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADwAAAAHwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHwAAAPAAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAJcAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAYAAAAIAAAACQAAAAkAAAAJAAAACQAAAAkAAAAHAAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAugAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAvwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAC9AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAABgAAAAgAAAAIAAAACAAAAAgAAAAIAAAABwAAAAQAAAABAAAAAAAAAAAAAAAAAAAAAAAAAJgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAO0AAAAaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAXAAAA7AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAmQAAAAAAAAAAAAAAAAAAAAAAAAABAAAABAAAAAcAAAAIAAAACAAAAAgAAAAIAAAACAAAAAYAAAABAAAA5QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9wAAAIoAAAABAAAAAAAAAAAAAAAqAAAA+AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA0AAAABIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASAAAAzwAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+AAAAC0AAAAAAAAAAAAAAAEAAACJAAAA9wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA5gAAAEwAAAD5AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAiwAAAAAAAAAAAAAAAAAAAFsAAAD9AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADNAAAAEwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASAAAAzAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/QAAAFwAAAAAAAAAAAAAAAAAAACKAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAE0AAAAAAAAAUQAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB+AAAAAAAAAAAAAAAAAAAAawAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANAAAAAVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVAAAAzwAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAG0AAAAAAAAAAAAAAAAAAAB7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+gAAAFMAAAAAAAAAAAAAAAAAAABVAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHIAAAAAAAAAAAAAAAAAAAB1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA1QAAABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbAAAA1QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHUAAAAAAAAAAAAAAAAAAABvAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD+AAAAZwAAAAAAAAAAAAAAAAAAAH0AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADeAAAAJAAAAAAAAAAAAAAAAAAAAAAAAAAkAAAA3gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAAAAAABmAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWQAAAPsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP0AAABhAAAAAAAAAAAAAAAAAAAAgAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAOUAAAAeAAAAAAAAAAAAAAAaAAAA5AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAH4AAAAAAAAAAAAAAAAAAABhAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABaAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAF0AAAAAAAAAAAAAAAAAAACAAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAbQAAAAAAAAAAAAAAbgAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAH4AAAAAAAAAAAAAAAAAAABfAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFsAAAD8AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD8AAAAXQAAAAAAAAAAAAAAAAAAAH4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHYAAAAAAAAAAAAAAHgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAH0AAAAAAAAAAAAAAAAAAABeAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWwAAAPwAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAPwAAABfAAAAAAAAAAAAAAAAAAAAeQAAAP4AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAB5AAAAAAAAAAAAAAB6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHkAAAAAAAAAAAAAAAAAAABfAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAF0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABbAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAGIAAAAAAAAAAAAAAAAAAABzAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAegAAAAAAAAAAAAAAegAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/gAAAHQAAAAAAAAAAAAAAAAAAABiAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAF0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFsAAAD8AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD9AAAAaAAAAAAAAAAAAAAAAAAAAG0AAAD+AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAHoAAAD/AAAA/wAAAP8AAAD/AAAA/gAAAG8AAAAAAAAAAAAAAAAAAABnAAAA/QAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWwAAAPwAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP4AAAByAAAAAAAAAAAAAAAAAAAAZAAAAPwAAAD/AAAA/wAAAP8AAAB7AAAAAAAAAAAAAAB5AAAA/wAAAP8AAAD/AAAA/QAAAGcAAAAAAAAAAAAAAAAAAABwAAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABbAAAA/AAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAHsAAAAAAAAAAAAAAAAAAABcAAAA+wAAAP8AAAD/AAAAfAAAAAAAAAAAAAAAeAAAAP8AAAD/AAAA/AAAAF8AAAAAAAAAAAAAAAAAAAB5AAAA/gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFoAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAiAAAAAEAAAAAAAAAAAAAAFAAAAD4AAAA/wAAAHwAAAAAAAAAAAAAAHcAAAD/AAAA+QAAAFQAAAAAAAAAAAAAAAEAAACGAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/AAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWQAAAPsAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAACXAAAAAgAAAAAAAAAAAAAAQwAAAPIAAAB7AAAAAAAAAAAAAAB2AAAA9AAAAEgAAAAAAAAAAAAAAAIAAACUAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABYAAAA+wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAKYAAAAGAAAAAAAAAAAAAAAvAAAAMwAAAAAAAAAAAAAANAAAADMAAAAAAAAAAAAAAAUAAACkAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFcAAAD7AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAtgAAAAsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsAAAC1AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVgAAAPoAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAADIAAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABUAAADHAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABVAAAA+gAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAANsAAAAoAAAAAAAAAAAAAAAAAAAAAAAAACgAAADaAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+wAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMAAAD6AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA8gAAAH8AAAA3AAAANwAAAH8AAADxAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+gAAAFQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUQAAAPgAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA+QAAAFIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABKAAAA9wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9wAAAEwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEQAAAD0AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA9QAAAEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPQAAAPAAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA8QAAAD4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1AAAA6wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA7AAAADYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACsAAADkAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA5QAAACwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHwAAANQAAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA1QAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPAAAAqAAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAAqQAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8AAAAowAAAOcAAAD9AAAA/QAAAOcAAACkAAAAPQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==">'
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
        "__IDLESTAT__": idle_stat,
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
        "__SCRIPT__": script,
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


