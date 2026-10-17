"""Fantasy point calculation. Scoring weights live in config.py.

Each position group (QB/RB/WR/DB/DL/ST) has its own weights in
config.FANTASY_SCORING, applied to the stats a player earned in that group
(their 'pos_stats'). Negative weights subtract; a negative stat value with a
positive weight (e.g. -3 rushing yards) scores 0.
"""

import config

from .stats import label


def _weight(group: str, stat: str) -> float:
    return config.FANTASY_SCORING.get(group, {}).get(stat, 0)


def _stat_points(group: str, stat: str, value: float) -> float:
    w = _weight(group, stat)
    pts = w * (value or 0)
    return pts if w < 0 else max(0.0, pts)


def stat_group(stat: str) -> str | None:
    """The first position group that scores this stat: used for stats with no position info
    (games entered by hand with /admin addgame + setstat)."""
    for group, weights in config.FANTASY_SCORING.items():
        if stat in weights:
            return group
    return None


def by_group(stats: dict[str, float], pos_stats: dict[str, dict[str, float]] | None) -> dict[str, dict]:
    """Stats split by position group: pos_stats when known, else each stat filed under stat_group()."""
    if pos_stats:
        return pos_stats
    out: dict[str, dict] = {}
    for stat, value in stats.items():
        group = stat_group(stat)
        if group:
            out.setdefault(group, {})[stat] = value
    return out


def _bonuses(stats: dict[str, float]) -> list[tuple[str, float, float]]:
    return [(stat, threshold, bonus) for stat, threshold, bonus in config.FANTASY_BONUSES
            if (stats.get(stat) or 0) >= threshold]


def group_points(group: str, stats: dict[str, float]) -> float:
    """Points for one position group's stats, including any bonuses they earn."""
    pts = sum(_stat_points(group, s, v) for s, v in stats.items())
    pts += sum(b for _, _, b in _bonuses(stats))
    return round(pts, 2)


def fantasy_points(stats: dict[str, float], pos_stats: dict[str, dict[str, float]] | None = None) -> float:
    return round(sum(group_points(g, st) for g, st in by_group(stats, pos_stats).items()), 2)


def breakdown(stats: dict[str, float], pos_stats: dict[str, dict[str, float]] | None = None
              ) -> list[tuple[str, float, float]]:
    """[('QB · Passing Yards', value, points)] for every stat that scored, plus bonuses."""
    rows = []
    for group, st in by_group(stats, pos_stats).items():
        for stat, value in st.items():
            pts = _stat_points(group, stat, value)
            if pts:
                rows.append((f"{group} · {label(stat)}", value, round(pts, 2)))
        for stat, threshold, bonus in _bonuses(st):
            rows.append((f"{group} · {label(stat)} ≥ {threshold:g} bonus", 1, bonus))
    return rows


def scored_stats() -> set[str]:
    """Stats worth points (non-zero weight) in any group."""
    return {s for weights in config.FANTASY_SCORING.values() for s, w in weights.items() if w}
