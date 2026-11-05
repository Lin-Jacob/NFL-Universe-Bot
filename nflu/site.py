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
    GET /seasons[/<n>]         every season, and one season's stat awards and voted awards

Most pages take ?season=N to show one season's numbers instead of everyone's overall totals
(the season picker at the top of the page sets it). Voting itself is on /vote (book.py), which
needs a league player's sportsbook login.
"""

from __future__ import annotations

import json
from html import escape
from urllib.parse import quote, urlencode

from aiohttp import web

import config

from . import analytics, charts
from .awards import STAT_AWARDS, vote_awards
from .db import Database, season_min_players
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
form.season{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
.tabs a{padding:4px 12px;border-radius:999px;background:var(--chip);color:var(--text)}
.tabs a.on{background:var(--accent);color:#fff}
.award{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--line)}
.award:last-child{border-bottom:none}
.prog{height:10px;border-radius:999px;background:var(--chip);overflow:hidden;margin:6px 0 2px;min-width:120px}
.prog span{display:block;height:100%;background:var(--accent);border-radius:999px}
""" + charts.CSS

# Turns <time datetime="..."> into the viewer's local date and time; the season picker submits on change.
JS = """
document.querySelectorAll('time[datetime]').forEach(function(t){var d=new Date(t.getAttribute('datetime'));
if(!isNaN(d))t.textContent=d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});});
document.querySelectorAll('form.season select').forEach(function(s){s.addEventListener('change',function(){
s.form.submit();});});
"""

