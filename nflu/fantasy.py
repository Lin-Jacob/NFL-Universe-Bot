"""Fantasy point calculation. Scoring weights live in config.py."""

import config

from .stats import label


def fantasy_points(stats: dict[str, float]) -> float:
    pts = 0.0
    for stat, value in stats.items():
        pts += config.FANTASY_SCORING.get(stat, 0) * (value or 0)
    for stat, threshold, bonus in config.FANTASY_BONUSES:
        if (stats.get(stat) or 0) >= threshold:
            pts += bonus
    return round(pts, 2)


def breakdown(stats: dict[str, float]) -> list[tuple[str, float, float]]:
    """[(stat, value, points)] for every stat that scored, plus bonuses."""
    rows = []
    for stat, value in stats.items():
        w = config.FANTASY_SCORING.get(stat, 0)
        if w and value:
            rows.append((stat, value, round(w * value, 2)))
    for stat, threshold, bonus in config.FANTASY_BONUSES:
        if (stats.get(stat) or 0) >= threshold:
            rows.append((f"{label(stat)} ≥ {threshold:g} bonus", 1, bonus))
    return rows
