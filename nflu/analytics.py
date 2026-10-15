"""Aggregations over stored games: player totals, leaderboards, standings."""

from __future__ import annotations

from collections import defaultdict

from .db import Database


def sum_stats(lines: list[dict]) -> dict[str, float]:
    tot: dict[str, float] = defaultdict(float)
    for ln in lines:
        for k, v in ln["stats"].items():
            tot[k] += v or 0
    return dict(tot)


def player_summary(db: Database, username_key: str, season: int | None = None) -> dict | None:
    lines = db.player_lines(username_key, season)
    if not lines:
        return None
    totals = sum_stats(lines)
    fps = [ln["fantasy_points"] for ln in lines]
    wins = 0
    for ln in lines:
        team = (ln["team"] or "").lower()
        if team == ln["home_team"].lower():
            wins += ln["home_score"] > ln["away_score"]
        elif team == ln["away_team"].lower():
            wins += ln["away_score"] > ln["home_score"]
    best = max(lines, key=lambda l: l["fantasy_points"])
    return {
        "username": lines[0]["username"],
        "games": len(lines),
        "wins": wins,
        "totals": totals,
        "fantasy_total": round(sum(fps), 2),
        "fantasy_avg": round(sum(fps) / len(fps), 2),
        "best_game": best,
        "teams": sorted({ln["team"] for ln in lines if ln["team"]}),
        "lines": lines,
    }


def leaders(db: Database, stat: str, season: int | None = None, limit: int = 10,
            per_game: bool = False, min_games: int = 1) -> list[tuple[str, float, int]]:
    """[(username, value, games)] sorted descending. stat='fantasy' ranks fantasy points."""
    agg: dict[str, list] = {}
    for ln in db.all_player_lines(season):
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


def fantasy_week(db: Database, season: int, week: int | None, limit: int = 10) -> list[tuple[str, float, int]]:
    agg: dict[str, list] = {}
    for ln in db.all_player_lines(season, week):
        entry = agg.setdefault(ln["username_key"], [ln["username"], 0.0, 0])
        entry[1] += ln["fantasy_points"]
        entry[2] += 1
    rows = sorted(((n, round(t, 2), g) for n, t, g in agg.values()), key=lambda r: r[1], reverse=True)
    return rows[:limit]


def standings(db: Database, season: int | None) -> list[dict]:
    teams: dict[str, dict] = {}

    def t(name: str) -> dict:
        return teams.setdefault(name.lower(), {"team": name, "w": 0, "l": 0, "t": 0, "pf": 0, "pa": 0})

    for g in db.recent_games(limit=100000, season=season):
        h, a = t(g["home_team"]), t(g["away_team"])
        h["pf"] += g["home_score"]; h["pa"] += g["away_score"]
        a["pf"] += g["away_score"]; a["pa"] += g["home_score"]
        if g["home_score"] > g["away_score"]:
            h["w"] += 1; a["l"] += 1
        elif g["home_score"] < g["away_score"]:
            a["w"] += 1; h["l"] += 1
        else:
            h["t"] += 1; a["t"] += 1
    rows = list(teams.values())
    for r in rows:
        gp = r["w"] + r["l"] + r["t"]
        r["pct"] = (r["w"] + 0.5 * r["t"]) / gp if gp else 0
        r["diff"] = r["pf"] - r["pa"]
    rows.sort(key=lambda r: (r["pct"], r["diff"], r["pf"]), reverse=True)
    return rows


def head_to_head(db: Database, team_a: str, team_b: str) -> list:
    return [g for g in db.recent_games(limit=100000)
            if {g["home_team"].lower(), g["away_team"].lower()} == {team_a.lower(), team_b.lower()}]
