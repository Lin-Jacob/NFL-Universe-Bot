"""
Sportsbook pages on the stats website (see sportsbook.py for markets, odds and settling).

Bettors (no login: a cookie remembers them; a new visitor picks a name and gets
sportsbook.START_BALANCE in fake money):
    GET  /book                     open matchups
    GET  /book/matchups/<id>       every bet on one matchup
    GET  /book/bet/<selection>     bet slip;  POST places the bet
    GET  /book/me                  my balance and bets
    GET  /book/leaderboard         everyone's money
    GET  /book/join;  POST /book/join
    POST /book/code                get your account back with its code (new link or device)

Admin (user "admin", password ADMIN_PASSWORD in .env; off when it isn't set):
    GET  /admin                    login, matchups, create a matchup
    GET  /admin/matchups/<id>      edit lines and odds, lock, add props/custom bets, settle
    GET  /admin/bettors            adjust balances
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

from aiohttp import web

from .db import Database
from .embeds import Names
from .site import _e, _page, _time
from .sportsbook import (START_BALANCE, Book, BookError, bettable_stats, decimal_odds, fmt_odds,
                         stat_label)

BETTOR_COOKIE = "nflu_bettor"
ADMIN_COOKIE = "nflu_admin"
ADMIN_USER = "admin"
ADMIN_SESSION_SECONDS = 7 * 24 * 3600
# Wrong passwords allowed per address before logins from it are refused for a while
LOGIN_TRIES, LOGIN_WINDOW = 5, 600

CSS = """<style>
.bar{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;margin-bottom:14px}
.bar .money{font-size:20px;font-weight:700}
.mk{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.mk:last-child{border-bottom:none}.mk .t{flex:1 1 220px}
a.sel{display:inline-flex;gap:8px;align-items:baseline;border:1px solid var(--line);border-radius:8px;
padding:5px 10px;background:var(--chip);color:var(--text)}a.sel:hover{border-color:var(--accent);text-decoration:none}
a.sel b,.odds{color:var(--accent);font-variant-numeric:tabular-nums}
span.sel{display:inline-flex;gap:8px;border:1px dashed var(--line);border-radius:8px;padding:5px 10px;color:var(--muted)}
.msg{background:var(--chip);border-left:4px solid var(--accent);padding:8px 12px;border-radius:6px;margin-bottom:14px}
.err{border-left-color:#c0392b}
.won{color:var(--win);font-weight:700}.lost{color:#c0392b}
form.stack{display:grid;gap:10px;max-width:420px}
input.n{width:90px}textarea{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);width:100%}
button.sec{background:var(--chip);color:var(--text);border:1px solid var(--line)}
button.bad{background:#c0392b}
.pick td{padding:4px 10px}.pick label{margin-right:10px;white-space:nowrap}
</style>"""

_STATUS = {"open": "Open", "locked": "Betting closed", "settled": "Settled", "void": "Void"}


def _money(v: float) -> str:
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


def _int(v, default=None):
    try:
        return int(str(v).strip().lstrip("+"))
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(str(v).strip().replace(",", "").lstrip("$"))
    except (TypeError, ValueError):
        return default


def _redirect(path: str, msg: str | None = None, err: bool = False) -> web.HTTPSeeOther:
    if msg:
        path += ("&" if "?" in path else "?") + urlencode({"err" if err else "msg": msg})
    return web.HTTPSeeOther(path)


def _flash(request: web.Request) -> str:
    if m := request.query.get("msg"):
        return f'<div class="msg">{_e(m)}</div>'
    if m := request.query.get("err"):
        return f'<div class="msg err">{_e(m)}</div>'
    return ""


def _secure(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


class BookSite:
    def __init__(self, db: Database, secret: str, admin_password: str | None = None):
        self.db = db
        self.book = Book(db)
        self.admin_password = admin_password if admin_password is not None else os.getenv("ADMIN_PASSWORD", "")
        self.key = hashlib.sha256(f"nflu-book:{secret}".encode()).digest()
        # Changing ADMIN_PASSWORD logs every admin session out
        self.admin_key = hashlib.sha256(f"nflu-admin:{secret}:{self.admin_password}".encode()).digest()
        self.failures: dict[str, list[float]] = {}

    def routes(self) -> list[web.RouteDef]:
        return [
            web.get("/book", self.index),
            web.get("/book/join", self.join_form),
            web.post("/book/join", self.join),
            web.post("/book/code", self.use_code),
            web.get("/book/matchups/{id}", self.matchup_page),
            web.get("/book/bet/{id}", self.slip),
            web.post("/book/bet/{id}", self.bet),
            web.get("/book/me", self.me),
            web.get("/book/leaderboard", self.leaderboard),
            web.get("/admin", self.admin),
            web.post("/admin/login", self.admin_login),
            web.post("/admin/logout", self.admin_logout),
            web.post("/admin/matchups", self.admin_create),
            web.get("/admin/matchups/{id}", self.admin_matchup),
            web.post("/admin/matchups/{id}", self.admin_matchup_action),
            web.post("/admin/markets/{id}", self.admin_market_action),
            web.get("/admin/bettors", self.admin_bettors),
            web.post("/admin/bettors/{id}", self.admin_bettor_action),
        ]

    # ------------------------------------------------------------- security
    def _sign(self, key: bytes, value: str) -> str:
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:32]

    def _csrf(self, request: web.Request, cookie: str) -> str:
        return self._sign(self.key, "csrf:" + (request.cookies.get(cookie) or ""))

    def _check_csrf(self, request: web.Request, form, cookie: str) -> None:
        if not hmac.compare_digest(str(form.get("csrf", "")), self._csrf(request, cookie)):
            raise web.HTTPForbidden(text="This form expired. Go back, reload the page and try again.")

    def _hidden(self, request: web.Request, cookie: str) -> str:
        return f'<input type="hidden" name="csrf" value="{self._csrf(request, cookie)}">'

    def _bettor(self, request: web.Request):
        return self.book.bettor_by_token(request.cookies.get(BETTOR_COOKIE))

    def _is_admin(self, request: web.Request) -> bool:
        if not self.admin_password:
            return False
        exp, _, sig = (request.cookies.get(ADMIN_COOKIE) or "").partition(".")
        return (exp.isdigit() and int(exp) > time.time()
                and hmac.compare_digest(sig, self._sign(self.admin_key, exp)))

    def _need_admin(self, request: web.Request) -> None:
        if not self._is_admin(request):
            raise web.HTTPSeeOther("/admin")

    # --------------------------------------------------------------- shared
    def _names(self):
        """username_key -> 'Real Name' (or their Roblox username), for keys with or without games."""
        names = Names(self.db)
        known = {u.lower(): u for u in self.db.known_usernames()}
        return lambda key: names(known.get(key, key))

    def _title(self, m: dict, name=None) -> str:
        """A market's title; props use the player's current name, so names set later show up."""
        if m.get("kind") == "prop" and m.get("player_key"):
            return f"{(name or self._names())(m['player_key'])}: {stat_label(m['stat'])}"
        return m.get("market_title") or m["title"]

    def _bar(self, request: web.Request, bettor) -> str:
        if not bettor:
            return ""
        return (f'<div class="card bar"><span>👋 {_e(bettor["name"])}</span>'
                f'<span class="money">{_money(bettor["balance"])}</span>'
                f'<a href="/book">Bets</a><a href="/book/me">My bets</a>'
                f'<a href="/book/leaderboard">Leaderboard</a></div>')

    @staticmethod
    def _sel_button(s: dict, open_: bool) -> str:
        inner = f'<span>{_e(s["label"])}</span><b>{fmt_odds(s["odds"])}</b>'
        if open_:
            return f'<a class="sel" href="/book/bet/{s["id"]}">{inner}</a>'
        mark = {"win": " ✅", "lose": "", "push": " ↩", "void": " ↩"}.get(s.get("outcome") or "", "")
        return f'<span class="sel">{inner}{mark}</span>'

    def _market_row(self, m: dict, mu: dict, name) -> str:
        open_ = m["status"] == "open" and mu["status"] == "open"
        note = (f'<div class="small muted">{_e(m["result"])}</div>' if m["result"]
                else "" if open_ else f'<div class="small muted">{_STATUS[m["status"]]}</div>')
        sels = " ".join(self._sel_button(s, open_) for s in m["selections"])
        return f'<div class="mk"><div class="t">{_e(self._title(m, name))}{note}</div><div>{sels}</div></div>'

    def _page(self, title: str, body: str) -> web.Response:
        return _page(title, CSS + body)

    def _join_html(self, nxt: str = "/book", error: str = "", name: str = "") -> str:
        err = f'<div class="msg err">{_e(error)}</div>' if error else ""
        return (f"<h1>Sportsbook</h1>{err}<div class='card'><p>Bet fake money on league games. "
                f"New here? Pick a name and you start with <b>{_money(START_BALANCE)}</b>.</p>"
                f'<form class="inline" method="post" action="/book/join">'
                f'<input type="hidden" name="next" value="{_e(nxt)}">'
                f'<input name="name" maxlength="24" required placeholder="Your name" value="{_e(name)}">'
                f"<button>Start betting</button></form></div>"
                f"<div class='card'><p>Already have an account? Enter its code (on your My bets page) to get it "
                f"back, e.g. when the website's link changed or you're on another device.</p>"
                f'<form class="inline" method="post" action="/book/code">'
                f'<input type="hidden" name="next" value="{_e(nxt)}">'
                f'<input name="code" maxlength="12" required placeholder="ABCD-2345" autocomplete="off">'
                f"<button class='sec'>Use my account</button></form></div>")

    # -------------------------------------------------------------- bettors
    async def index(self, request: web.Request) -> web.Response:
        bettor = self._bettor(request)
        if not bettor:
            return self._page("Sportsbook", self._join_html())
        cards, name = [], self._names()
        for mu in self.book.matchups(("open", "locked")):
            markets = self.book.markets(mu["id"])
            main = [m for m in markets if m["kind"] in ("moneyline", "total")]
            n_props = len(markets) - len(main)
            cards.append(
                f'<div class="card"><h2 style="margin-top:0"><a href="/book/matchups/{mu["id"]}">{_e(mu["title"])}</a> '
                f'<span class="chip">{_STATUS[mu["status"]]}</span></h2>'
                + "".join(self._market_row(m, mu, name) for m in main)
                + f'<p class="small"><a href="/book/matchups/{mu["id"]}">All {len(markets)} bets'
                  f'{f" ({n_props} player props and more)" if n_props else ""} →</a></p></div>')
        recent = self.book.matchups(("settled",))[:5]
        past = "".join(f'<li><a href="/book/matchups/{mu["id"]}">{_e(mu["title"])}</a></li>' for mu in recent)
        body = (self._bar(request, bettor) + _flash(request) + "<h1>Sportsbook</h1>"
                + ("".join(cards) or "<p class='muted'>No games are open for betting right now. Check back soon.</p>")
                + (f"<h2>Recently settled</h2><ul>{past}</ul>" if past else ""))
        return self._page("Sportsbook", body)

    async def join_form(self, request: web.Request) -> web.Response:
        if self._bettor(request):
            raise web.HTTPSeeOther("/book")
        return self._page("Join", self._join_html(self._safe_next(request.query.get("next"))))

    @staticmethod
    def _safe_next(nxt: str | None) -> str:
        return nxt if nxt and nxt.startswith("/book") and "//" not in nxt else "/book"

    async def join(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(form.get("next"))
        if self._bettor(request):
            raise web.HTTPSeeOther(nxt)
        name = str(form.get("name", ""))
        try:
            bid, token = self.book.create_bettor(name)
        except BookError as e:
            return self._page("Join", self._join_html(nxt, str(e), name))
        code = self.book.bettor(bid)["code"]
        raise self._login_bettor(request, token, _redirect(
            nxt, f"Welcome! Your account code is {code}. Save it: it gets your account back if the "
                 f"website's link changes or you switch devices (it's also on My bets)."))

    @staticmethod
    def _login_bettor(request: web.Request, token: str, resp: web.HTTPException) -> web.HTTPException:
        resp.set_cookie(BETTOR_COOKIE, token, max_age=10 * 365 * 24 * 3600, httponly=True, samesite="Lax",
                        secure=_secure(request), path="/")
        return resp

    def _too_many(self, key: str, failed: bool = False) -> bool:
        """Per-address limit on wrong passwords / account codes (LOGIN_TRIES per LOGIN_WINDOW)."""
        now = time.time()
        recent = [t for t in self.failures.get(key, []) if now - t < LOGIN_WINDOW]
        if failed:
            recent.append(now)
        self.failures[key] = recent
        return len(recent) >= LOGIN_TRIES

    async def use_code(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(form.get("next"))
        ip = "code:" + (request.headers.get("CF-Connecting-IP") or request.remote or "?")
        if self._too_many(ip):
            return self._page("Join", self._join_html(nxt, "Too many wrong codes. Wait 10 minutes."))
        b = self.book.bettor_by_code(str(form.get("code", "")))
        if not b:
            self._too_many(ip, failed=True)
            await asyncio.sleep(1)
            return self._page("Join", self._join_html(nxt, "No account has that code."))
        raise self._login_bettor(request, b["token"], _redirect(nxt, f"Welcome back, {b['name']}!"))

    def _require_bettor(self, request: web.Request):
        bettor = self._bettor(request)
        if not bettor:
            raise web.HTTPSeeOther("/book/join?" + urlencode({"next": request.path_qs}))
        return bettor

    async def matchup_page(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        mu = self.book.matchup(_int(request.match_info["id"], 0))
        if not mu:
            raise web.HTTPNotFound(text="No such matchup")
        name = self._names()
        markets = self.book.markets(mu["id"])
        groups: dict[str, list[dict]] = {}
        for m in markets:
            head = ("Game" if m["kind"] in ("moneyline", "total") else
                    "Specials" if m["kind"] == "custom" else name(m["player_key"]))
            groups.setdefault(head, []).append(m)
        cards = "".join(f'<div class="card"><h2 style="margin-top:0">{_e(head)}</h2>'
                        + "".join(self._market_row(m, mu, name) for m in ms) + "</div>" for head, ms in groups.items())
        lineups = " · ".join(f'<b>{_e(mu[t])}</b>: ' + ", ".join(_e(name(k)) for k in mu[p])
                             for t, p in (("team1", "players1"), ("team2", "players2")))
        body = (self._bar(request, bettor) + _flash(request)
                + f'<p class="small"><a href="/book">← all games</a></p><h1>{_e(mu["title"])} '
                  f'<span class="chip">{_STATUS[mu["status"]]}</span></h1>'
                  f'<p class="muted small">{lineups}</p>'
                  f'<p class="small muted">Lines and odds come from each player\'s past games. Props settle on the '
                  f'stat in the game; a player who doesn\'t play has their props voided (stakes returned).</p>'
                + cards)
        return self._page(mu["title"], body)

    async def slip(self, request: web.Request, error: str = "", stake: str = "") -> web.Response:
        bettor = self._require_bettor(request)
        s = self.book.selection(_int(request.match_info["id"], 0))
        if not s:
            raise web.HTTPNotFound(text="No such bet")
        dec = decimal_odds(s["odds"])
        open_ = s["matchup_status"] == "open" and s["market_status"] == "open"
        err = f'<div class="msg err">{_e(error)}</div>' if error else ""
        form = (f'<form class="stack" method="post" action="/book/bet/{s["id"]}">{self._hidden(request, BETTOR_COOKIE)}'
                f'<input type="hidden" name="odds" value="{s["odds"]}">'
                f'<label>Stake ($)<br><input id="stake" name="stake" inputmode="decimal" required '
                f'value="{_e(stake)}" placeholder="100"></label>'
                f'<div>To win <b id="win">$0.00</b>, paid back <b id="ret">$0.00</b></div>'
                f"<button>Place bet</button></form>"
                f"<script>(function(){{var i=document.getElementById('stake'),d={dec};"
                f"function f(v){{return '$'+v.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}}"
                f"function u(){{var s=parseFloat((i.value||'').replace(/[$,]/g,''))||0;"
                f"document.getElementById('win').textContent=f(s*(d-1));document.getElementById('ret').textContent=f(s*d);}}"
                f"i.addEventListener('input',u);u();}})();</script>"
                if open_ else "<p class='muted'>Betting on this is closed.</p>")
        body = (self._bar(request, bettor) + err
                + f'<p class="small"><a href="/book/matchups/{s["matchup_id"]}">← {_e(s["matchup_title"])}</a></p>'
                  f'<h1>Bet slip</h1><div class="card"><div class="muted small">{_e(self._title(s))}</div>'
                  f'<div class="score">{_e(s["label"])} <span class="odds">{fmt_odds(s["odds"])}</span></div>'
                  f'<p class="small muted">Decimal {dec:.2f}: a $100 bet pays back {_money(100 * dec)}. '
                  f'You have {_money(bettor["balance"])}.</p>{form}</div>')
        return self._page("Bet slip", body)

    async def bet(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        sel_id = _int(request.match_info["id"], 0)
        stake = _float(form.get("stake"))
        try:
            if stake is None:
                raise BookError("Enter how much to bet.")
            self.book.place_bet(bettor["id"], sel_id, stake, _int(form.get("odds")))
        except BookError as e:
            return await self.slip(request, str(e), str(form.get("stake", "")))
        s = self.book.selection(sel_id)
        raise _redirect(f"/book/matchups/{s['matchup_id']}",
                        f"Bet placed: {_money(stake)} on {s['label']} ({fmt_odds(s['odds'])}), "
                        f"{self._title(s)}.")

    def _bets_table(self, bets: list[dict]) -> str:
        if not bets:
            return "<p class='muted'>No bets yet.</p>"
        rows, name = [], self._names()
        for b in bets:
            if b["status"] == "pending":
                res = f'<span class="muted">Pending · to win {_money(b["stake"] * (decimal_odds(b["odds"]) - 1))}</span>'
            elif b["status"] == "won":
                res = f'<span class="won">Won {_money(b["payout"] - b["stake"])}</span>'
            elif b["status"] == "lost":
                res = f'<span class="lost">Lost {_money(b["stake"])}</span>'
            else:
                res = f'<span class="muted">{"Push" if b["status"] == "push" else "Void"}, stake returned</span>'
            result = f'<br><span class="small muted">{_e(b["result"])}</span>' if b["result"] else ""
            rows.append(f'<tr><td><a href="/book/matchups/{b["matchup_id"]}">{_e(b["matchup_title"])}</a><br>'
                        f'<span class="small">{_e(self._title(b, name))}</span></td>'
                        f'<td>{_e(b["label"])} <span class="odds">{fmt_odds(b["odds"])}</span></td>'
                        f'<td class="n">{_money(b["stake"])}</td><td>{res}{result}</td>'
                        f'<td class="small muted">{_time(b["placed_at"])}</td></tr>')
        return ('<div class="card scroll"><table><tr><th>Game</th><th>Pick</th><th class="n">Stake</th>'
                f'<th>Result</th><th>Placed</th></tr>{"".join(rows)}</table></div>')

    async def me(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        bets = self.book.bets_for(bettor["id"])
        at_risk = sum(b["stake"] for b in bets if b["status"] == "pending")
        worth = bettor["balance"] + at_risk
        body = (self._bar(request, bettor) + _flash(request) + f"<h1>My bets</h1><div class='card'>"
                f'<span class="stat"><b>{_money(bettor["balance"])}</b><span class="muted">to bet</span></span>'
                f'<span class="stat"><b>{_money(at_risk)}</b><span class="muted">in open bets</span></span>'
                f'<span class="stat"><b class="{"won" if worth >= START_BALANCE else "lost"}">'
                f'{_money(worth - START_BALANCE)}</b><span class="muted">profit</span></span>'
                f'<div class="small">Account code <b>{_e(bettor["code"])}</b>: enter it on the join page to get '
                f"this account back if the website's link changes or you use another device. Keep it to "
                f"yourself.</div></div>"
                + self._bets_table(bets))
        return self._page("My bets", body)

    def _leader_table(self, rows: list[dict]) -> str:
        if not rows:
            return "<p class='muted'>Nobody has joined yet.</p>"
        body = "".join(
            f'<tr><td class="n">{i + 1}</td><td>{_e(r["name"])}</td><td class="n"><b>{_money(r["worth"])}</b></td>'
            f'<td class="n {"won" if r["worth"] >= START_BALANCE else "lost"}">{_money(r["worth"] - START_BALANCE)}</td>'
            f'<td class="n">{_money(r["at_risk"])}</td><td class="n">{r["won"]}-{r["lost"]}</td>'
            f'<td class="n">{r["bets"]}</td></tr>' for i, r in enumerate(rows))
        return ('<div class="card scroll"><table><tr><th class="n">#</th><th>Bettor</th><th class="n">Total</th>'
                '<th class="n">Profit</th><th class="n">In open bets</th><th class="n">W-L</th>'
                f'<th class="n">Bets</th></tr>{body}</table></div>')

    async def leaderboard(self, request: web.Request) -> web.Response:
        body = (self._bar(request, self._bettor(request)) + "<h1>Sportsbook leaderboard</h1>"
                "<p class='muted'>Total = money left to bet plus money in open bets.</p>"
                + self._leader_table(self.book.leaderboard()))
        return self._page("Leaderboard", body)

    # ---------------------------------------------------------------- admin
    def _admin_bar(self, request: web.Request) -> str:
        return (f'<div class="card bar"><b>Sportsbook admin</b><a href="/admin">Matchups</a>'
                f'<a href="/admin/bettors">Bettors</a><a href="/book">Bettor view</a>'
                f'<form method="post" action="/admin/logout" style="margin-left:auto">'
                f'{self._hidden(request, ADMIN_COOKIE)}<button class="sec">Log out</button></form></div>')

    def _login_html(self, error: str = "") -> str:
        if not self.admin_password:
            return ("<h1>Sportsbook admin</h1><div class='card'>Admin is off: set <code>ADMIN_PASSWORD</code> in "
                    "<code>.env</code> and restart the bot.</div>")
        err = f'<div class="msg err">{_e(error)}</div>' if error else ""
        return (f"<h1>Sportsbook admin</h1>{err}<div class='card'><form class='stack' method='post' "
                f"action='/admin/login'><input name='username' placeholder='Username' autocomplete='username' "
                f"required><input name='password' type='password' placeholder='Password' "
                f"autocomplete='current-password' required><button>Log in</button></form></div>")

    async def admin_login(self, request: web.Request) -> web.Response:
        ip = request.headers.get("CF-Connecting-IP") or request.remote or "?"
        now = time.time()
        form = await request.post()
        if self._too_many(ip):
            return self._page("Admin", self._login_html("Too many wrong passwords. Wait 10 minutes."))
        ok = (self.admin_password
              and hmac.compare_digest(str(form.get("username", "")).strip().lower(), ADMIN_USER)
              and hmac.compare_digest(str(form.get("password", "")).encode(), self.admin_password.encode()))
        if not ok:
            self._too_many(ip, failed=True)
            await asyncio.sleep(1)
            return self._page("Admin", self._login_html("Wrong username or password."))
        exp = str(int(now) + ADMIN_SESSION_SECONDS)
        resp = web.HTTPSeeOther("/admin")
        resp.set_cookie(ADMIN_COOKIE, f"{exp}.{self._sign(self.admin_key, exp)}", max_age=ADMIN_SESSION_SECONDS,
                        httponly=True, samesite="Strict", secure=_secure(request), path="/")
        raise resp

    async def admin_logout(self, request: web.Request) -> web.Response:
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        resp = web.HTTPSeeOther("/admin")
        resp.del_cookie(ADMIN_COOKIE, path="/")
        raise resp

    async def admin(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return self._page("Admin", self._login_html())
        name = self._names()
        rows = "".join(
            f'<tr><td><a href="/admin/matchups/{mu["id"]}">#{mu["id"]} {_e(mu["title"])}</a><br>'
            f'<span class="small muted">{_e(mu["team1"])}: {_e(", ".join(name(k) for k in mu["players1"]))}<br>'
            f'{_e(mu["team2"])}: {_e(", ".join(name(k) for k in mu["players2"]))}</span></td>'
            f'<td>{_STATUS[mu["status"]]}{" · game #" + str(mu["game_id"]) if mu["game_id"] else ""}</td>'
            f'<td class="small muted">{_time(mu["created_at"])}</td></tr>'
            for mu in self.book.matchups())
        table = (f'<div class="card scroll"><table><tr><th>Matchup</th><th>Status</th><th>Created</th></tr>'
                 f"{rows}</table></div>" if rows else "<p class='muted'>No matchups yet.</p>")
        pick = "".join(
            f'<tr><td>{_e(Names(self.db).dashed(u))}</td><td>'
            + "".join(f'<label><input type="radio" name="p:{_e(u.lower())}" value="{v}"{" checked" if not v else ""}>'
                      f" {lbl}</label>" for v, lbl in (("", "Out"), ("1", "Team 1"), ("2", "Team 2")))
            + "</td></tr>" for u in sorted(self.db.known_usernames(), key=lambda u: name(u.lower()).lower()))
        create = (f'<div class="card"><h2 style="margin-top:0">New matchup</h2>'
                  f'<form method="post" action="/admin/matchups">{self._hidden(request, ADMIN_COOKIE)}'
                  f'<div class="inline" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">'
                  f'<input name="title" placeholder="Title (optional)"></div>'
                  f'<p class="small muted">Put each player on Team 1 or Team 2. When the game comes in, the bot '
                  f"works out which side each team played from its players and settles the winner.</p>"
                  f'<div class="scroll"><table class="pick">{pick}</table></div>'
                  f'<p class="small muted">Players with no games yet (comma-separated names):</p>'
                  f'<div style="display:flex;gap:8px;flex-wrap:wrap"><input name="extra1" placeholder="Team 1 extras">'
                  f'<input name="extra2" placeholder="Team 2 extras"></div>'
                  f'<p><button>Create with auto-generated odds</button></p></form></div>')
        body = self._admin_bar(request) + _flash(request) + "<h1>Matchups</h1>" + table + create
        return self._page("Admin", body)

    async def admin_create(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        sides: dict[str, list[str]] = {"1": [], "2": []}
        for k, v in form.items():
            if k.startswith("p:") and v in sides:
                sides[v].append(k[2:])
        for team in ("1", "2"):
            for raw in str(form.get(f"extra{team}", "")).split(","):
                if raw.strip():
                    key = self.db.resolve_player(raw) or raw.strip().lower()
                    if key not in sides[team]:
                        sides[team].append(key)
        try:
            mid = self.book.create_matchup(str(form.get("title", "")), sides["1"], sides["2"], self._names())
        except BookError as e:
            raise _redirect("/admin", str(e), err=True)
        raise _redirect(f"/admin/matchups/{mid}", "Matchup created and open for betting. Check the lines below.")

    def _admin_market(self, request: web.Request, m: dict, name) -> str:
        done = m["status"] in ("settled", "void")
        dis = " disabled" if done else ""
        line = (f'<label>Line <input class="n" name="line" value="{m["line"]:g}"{dis}></label>'
                if m["kind"] in ("total", "prop") else "")
        odds = " ".join(
            f'<label>{_e(s["label"])} <input class="n" name="odds_{s["id"]}" value="{fmt_odds(s["odds"])}"{dis}>'
            f'</label><span class="small muted">{s["bets"]} bets · {_money(s["handle"])}'
            + (f' · model {s["fair_p"] * 100:.0f}%' if s["fair_p"] is not None else "")
            + (f' · <b>{_e(s["outcome"])}</b>' if s["outcome"] else "") + "</span>"
            for s in m["selections"])
        if done:
            buttons = '<button class="sec" name="action" value="unsettle">Undo settlement</button>'
        else:
            winner = "".join(f'<option value="{s["id"]}">{_e(s["label"])} wins</option>' for s in m["selections"])
            auto = ('<button class="sec" name="action" value="reprice">Auto-price</button>'
                    if m["kind"] != "custom" else "")
            toggle = ('<button class="sec" name="action" value="lock">Close betting</button>' if m["status"] == "open"
                      else '<button class="sec" name="action" value="open">Reopen betting</button>')
            buttons = (f'<button name="action" value="save">Save</button>{auto}{toggle}'
                       f'<select name="winner">{winner}<option value="push">Push (refund all)</option></select>'
                       f'<button class="sec" name="action" value="settle">Settle</button>'
                       f'<button class="sec" name="action" value="void">Void</button>'
                       f'<button class="bad" name="action" value="delete">Delete</button>')
        result = f'<div class="small muted">{_e(m["result"])}</div>' if m["result"] else ""
        return (f'<form class="mk" method="post" action="/admin/markets/{m["id"]}">{self._hidden(request, ADMIN_COOKIE)}'
                f'<div class="t"><b>{_e(self._title(m, name))}</b> <span class="chip">{_STATUS[m["status"]]}</span>{result}</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center">{line}{odds}</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:6px">{buttons}</div></form>')

    async def admin_matchup(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        mu = self.book.matchup(_int(request.match_info["id"], 0))
        if not mu:
            raise web.HTTPNotFound(text="No such matchup")
        name = self._names()
        csrf = self._hidden(request, ADMIN_COOKIE)
        act = f'/admin/matchups/{mu["id"]}'
        markets = self.book.markets(mu["id"])
        live = mu["status"] in ("open", "locked")
        controls = []
        if live:
            controls.append(
                f'<form class="inline" method="post" action="{act}">{csrf}'
                + (f'<button name="action" value="lock">Close all betting</button>' if mu["status"] == "open"
                   else f'<button name="action" value="open">Reopen betting</button>')
                + f'<button class="sec" name="action" value="auto">'
                  f'{"Turn off" if mu["auto_settle"] else "Turn on"} auto-settle</button>'
                  f'<button class="sec" name="action" value="void">Void everything (refund)</button></form>')
            games = "".join(f'<option value="{g["id"]}">#{g["id"]} {_e(g["home_team"])} {g["home_score"]} – '
                            f'{g["away_score"]} {_e(g["away_team"])}</option>' for g in self.db.recent_games(25))
            controls.append(
                f'<form class="inline" method="post" action="{act}">{csrf}<select name="game">{games}</select>'
                f'<button name="action" value="settle_game">Settle from this game</button></form>'
                if games else "")
            players = "".join(f'<option value="{_e(k)}">{_e(name(k))}</option>'
                              for k in mu["players1"] + mu["players2"])
            stats = "".join(f'<option value="{k}">{_e(lbl)}</option>' for k, lbl in bettable_stats())
            controls.append(
                f'<div class="card"><h2 style="margin-top:0">Add a player prop</h2>'
                f'<form class="inline" method="post" action="{act}">{csrf}<select name="player">{players}</select>'
                f'<select name="stat">{stats}</select><input class="n" name="line" placeholder="Line (auto)">'
                f'<button name="action" value="prop">Add over/under</button></form>'
                f'<h2>Add a custom bet</h2><form class="stack" style="max-width:none" method="post" action="{act}">'
                f'{csrf}<input name="title" placeholder="Question, e.g. First score is a pick-six" required>'
                f'<textarea name="options" rows="3" required placeholder="One option per line: label | odds&#10;'
                f'Yes | +400&#10;No | -600"></textarea><button name="action" value="custom">Add custom bet'
                f"</button></form></div>")
        auto = ("Auto-settle is <b>on</b>: the next game with at least half of these players settles it."
                if mu["auto_settle"] else "Auto-settle is <b>off</b>.")
        lineups = "<br>".join(f'<b>{_e(mu[t])}</b>: ' + ", ".join(_e(name(k)) for k in mu[p])
                              for t, p in (("team1", "players1"), ("team2", "players2")))
        body = (self._admin_bar(request) + _flash(request)
                + f'<h1>#{mu["id"]} {_e(mu["title"])} <span class="chip">{_STATUS[mu["status"]]}</span></h1>'
                  f'<div class="card small">{lineups}<br>{auto if live else ""}'
                  f'{" Settled from game #" + str(mu["game_id"]) + "." if mu["game_id"] else ""} '
                  f'<a href="/book/matchups/{mu["id"]}">Bettor view →</a></div>'
                + "".join(controls)
                + '<div class="card"><h2 style="margin-top:0">Markets</h2>'
                  '<p class="small muted">Edit a line and press Auto-price to re-price it from the stats, or type '
                  'your own odds and Save. Changes apply to new bets only.</p>'
                + "".join(self._admin_market(request, m, name) for m in markets) + "</div>")
        return self._page(f'Admin: {mu["title"]}', body)

    async def admin_matchup_action(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        mid = _int(request.match_info["id"], 0)
        mu = self.book.matchup(mid)
        if not mu:
            raise web.HTTPNotFound(text="No such matchup")
        back = f"/admin/matchups/{mid}"
        action = form.get("action")
        try:
            if action in ("lock", "open"):
                self.book.set_matchup_status(mid, "locked" if action == "lock" else "open")
                msg = "Betting closed." if action == "lock" else "Betting reopened."
            elif action == "auto":
                self.book.set_auto_settle(mid, not mu["auto_settle"])
                msg = "Auto-settle turned " + ("off." if mu["auto_settle"] else "on.")
            elif action == "void":
                self.book.void_matchup(mid)
                msg = "Matchup voided; every stake was returned."
            elif action == "settle_game":
                res = self.book.settle_from_game(mid, _int(form.get("game"), 0))
                msg = f"Settled {res['settled']} markets."
                if res["left"]:
                    msg += " Settle these by hand: " + ", ".join(res["left"])
            elif action == "prop":
                line = _float(form.get("line")) if str(form.get("line", "")).strip() else None
                key = str(form.get("player", ""))
                if key not in mu["players1"] + mu["players2"]:
                    raise BookError("Pick a player from this matchup.")
                self.book.add_prop(mid, key, str(form.get("stat", "")), line, names=self._names())
                msg = f"Added {self._names()(key)}: {stat_label(str(form.get('stat', '')))}."
            elif action == "custom":
                options = []
                for row in str(form.get("options", "")).splitlines():
                    if not row.strip():
                        continue
                    lbl, _, odds = row.rpartition("|")
                    o = _int(odds)
                    if not lbl.strip() or o is None or -100 < o < 100:
                        raise BookError(f"Couldn't read “{row.strip()}”. Write each option as: label | odds")
                    options.append((lbl.strip(), o))
                self.book.add_custom(mid, str(form.get("title", "")), options)
                msg = "Custom bet added."
            else:
                raise BookError("Unknown action.")
        except BookError as e:
            raise _redirect(back, str(e), err=True)
        raise _redirect(back, msg)

    async def admin_market_action(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        m = self.book.market(_int(request.match_info["id"], 0))
        if not m:
            raise web.HTTPNotFound(text="No such market")
        back = f"/admin/matchups/{m['matchup_id']}"
        m["title"] = self._title(m)
        action = form.get("action")
        try:
            if action in ("save", "reprice"):
                line = _float(form.get("line")) if "line" in form else None
                if "line" in form and line is None:
                    raise BookError("The line must be a number.")
                odds = {s["id"]: _int(form.get(f"odds_{s['id']}")) for s in m["selections"]}
                self.book.update_market(m["id"], line, odds, reprice=action == "reprice")
                msg = f"{m['title']}: saved."
            elif action in ("lock", "open"):
                self.book.set_market_status(m["id"], "locked" if action == "lock" else "open")
                msg = f"{m['title']}: betting {'closed' if action == 'lock' else 'reopened'}."
            elif action == "settle":
                w = form.get("winner")
                self.book.settle_market(m["id"], None if w == "push" else _int(w, -1))
                msg = f"{m['title']}: settled and paid out."
            elif action == "void":
                self.book.void_market(m["id"])
                msg = f"{m['title']}: voided, stakes returned."
            elif action == "unsettle":
                self.book.unsettle_market(m["id"])
                msg = f"{m['title']}: settlement undone (payouts taken back, bets pending again)."
            elif action == "delete":
                self.book.delete_market(m["id"])
                msg = f"{m['title']}: deleted, stakes returned."
            else:
                raise BookError("Unknown action.")
        except BookError as e:
            raise _redirect(back, str(e), err=True)
        raise _redirect(back, msg)

    async def admin_bettors(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        csrf = self._hidden(request, ADMIN_COOKIE)
        rows = "".join(
            f'<tr><td>{_e(r["name"])}</td><td class="n">{_money(r["balance"])}</td>'
            f'<td class="n">{_money(r["at_risk"])}</td><td class="n">{r["bets"]}</td>'
            f'<td><form class="inline" style="margin:0" method="post" action="/admin/bettors/{r["id"]}">{csrf}'
            f'<input class="n" name="amount" placeholder="+500"><button name="action" value="adjust">Add</button>'
            f'<button class="bad" name="action" value="delete">Delete</button></form></td></tr>'
            for r in self.book.leaderboard())
        body = (self._admin_bar(request) + _flash(request) + "<h1>Bettors</h1>"
                "<p class='muted small'>Add money with a positive amount, take it away with a negative one. "
                "Deleting a bettor removes their bets too.</p>"
                + (f'<div class="card scroll"><table><tr><th>Name</th><th class="n">Balance</th>'
                   f'<th class="n">In open bets</th><th class="n">Bets</th><th></th></tr>{rows}</table></div>'
                   if rows else "<p class='muted'>Nobody has joined yet.</p>"))
        return self._page("Bettors", body)

    async def admin_bettor_action(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        b = self.book.bettor(_int(request.match_info["id"], 0))
        if not b:
            raise web.HTTPNotFound(text="No such bettor")
        if form.get("action") == "delete":
            self.book.delete_bettor(b["id"])
            raise _redirect("/admin/bettors", f"Deleted {b['name']}.")
        amount = _float(form.get("amount"))
        if not amount:
            raise _redirect("/admin/bettors", "Enter an amount, like 500 or -500.", err=True)
        self.book.adjust_balance(b["id"], amount)
        raise _redirect("/admin/bettors", f"{b['name']}: {'+' if amount > 0 else ''}{_money(amount)}.")
