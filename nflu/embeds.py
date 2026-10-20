"""Shared formatting helpers for Discord embeds."""

from __future__ import annotations

import io
import struct
import zlib
from datetime import datetime, timezone

import discord

import config

from .db import Database
from .fantasy import scored_stats
from .stats import CATEGORY_ORDER, CORE_DISPLAY, MAX_STATS, STATS, label

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


def displayed(stat: str) -> bool:
    """Main stats, plus any stat worth points in config.py, so 60 stat-sheet columns don't flood embeds."""
    return stat in CORE_DISPLAY or stat in scored_stats()


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
    shown = {a for items in _SHORT.values() for a, _, b, _ in items} | {"pass_attempts", "int_charged", "snaps"}
    others = [f"{fmt_num(v)} {label(k)}" for k, v in stats.items() if v and k not in shown and displayed(k)]
    if others:
        parts.append(", ".join(others))
    return " · ".join(parts) or "no stats"


def stats_block(stats: dict[str, float]) -> list[tuple[str, str]]:
    """[(category title, lines)] for a full stat breakdown embed."""
    out = []
    by_cat: dict[str, list[str]] = {}
    for k, v in stats.items():
        if not v or not displayed(k):
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

    def dashed(self, username: str) -> str:
        """'Real Name - roblox_username', or just the username if no name is mapped."""
        d = self.map.get(username.lower())
        return f"{d} - {username}" if d and d.lower() != username.lower() else username


def score_line(g, bold_winner: bool = True) -> str:
    """'Team A 0 vs **Team B 6**' in the order the game listed the teams (stored as home/away)."""
    h, a = g["home_score"], g["away_score"]
    hb, ab = ("**", "") if h > a else ("", "**") if a > h else ("", "")
    if not bold_winner:
        hb = ab = ""
    return f"{hb}{g['home_team']} {h}{hb} vs {ab}{g['away_team']} {a}{ab}"