NAV = [("/games", "Games"), ("/players", "Players"), ("/fantasy", "Fantasy"), ("/positions", "Positions"),
       ("/leaders", "Leaders"), ("/seasons", "Seasons"), ("/vote", "Vote"), ("/scoring", "Scoring"),
       ("/book", "Sportsbook")]


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
                                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
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
            web.get("/seasons", self.seasons),
            web.get("/seasons/{id}", self.season_page),
            web.get("/scoring", self.scoring),
        ]

    # ------------------------------------------------------------- seasons
    def _sid(self, request: web.Request) -> int | None:
        """?season=N when N is a real season, else None (= overall)."""
        try:
            n = int(request.query.get("season", "0"))
        except ValueError:
            return None
        return n if n and self.db.season(n) else None

    @staticmethod
    def _q(sid: int | None, **extra) -> str:
        """Query string that keeps the chosen season: '?season=2&stat=x' (or '' for overall)."""
        params = {k: v for k, v in extra.items() if v not in (None, "")}
        if sid:
            params = {"season": sid, **params}
        return ("?" + urlencode(params)) if params else ""

    def _picker(self, request: web.Request, sid: int | None) -> str:
        """Overall / season selector. Keeps the page's other query settings (stat, per_game, q)."""
        rows = self.db.seasons()
        if not rows:
            return ""
        active = self.db.active_season()
        opts = [f'<option value="0"{"" if sid else " selected"}>Overall (all games)</option>']
        for srow in reversed(rows):
            tag = " · current" if active and active["id"] == srow["id"] else ""
            opts.append(f'<option value="{srow["id"]}"{" selected" if sid == srow["id"] else ""}>'
                        f'{_e(srow["name"])}{tag}</option>')
        keep = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                       for k, v in request.query.items() if k != "season")
        return (f'<form class="season" method="get" action="{_e(request.path)}"><label class="muted small" '
                f'for="season-pick">Stats for</label><select id="season-pick" name="season">{"".join(opts)}</select>'
                f'{keep}<noscript><button>Show</button></noscript></form>')

    def _progress(self, sid: int, bar: bool = True) -> str:
        """'34 of 80 games · 42.5% complete' with a progress bar."""
        n, total, pct = self.db.season_progress(sid)
        if not total:
            return f"{n} games"
        text = f"{n} of {total} games · {pct:.1f}% complete"
        return text + (f'<div class="prog" title="{text}"><span style="width:{pct:.1f}%"></span></div>' if bar else "")

    def _scope_note(self, sid: int | None) -> str:
        if not sid:
            return "across all games"
        srow = self.db.season(sid)
        return f"in {_e(srow['name'])}" if srow else "this season"

    # ------------------------------------------------------------- helpers
    def _player_link(self, names: Names, username: str, dashed: bool = False, sid: int | None = None) -> str:
        text = names.dashed(username) if dashed else names(username)
        return f'<a href="/players/{quote(username.lower(), safe="")}{self._q(sid)}">{_e(text)}</a>'

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
        seasons_on = bool(self.db.seasons())
        out = []
        for g in rows:
            players = self.db.game_players(g["id"])  # highest points first
            star = ""
            if players and players[0]["fantasy_points"] > 0:
                star = f'{self._player_link(names, players[0]["username"])} ' \
                       f'<span class="muted">{_num(players[0]["fantasy_points"])} pts</span>'
            wk = f'<span class="chip">Wk {g["week"]}</span>' if g["week"] else ""
            ss = (f'<span class="chip">S{g["season"]}</span>' if g["season"] else
                  f'<span class="chip" title="Under {season_min_players()} players: kept in the all-time stats, '
                  f'but not in a season">not counted</span>' if seasons_on else "")
            out.append(f'<tr><td><a href="/games/{g["id"]}">#{g["id"]}</a></td>'
                       f'<td><a href="/games/{g["id"]}">{self._score(g)}</a> {wk}{ss}</td>'
                       f'<td class="small muted">{_time(g["played_at"])}</td><td>{star}</td></tr>')
        return ('<div class="card scroll"><table><tr><th>#</th><th>Game</th><th>Played</th>'
                f'<th>Player of the game</th></tr>{"".join(out)}</table></div>')

    def _rate_table(self, rows: list[dict], names: Names, sid: int | None = None) -> str:
        per = config.FANTASY_RATE_SNAPS
        body = "".join(
            f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, r["username"], True, sid)}</td>'
            f'<td class="n"><b>{_num(r["rate"])}</b></td><td class="n">{_num(r["points"])}</td>'
            f'<td class="n">{_num(r["snaps"])}</td><td class="n">{r["games"]}</td></tr>'
            for i, r in enumerate(rows))
        return (f'<div class="card scroll"><table><tr><th class="n">#</th><th>Player</th>'
                f'<th class="n">Pts / {per} snaps</th><th class="n">Points</th><th class="n">Snaps</th>'
                f'<th class="n">GP</th></tr>{body}</table></div>')

    def _position_table(self, rows: list[dict], names: Names, sid: int | None = None) -> str:
        if not rows:
            return '<p class="muted">No one has scored at this position yet.</p>'
        body = "".join(
            f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, r["username"], sid=sid)}</td>'
            f'<td class="n"><b>{_num(r["points"])}</b></td><td class="n">{r["games"]}</td>'
            f'<td class="small">{_e(short_line(r["stats"]))}</td></tr>'
            for i, r in enumerate(rows))
        return (f'<div class="scroll"><table><tr><th class="n">#</th><th>Player</th><th class="n">Pts</th>'
                f'<th class="n">GP</th><th>Stats</th></tr>{body}</table></div>')

    def _bar_rows(self, rows, names: Names, value, tip, sid: int | None = None, limit: int = 15):
        """Rows for charts.bars from leaderboard rows: (player link, value, tooltip)."""
        return [(self._player_link(names, r["username"], sid=sid), value(r), tip(r)) for r in rows[:limit]]

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
        sid = self._sid(request)
        games = self._games(sid)[:8]
        n_games = self.db.season_game_count(sid) if sid else self.db.one("SELECT count(*) n FROM games")["n"]
        n_players = len({ln["username_key"] for ln in self.db.all_player_lines(season=sid)})
        ranked, _ = analytics.fantasy_rates(self.db, sid)
        pos_cards = "".join(
            f'<div class="card"><h2 style="margin-top:0"><a href="/positions/{code}{self._q(sid)}">{code} · '
            f'{_e(title)}</a></h2>{self._position_table(analytics.position_leaders(self.db, code, 3, sid), names, sid)}</div>'
            for code, (title, _) in POSITIONS.items())
        active, voting = self.db.active_season(), self.db.voting_season()
        banner = ""
        if voting:
            banner += (f'<div class="card">🗳️ <b>Voting is open</b> for {_e(voting["name"])}\'s awards until '
                       f'{_time(voting["vote_ends_at"])}. <a href="/vote">Cast your votes →</a></div>')
        if active:
            banner += (f'<div class="card">📅 <b>{_e(active["name"])}</b>: {self._progress(active["id"])}'
                       f'<a href="/seasons/{active["id"]}">Season games and awards race →</a></div>')
        body = (f"<h1>{_e(SITE_NAME)}</h1>{self._picker(request, sid)}{banner}"
                f'<div class="card"><span class="stat"><b>{n_games}</b><span class="muted">games</span></span>'
                f'<span class="stat"><b>{n_players}</b><span class="muted">players</span></span></div>'
                f'<h2>Latest games <a class="small" href="/games{self._q(sid)}">all games →</a></h2>'
                f"{self._games_table(games, names)}"
                f'<h2>Fantasy leaders <a class="small" href="/fantasy{self._q(sid)}">full board →</a></h2>'
                f"{self._rate_table(ranked[:5], names, sid) if ranked else '<p class=muted>No fantasy points yet.</p>'}"
                f'<h2>Position leaders <a class="small" href="/positions{self._q(sid)}">all positions →</a></h2>'
                f'<div class="grid">{pos_cards}</div>')
        return _page("Home", body)

    def _games(self, sid: int | None):
        if sid:
            return self.db.q("SELECT * FROM games WHERE season = ? ORDER BY played_at DESC, id DESC", (sid,))
        return self.db.recent_games(100000)

    async def games(self, request: web.Request) -> web.Response:
        sid = self._sid(request)
        rows = self._games(sid)
        note = (f"<div class='card'>{self._progress(sid)}</div>" if sid else
                f"<p class='muted'>Every game recorded. Games marked <i>not counted</i> had under "
                f"{season_min_players()} players: they count in the all-time stats but not toward a season.</p>"
                if self.db.seasons() else "")
        return _page("Games", f"<h1>{'Season game log' if sid else 'Games played'}</h1>{self._picker(request, sid)}{note}"
                              f"<p class='muted'>{len(rows)} games {self._scope_note(sid)}, newest first.</p>"
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
        if g["season"] and (srow := self.db.season(g["season"])):
            wk += f' <a class="chip" href="/seasons/{srow["id"]}">{_e(srow["name"])}</a>'
        elif self.db.seasons():
            wk += (f' <span class="chip">Not counted for a season: {len(players)} players, needs '
                   f'{season_min_players()}</span>')
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
        chart = charts.bars([(self._player_link(names, p["username"]), p["fantasy_points"],
                              f'{names(p["username"])}: {_num(p["fantasy_points"])} fantasy points')
                             for p in players], "Fantasy points this game", " pts")
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
                + (f'<div class="card">{chart}</div>' if chart else "") + f"<h2>Players</h2>{table}")
        return _page(f'{g["home_team"]} vs {g["away_team"]}', body)

    async def players(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        sid = self._sid(request)
        q = request.query.get("q", "").strip().lower()
        agg: dict[str, dict] = {}
        for ln in self.db.all_player_lines(season=sid):
            e = agg.setdefault(ln["username_key"], {"username": ln["username"], "points": 0.0, "snaps": 0.0, "games": 0})
            e["points"] += ln["fantasy_points"]
            e["snaps"] += ln["extra"].get("Snaps") or 0
            e["games"] += 1
        rows = [r for r in agg.values() if not q or q in names.dashed(r["username"]).lower()]
        rows.sort(key=lambda r: names(r["username"]).lower())
        per = config.FANTASY_RATE_SNAPS
        body = "".join(
            f'<tr><td>{self._player_link(names, r["username"], True, sid)}</td><td class="n">{r["games"]}</td>'
            f'<td class="n">{_num(round(r["points"], 2))}</td><td class="n">{_num(round(r["points"] / r["games"], 2))}</td>'
            f'<td class="n">{_num(analytics.per_snap(r["points"], r["snaps"]))}</td></tr>'
            for r in rows)
        table = (f'<div class="card scroll"><table><tr><th>Player</th><th class="n">GP</th><th class="n">Points</th>'
                 f'<th class="n">Pts / game</th><th class="n">Pts / {per} snaps</th></tr>{body}</table></div>'
                 if rows else '<p class="muted">No players found.</p>')
        keep = f'<input type="hidden" name="season" value="{sid}">' if sid else ""
        form = (f'<form class="inline" action="/players">{keep}<input name="q" value="{_e(q)}" '
                f'placeholder="Search name or Roblox username"><button>Search</button></form>')
        return _page("Players", f"<h1>Players</h1>{self._picker(request, sid)}{form}{table}")

    async def player(self, request: web.Request) -> web.Response:
        key = self.db.resolve_player(request.match_info["key"])
        sid = self._sid(request)
        summ = analytics.player_summary(self.db, key, sid) if key else None
        names = Names(self.db)
        if not summ:
            if key and sid and analytics.player_summary(self.db, key):
                srow = self.db.season(sid)
                return _page(names(key), f"<h1>{_e(names.dashed(key))}</h1>{self._picker(request, sid)}"
                                         f"<p class='muted'>No games in {_e(srow['name'])}.</p>")
            raise web.HTTPNotFound(text="No such player")
        per = config.FANTASY_RATE_SNAPS
        b = summ["best_game"]
        head = (f'<div class="card"><span class="stat"><b>{summ["games"]}</b><span class="muted">games</span></span>'
                f'<span class="stat"><b>{_num(summ["fantasy_total"])}</b><span class="muted">fantasy points</span></span>'
                f'<span class="stat"><b>{_num(summ["fantasy_rate"])}</b><span class="muted">pts / {per} snaps</span></span>'
                f'<span class="stat"><b>{_num(summ["fantasy_avg"])}</b><span class="muted">pts / game</span></span>'
                f'<span class="stat"><b>{_num(summ["snaps"])}</b><span class="muted">snaps</span></span>'
                f'<div class="small">Best game: <a href="/games/{b["game_id"]}">#{b["game_id"]}</a> · '
                f'{_num(b["fantasy_points"])} pts · {_e(short_line(b["stats"]))}</div></div>')
        # Charts: points per game over time, points by position, and (overall) points by season
        lines = list(reversed(summ["lines"]))  # oldest first
        trend = charts.line([(f"#{ln['game_id']}", ln["fantasy_points"],
                              f"Game #{ln['game_id']}: {_num(ln['fantasy_points'])} pts · {short_line(ln['stats'])}",
                              f"/games/{ln['game_id']}") for ln in lines],
                            "Fantasy points by game", "points per game")
        pos_pts: dict[str, float] = {}
        for ln in summ["lines"]:
            for code, st in by_group(ln["stats"], ln["pos_stats"]).items():
                pos_pts[code] = pos_pts.get(code, 0.0) + group_points(code, st)
        by_pos = charts.bars([(f"{code} · {_e(POSITIONS.get(code, (code,))[0])}", round(v, 2),
                               f"{code}: {_num(round(v, 2))} pts") for code, v in
                              sorted(pos_pts.items(), key=lambda kv: kv[1], reverse=True) if v],
                             "Points by position", " pts")
        by_season = ""
        if not sid:
            per_season: dict[int, list[float]] = {}
            for ln in summ["lines"]:
                if ln.get("season"):
                    per_season.setdefault(ln["season"], []).append(ln["fantasy_points"])
            rows = []
            for n, pts in sorted(per_season.items()):
                srow = self.db.season(n)
                nm = srow["name"] if srow else f"Season {n}"
                rows.append((f'<a href="?season={n}">{_e(nm)}</a>', round(sum(pts), 2),
                             f"{nm}: {_num(round(sum(pts), 2))} pts in {len(pts)} games "
                             f"({_num(round(sum(pts) / len(pts), 2))} per game)"))
            if len(rows) > 0:
                by_season = charts.bars(rows, "Points by season", " pts")
        chart_cards = "".join(f'<div class="card">{c}</div>' for c in (by_pos, by_season) if c)
        log = "".join(
            f'<tr><td><a href="/games/{ln["game_id"]}">#{ln["game_id"]}</a></td>'
            f'<td>{self._score(ln)}<br><span class="small muted">{_time(ln["played_at"])}</span></td>'
            f'<td class="n"><b>{_num(ln["fantasy_points"])}</b></td>'
            f'<td class="small">{_e(short_line(ln["stats"]))}<br>'
            + "".join(f'<span class="chip">{_e(s)} {p:+.2f}</span>'
                      for s, _, p in breakdown(ln["stats"], ln["pos_stats"]))
            + "</td></tr>"
            for ln in summ["lines"])
        awards_html = self._player_awards(key, names)
        totals_title = "Season totals" if sid else "Career totals"
        body = (f'<h1>{_e(names.dashed(summ["username"]))}</h1>{self._picker(request, sid)}{head}{awards_html}'
                f'<div class="card">{trend}</div>'
                + (f'<div class="grid">{chart_cards}</div>' if chart_cards else "")
                + f'<h2>{totals_title}</h2>{self._stats_cards(summ["totals"])}'
                f'<h2>{"Season game log" if sid else "Game log"}</h2><div class="card scroll"><table><tr><th>#</th><th>Game</th><th class="n">Pts</th>'
                f'<th>Stats and how the points were scored</th></tr>{log}</table></div>')
        return _page(names(summ["username"]), body)

    def _player_awards(self, key: str, names: Names) -> str:
        """Trophy case: every finished season's award this player won."""
        won = []
        votes = vote_awards()
        for srow in self.db.seasons():
            if srow["mvps"]:
                for code, rows in json.loads(srow["mvps"])["roles"].items():
                    if rows and rows[0]["username_key"] == key and code in STAT_AWARDS:
                        t, icon = STAT_AWARDS[code]
                        won.append(f'<a class="chip" href="/seasons/{srow["id"]}">{icon} {_e(t)} · {_e(srow["name"])}</a>')
            if srow["vote_results"]:
                for code, rows in json.loads(srow["vote_results"])["roles"].items():
                    if rows and rows[0]["username_key"] == key and code in votes:
                        t, icon = votes[code]
                        won.append(f'<a class="chip" href="/seasons/{srow["id"]}">{icon} {_e(t)} · {_e(srow["name"])}</a>')
        return f'<div class="card"><b>Awards</b><br>{"".join(won)}</div>' if won else ""

    async def fantasy(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        sid = self._sid(request)
        ranked, short = analytics.fantasy_rates(self.db, sid)
        per = config.FANTASY_RATE_SNAPS
        chart = charts.bars(self._bar_rows(ranked, names, lambda r: r["rate"],
                                           lambda r: f'{names(r["username"])}: {_num(r["rate"])} per {per} snaps, '
                                                     f'{_num(r["points"])} pts in {r["games"]} games', sid),
                            f"Points per {per} snaps (top 15)")
        body = (f"<h1>Fantasy leaderboard</h1>{self._picker(request, sid)}<p class='muted'>Fantasy points per {per} "
                f"snaps on the field {self._scope_note(sid)}, so joining a game late doesn't cost you. "
                f"A full game is about 130-145 snaps.</p>"
                + (f'<div class="card">{chart}</div>' if chart else "")
                + (self._rate_table(ranked, names, sid) if ranked else "<p class='muted'>Nobody has enough snaps yet.</p>"))
        if short:
            body += (f"<h2>Not enough snaps yet (under {config.FANTASY_MIN_SNAPS})</h2>"
                     + self._rate_table(short, names, sid))
        return _page("Fantasy", body)

    async def positions(self, request: web.Request) -> web.Response:
        names = Names(self.db)
        sid = self._sid(request)
        cards = []
        for code, (title, _) in POSITIONS.items():
            rows = analytics.position_leaders(self.db, code, 5, sid)
            chart = charts.bars(self._bar_rows(rows, names, lambda r: r["points"],
                                               lambda r: f'{names(r["username"])}: {_num(r["points"])} pts in '
                                                         f'{r["games"]} games', sid), "", " pts")
            cards.append(f'<div class="card"><h2 style="margin-top:0"><a href="/positions/{code}{self._q(sid)}">'
                         f'{code} · {_e(title)}</a></h2>{chart}{self._position_table(rows, names, sid)}</div>')
        return _page("Positions", f"<h1>Position leaderboards</h1>{self._picker(request, sid)}"
                                  f"<p class='muted'>Fantasy points earned at each position {self._scope_note(sid)}. "
                                  "Positions come from the NFL Universe stat sheet: Passing, Rushing, Receiving, "
                                  "Cornerback, DLine, Special Teams.</p>" + "".join(cards))

    async def position(self, request: web.Request) -> web.Response:
        code = request.match_info["code"].upper()
        if code not in POSITIONS:
            raise web.HTTPNotFound(text="No such position")
        sid = self._sid(request)
        names = Names(self.db)
        rows = analytics.position_leaders(self.db, code, 100000, sid)
        tabs = "".join(f'<a class="{"on" if c == code else ""}" href="/positions/{c}{self._q(sid)}">{c}</a>'
                       for c in POSITIONS)
        chart = charts.bars(self._bar_rows(rows, names, lambda r: r["points"],
                                           lambda r: f'{names(r["username"])}: {_num(r["points"])} pts in '
                                                     f'{r["games"]} games ({_num(round(r["points"] / r["games"], 2))}'
                                                     f' per game)', sid), "Points (top 15)", " pts")
        return _page(code, f"<h1>{code} · {_e(POSITIONS[code][0])}</h1>{self._picker(request, sid)}"
                           f'<div class="tabs">{tabs}</div>'
                           + (f'<div class="card">{chart}</div>' if chart else "")
                           + f'<div class="card">{self._position_table(rows, names, sid)}</div>')

    async def leaders(self, request: web.Request) -> web.Response:
        sid = self._sid(request)
        stat = request.query.get("stat", "fantasy")
        if stat != "fantasy" and stat not in STATS:
            stat = "fantasy"
        per_game = request.query.get("per_game") == "1"
        opts = [("fantasy", "Fantasy points")] + [(k, lbl) for k, (lbl, _) in STATS.items()]
        select = "".join(f'<option value="{k}"{" selected" if k == stat else ""}>{_e(lbl)}</option>'
                         for k, lbl in opts)
        keep = f'<input type="hidden" name="season" value="{sid}">' if sid else ""
        form = (f'<form class="inline" action="/leaders">{keep}<select name="stat">{select}</select>'
                f'<label><input type="checkbox" name="per_game" value="1"{" checked" if per_game else ""}> '
                f'per game</label><button>Show</button></form>')
        names = Names(self.db)
        rows = analytics.leaders(self.db, stat, 25, per_game, season=sid)
        if stat in MAX_STATS and not per_game:
            rows = self._max_leaders(stat, sid)
        body = "".join(f'<tr><td class="n">{i + 1}</td><td>{self._player_link(names, n, sid=sid)}</td>'
                       f'<td class="n"><b>{_num(v)}</b></td><td class="n">{g}</td></tr>'
                       for i, (n, v, g) in enumerate(rows))
        name = "Fantasy points" if stat == "fantasy" else label(stat)
        chart = charts.bars([(self._player_link(names, n, sid=sid), v, f"{names(n)}: {_num(v)} ({g} games)")
                             for n, v, g in rows[:15]], f"{name}{' per game' if per_game else ''} (top 15)")
        table = (f'<div class="card scroll"><table><tr><th class="n">#</th><th>Player</th>'
                 f'<th class="n">{_e(name)}{" / game" if per_game else ""}</th><th class="n">GP</th></tr>'
                 f"{body}</table></div>" if rows else "<p class='muted'>Nobody has recorded that stat yet.</p>")
        return _page(f"{name} leaders", f"<h1>{_e(name)} leaders</h1>{self._picker(request, sid)}{form}"
                                        + (f'<div class="card">{chart}</div>' if chart else "") + table)

    def _max_leaders(self, stat: str, sid: int | None = None) -> list[tuple[str, float, int]]:
        """'Longest ...' stats: best single play, not the sum across games."""
        agg: dict[str, list] = {}
        for ln in self.db.all_player_lines(season=sid):
            e = agg.setdefault(ln["username_key"], [ln["username"], 0.0, 0])
            e[1] = max(e[1], ln["stats"].get(stat) or 0)
            e[2] += 1
        rows = [tuple(r) for r in agg.values() if r[1]]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:25]

    async def seasons(self, request: web.Request) -> web.Response:
        rows = list(reversed(self.db.seasons()))
        if not rows:
            return _page("Seasons", "<h1>Seasons</h1><p class='muted'>No seasons yet. The next game played "
                                    "starts Season 1.</p>")
        _, limit, _ = self.db.season_progress(rows[0]["id"])
        body = []
        for srow in rows:
            status = ("Voting open" if srow["ended_at"] and not srow["vote_results"] and srow["vote_ends_at"]
                      else "Finished" if srow["ended_at"] else "In progress")
            body.append(f'<tr><td><a href="/seasons/{srow["id"]}">{_e(srow["name"])}</a></td><td>{status}</td>'
                        f'<td class="small">{self._progress(srow["id"])}</td>'
                        f'<td class="small muted">{_time(srow["started_at"])}</td>'
                        f'<td class="small"><a href="/games?season={srow["id"]}">games</a> · '
                        f'<a href="/fantasy?season={srow["id"]}">stats</a></td></tr>')
        need = season_min_players()
        return _page("Seasons", "<h1>Seasons</h1><p class='muted'>"
                                + (f"A season is {limit} games. " if limit else "")
                                + (f"Only games with {need} or more players count toward a season; smaller games "
                                   f"are still saved and count in the all-time stats. " if need else "")
                                + "When a season ends, the stat awards are handed out and the players vote for the "
                                  "rest.</p><div class='card scroll'><table><tr><th>Season</th><th>Status</th>"
                                  "<th>Progress</th><th>Started</th><th></th></tr>"
                                + "".join(body) + "</table></div>")

    async def season_page(self, request: web.Request) -> web.Response:
        try:
            srow = self.db.season(int(request.match_info["id"]))
        except ValueError:
            srow = None
        if not srow:
            raise web.HTTPNotFound(text="No such season")
        names = Names(self.db)
        sid = srow["id"]
        final = bool(srow["ended_at"] and srow["mvps"])
        race = json.loads(srow["mvps"]) if final else analytics.season_mvps(self.db, sid)

        def award_card(title: str, icon: str, rows: list[dict], value, unit: str, empty: str) -> str:
            if not rows:
                return (f'<div class="card"><h2 style="margin-top:0">{icon} {_e(title)}</h2>'
                        f'<p class="muted">{empty}</p></div>')
            lines_ = "".join(f'<div class="award"><span>{["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}."} '
                             f'{self._player_link(names, r["username"], sid=sid)}</span>'
                             f'<b>{value(r)}{unit}</b></div>' for i, r in enumerate(rows[:3]))
            return f'<div class="card"><h2 style="margin-top:0">{icon} {_e(title)}</h2>{lines_}</div>'

        stat_cards = "".join(award_card(t, icon, race["roles"].get(code) or [], lambda r: _num(r["points"]), " pts",
                                        "No eligible players yet")
                             for code, (t, icon) in STAT_AWARDS.items())
        status = f"Ended {_time(srow['ended_at'])}" if srow["ended_at"] else "In progress"
        if srow["vote_results"]:
            res = json.loads(srow["vote_results"])
            vote_html = (f"<h2>🗳️ Voted awards</h2><p class='muted'>{res['voters']} players voted. Votes are "
                         f"weighted by the share of the season's games each voter played.</p><div class='grid'>"
                         + "".join(award_card(t, icon, res["roles"].get(code) or [], lambda r: _num(r["weight"]),
                                              "", "No votes") for code, (t, icon) in vote_awards().items())
                         + "</div>")
        elif srow["ended_at"] and srow["vote_ends_at"]:
            vote_html = (f'<div class="card">🗳️ <b>Voting is open</b> until {_time(srow["vote_ends_at"])} for: '
                         + ", ".join(_e(t) for t, _ in vote_awards().values())
                         + '. <a href="/vote">Vote now →</a> Results show here when it closes.</div>')
        else:
            vote_html = ("<div class='card muted'>🗳️ When the season ends, players vote for "
                         + ", ".join(_e(t) for t, _ in vote_awards().values()) + ".</div>")
        sub = (f"<div class='card'><b>{status}</b> · {self._progress(sid)}<span class='small muted'>Stat awards go "
               f"to the most fantasy points earned in that role (needs {race['min_games']}+ games in the role). "
               f"<a href='/fantasy?season={sid}'>Season stats →</a></span></div>")
        games = self._games(sid)
        log = (f"<h2>Season game log <a class='small' href='/games?season={sid}'>{len(games)} games →</a></h2>"
               + self._games_table(games, names)
               + f"<p class='small muted'>Only games with {season_min_players()}+ players count toward a season.</p>")
        body = (f"<h1>{_e(srow['name'])}</h1>{sub}<h2>{'🏆 Stat awards' if final else '📊 Stat awards race'}</h2>"
                f"<div class='grid'>{stat_cards}</div>{vote_html}{log}")
        return _page(srow["name"], body)

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
