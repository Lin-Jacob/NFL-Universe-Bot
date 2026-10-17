"""Aggregations over stored games: player totals and leaderboards."""

from __future__ import annotations

from collections import defaultdict

import config

from .db import Database
from .fantasy import group_points
from .stats import MAX_STATS


def sum_stats(lines: list[dict]) -> dict[str, float]:
    tot: dict[str, float] = defaultdict(float)
    for ln in lines:
        for k, v in ln["stats"].items():
            tot[k] = max(tot[k], v or 0) if k in MAX_STATS else tot[k] + (v or 0)
    return dict(tot)


def player_summary(db: Database, username_key: str) -> dict | None:
    lines = db.player_lines(username_key)
    if not lines:
        return None
    totals = sum_stats(lines)
    fps = [ln["fantasy_points"] for ln in lines]
    best = max(lines, key=lambda l: l["fantasy_points"])
    return {
        "username": lines[0]["username"],
        "games": len(lines),
        "totals": totals,
        "fantasy_total": round(sum(fps), 2),
        "fantasy_avg": round(sum(fps) / len(fps), 2),
        "snaps": (snaps := sum(ln["extra"].get("Snaps") or 0 for ln in lines)),
        "fantasy_rate": per_snap(sum(fps), snaps),
        "best_game": best,
        "lines": lines,
    }


def leaders(db: Database, stat: str, limit: int = 10,
            per_game: bool = False, min_games: int = 1) -> list[tuple[str, float, int]]:
    """[(username, value, games)] sorted descending. stat='fantasy' ranks fantasy points."""
    agg: dict[str, list] = {}
    for ln in db.all_player_lines():
        val = ln["fantasy_points"] if stat == "fantasy" else (ln["stats"].get(stat) or 0)
        entry = agg.setdefault(ln["username_key"], [ln["username"], 0.0, 0])
        entry[1] += val
        entry[2] += 1
    rows = []
    for name, total, games in agg.values():
        if games < min_games:
            continue
        value = total / games if per_game else total
        if value:
            rows.append((name, round(value, 2), games))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def per_snap(points: float, snaps: float) -> float:
    """Fantasy points per config.FANTASY_RATE_SNAPS snaps (0 if no snaps recorded)."""
    return round(points / snaps * config.FANTASY_RATE_SNAPS, 2) if snaps else 0.0


def fantasy_rates(db: Database) -> tuple[list[dict], list[dict]]:
    """Fantasy leaderboard by points per snap, so players who joined late aren't penalized for it.

    Returns (ranked, not_enough_snaps), each [{username, points, snaps, games, rate}] best rate first.
    Players under config.FANTASY_MIN_SNAPS total snaps go in the second list. Snaps come from the
    NFL Universe stat sheet (every section's 'Snaps' added up = plays on the field)."""
    agg: dict[str, dict] = {}
    for ln in db.all_player_lines():
        e = agg.setdefault(ln["username_key"], {"username": ln["username"], "points": 0.0, "snaps": 0.0, "games": 0})
        e["points"] += ln["fantasy_points"]
        e["snaps"] += ln["extra"].get("Snaps") or 0
        e["games"] += 1
    rows = [dict(e, points=round(e["points"], 2), rate=per_snap(e["points"], e["snaps"])) for e in agg.values()]
    rows.sort(key=lambda r: (r["rate"], r["points"]), reverse=True)
    ranked = [r for r in rows if r["snaps"] >= config.FANTASY_MIN_SNAPS]
    return ranked, [r for r in rows if r["snaps"] < config.FANTASY_MIN_SNAPS]


def position_leaders(db: Database, pos: str, limit: int = 10) -> list[dict]:
    """Players ranked by fantasy points earned at one position (QB/RB/WR/DB/DL/ST), across all games.
    [{username, points, games, stats}] highest first. Points can be negative (interception penalty)."""
    agg: dict[str, dict] = {}
    for ln in db.all_player_lines():
        st = ln["pos_stats"].get(pos)
        if not st:
            continue
        e = agg.setdefault(ln["username_key"], {"username": ln["username"], "points": 0.0, "games": 0,
                                                "stats": defaultdict(float)})
        e["points"] += group_points(pos, st)
        e["games"] += 1
        for k, v in st.items():
            e["stats"][k] = max(e["stats"][k], v) if k in MAX_STATS else e["stats"][k] + v
    rows = [dict(e, points=round(e["points"], 2), stats=dict(e["stats"])) for e in agg.values()]
    rows.sort(key=lambda r: r["points"], reverse=True)
    return rows[:limit]