def when(played_at: str, style: str = "f") -> str:
    """Discord timestamp tag, shown in each viewer's own time zone (style f: 'October 17, 2026 4:48 PM')."""
    try:
        dt = datetime.fromisoformat(played_at)
    except (TypeError, ValueError):
        return str(played_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def game_header(g) -> str:
    wk = f"Wk {g['week']} · " if g["week"] else ""
    return f"{wk}Game #{g['id']}"


def _transparent_png(width: int, height: int) -> bytes:
    """A fully transparent RGBA PNG, built by hand (no image library needed)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


_SPACER = _transparent_png(1200, 1)


def full_width(e: discord.Embed) -> discord.File:
    """Stretch an embed to Discord's maximum width with an invisible wide image.
    Send the returned file with the embed (send_message(embed=e, file=...))."""
    e.set_image(url="attachment://spacer.png")
    return discord.File(io.BytesIO(_SPACER), filename="spacer.png")


GROUP_ICON = {"QB": "🎯", "RB": "🏃", "WR": "🙌", "DB": "🛡️", "DL": "💥", "ST": "🦶"}


def player_card(g, p: dict, rank: int, total: int, names: Names) -> discord.Embed:
    """One player's full stat card for one game: every stat they recorded, grouped by position."""
    from .fantasy import by_group, group_points  # local: fantasy imports stats, avoids a cycle at import

    e = discord.Embed(title=f"{_rank_icon(rank)} {names.dashed(p['username'])}", color=config.EMBED_COLOR,
                      description=f"**{score_line(g, bold_winner=False)}**\n"
                                  f"Game #{g['id']} · {when(g['played_at'])}")
    snaps = p["extra"].get("Snaps") or 0
    e.add_field(name="Fantasy points", value=f"**{fmt_num(p['fantasy_points'])}**", inline=True)
    e.add_field(name="Rank this game", value=f"**{rank}** of {total}", inline=True)
    e.add_field(name="Snaps", value=f"**{fmt_num(snaps)}**", inline=True)
    groups = by_group(p["stats"], p["pos_stats"])
    for code in config.FANTASY_SCORING:  # QB, RB, WR, DB, DL, ST in config order
        st = groups.get(code)
        if not st:
            continue
        order = list(config.FANTASY_SCORING[code])  # the config lists each group's stats in a sensible order
        keys = sorted((k for k, v in st.items() if v and k != "snaps"),
                      key=lambda k: order.index(k) if k in order else len(order))
        if not keys:
            continue
        body = "\n".join(f"{label(k)}: **{fmt_num(st[k])}**" for k in keys)
        if st.get("snaps"):
            body += f"\n*{fmt_num(st['snaps'])} snaps*"
        title = f"{GROUP_ICON.get(code, '')} {code} · {fmt_num(group_points(code, st))} pts"
        e.add_field(name=title, value=body[:1024], inline=True)
    return e


def career_card(c: dict, rank: int, total: int, names: Names) -> discord.Embed:
    """A player's combined stats over every game: totals with a per-game average on everything."""
    n = c["games"]
    e = discord.Embed(title=f"{_rank_icon(rank)} {names.dashed(c['username'])}", color=config.EMBED_COLOR,
                      description=f"Career · **{n}** game{'s' if n != 1 else ''} · "
                                  f"**{fmt_num(c['snaps'])}** snaps ({fmt_num(c['snaps'] / n)}/game)")
    e.add_field(name="Fantasy points", value=f"**{fmt_num(c['fantasy_total'])}**\n{fmt_num(c['fantasy_avg'])}/game",
                inline=True)
    e.add_field(name=f"Per {config.FANTASY_RATE_SNAPS} snaps",
                value=f"**{fmt_num(c['fantasy_rate'])}**\nRank {rank} of {total}", inline=True)
    b = c["best_game"]
    e.add_field(name="Best game", value=f"**{fmt_num(b['fantasy_points'])}** pts\nGame #{b['game_id']}", inline=True)
    for code in config.FANTASY_SCORING:  # QB, RB, WR, DB, DL, ST in config order
        st = c["groups"].get(code)
        if not st:
            continue
        order = list(config.FANTASY_SCORING[code])
        keys = sorted((k for k, (tot, _) in st.items() if tot and k != "snaps"),
                      key=lambda k: order.index(k) if k in order else len(order))
        if not keys:
            continue
        body = ""
        for k in keys:  # whole lines only, within Discord's 1024-character field limit
            tot, avg = st[k]
            line = (f"{label(k)}: **{fmt_num(tot)}** (best) · {fmt_num(avg)} avg\n" if k in MAX_STATS
                    else f"{label(k)}: **{fmt_num(tot)}** · {fmt_num(avg)}/g\n")
            if len(body) + len(line) > 1000:
                break
            body += line
        if "snaps" in st:
            body += f"*{fmt_num(st['snaps'][0])} snaps · {fmt_num(st['snaps'][1])}/g*"
        pts = c["group_points"].get(code, 0)
        e.add_field(name=f"{GROUP_ICON.get(code, '')} {code} · {fmt_num(pts)} pts ({fmt_num(pts / n)}/g)",
                    value=body, inline=True)
    return e


def _rank_icon(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")


def game_embed(db: Database, game_id: int, title_prefix: str = "") -> discord.Embed | None:
    g = db.game(game_id)
    if not g:
        return None
    names = Names(db)
    players = db.game_players(game_id)
    e = discord.Embed(title=f"{title_prefix}{score_line(g, bold_winner=False)}",
                      description=f"{game_header(g)}\nPlayed {when(g['played_at'], 'F')}",
                      color=config.EMBED_COLOR)
    by_team: dict[str, list[dict]] = {}
    for p in players:  # NFL Universe's stat sheet has no teams, so those players are listed together
        by_team.setdefault(p["team"] or "Players", []).append(p)
    order = [g["home_team"], g["away_team"]] + [t for t in by_team if t not in (g["home_team"], g["away_team"])]
    for team in order:
        plist = by_team.get(team)
        if not plist:
            continue
        lines = [f"**{i + 1}. {names(p['username'])}** · {fmt_num(p['fantasy_points'])} pts\n"
                 f"{short_line(p['stats'])}" for i, p in enumerate(plist)]
        _add_long_field(e, team, lines)
    mvp = max(players, key=lambda p: p["fantasy_points"]) if players else None
    if mvp and mvp["fantasy_points"] > 0:
        e.set_footer(text=f"Player of the game: {names(mvp['username'])} ({fmt_num(mvp['fantasy_points'])} pts)")
    else:
        e.set_footer(text="No fantasy points scored" if players else "No player stats recorded")
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
