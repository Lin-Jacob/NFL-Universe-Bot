"""Aggregations over stored games: player totals and leaderboards."""

from __future__ import annotations

import math
from collections import defaultdict

import config

from .db import Database
from .fantasy import by_group, group_points
from .stats import MAX_STATS


def sum_stats(lines: list[dict]) -> dict[str, float]:
    tot: dict[str, float] = defaultdict(float)
    for ln in lines:
        for k, v in ln["stats"].items():
            tot[k] = max(tot[k], v or 0) if k in MAX_STATS else tot[k] + (v or 0)
    return dict(tot)


def player_summary(db: Database, username_key: str, season: int | None = None) -> dict | None:
    """A player's totals over every game, or one season's games (season=N)."""
    lines = db.player_lines(username_key, season)
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
            per_game: bool = False, min_games: int = 1, season: int | None = None) -> list[tuple[str, float, int]]:
    """[(username, value, games)] sorted descending. stat='fantasy' ranks fantasy points."""
    agg: dict[str, list] = {}
    for ln in db.all_player_lines(season=season):
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


def career(db: Database, username_key: str, season: int | None = None) -> dict | None:
    """A player's stats combined over every game, split by position group, for the /player card.

    groups = {"WR": {stat: (total, per_game_avg)}}; 'longest_*' stats give (best, average) instead.
    Averages divide by all games played, so a stat they skipped some games counts as 0 there."""
    lines = db.player_lines(username_key, season)
    if not lines:
        return None
    n = len(lines)
    per_game: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    points: dict[str, float] = defaultdict(float)
    for ln in lines:
        for code, st in by_group(ln["stats"], ln["pos_stats"]).items():
            points[code] += group_points(code, st)
            for k, v in st.items():
                per_game[code][k].append(v or 0)
    groups = {code: {k: ((max(vals) if k in MAX_STATS else sum(vals)), sum(vals) / n) for k, vals in st.items()}
              for code, st in per_game.items()}
    fps = [ln["fantasy_points"] for ln in lines]
    snaps = sum(ln["extra"].get("Snaps") or 0 for ln in lines)
    return {"username": lines[0]["username"], "games": n, "snaps": snaps, "fantasy_total": round(sum(fps), 2),
            "fantasy_avg": round(sum(fps) / n, 2), "fantasy_rate": per_snap(sum(fps), snaps),
            "best_game": max(lines, key=lambda l: l["fantasy_points"]),
            "groups": groups, "group_points": {c: round(p, 2) for c, p in points.items()}}


def player_ratings(db: Database) -> tuple[dict[str, dict], dict]:
    """Each player's expected fantasy points per full game, and how much that varies game to game.

    A game's rate = points / snaps * ODDS_FULL_GAME_SNAPS (so a late joiner's partial game counts
    as if they'd played it all; games under ODDS_MIN_GAME_SNAPS snaps are too short to judge and
    are skipped). The mean is shrunk toward the league average by ODDS_PRIOR_GAMES pseudo-games, so
    one standout game doesn't decide it. Returns ({username_key: rating}, league)."""
    full, min_snaps, prior = config.ODDS_FULL_GAME_SNAPS, config.ODDS_MIN_GAME_SNAPS, config.ODDS_PRIOR_GAMES
    samples: dict[str, list[float]] = defaultdict(list)
    names: dict[str, str] = {}
    for ln in db.all_player_lines():
        snaps = ln["extra"].get("Snaps") or 0
        if snaps >= min_snaps:
            samples[ln["username_key"]].append(ln["fantasy_points"] / snaps * full)
            names[ln["username_key"]] = ln["username"]
    every = [r for rs in samples.values() for r in rs]
    league_mean = sum(every) / len(every) if every else 0.0
    # Typical game-to-game spread of one player (pooled over players with 2+ games)
    dev = [(r - sum(rs) / len(rs)) ** 2 for rs in samples.values() if len(rs) > 1 for r in rs]
    dof = sum(len(rs) - 1 for rs in samples.values() if len(rs) > 1)
    league_sd = (sum(dev) / dof) ** 0.5 if dof else (
        (sum((r - league_mean) ** 2 for r in every) / max(1, len(every) - 1)) ** 0.5 if every else 1.0)
    ratings = {}
    for key, rs in samples.items():
        n = len(rs)
        raw = sum(rs) / n
        own_var = sum((r - raw) ** 2 for r in rs) / (n - 1) if n > 1 else league_sd ** 2
        var = (prior * league_sd ** 2 + (n - 1) * own_var) / (prior + n - 1)  # shrink the spread too
        ratings[key] = {"username": names[key], "games": n, "raw": raw,
                        "mean": (n * raw + prior * league_mean) / (n + prior), "sd": var ** 0.5}
    return ratings, {"mean": league_mean, "sd": league_sd, "games": len(every)}


