"""Shared formatting helpers for Discord embeds."""

from __future__ import annotations

import discord

import config

from .db import Database
from .stats import CATEGORY_ORDER, STATS, label

# A short stat line per category, in display order
_SHORT = {
    "passing": [("pass_completions", "/", "pass_attempts", ""), ("pass_yards", " yds", None, None),
                ("pass_tds", " TD", None, None), ("interceptions", " INT", None, None)],
    "rushing": [("rush_attempts", " car", None, None), ("rush_yards", " yds", None, None),
                ("rush_tds", " TD", None, None)],
    "receiving": [("receptions", " rec", None, None), ("rec_yards", " yds", None, None),
                  ("rec_tds", " TD", None, None)],
    "defense": [("tackles", " tkl", None, None), ("sacks", " sck", None, None), ("def_ints", " INT", None, None),
                ("pass_deflections", " PD", None, None), ("def_tds", " TD", None, None)],
}


def fmt_num(v: float) -> str:
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def short_line(stats: dict[str, float]) -> str:
    """'15/22 214 yds 2 TD · 6 car 31 yds' style summary."""
    parts = []
    for cat, items in _SHORT.items():
        bits = []
        for a, sep, b, _ in items:
            if b:
                if stats.get(a) or stats.get(b):
                    bits.append(f"{fmt_num(stats.get(a, 0))}{sep}{fmt_num(stats.get(b, 0))}")
            elif stats.get(a):
                bits.append(f"{fmt_num(stats[a])}{sep}")
        if bits:
            parts.append(" ".join(bits))
    shown = {a for items in _SHORT.values() for a, _, b, _ in items} | {"pass_attempts"}
    others = [f"{fmt_num(v)} {label(k)}" for k, v in stats.items() if v and k not in shown]
    if others:
        parts.append(", ".join(others))
    return " · ".join(parts) or "no stats"


def stats_block(stats: dict[str, float]) -> list[tuple[str, str]]:
    """[(category title, lines)] for a full stat breakdown embed."""
    out = []
    by_cat: dict[str, list[str]] = {}
    for k, v in stats.items():
        if not v:
            continue
        cat = STATS.get(k, ("", "other"))[1]
        by_cat.setdefault(cat, []).append(f"{label(k)}: **{fmt_num(v)}**")
    for cat in CATEGORY_ORDER + ["other"]:
        if cat in by_cat:
            out.append((cat.title(), "\n".join(by_cat[cat])))
    return out


class Names:
    """Caches the roblox-username -> display-name map for one command."""

    def __init__(self, db: Database):
        self.map = db.name_map()

    def __call__(self, username: str) -> str:
        return self.map.get(username.lower(), username)

    def full(self, username: str) -> str:
        d = self.map.get(username.lower())
        return f"{d} ({username})" if d and d.lower() != username.lower() else username


def score_line(g) -> str:
    h, a = g["home_score"], g["away_score"]
    hb, ab = ("**", "") if h > a else ("", "**") if a > h else ("", "")
    return f"{ab}{g['away_team']} {a}{ab} @ {hb}{g['home_team']} {h}{hb}"


def game_header(g) -> str:
    wk = f"Wk {g['week']} · " if g["week"] else ""
    return f"S{g['season']} · {wk}Game #{g['id']}"


def game_embed(db: Database, game_id: int, title_prefix: str = "") -> discord.Embed | None:
    g = db.game(game_id)
    if not g:
        return None
    names = Names(db)
    players = db.game_players(game_id)
    e = discord.Embed(title=f"{title_prefix}{g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']}",
                      description=game_header(g), color=config.EMBED_COLOR)
    by_team: dict[str, list[dict]] = {}
    for p in players:
        by_team.setdefault(p["team"] or "Unknown team", []).append(p)
    order = [g["away_team"], g["home_team"]] + [t for t in by_team if t not in (g["away_team"], g["home_team"])]
    for team in order:
        plist = by_team.get(team)
        if not plist:
            continue
        lines = [f"**{names(p['username'])}** · {fmt_num(p['fantasy_points'])} FP\n{short_line(p['stats'])}"
                 for p in plist]
        _add_long_field(e, team, lines)
    if players:
        mvp = max(players, key=lambda p: p["fantasy_points"])
        e.set_footer(text=f"Player of the game: {names(mvp['username'])} ({fmt_num(mvp['fantasy_points'])} FP)"
                          f" · played {g['played_at'][:10]}")
    else:
        e.set_footer(text=f"No player stats recorded · played {g['played_at'][:10]}")
    return e


def _add_long_field(e: discord.Embed, name: str, lines: list[str]) -> None:
    """Split lines across fields to respect Discord's 1024-char field limit."""
    chunk, first = "", True
    for ln in lines:
        if len(chunk) + len(ln) + 1 > 1000:
            e.add_field(name=name if first else f"{name} (cont.)", value=chunk, inline=False)
            chunk, first = "", False
        chunk += ln + "\n"
    if chunk:
        e.add_field(name=name if first else f"{name} (cont.)", value=chunk, inline=False)


def table(rows: list[list[str]], headers: list[str]) -> str:
    """Monospace table inside a code block."""
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    out += [fmt.format(*[str(c) for c in r]) for r in rows]
    text = "\n".join(out)
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    return f"```\n{text}\n```"
