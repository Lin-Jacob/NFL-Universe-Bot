"""Small server-rendered charts for the stats website: plain HTML bars and inline SVG lines.

No JavaScript or outside files (the site's Content-Security-Policy allows neither). Colors come
from the page's CSS variables, so charts follow light and dark mode. Hovering a bar or a point
shows its exact value (a native tooltip), and every chart sits next to a table of the same numbers.
"""

from __future__ import annotations

from html import escape

from .embeds import fmt_num

CSS = """
.chart{margin:4px 0 2px}.chart h3{font-size:14px;margin:0 0 8px;color:var(--muted);font-weight:600}
.bars{display:grid;grid-template-columns:minmax(90px,max-content) 1fr max-content;gap:6px 10px;align-items:center}
.bars .lbl{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
.bars .track{height:14px;position:relative}
.bars .fill{height:100%;background:var(--accent);border-radius:0 4px 4px 0;min-width:2px}
.bars .fill.neg{background:var(--muted)}
.bars .val{font-size:13px;font-variant-numeric:tabular-nums;text-align:right}
svg.line{width:100%;height:auto;display:block;overflow:visible}
svg.line .grid{stroke:var(--line);stroke-width:1}
svg.line .axis{fill:var(--muted);font-size:11px}
svg.line .ln{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg.line .avg{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 4}
svg.line .pt{fill:var(--accent);stroke:var(--card);stroke-width:2}
svg.line .hit{fill:transparent}
svg.line a:hover .pt{r:6}
.cmp{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948}
@media (prefers-color-scheme:dark){.cmp{--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;
--s6:#008300;--s7:#9085e9;--s8:#e66767}}
.cmp svg .sr path{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.cmp svg .sr circle{stroke:var(--card);stroke-width:1.5}
.cmp svg:hover .sr{opacity:.25}.cmp svg .sr:hover{opacity:1}.cmp svg .sr:hover path{stroke-width:3}
.cmp svg .sr .hit{stroke:transparent;stroke-width:12;fill:none}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:8px 0 0;font-size:13px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px}
.filters{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end;margin:0 0 12px}
.filters label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}
.filters fieldset{border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin:0;display:flex;
flex-wrap:wrap;gap:4px 12px;font-size:13px;max-width:100%}
.filters fieldset legend{font-size:12px;color:var(--muted);padding:0 4px}
.filters fieldset label{flex-direction:row;align-items:center;gap:4px;color:var(--text);font-size:13px}
"""

SERIES_MAX = 8  # categorical slots; more players than this don't fit on one chart


def _e(v) -> str:
    return escape(str(v), quote=True)


def bars(rows: list[tuple[str, float, str]], title: str = "", unit: str = "") -> str:
    """Horizontal bar chart. rows = [(label HTML, value, tooltip text)], drawn in the given order.
    Bars are scaled to the largest value; a negative value gets an empty grey stub."""
    if not rows:
        return ""
    top = max((v for _, v, _ in rows), default=0) or 1
    cells = []
    for lbl, v, tip in rows:
        pct = max(0.0, min(100.0, v / top * 100)) if top > 0 else 0
        cls = "fill neg" if v < 0 else "fill"
        cells.append(f'<div class="lbl">{lbl}</div>'
                     f'<div class="track" title="{_e(tip)}"><div class="{cls}" style="width:{pct:.1f}%"></div></div>'
                     f'<div class="val">{fmt_num(round(v, 2))}{_e(unit)}</div>')
    head = f"<h3>{_e(title)}</h3>" if title else ""
    return f'<div class="chart">{head}<div class="bars">{"".join(cells)}</div></div>'