OFFENSE, DEFENSE, SPECIAL = ("QB", "RB", "WR"), ("DB", "DL"), ("ST",)


def role_ratings(db: Database) -> tuple[dict[str, dict], dict]:
    """Fantasy points per snap at each role (QB, receiver, defense, special teams) for every player,
    pulled toward the league average for that role by ODDS_PRIOR_GAMES games' worth of snaps.
    A role someone has never played is rated at ODDS_UNPROVEN_ROLE x the league average.
    Returns ({username_key: {"QB": rate, "WR": rate, "DEF": rate, "ST": rate, "qb_snaps": n}}, league)."""
    roles = {"QB": ("QB",), "WR": ("WR", "RB"), "DEF": DEFENSE, "ST": SPECIAL}
    pts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    snaps: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    per_game = {"off": [], "def": [], "st": []}
    for ln in db.all_player_lines():
        ps = ln["pos_stats"] or {}
        snap = {g: (ps.get(g) or {}).get("snaps", 0) or 0 for g in OFFENSE + DEFENSE + SPECIAL}
        for role, groups in roles.items():
            # DB and DL both count every defensive play, so defensive snaps = the larger of the two
            n = max(snap[g] for g in groups) if role == "DEF" else sum(snap[g] for g in groups)
            if n:
                snaps[ln["username_key"]][role] += n
                pts[ln["username_key"]][role] += sum(group_points(g, ps[g]) for g in groups if ps.get(g))
        for key, groups in (("off", OFFENSE), ("def", DEFENSE), ("st", SPECIAL)):
            n = max(snap[g] for g in groups) if key == "def" else sum(snap[g] for g in groups)
            if n:
                per_game[key].append(n)
    league = {r: (sum(p[r] for p in pts.values()) / s if (s := sum(sn[r] for sn in snaps.values())) else 0.0)
              for r in roles}
    league.update({f"{k}_snaps": (sorted(v)[len(v) // 2] if v else 30) for k, v in per_game.items()})
    prior = config.ODDS_PRIOR_GAMES * league["off_snaps"]
    ratings = {}
    for key in set(pts) | set(snaps):
        r = {}
        for role in roles:
            n, base = snaps[key][role], league[role]
            if not n:
                base *= config.ODDS_UNPROVEN_ROLE
            r[role] = (pts[key][role] + prior * base) / (n + prior)
        r["qb_snaps"] = snaps[key]["QB"]
        offense_snaps = snaps[key]["QB"] + snaps[key]["WR"]
        # Usual role: a QB if they spend at least ODDS_QB_SHARE of their offense snaps at QB
        r["is_qb"] = bool(offense_snaps) and snaps[key]["QB"] / offense_snaps >= config.ODDS_QB_SHARE
        ratings[key] = r
    return ratings, league


def lineup_projection(keys: list[str], rr: dict[str, dict], league: dict) -> dict:
    """Projected fantasy points for one lineup in a full game, using each player's usual role.

    QBs (see role_ratings' is_qb) throw; everyone else catches. Only one player throws at a time,
    so the team passes at its best QB's level and any other QBs on the team catch instead.
    Receivers' output scales with that passing level, so a lineup with no QB can't ride its
    receivers: with no QB, someone has to throw at ODDS_UNPROVEN_ROLE of an average QB.
    Everyone plays defense and special teams."""
    unproven = {r: league[r] * config.ODDS_UNPROVEN_ROLE for r in ("QB", "WR", "DEF", "ST")}
    unproven.update(qb_snaps=0, is_qb=False)
    get = lambda k: rr.get(k, unproven)  # noqa: E731 - players with no games at all
    off, dfn, st = league["off_snaps"], league["def_snaps"], league["st_snaps"]
    qbs = [k for k in keys if get(k)["is_qb"]]
    passer = max(qbs, key=lambda k: get(k)["QB"]) if qbs else None
    passing_rate = get(passer)["QB"] if passer else unproven["QB"]
    factor = min(max(passing_rate / league["QB"], 0.2), 1.5) if league["QB"] > 0 else 1.0
    parts = {}
    for k in keys:
        r = get(k)
        offense = r["QB"] * off if k == passer else r["WR"] * off * factor
        parts[k] = {"offense": offense, "defense": r["DEF"] * dfn, "special": r["ST"] * st}
    no_qb_passing = 0.0 if passer else passing_rate * off  # whoever ends up throwing
    total = sum(sum(p.values()) for p in parts.values()) + no_qb_passing
    return {"total": total, "parts": parts, "passer": passer, "qbs": qbs, "qb_factor": factor,
            "no_qb_passing": no_qb_passing}


def win_odds(m1: float, m2: float, var: float) -> tuple[float, float]:
    """Chance each side out-produces the other, given projected totals and the combined variance."""
    z = (m1 - m2) / var ** 0.5 if var > 0 else 0.0
    p1 = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return p1, 1 - p1


def american_odds(p: float) -> str:
    """Moneyline for a win probability: -150 = bet 150 to win 100, +150 = bet 100 to win 150."""
    p = min(max(p, 0.001), 0.999)
    return f"{-round(100 * p / (1 - p)):+d}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def per_snap(points: float, snaps: float) -> float:
    """Fantasy points per config.FANTASY_RATE_SNAPS snaps (0 if no snaps recorded)."""
    return round(points / snaps * config.FANTASY_RATE_SNAPS, 2) if snaps else 0.0


def fantasy_rates(db: Database, season: int | None = None) -> tuple[list[dict], list[dict]]:
    """Fantasy leaderboard by points per snap, so players who joined late aren't penalized for it.

    Returns (ranked, not_enough_snaps), each [{username, points, snaps, games, rate}] best rate first.
    Players under config.FANTASY_MIN_SNAPS total snaps go in the second list. Snaps come from the
    NFL Universe stat sheet (every section's 'Snaps' added up = plays on the field)."""
    agg: dict[str, dict] = {}
    for ln in db.all_player_lines(season=season):
        e = agg.setdefault(ln["username_key"], {"username": ln["username"], "points": 0.0, "snaps": 0.0, "games": 0})
        e["points"] += ln["fantasy_points"]
        e["snaps"] += ln["extra"].get("Snaps") or 0
        e["games"] += 1
    rows = [dict(e, points=round(e["points"], 2), rate=per_snap(e["points"], e["snaps"])) for e in agg.values()]
    rows.sort(key=lambda r: (r["rate"], r["points"]), reverse=True)
    ranked = [r for r in rows if r["snaps"] >= config.FANTASY_MIN_SNAPS]
    return ranked, [r for r in rows if r["snaps"] < config.FANTASY_MIN_SNAPS]


def position_leaders(db: Database, pos: str, limit: int = 10, season: int | None = None) -> list[dict]:
    """Players ranked by fantasy points earned at one position (QB/RB/WR/DB/DL/ST), across all games.
    [{username, points, games, stats}] highest first. Points can be negative (interception penalty)."""
    agg: dict[str, dict] = {}
    for ln in db.all_player_lines(season=season):
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


def season_mvps(db: Database, season_id: int, top: int = 3) -> dict:
    """The stat awards race for one season: for the top scorer (PTS, all fantasy points) and each
    position (QB/RB/WR/DB/DL/ST), players ranked by the fantasy points they earned there in that
    season's games, best first.

    The same points as /stats, so the scoring in config.py decides it. To be eligible a player must
    have played that role in SEASON_MVP_MIN_GAMES games (default 3), lowered to half the season's
    games when the season is shorter, so someone with one huge game can't win it. Ties go to the
    better points-per-game. Returns {"games": n, "min_games": m, "roles": {"PTS": [row...], "QB": ...}},
    each row {username_key, username, points, games, avg}."""
    lines = db.all_player_lines(season=season_id)
    n_games = len({ln["game_id"] for ln in lines})
    min_games = min(int(getattr(config, "SEASON_MVP_MIN_GAMES", 3)), max(1, math.ceil(n_games / 2)))
    agg: dict[str, dict[str, dict]] = {role: {} for role in ("PTS", "QB", "RB", "WR", "DB", "DL", "ST")}

    def add(role: str, ln: dict, points: float) -> None:
        e = agg[role].setdefault(ln["username_key"], {"username_key": ln["username_key"],
                                                       "username": ln["username"], "points": 0.0, "games": 0})
        e["points"] += points
        e["games"] += 1

    for ln in lines:
        add("PTS", ln, ln["fantasy_points"])
        for code, st in by_group(ln["stats"], ln["pos_stats"]).items():
            if code in agg and st:
                add(code, ln, group_points(code, st))
    roles = {}
    for role, players in agg.items():
        rows = [dict(e, points=round(e["points"], 2), avg=round(e["points"] / e["games"], 2))
                for e in players.values() if e["games"] >= min_games and e["points"] > 0]
        rows.sort(key=lambda r: (r["points"], r["avg"]), reverse=True)
        roles[role] = rows[:top]
    return {"games": n_games, "min_games": min_games, "roles": roles}


# -------------------------------------------------------------------- MVP voting
# (share of the season's games played, vote weight). Points in between are interpolated, so
# 75%+ of the games = 1, 50% = 0.75, 25% = 0.5, and one game out of 80 = about 0.26.
DEFAULT_VOTE_WEIGHTS = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 1.0)]


def vote_weight(share: float) -> float:
    """How much a vote counts, from the share (0-1) of the season's games the voter played.
    SEASON_VOTE_WEIGHTS in config.py sets the curve. Someone who played no games can't vote (0)."""
    if share <= 0:
        return 0.0
    pts = sorted(getattr(config, "SEASON_VOTE_WEIGHTS", None) or DEFAULT_VOTE_WEIGHTS)
    if share <= pts[0][0]:
        return round(pts[0][1], 3)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if share <= x1:
            return round(y0 + (y1 - y0) * (share - x0) / (x1 - x0) if x1 > x0 else y1, 3)
    return round(pts[-1][1], 3)


def season_roles_played(db: Database, season_id: int) -> tuple[int, dict[str, dict[str, int]], dict[str, str]]:
    """(season's game count, {username_key: {"ALL": games, "QB": games at QB, ...}}, {key: username})."""
    lines = db.all_player_lines(season=season_id)
    played: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    names: dict[str, str] = {}
    for ln in lines:
        k = ln["username_key"]
        names[k] = ln["username"]
        played[k]["ALL"] += 1
        for code, st in by_group(ln["stats"], ln["pos_stats"]).items():
            if st:
                played[k][code] += 1
    return len({ln["game_id"] for ln in lines}), played, names


def voter_weight(db: Database, season_id: int, voter_key: str) -> tuple[float, int, int]:
    """(weight, games the voter played that season, games in the season)."""
    total, played, _ = season_roles_played(db, season_id)
    games = played.get(voter_key, {}).get("ALL", 0)
    return (vote_weight(games / total) if total else 0.0), games, total


def vote_results(db: Database, season_id: int) -> dict:
    """Weighted vote totals per voted award: {"voters": n, "roles": {"MVP": [{username_key, username,
    weight, votes}]}} best first. Ties go to whoever has more votes."""
    names = {r["username_key"]: r["username"]
             for r in db.q("SELECT username_key, username FROM player_games GROUP BY username_key")}
    from .awards import vote_awards  # local: awards imports this module
    roles: dict[str, dict[str, dict]] = {r: {} for r in vote_awards()}
    voters = set()
    for v in db.season_votes(season_id):
        voters.add(v["voter_key"])
        e = roles.setdefault(v["role"], {}).setdefault(v["candidate_key"], {
            "username_key": v["candidate_key"], "username": names.get(v["candidate_key"], v["candidate_key"]),
            "weight": 0.0, "votes": 0})
        e["weight"] += v["weight"]
        e["votes"] += 1
    out = {}
    for role, cands in roles.items():
        rows = [dict(e, weight=round(e["weight"], 3)) for e in cands.values()]
        rows.sort(key=lambda r: (r["weight"], r["votes"]), reverse=True)
        out[role] = rows
    return {"voters": len(voters), "roles": out}


# ---------------------------------------------------------- comparison charts
def line_points(ln: dict, pos: str | None) -> float | None:
    """A stat line's fantasy points, or only those earned at one position (None = didn't play it)."""
    if not pos:
        return ln["fantasy_points"]
    st = by_group(ln["stats"], ln["pos_stats"]).get(pos)
    return group_points(pos, st) if st else None


def game_series(db: Database, season: int | None = None, pos: str | None = None, last: int | None = None
                ) -> tuple[list[dict], dict[str, dict]]:
    """Every player's points game by game, for comparing them on one chart.

    Returns (games, players): games = [{id, played_at}] oldest first (the last `last` of them),
    players = {username_key: {username, values: [points or None per game], total, games}}, where
    None = they didn't play that game (or not at `pos`). Totals cover only those games."""
    rows = db.q("SELECT id, played_at FROM games" + (" WHERE season = ?" if season else "")
                + " ORDER BY played_at, id", (season,) if season else ())
    games = [dict(r) for r in rows][-last:] if last else [dict(r) for r in rows]
    index = {g["id"]: i for i, g in enumerate(games)}
    players: dict[str, dict] = {}
    for ln in db.all_player_lines(season=season):
        i = index.get(ln["game_id"])
        if i is None:
            continue
        v = line_points(ln, pos)
        if v is None:
            continue
        e = players.setdefault(ln["username_key"], {"username": ln["username"], "values": [None] * len(games),
                                                    "total": 0.0, "games": 0})
        e["values"][i] = round(v, 2)
        e["total"] += v
        e["games"] += 1
    for e in players.values():
        e["total"] = round(e["total"], 2)
    return games, players


def cumulative(values: list[float | None]) -> list[float | None]:
    """Running total; None before a player's first game, then flat through games they missed."""
    out, run, started = [], 0.0, False
    for v in values:
        if v is not None:
            run += v
            started = True
        out.append(round(run, 2) if started else None)
    return out
