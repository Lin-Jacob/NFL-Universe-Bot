"""
Turn whatever JSON the NFL Universe webhook sends into a ParsedGame.

The exact payload format of the NFL Universe webhook service is not publicly
documented, so this parser is deliberately forgiving. See README.md
("Webhook payload format") for the shape it expects first, plus the variations
it also understands. Every payload is stored raw in the database before it is
parsed, so nothing is lost if the format turns out to be different: fix the
parser, then run /admin reparse.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .stats import _norm, canonical_stat, is_category


class ParseError(Exception):
    pass


@dataclass
class ParsedPlayer:
    username: str
    team: str | None = None
    user_id: str | None = None
    position: str | None = None
    stats: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # numeric keys we didn't recognize
    # Same stats split by the position they were earned at (QB/RB/WR/DB/DL/ST), when the source says so
    pos_stats: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class ParsedGame:
    external_id: str | None
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    week: int | None = None
    played_at: str | None = None
    players: list[ParsedPlayer] = field(default_factory=list)
    unknown_stats: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get(d: dict, *names: str, default=None):
    """Case/punctuation-insensitive dict lookup trying several names."""
    if not isinstance(d, dict):
        return default
    index = {_norm(k): k for k in d.keys()}
    for n in names:
        k = index.get(_norm(n))
        if k is not None and d[k] is not None:
            return d[k]
    return default


def _num(v) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", s)  # "12/20" completions/attempts
            if m:
                return None
    return None


def _int(v, default=0) -> int:
    n = _num(v)
    return int(n) if n is not None else default


USERNAME_KEYS = ("username", "robloxusername", "roblox_username", "player", "playername",
                 "name", "user", "displayname", "display_name")
USERID_KEYS = ("userid", "user_id", "robloxid", "roblox_id", "playerid", "id")
TEAM_KEYS = ("team", "teamname", "team_name", "side", "club")
NON_STAT_KEYS = {_norm(k) for k in USERNAME_KEYS + USERID_KEYS + TEAM_KEYS +
                 ("position", "pos", "stats", "statistics", "number", "jersey", "avatar",
                  "thumbnail", "rank", "level", "home", "away", "ishome")}


def _flatten_stats(obj: dict, out: dict[str, float], extra: dict[str, Any],
                   category: str | None = None, prefix: str = "") -> None:
    for key, val in obj.items():
        if isinstance(val, dict):
            cat = key if is_category(key) else category
            _flatten_stats(val, out, extra, cat, f"{prefix}{key}.")
            continue
        if not category and _norm(key) in NON_STAT_KEYS:
            continue
        # "12/20" style completions/attempts
        if isinstance(val, str) and re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", val):
            a, b = (int(x) for x in val.split("/"))
            k = _norm(key)
            if k in ("compatt", "cmpatt", "completionsattempts", "catt") or (
                    category and _norm(category).startswith("pass")):
                out["pass_completions"] = out.get("pass_completions", 0) + a
                out["pass_attempts"] = out.get("pass_attempts", 0) + b
                continue
            if k in ("fg", "fgmadeatt", "fieldgoals"):
                out["fg_made"] = out.get("fg_made", 0) + a
                out["fg_missed"] = out.get("fg_missed", 0) + (b - a)
                continue
            if k in ("xp", "xpmadeatt", "pat"):
                out["xp_made"] = out.get("xp_made", 0) + a
                out["xp_missed"] = out.get("xp_missed", 0) + (b - a)
                continue
        n = _num(val)
        if n is None:
            continue
        canon = canonical_stat(key, category)
        if canon:
            out[canon] = out.get(canon, 0) + n
        else:
            extra[f"{prefix}{key}"] = n


def _parse_player(obj: Any, team: str | None, username_hint: str | None = None) -> ParsedPlayer | None:
    if not isinstance(obj, dict):
        return None
    username = username_hint or _get(obj, *USERNAME_KEYS)
    if not username:
        return None
    p = ParsedPlayer(
        username=str(username).strip(),
        team=_get(obj, *TEAM_KEYS) or team,
        user_id=str(_get(obj, *USERID_KEYS)) if _get(obj, *USERID_KEYS) is not None else None,
        position=_get(obj, "position", "pos"),
    )
    if isinstance(p.team, dict):
        p.team = _get(p.team, "name", "teamname")
    stats_obj = _get(obj, "stats", "statistics", "statline")
    if isinstance(stats_obj, dict):
        _flatten_stats(stats_obj, p.stats, p.extra)
    # stats may also (or instead) sit directly on the player object
    _flatten_stats({k: v for k, v in obj.items() if _norm(k) not in ("stats", "statistics", "statline")},
                   p.stats, p.extra)
    p.stats = {k: v for k, v in p.stats.items() if v}
    return p


def _parse_player_list(raw: Any, team: str | None) -> list[ParsedPlayer]:
    players: list[ParsedPlayer] = []
    if isinstance(raw, list):
        for item in raw:
            p = _parse_player(item, team)
            if p:
                players.append(p)
    elif isinstance(raw, dict):
        # {"username": {...stats}} form
        for uname, item in raw.items():
            p = _parse_player(item, team, username_hint=uname)
            if p:
                players.append(p)
    return players


def _team_obj(v) -> tuple[str | None, int | None, Any]:
    """Return (name, score, players) from a team value that may be a str or dict."""
    if isinstance(v, str):
        return v, None, None
    if isinstance(v, dict):
        name = _get(v, "name", "team", "teamname", "abbreviation", "abbr")
        score = _get(v, "score", "points", "pts", "final")
        players = _get(v, "players", "roster", "stats", "playerstats", "player_stats")
        return (str(name) if name is not None else None,
                _int(score) if score is not None else None, players)
    return None, None, None


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def parse_payload(payload: Any) -> ParsedGame:
    if not isinstance(payload, dict):
        raise ParseError("payload is not a JSON object")

    # Unwrap common envelopes: {"data": {...}}, {"game": {...}}, {"event": "...", "payload": {...}}
    for _ in range(3):
        inner = _get(payload, "data", "game", "payload", "match", "result")
        if isinstance(inner, dict) and not _get(payload, "home", "hometeam", "teams", "players"):
            payload = inner
        else:
            break

    if isinstance(payload.get("nfl_universe"), dict):
        return _parse_nflu(payload["nfl_universe"])

    if "embeds" in payload or "content" in payload and not _get(payload, "home", "teams", "players"):
        return _parse_discord_style(payload)

    home_name = away_name = None
    home_score = away_score = None
    players: list[ParsedPlayer] = []

    # Shape A/B: home/away keys
    h_name, h_score, h_players = _team_obj(_get(payload, "home", "hometeam", "home_team", "team1"))
    a_name, a_score, a_players = _team_obj(_get(payload, "away", "awayteam", "away_team", "team2"))
    home_name, away_name = h_name, a_name
    home_score = h_score if h_score is not None else _get(payload, "homescore", "home_score", "team1score")
    away_score = a_score if a_score is not None else _get(payload, "awayscore", "away_score", "team2score")
    if h_players is not None:
        players += _parse_player_list(h_players, home_name)
    if a_players is not None:
        players += _parse_player_list(a_players, away_name)

    # Shape C: teams list
    teams = _get(payload, "teams")
    if isinstance(teams, list) and len(teams) >= 2 and not home_name:
        t0, t1 = teams[0], teams[1]
        # honour an explicit home flag if present
        if isinstance(t1, dict) and (_get(t1, "ishome", "home") is True or
                                     str(_get(t1, "side", default="")).lower() == "home"):
            t0, t1 = t1, t0
        home_name, home_score, hp = _team_obj(t0)
        away_name, away_score, ap = _team_obj(t1)
        if hp is not None:
            players += _parse_player_list(hp, home_name)
        if ap is not None:
            players += _parse_player_list(ap, away_name)
    elif isinstance(teams, dict) and not home_name and len(teams) >= 2:
        (n0, v0), (n1, v1) = list(teams.items())[:2]
        home_name, home_score, hp = _team_obj(v0)
        away_name, away_score, ap = _team_obj(v1)
        home_name, away_name = home_name or n0, away_name or n1
        players += _parse_player_list(hp, home_name) + _parse_player_list(ap, away_name)

    # Top-level player list
    top_players = _get(payload, "players", "playerstats", "player_stats", "stats", "boxscore")
    if top_players is not None and not isinstance(top_players, (int, float, str)):
        players += _parse_player_list(top_players, None)

    # Score from a "score": {"home": x, "away": y} or "score": "21-14"
    score = _get(payload, "score", "final", "finalscore")
    if isinstance(score, dict):
        home_score = home_score if home_score is not None else _get(score, "home", "team1")
        away_score = away_score if away_score is not None else _get(score, "away", "team2")
    elif isinstance(score, str):
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", score)
        if m and home_score is None:
            home_score, away_score = m.group(1), m.group(2)

    # Infer team names from players if the payload never named them
    if not home_name or not away_name:
        seen = [p.team for p in players if p.team]
        uniq = list(dict.fromkeys(seen))
        if not home_name and uniq:
            home_name = uniq[0]
        if not away_name and len(uniq) > 1:
            away_name = uniq[1]

    if not home_name and not away_name and not players:
        raise ParseError("could not find teams, scores or players in payload")

    game = ParsedGame(
        external_id=_str_or_none(_get(payload, "gameid", "game_id", "matchid", "match_id", "id", "serverid", "jobid")),
        home_team=str(home_name or "Home"),
        away_team=str(away_name or "Away"),
        home_score=_int(home_score),
        away_score=_int(away_score),
        week=_int(_get(payload, "week", "round", "gameweek"), default=0) or None,
        played_at=_str_or_none(_get(payload, "timestamp", "time", "date", "playedat", "played_at", "endedat", "ended_at")),
        players=_merge_players(players),
    )
    for p in game.players:
        game.unknown_stats.update(p.extra.keys())
    return game


def _str_or_none(v) -> str | None:
    return None if v is None or v == "" else str(v)


def _merge_players(players: list[ParsedPlayer]) -> list[ParsedPlayer]:
    """Combine duplicate entries for the same username (e.g. listed under team and top-level)."""
    merged: dict[str, ParsedPlayer] = {}
    for p in players:
        key = p.username.lower()
        if key not in merged:
            merged[key] = p
            continue
        m = merged[key]
        m.team = m.team or p.team
        m.user_id = m.user_id or p.user_id
        m.position = m.position or p.position
        for k, v in p.stats.items():
            m.stats[k] = max(m.stats.get(k, 0), v)  # same numbers listed twice shouldn't double
        m.extra.update(p.extra)
    return list(merged.values())


# ---------------------------------------------------------------------------
# Discord-webhook-style payloads (content + embeds)
# ---------------------------------------------------------------------------
# Some Roblox games only know how to post to a *Discord* webhook URL, so their
# "webhook" is a Discord message: {"content": ..., "embeds": [{title, description,
# fields: [{name, value}]}]}. We make a best effort to read scores and stat lines
# out of the text.

_SCORE_RE = re.compile(r"([A-Za-z][\w .'&-]{0,40}?)\s*[:(]?\s*(\d{1,3})\s*\)?\s*(?:-|–|to|vs\.?|@)\s*\(?\s*(\d{1,3})\s*\)?\s*[:]?\s*([A-Za-z][\w .'&-]{0,40})")
_STAT_LINE_RE = re.compile(r"([A-Za-z][A-Za-z0-9 ._/-]{0,30}?)\s*[:=]\s*(-?[\d,.]+)")


def _strip_md(s: str) -> str:
    return re.sub(r"[*_`~|>]", "", s or "")


def _parse_discord_style(payload: dict) -> ParsedGame:
    texts: list[str] = [_strip_md(payload.get("content") or "")]
    fields: list[tuple[str, str]] = []
    for e in payload.get("embeds") or []:
        if not isinstance(e, dict):
            continue
        texts.append(_strip_md(e.get("title") or ""))
        texts.append(_strip_md(e.get("description") or ""))
        for f in e.get("fields") or []:
            if isinstance(f, dict):
                fields.append((_strip_md(str(f.get("name", ""))), _strip_md(str(f.get("value", "")))))

    home = away = None
    hs = as_ = 0
    for t in texts + [f"{n} {v}" for n, v in fields]:
        m = _SCORE_RE.search(t)
        if m:
            home, hs, as_, away = m.group(1).strip(), int(m.group(2)), int(m.group(3)), m.group(4).strip()
            break

    players: list[ParsedPlayer] = []
    for name, value in fields:
        stats: dict[str, float] = {}
        extra: dict[str, Any] = {}
        for k, v in _STAT_LINE_RE.findall(value):
            n = _num(v)
            if n is None:
                continue
            canon = canonical_stat(k.strip())
            if canon:
                stats[canon] = stats.get(canon, 0) + n
            else:
                extra[k.strip()] = n
        if stats:
            uname = re.sub(r"\s*\(.*?\)\s*$", "", name).strip() or name
            players.append(ParsedPlayer(username=uname, stats=stats, extra=extra))

    if not home and not players:
        raise ParseError("Discord-style payload: couldn't find a score or stat lines in the embed text")

    game = ParsedGame(external_id=None, home_team=home or "Home", away_team=away or "Away",
                      home_score=hs, away_score=as_, players=players)
    for p in players:
        game.unknown_stats.update(p.extra.keys())
    return game


# ---------------------------------------------------------------------------
# NFL Universe Football's own Discord feed
# ---------------------------------------------------------------------------
# The game posts two webhook messages per game: a CSV attachment with per-player
# stats (sections Passing, Rushing, Receiving, Cornerback, DLine, SpecialTeams),
# then an embed titled "<Team A> vs <Team B>" with a "Scoring History" table.
# bot.py pairs them into {"nfl_universe": {"embed": {...}, "csv": "..."}}.
# The CSV has no team column, so players are stored without a team.

# (section, column) -> canonical stat, for every column on NFL Universe's stat sheet.
# The same stat name can appear in several sections (e.g. "tackles" in Cornerback and
# DLine, "third_down_conv" in Passing/Rushing/Receiving); each is scored by the
# position group it came from (config.FANTASY_SCORING). Unknown future columns go to 'extra'.
_NFLU_COLUMNS: dict[tuple[str, str], str] = {
    # Passing -> QB
    ("passing", "completions"): "pass_completions",
    ("passing", "attempts"): "pass_attempts",
    ("passing", "passingyards"): "pass_yards",
    ("passing", "touchdowns"): "pass_tds",
    ("passing", "interceptions"): "interceptions",
    ("passing", "fumbles"): "fumbles",
    ("passing", "interceptiontds"): "pick_sixes_thrown",
    ("passing", "sackstaken"): "sacks_taken",
    ("passing", "3rddownconv"): "third_down_conv",
    ("passing", "4thdownconv"): "fourth_down_conv",
    ("passing", "longestthrow"): "longest_pass",
    ("passing", "15ydatts"): "pass_att_lt15",   # "<15 YD ATTs" (symbols are stripped)
    ("passing", "15ydcmps"): "pass_cmp_lt15",
    ("passing", "35ydatts"): "pass_att_lt35",
    ("passing", "35ydcmps"): "pass_cmp_lt35",
    ("passing", "50ydatts"): "pass_att_lt50",
    ("passing", "50ydcmps"): "pass_cmp_lt50",
    # ">50 YD ATTs/CMPs" normalize the same as "<50": told apart by column order below
    # Rushing -> RB
    ("rushing", "rushingattempts"): "rush_attempts",
    ("rushing", "rushingyards"): "rush_yards",
    ("rushing", "touchdowns"): "rush_tds",
    ("rushing", "carriesover20"): "rush_20plus",
    ("rushing", "3rddownconv"): "third_down_conv",
    ("rushing", "4thdownconv"): "fourth_down_conv",
    ("rushing", "fumbles"): "fumbles",
    ("rushing", "rushingfirstdowns"): "rush_first_downs",
    ("rushing", "longestrush"): "longest_rush",
    # Receiving -> WR
    ("receiving", "receptionss"): "receptions",  # sic, the game's header has the typo
    ("receiving", "receptions"): "receptions",
    ("receiving", "targets"): "targets",
    ("receiving", "touchdowns"): "rec_tds",
    ("receiving", "receivingyards"): "rec_yards",
    ("receiving", "fumbles"): "fumbles",
    ("receiving", "yardsaftercatch"): "yac",
    ("receiving", "receivingfirstdowns"): "rec_first_downs",
    ("receiving", "longestreception"): "longest_rec",
    ("receiving", "catchesintraffic"): "catches_in_traffic",
    ("receiving", "drops"): "drops",
    ("receiving", "interceptionsallowed"): "ints_allowed",
    ("receiving", "tossups"): "toss_ups",
    ("receiving", "3rddownconv"): "third_down_conv",
    ("receiving", "4thdownconv"): "fourth_down_conv",
    ("receiving", "15ydrecs"): "rec_lt15",
    ("receiving", "35ydrecs"): "rec_lt35",
    ("receiving", "50ydrecs"): "rec_lt50",
    # Cornerback -> DB
    ("cornerback", "tackles"): "tackles",
    ("cornerback", "tacklesforloss"): "tfl",
    ("cornerback", "tacklesforlossyards"): "tfl_yards",
    ("cornerback", "passbreakups"): "pass_deflections",
    ("cornerback", "interceptions"): "def_ints",
    ("cornerback", "interceptiontds"): "def_tds",
    ("cornerback", "intsintraffic"): "ints_in_traffic",
    ("cornerback", "interceptionyards"): "int_return_yards",
    ("cornerback", "longestinterceptionreturn"): "longest_int_return",
    ("cornerback", "completionsallowed"): "completions_allowed",
    ("cornerback", "targets"): "targets_allowed",
    ("cornerback", "passingyardsallowed"): "pass_yards_allowed",
    ("cornerback", "passingtouchdownsallowed"): "pass_tds_allowed",
    ("cornerback", "tossups"): "toss_ups_def",
    # DLine -> DL
    ("dline", "tackles"): "tackles",
    ("dline", "tacklesforloss"): "tfl",
    ("dline", "tacklesforlossyards"): "tfl_yards",
    ("dline", "rushes"): "pass_rushes",
    ("dline", "sacks"): "sacks",
    ("dline", "sacklossyards"): "sack_yards",
    ("dline", "quarterbackhits"): "qb_hits",
    ("dline", "swats"): "pass_deflections",
    ("dline", "qbpressures"): "qb_pressures",
    ("dline", "safeties"): "safeties",
    ("dline", "fumblesforced"): "forced_fumbles",
    ("dline", "fumblesrecovered"): "fumble_recoveries",
    ("dline", "fumblesyards"): "fumble_return_yards",
    ("dline", "fumbletouchdowns"): "def_tds",
    # SpecialTeams -> ST ("Field Goals" made/attempted is handled separately)
    ("specialteams", "fieldgoalyards"): "fg_yards",
    ("specialteams", "longestfieldgoal"): "longest_fg",
    ("specialteams", "kickreturns"): "kick_returns",
    ("specialteams", "kickreturnyards"): "kick_return_yards",
    ("specialteams", "kickreturntouchdowns"): "kick_return_tds",
    ("specialteams", "puntreturns"): "punt_returns",
    ("specialteams", "puntreturnyards"): "punt_return_yards",
    ("specialteams", "puntreturntouchdowns"): "punt_return_tds",
    ("specialteams", "puntblocks"): "punt_blocks",
    ("specialteams", "kicks"): "kickoffs",
    ("specialteams", "kicktouchbacks"): "touchbacks",
    ("specialteams", "punts"): "punts",
    ("specialteams", "puntyards"): "punt_yards",
    ("specialteams", "puntsin10ydline"): "punts_inside_10",
    ("specialteams", "longestpunt"): "longest_punt",
    ("specialteams", "onsiderecoveries"): "onside_recoveries",
    ("specialteams", "onsiderecoveryyards"): "onside_recovery_yards",
}
# Columns whose header starts with ">" (over 50 yards): "<50" and ">50" normalize alike.
_NFLU_OVER50 = {"passing": {"50ydatts": "pass_att_gt50", "50ydcmps": "pass_cmp_gt50"},
                "receiving": {"50ydrecs": "rec_gt50"}}
# Averages and percentages are derived from other columns, so they aren't stored or scored.
_NFLU_DERIVED = {("rushing", "averageyards"), ("specialteams", "fieldgoalpercent"),
                 ("specialteams", "kickreturnaverage"), ("specialteams", "puntreturnaverage")}


# CSV section -> position it counts toward in /stats
_NFLU_SECTION_POS = {"passing": "QB", "rushing": "RB", "receiving": "WR",
                     "cornerback": "DB", "dline": "DL", "specialteams": "ST"}


def _add(p: ParsedPlayer, pos: str | None, stat: str, n: float) -> None:
    p.stats[stat] = p.stats.get(stat, 0) + n
    if pos:
        ps = p.pos_stats.setdefault(pos, {})
        ps[stat] = ps.get(stat, 0) + n


def _parse_nflu_csv(text: str) -> list[ParsedPlayer]:
    players: dict[str, ParsedPlayer] = {}
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        section = lines[0].strip().strip('"')
        sec = _norm(section)
        pos = _NFLU_SECTION_POS.get(sec)
        rows = list(csv.reader(lines[1:]))
        header = rows[0]
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            uname = row[0].strip()
            p = players.setdefault(uname.lower(), ParsedPlayer(username=uname))
            for col, val in zip(header[1:], row[1:]):
                if not col.strip():
                    continue
                col_n = _norm(col)
                val = val.strip()
                if (sec, col_n) in _NFLU_DERIVED:
                    continue
                if sec == "specialteams" and col_n == "fieldgoals":
                    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", val)
                    if m:
                        made, att = int(m.group(1)), int(m.group(2))
                        _add(p, pos, "fg_made", made)
                        _add(p, pos, "fg_missed", att - made)
                    continue
                n = _num(val.rstrip("%"))
                if n is None:
                    continue
                if col.strip().startswith(">") and col_n in _NFLU_OVER50.get(sec, {}):
                    canon = _NFLU_OVER50[sec][col_n]
                else:
                    canon = _NFLU_COLUMNS.get((sec, col_n))
                if col_n == "snaps":
                    _add(p, pos, "snaps", n)  # per position, and the total for /fantasy's per-snap rate
                    p.extra["Snaps"] = p.extra.get("Snaps", 0) + n
                elif canon:
                    _add(p, pos, canon, n)
                elif n:
                    p.extra[f"{section} {col.strip()}"] = n
    for p in players.values():
        p.stats = {k: v for k, v in p.stats.items() if v}
        p.pos_stats = {pos: nonzero for pos, st in p.pos_stats.items()
                       if (nonzero := {k: v for k, v in st.items() if v})}
    return list(players.values())


def _parse_nflu(feed: dict) -> ParsedGame:
    embed = feed.get("embed") or {}
    title = _strip_md(embed.get("title") or "")
    m = re.fullmatch(r"\s*(.+?)\s+vs\.?\s+(.+?)\s*", title)
    if not m:
        raise ParseError(f"NFL Universe embed title isn't '<team> vs <team>': {title!r}")
    home, away = m.group(1), m.group(2)

    fields = {_norm(f.get("name", "")): str(f.get("value", "")) for f in embed.get("fields") or []
              if isinstance(f, dict)}
    # Scoring History rows look like "Browns    0   0   0   0   0   0" (Q1..Q4, OT, F).
    finals: list[tuple[str, int]] = []
    for line in _strip_md(fields.get("scoringhistory", "")).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].isdigit() and not parts[0].startswith("#"):
            name_parts = [x for x in parts if not x.isdigit()]
            finals.append((" ".join(name_parts), int(parts[-1])))
    scores: dict[str, int] = {}
    for i, (short, pts) in enumerate(finals[:2]):
        s = short.lower()
        if home.lower().endswith(s) or s in home.lower():
            scores.setdefault("home", pts)
        elif away.lower().endswith(s) or s in away.lower():
            scores.setdefault("away", pts)
        else:
            scores.setdefault("home" if i == 0 else "away", pts)

    server_id = fields.get("serverid", "").strip()
    played_at = embed.get("timestamp")
    players = _parse_nflu_csv(feed["csv"]) if feed.get("csv") else []
    return ParsedGame(
        external_id=f"nflu:{server_id}:{played_at}" if played_at else None,
        home_team=home, away_team=away,
        home_score=scores.get("home", 0), away_score=scores.get("away", 0),
        played_at=played_at, players=players,
    )


def payload_hash(body: bytes) -> str:
    """Stable hash of a payload so the same game posted twice is only stored once."""
    try:
        canon = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
    except Exception:
        canon = body
    return hashlib.sha256(canon).hexdigest()