def line(points: list[tuple[str, float, str, str | None]], title: str = "", avg_label: str = "average") -> str:
    """Line chart over games. points = [(x label, value, tooltip, link or None)], oldest first.
    A dashed line marks the average. One point or more; with one it's a single dot."""
    if not points:
        return ""
    w, h, left, right, top, bottom = 640, 220, 40, 10, 12, 26
    vals = [p[1] for p in points]
    lo, hi = min(0.0, min(vals)), max(vals)
    if hi == lo:
        hi = lo + 1
    step = _nice_step((hi - lo) / 4)
    lo_t, hi_t = step * (lo // step), step * -(-hi // step)
    span = (hi_t - lo_t) or 1
    n = len(points)

    def x(i: int) -> float:
        return left + (w - left - right) * (i / (n - 1) if n > 1 else 0.5)

    def y(v: float) -> float:
        return top + (h - top - bottom) * (1 - (v - lo_t) / span)

    parts = []
    t = lo_t
    while t <= hi_t + 1e-9:
        parts.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                     f'<text class="axis" x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{fmt_num(round(t, 1))}</text>')
        t += step
    every = max(1, round(n / 10))  # about ten x labels at most
    for i, p in enumerate(points):
        if i == n - 1 or (i % every == 0 and n - 1 - i >= max(1, every * 0.6)):  # last label never collides
            parts.append(f'<text class="axis" x="{x(i):.1f}" y="{h - 8}" text-anchor="middle">{_e(p[0])}</text>')
    avg = sum(vals) / n
    parts.append(f'<line class="avg" x1="{left}" x2="{w - right}" y1="{y(avg):.1f}" y2="{y(avg):.1f}">'
                 f'<title>{_e(avg_label)}: {fmt_num(round(avg, 2))}</title></line>')
    if n > 1:
        d = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
        parts.append(f'<path class="ln" d="{d}"/>')
    for i, (_, v, tip, href) in enumerate(points):
        dot = (f'<circle class="hit" cx="{x(i):.1f}" cy="{y(v):.1f}" r="10"/>'
               f'<circle class="pt" cx="{x(i):.1f}" cy="{y(v):.1f}" r="4"/><title>{_e(tip)}</title>')
        parts.append(f'<a href="{_e(href)}">{dot}</a>' if href else f"<g>{dot}</g>")
    head = f"<h3>{_e(title)}</h3>" if title else ""
    return (f'<div class="chart">{head}<svg class="line" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{_e(title)}">{"".join(parts)}</svg>'
            f'<div class="small muted">Dashed line: {_e(avg_label)} ({fmt_num(round(avg, 2))})</div></div>')


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1.0
    while mag > raw:
        mag /= 10
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def compare(xlabels: list[str], series: list[tuple[str, list[float | None], str]], title: str = "",
            unit: str = "pts", links: list[str | None] | None = None) -> str:
    """Several players' lines on one chart. series = [(name, values per x, tooltip prefix)], at most
    SERIES_MAX, each in its fixed color slot. None values are gaps (the line breaks there).
    Hovering a line brings it forward; hovering a point shows that game's value."""
    series = series[:SERIES_MAX]
    allv = [v for _, vals, _ in series for v in vals if v is not None]
    if not series or not allv or not xlabels:
        return ""
    w, h, left, right, top, bottom = 760, 300, 46, 12, 12, 26
    lo, hi = min(0.0, min(allv)), max(allv)
    if hi == lo:
        hi = lo + 1
    step = _nice_step((hi - lo) / 5)
    lo_t, hi_t = step * (lo // step), step * -(-hi // step)
    span = (hi_t - lo_t) or 1
    n = len(xlabels)

    def x(i: int) -> float:
        return left + (w - left - right) * (i / (n - 1) if n > 1 else 0.5)

    def y(v: float) -> float:
        return top + (h - top - bottom) * (1 - (v - lo_t) / span)

    parts = []
    t = lo_t
    while t <= hi_t + 1e-9:
        parts.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                     f'<text class="axis" x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{fmt_num(round(t, 1))}</text>')
        t += step
    every = max(1, round(n / 10))
    for i, lbl in enumerate(xlabels):
        if i == n - 1 or (i % every == 0 and n - 1 - i >= max(1, every * 0.6)):
            parts.append(f'<text class="axis" x="{x(i):.1f}" y="{h - 8}" text-anchor="middle">{_e(lbl)}</text>')
    legend = []
    for k, (name, vals, tip) in enumerate(series):
        color = f"var(--s{k + 1})"
        segs, cur = [], []
        for i, v in enumerate(vals):
            if v is None:
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append((i, v))
        if cur:
            segs.append(cur)
        d = " ".join(" ".join(f"{'M' if j == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for j, (i, v) in enumerate(seg))
                     for seg in segs)
        dots = "".join(
            (f'<a href="{_e(links[i])}">' if links and links[i] else "<g>")
            + f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="{4 if n <= 40 else 3}" fill="{color}">'
              f'<title>{_e(name)} · {_e(xlabels[i])}: {fmt_num(v)} {_e(unit)}{_e(tip)}</title></circle>'
            + ("</a>" if links and links[i] else "</g>")
            for i, v in enumerate(vals) if v is not None)
        parts.append(f'<g class="sr"><title>{_e(name)}</title><path class="hit" d="{d}"/>'
                     f'<path d="{d}" stroke="{color}"/>{dots}</g>')
        legend.append(f'<span><i style="background:{color}"></i>{_e(name)}</span>')
    head = f"<h3>{_e(title)}</h3>" if title else ""
    return (f'<div class="chart cmp">{head}<svg class="line" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{_e(title)}">{"".join(parts)}</svg><div class="legend">{"".join(legend)}</div></div>')
