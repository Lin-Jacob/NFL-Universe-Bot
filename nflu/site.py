"""
Read-only stats website, served by the same aiohttp server as the webhook (so the tunnel URL
shows it too). Pages are plain server-rendered HTML; nothing here can change data, and the
webhook secret, raw payloads and sender addresses are never shown.

    GET /                      home: latest games, fantasy and position leaders
    GET /games                 every game
    GET /games/<id>            box score, every player's stats and points
    GET /players[?q=]          every player (search by name)
    GET /players/<username>    career totals and game log
    GET /fantasy               points-per-snap leaderboard (same as /fantasy)
    GET /positions[/<code>]    QB / RB / WR / DB / DL / ST rankings (same as /stats)
    GET /leaders?stat=         top players in any stat
    GET /scoring               fantasy scoring rules
"""

from __future__ import annotations

from html import escape
from urllib.parse import quote

from aiohttp import web

import config

from . import analytics
from .db import Database
from .embeds import Names, displayed, fmt_num, short_line
from .fantasy import breakdown, by_group, group_points
from .stats import CATEGORY_ORDER, MAX_STATS, POSITIONS, STATS, label

SITE_NAME = "NFL Universe League"

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#5f6673;--line:#e3e6eb;--accent:#013369;--win:#0a7a3d;
--head:#013369;--headtext:#fff;--chip:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--text:#e8eaee;--muted:#9aa3b2;
--line:#2a303b;--accent:#7fb0ff;--win:#4cd38a;--head:#0b1d38;--headtext:#e8eaee;--chip:#222836}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--head);color:var(--headtext)}
header .in{max-width:1100px;margin:0 auto;padding:12px 16px;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center}
header .brand{font-weight:700;font-size:18px;color:var(--headtext);margin-right:auto}
header nav a{color:var(--headtext);opacity:.85}header nav a:hover{opacity:1}
header nav{display:flex;flex-wrap:wrap;gap:4px 14px}
main{max-width:1100px;margin:0 auto;padding:18px 16px 40px}
h1{font-size:24px;margin:4px 0 14px}h2{font-size:18px;margin:22px 0 10px}
.muted{color:var(--muted)}.small{font-size:13px}td.small{min-width:200px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
.score{font-size:22px;font-weight:700}
.win{color:var(--win);font-weight:700}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 9px;font-size:12px;margin:2px 4px 2px 0}
.stat{display:inline-block;margin:0 18px 8px 0}.stat b{display:block;font-size:20px}
form.inline{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
input,select,button{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text)}
button{background:var(--accent);color:#fff;border:none;cursor:pointer}
footer{max-width:1100px;margin:0 auto;padding:0 16px 30px;color:var(--muted);font-size:12px}
"""

# Turns <time datetime="..."> into the viewer's local date and time.
JS = """
document.querySelectorAll('time[datetime]').forEach(function(t){var d=new Date(t.getAttribute('datetime'));
if(!isNaN(d))t.textContent=d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});});
"""

NAV = [("/games", "Games"), ("/players", "Players"), ("/fantasy", "Fantasy"), ("/positions", "Positions"),
       ("/leaders", "Leaders"), ("/scoring", "Scoring")]


def _e(v) -> str:
    return escape(str(v), quote=True)


def _page(title: str, body: str) -> web.Response:
    nav = "".join(f'<a href="{href}">{name}</a>' for href, name in NAV)
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_e(title)} · {_e(SITE_NAME)}</title><style>{CSS}</style></head><body>"
            f'<header><div class="in"><a class="brand" href="/">🏈 {_e(SITE_NAME)}</a><nav>{nav}</nav></div></header>'
            f"<main>{body}</main>"
            f"<footer>Stats from NFL Universe games recorded by the league bot. "
            f"Times are shown in your time zone.</footer><script>{JS}</script></body></html>")
    return web.Response(text=html, content_type="text/html", headers={
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                   "img-src 'self' data:; frame-ancestors 'none'",
    })


def _time(played_at: str) -> str:
    return f'<time datetime="{_e(played_at)}">{_e(played_at)}</time>'


def _num(v: float) -> str:
    return fmt_num(v or 0)


class Site:
    def __init__(self, db: Database):
        self.db = db

    def routes(self) -> list[web.RouteDef]:
        return [
            web.get("/", self.home),
            web.get("/games", self.games),
            web.get("/games/{id}", self.game),
            web.get("/players", self.players),
            web.get("/players/{key}", self.player),
            web.get("/fantasy", self.fantasy),
            web.get("/positions", self.positions),
            web.get("/positions/{code}", self.position),
            web.get("/leaders", self.leaders),
            web.get("/scoring", self.scoring),
        ]

    # ------------------------------------------------------------- helpers
    def _player_link(self, names: Names, username: str, dashed: bool = False) -> str:
        text = names.dashed(username) if dashed else names(username)
        return f'<a href="/players/{quote(username.lower(), safe="")}">{_e(text)}</a>'

    @staticmethod
    def _score(g) -> str:
        """'Team A 21 – 14 Team B' with the winner highlighted, in the order the game listed the teams."""
        h, a = g["home_score"], g["away_score"]
        hc, ac = ("win", "") if h > a else ("", "win") if a > h else ("", "")
        return (f'<span class="{hc}">{_e(g["home_team"])} {h}</span> – '
                f'<span class="{ac}">{a} {_e(g["away_team"])}</span>')

    def _games_table(self, rows, names: Names) -> str:
        if not rows:
            return '<p class="muted">No games recorded yet.</p>'
        out = []
        for g in rows:
            players = self.db.game_players(g["id"])  # highest points first
            star = ""
            if players and players[0]["fantasy_points"] > 0:
                star = f'{self._player_link(names, players[0]["username"])} ' \
                       f'<span class="muted">{_num(players[0]["fantasy_points"])} pts</span>'
            wk = f'<span class="chip">Wk {g["week"]}</span>' if g["week"] else ""
            out.append(f'<tr><td><a href="/games/{g["id"]}">#{g["id"]}</a></td>'
                       f'<td><a href="/games/{g["id"]}">{self._score(g)}</a> {wk}</td>'
                       f'<td class="small muted">{_time(g["played_at"])}</td><td>{star}</td></tr>')
        return ('<div class="card scroll"><table><tr><th>#</th><th>Game</th><th>Played</th>'
                f'<th>Player of the game</th></tr>{"".join(out)}</table></div>')

    def _rate_table(self, rows: list[dict], names: Names) -> str:
        per = config.FANTASY_RATE_SNAPS
        body = "".join(
            f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, r["username"], dashed=True)}</td>'
            f'<td class="n"><b>{_num(r["rate"])}</b></td><td class="n">{_num(r["points"])}</td>'
            f'<td class="n">{_num(r["snaps"])}</td><td class="n">{r["games"]}</td></tr>'
            for i, r in enumerate(rows))
        return (f'<div class="card scroll"><table><tr><th class="n">#</th><th>Player</th>'
                f'<th class="n">Pts / {per} snaps</th><th class="n">Points</th><th class="n">Snaps</th>'
                f'<th class="n">GP</th></tr>{body}</table></div>')

    def _position_table(self, rows: list[dict], names: Names) -> str:
        if not rows:
            return '<p class="muted">No one has scored at this position yet.</p>'
        body = "".join(
            f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, r["username"])}</td>'
            f'<td class="n"><b>{_num(r["points"])}</b></td><td class="n">{r["games"]}</td>'
            f'<td class="small">{_e(short_line(r["stats"]))}</td></tr>'
            for i, r in enumerate(rows))
        return (f'<div class="scroll"><table><tr><th class="n">#</th><th>Player</th><th class="n">Pts</th>'
                f'<th class="n">GP</th><th>Stats</th></tr>{body}</table></div>')

    @staticmethod
    def _stats_cards(stats: dict[str, float]) -> str:
        by_cat: dict[str, list[str]] = {}
        for k, v in stats.items():
            if v and displayed(k):
                by_cat.setdefault(STATS.get(k, ("", "other"))[1], []).append(
                    f'<tr><td>{_e(label(k))}</td><td class="n"><b>{_num(v)}</b></td></tr>')
        cards = [f'<div class="card"><h2 style="margin-top:0">{_e(cat.title())}</h2>'
                 f'<table>{"".join(by_cat[cat])}</table></div>'
                 for cat in CATEGORY_ORDER + ["other"] if cat in by_cat]
        return f'<div class="grid">{"".join(cards)}</div>' if cards else '<p class="muted">No stats recorded.</p>'

    # --------------------------------------------------------------- pages
    async def home(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        games = self.db.recent_games(8)
        n_games = self.db.one("SELECT count(*) n FROM games")["n"]
        n_players = self.db.one("SELECT count(DISTINCT username_key) n FROM player_games")["n"]
        ranked, _ = analytics.fantasy_rates(self.db)
        pos_cards = "".join(
            f'<div class="card"><h2 style="margin-top:0"><a href="/positions/{code}">{code} · {_e(title)}</a></h2>'
            f'{self._position_table(analytics.position_leaders(self.db, code, 3), names)}</div>'
            for code, (title, _) in POSITIONS.items())
        body = (f"<h1>{_e(SITE_NAME)}</h1>"
                f'<div class="card"><span class="stat"><b>{n_games}</b><span class="muted">games</span></span>'
                f'<span class="stat"><b>{n_players}</b><span class="muted">players</span></span></div>'
                f'<h2>Latest games <a class="small" href="/games">all games →</a></h2>'
                f"{self._games_table(games, names)}"
                f'<h2>Fantasy leaders <a class="small" href="/fantasy">full board →</a></h2>'
                f"{self._rate_table(ranked[:5], names) if ranked else '<p class=muted>No fantasy points yet.</p>'}"
                f'<h2>Position leaders <a class="small" href="/positions">all positions →</a></h2>'
                f'<div class="grid">{pos_cards}</div>')
        return _page("Home", body)

    async def games(self, request: web.Request) -> web.Response:
        rows = self.db.recent_games(100000)
        return _page("Games", f"<h1>Games played</h1><p class='muted'>{len(rows)} games, newest first.</p>"
                              f"{self._games_table(rows, Names(self.db))}")

    async def game(self, request: web.Request) -> web.Response:
        try:
            g = self.db.game(int(request.match_info["id"]))
        except ValueError:
            g = None
        if not g:
            raise web.HTTPNotFound(text="No such game")
        names = Names(self.db)
        players = self.db.game_players(g["id"])
        wk = f'<span class="chip">Week {g["week"]}</span>' if g["week"] else ""
        rows = []
        for i, p in enumerate(players):
            groups = by_group(p["stats"], p["pos_stats"])
            chips = "".join(f'<span class="chip">{code} {_num(group_points(code, st))}</span>'
                            for code in config.FANTASY_SCORING if (st := groups.get(code)))
            rows.append(f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, p["username"], dashed=True)}'
                        f'<br>{chips}</td><td class="n"><b>{_num(p["fantasy_points"])}</b></td>'
                        f'<td class="n">{_num(p["extra"].get("Snaps"))}</td>'
                        f'<td class="small">{_e(short_line(p["stats"]))}</td></tr>')
        table = (f'<div class="card scroll"><table><tr><th class="n">#</th><th>Player</th><th class="n">Pts</th>'
                 f'<th class="n">Snaps</th><th>Stats</th></tr>{"".join(rows)}</table></div>'
                 if rows else '<p class="muted">No player stats recorded for this game.</p>')
        ids = [r["id"] for r in self.db.q("SELECT id FROM games ORDER BY played_at, id")]
        i = ids.index(g["id"])
        nav = []
        if i > 0:
            nav.append(f'<a href="/games/{ids[i - 1]}">← older game</a>')
        if i < len(ids) - 1:
            nav.append(f'<a href="/games/{ids[i + 1]}">newer game →</a>')
        body = (f'<p class="small">{" · ".join(nav)}</p>'
                f'<div class="card"><div class="muted small">Game #{g["id"]} {wk}</div>'
                f'<div class="score">{self._score(g)}</div>'
                f'<div class="muted small">Played {_time(g["played_at"])}</div></div>'
                f"<h2>Players</h2>{table}")
        return _page(f'{g["home_team"]} vs {g["away_team"]}', body)

    async def players(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        q = request.query.get("q", "").strip().lower()
        agg: dict[str, dict] = {}
        for ln in self.db.all_player_lines():
            e = agg.setdefault(ln["username_key"], {"username": ln["username"], "points": 0.0, "snaps": 0.0, "games": 0})
            e["points"] += ln["fantasy_points"]
            e["snaps"] += ln["extra"].get("Snaps") or 0
            e["games"] += 1
        rows = [r for r in agg.values() if not q or q in names.dashed(r["username"]).lower()]
        rows.sort(key=lambda r: names(r["username"]).lower())
        per = config.FANTASY_RATE_SNAPS
        body = "".join(
            f'<tr><td>{self._player_link(names, r["username"], dashed=True)}</td><td class="n">{r["games"]}</td>'
            f'<td class="n">{_num(round(r["points"], 2))}</td><td class="n">{_num(round(r["points"] / r["games"], 2))}</td>'
            f'<td class="n">{_num(analytics.per_snap(r["points"], r["snaps"]))}</td></tr>'
            for r in rows)
        table = (f'<div class="card scroll"><table><tr><th>Player</th><th class="n">GP</th><th class="n">Points</th>'
                 f'<th class="n">Pts / game</th><th class="n">Pts / {per} snaps</th></tr>{body}</table></div>'
                 if rows else '<p class="muted">No players found.</p>')
        form = (f'<form class="inline" action="/players"><input name="q" value="{_e(q)}" '
                f'placeholder="Search name or Roblox username"><button>Search</button></form>')
        return _page("Players", f"<h1>Players</h1>{form}{table}")

    async def player(self, request: web.Request) -> web.Response:
        key = self.db.resolve_player(request.match_info["key"])
        summ = analytics.player_summary(self.db, key) if key else None
        if not summ:
            raise web.HTTPNotFound(text="No such player")
        names = Names(self.db)
        per = config.FANTASY_RATE_SNAPS
        b = summ["best_game"]
        head = (f'<div class="card"><span class="stat"><b>{summ["games"]}</b><span class="muted">games</span></span>'
                f'<span class="stat"><b>{_num(summ["fantasy_total"])}</b><span class="muted">fantasy points</span></span>'
                f'<span class="stat"><b>{_num(summ["fantasy_rate"])}</b><span class="muted">pts / {per} snaps</span></span>'
                f'<span class="stat"><b>{_num(summ["fantasy_avg"])}</b><span class="muted">pts / game</span></span>'
                f'<span class="stat"><b>{_num(summ["snaps"])}</b><span class="muted">snaps</span></span>'
                f'<div class="small">Best game: <a href="/games/{b["game_id"]}">#{b["game_id"]}</a> · '
                f'{_num(b["fantasy_points"])} pts · {_e(short_line(b["stats"]))}</div></div>')
        log = "".join(
            f'<tr><td><a href="/games/{ln["game_id"]}">#{ln["game_id"]}</a></td>'
            f'<td>{self._score(ln)}<br><span class="small muted">{_time(ln["played_at"])}</span></td>'
            f'<td class="n"><b>{_num(ln["fantasy_points"])}</b></td>'
            f'<td class="small">{_e(short_line(ln["stats"]))}<br>'
            + "".join(f'<span class="chip">{_e(s)} {p:+.2f}</span>'
                      for s, _, p in breakdown(ln["stats"], ln["pos_stats"]))
            + "</td></tr>"
            for ln in summ["lines"])
        body = (f'<h1>{_e(names.dashed(summ["username"]))}</h1>{head}'
                f'<h2>Career totals</h2>{self._stats_cards(summ["totals"])}'
                f'<h2>Game log</h2><div class="card scroll"><table><tr><th>#</th><th>Game</th><th class="n">Pts</th>'
                f'<th>Stats and how the points were scored</th></tr>{log}</table></div>')
        return _page(names(summ["username"]), body)

    async def fantasy(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        ranked, short = analytics.fantasy_rates(self.db)
        per = config.FANTASY_RATE_SNAPS
        body = (f"<h1>Fantasy leaderboard</h1><p class='muted'>Ranked by fantasy points per {per} snaps on the field, "
                f"so joining a game late doesn't cost you. A full game is about 130-145 snaps.</p>"
                + (self._rate_table(ranked, names) if ranked else "<p class='muted'>Nobody has enough snaps yet.</p>"))
        if short:
            body += (f"<h2>Not enough snaps yet (under {config.FANTASY_MIN_SNAPS})</h2>"
                     + self._rate_table(short, names))
        return _page("Fantasy", body)

    async def positions(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        cards = "".join(
            f'<div class="card"><h2 style="margin-top:0"><a href="/positions/{code}">{code} · {_e(title)}</a></h2>'
            f'{self._position_table(analytics.position_leaders(self.db, code, 5), names)}</div>'
            for code, (title, _) in POSITIONS.items())
        return _page("Positions", "<h1>Position leaderboards</h1><p class='muted'>Fantasy points earned at each "
                                  "position across all games. Positions come from the NFL Universe stat sheet: "
                                  "Passing, Rushing, Receiving, Cornerback, DLine, Special Teams.</p>" + cards)

    async def position(self, request: web.Request) -> web.Response:
        code = request.match_info["code"].upper()
        if code not in POSITIONS:
            raise web.HTTPNotFound(text="No such position")
        rows = analytics.position_leaders(self.db, code, 100000)
        tabs = " ".join(f'<a class="chip" href="/positions/{c}">{c}</a>' for c in POSITIONS)
        return _page(code, f"<h1>{code} · {_e(POSITIONS[code][0])}</h1><p>{tabs}</p>"
                           f'<div class="card">{self._position_table(rows, Names(self.db))}</div>')

    async def leaders(self, request: web.Request) -> web.Response:
        stat = request.query.get("stat", "fantasy")
        if stat != "fantasy" and stat not in STATS:
            stat = "fantasy"
        per_game = request.query.get("per_game") == "1"
        opts = [("fantasy", "Fantasy points")] + [(k, lbl) for k, (lbl, _) in STATS.items()]
        select = "".join(f'<option value="{k}"{" selected" if k == stat else ""}>{_e(lbl)}</option>'
                         for k, lbl in opts)
        form = (f'<form class="inline" action="/leaders"><select name="stat">{select}</select>'
                f'<label><input type="checkbox" name="per_game" value="1"{" checked" if per_game else ""}> '
                f'per game</label><button>Show</button></form>')
        names = Names(self.db)
        rows = analytics.leaders(self.db, stat, 25, per_game)
        if stat in MAX_STATS and not per_game:
            rows = self._max_leaders(stat)
        body = "".join(f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, n)}</td>'
                       f'<td class="n"><b>{_num(v)}</b></td><td class="n">{g}</td></tr>'
                       for i, (n, v, g) in enumerate(rows))
        name = "Fantasy points" if stat == "fantasy" else label(stat)
        table = (f'<div class="card scroll"><table><tr><th class="n">#</th><th>Player</th>'
                 f'<th class="n">{_e(name)}{" / game" if per_game else ""}</th><th class="n">GP</th></tr>'
                 f"{body}</table></div>" if rows else "<p class='muted'>Nobody has recorded that stat yet.</p>")
        return _page(f"{name} leaders", f"<h1>{_e(name)} leaders</h1>{form}{table}")

    def _max_leaders(self, stat: str) -> list[tuple[str, float, int]]:
        """'Longest ...' stats: best single play, not the sum across games."""
        agg: dict[str, list] = {}
        for ln in self.db.all_player_lines():
            e = agg.setdefault(ln["username_key"], [ln["username"], 0.0, 0])
            e[1] = max(e[1], ln["stats"].get(stat) or 0)
            e[2] += 1
        rows = [tuple(r) for r in agg.values() if r[1]]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:25]

    async def scoring(self, request: web.Request) -> web.Response:
        cards = []
        for code, (title, section) in POSITIONS.items():
            rows = "".join(f'<tr><td>{_e(label(k))}</td><td class="n">{v:+g}</td></tr>'
                           for k, v in config.FANTASY_SCORING.get(code, {}).items() if v)
            if rows:
                cards.append(f'<div class="card"><h2 style="margin-top:0">{code} · {_e(title)}</h2>'
                             f'<p class="small muted">From the {_e(section)} section</p><table>{rows}</table></div>')
        bonuses = "".join(f'<tr><td>{_e(label(s))} ≥ {t:g} in one game</td><td class="n">{b:+g}</td></tr>'
                          for s, t, b in config.FANTASY_BONUSES)
        if bonuses:
            cards.append(f'<div class="card"><h2 style="margin-top:0">Bonuses</h2><table>{bonuses}</table></div>')
        return _page("Scoring", "<h1>Fantasy scoring</h1><p class='muted'>Points per unit of each stat, by the "
                                "position it was earned at. Stats not listed are worth 0.</p>"
                                f'<div class="grid">{"".join(cards)}</div>')
