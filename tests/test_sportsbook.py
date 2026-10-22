import asyncio
import json
import random
import re

from aiohttp.test_utils import TestClient, TestServer

from nflu.db import Database
from nflu.parser import ParsedGame, ParsedPlayer
from nflu.sportsbook import (START_BALANCE, Book, BookError, Models, decimal_odds, default_line, p_over, price,
                             to_american)
from nflu.webhook import WebhookServer, ingest
from tests.test_bot import SAMPLE, SECRET

PASSWORD = "hunter2-admin"


def _game(i: int, rng: random.Random, home="Ravens", away="Bills", score=(28, 21)) -> ParsedGame:
    def player(name, qb):
        st = {"snaps": 130}
        if qb:
            pos = {"QB": {"pass_yards": rng.randint(150, 350), "pass_tds": rng.randint(0, 4),
                          "interceptions": rng.randint(0, 2), "pass_completions": rng.randint(10, 25), **st}}
        else:
            pos = {"WR": {"receptions": rng.randint(1, 9), "rec_yards": rng.randint(10, 150),
                          "rec_tds": rng.randint(0, 2), **st},
                   "DB": {"tackles": rng.randint(0, 6), "def_ints": rng.randint(0, 1), **st}}
        flat: dict[str, float] = {}
        for g in pos.values():
            for k, v in g.items():
                if k != "snaps":
                    flat[k] = flat.get(k, 0) + v
        return ParsedPlayer(username=name, stats=flat, pos_stats=pos, extra={"Snaps": 130})
    return ParsedGame(external_id=f"g{i}", home_team=home, away_team=away, home_score=score[0],
                      away_score=score[1], played_at=f"2026-10-{i + 1:02d}T19:00:00+00:00",
                      players=[player("QBOne", True), player("WrOne", False), player("QBTwo", True),
                               player("WrTwo", False)])


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "b.db")
    rng = random.Random(4)
    for i in range(6):
        db.save_game(_game(i, rng, score=(rng.randint(7, 35), rng.randint(7, 35))))
    return db


def test_odds_math():
    assert to_american(0.5 * 1.045) == -110
    assert to_american(0.25) == 300 and to_american(0.75) == -300
    assert decimal_odds(-110) == 1 + 100 / 110 and decimal_odds(150) == 2.5
    o1, o2 = price([0.5, 0.5])
    assert o1 == o2 == -110
    pois = {"mean": 0.4, "sd": 0.6, "dist": "poisson"}
    assert default_line(pois) == 0.5 and 0.3 < p_over(pois, 0.5) < 0.35
    norm = {"mean": 240.3, "sd": 50, "dist": "normal"}
    assert default_line(norm) == 240.5 and abs(p_over(norm, 240.5) - 0.5) < 0.01


def test_models_and_settlement(tmp_path):
    db = _db(tmp_path)
    models = Models(db)
    py = models.stat("qbone", "pass_yards")
    assert py["dist"] == "normal" and 150 < py["mean"] < 350 and py["games"] == 6
    assert models.stat("qbone", "interceptions")["dist"] == "poisson"
    assert models.stat("wrone", "pass_yards")["mean"] == 0  # never threw: no invented yards

    book = Book(db)
    mid = book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    assert book.matchup(mid)["title"] == "Team qbone vs Team qbtwo"
    kinds = [m["kind"] for m in book.markets(mid)]
    assert kinds[:3] == ["moneyline", "spread", "total"] and kinds.count("prop") >= 8
    bettor, _ = book.create_bettor("Sam")
    ml = book.markets(mid)[0]
    prop = next(m for m in book.markets(mid) if m["stat"] == "pass_yards" and m["player_key"] == "qbone")
    over = next(s for s in prop["selections"] if s["side"] == "over")
    book.place_bet(bettor, ml["selections"][0]["id"], 1000)
    book.place_bet(bettor, over["id"], 500)
    assert book.bettor(bettor)["balance"] == START_BALANCE - 1500

    # a later game with these players settles everything; team 1 (QBOne's) played as the Bills (listed
    # second) and won 30-10, which the bot works out from the TDs; QBOne throws 999
    g = _game(10, random.Random(1), home="Ravens", away="Bills", score=(10, 30))
    for p in g.players:
        for k in ("pass_tds", "rec_tds", "rush_tds", "def_tds"):
            p.stats.pop(k, None)
    g.players[0].stats.update(pass_tds=4)  # QBOne: 24 points of team 1's 30
    g.players[2].stats.update(pass_tds=1)  # QBTwo: 6 of team 2's 10
    g.players[0].stats["pass_yards"] = g.players[0].pos_stats["QB"]["pass_yards"] = 999
    gid, _ = db.save_game(g)
    assert book.on_game_saved(gid) == [mid]
    assert book.matchup(mid)["status"] == "settled"
    assert (book.matchup(mid)["team1"], book.matchup(mid)["team2"]) == ("Bills", "Ravens")
    assert book.market(ml["id"])["selections"][0]["label"] == "Bills (Team qbone)"
    win = round(1000 * decimal_odds(ml["selections"][0]["odds"]), 2) + round(500 * decimal_odds(over["odds"]), 2)
    assert book.bettor(bettor)["balance"] == round(START_BALANCE - 1500 + win, 2)
    assert book.market(prop["id"])["result"].startswith("999 Passing Yards")

    # undo and settle by hand the other way
    book.unsettle_market(ml["id"])
    book.settle_market(ml["id"], ml["selections"][1]["id"])
    assert book.bettor(bettor)["balance"] == round(START_BALANCE - 1500 + round(500 * decimal_odds(over["odds"]), 2), 2)


def test_web_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    async def run():
        db = _db(tmp_path)
        srv = WebhookServer(db, SECRET)
        async with TestClient(TestServer(srv.app)) as c:
            # new visitor is asked for a name
            html = await (await c.get("/book")).text()
            assert "Pick a password" in html and "$10,000.00" in html
            assert "Sportsbook" in await (await c.get("/")).text()
            assert "at least 4" in await (await c.post("/book/join", data={"name": "Ann", "password": "x"})).text()
            r = await c.post("/book/join", data={"name": "<i>Ann</i>", "password": "pass1", "next": "/book"})
            html = await r.text()
            assert r.status == 200 and "&lt;i&gt;Ann&lt;/i&gt;" in html and "Welcome!" in html
            assert (await c.post("/book/join", data={"name": "Ann2"})).status == 200  # already joined: no-op
            assert len(Book(db).leaderboard()) == 1

            # admin: wrong password, then right
            assert "Wrong username" in await (await c.post("/admin/login", data={
                "username": "admin", "password": "nope"})).text()
            assert (await c.get("/admin/matchups/1")).url.path == "/admin"
            r = await c.post("/admin/login", data={"username": "admin", "password": PASSWORD})
            html = await r.text()
            assert "New matchup" in html
            csrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
            assert (await c.post("/admin/matchups", data={"csrf": "bad", "team1": "A", "team2": "B"})).status == 403
            r = await c.post("/admin/matchups", data={"csrf": csrf,
                                                     "p:qbone": "1", "p:wrone": "1", "p:qbtwo": "2",
                                                     "extra2": "Newbie"})
            html = await r.text()
            assert r.url.path == "/admin/matchups/1" and "Matchup created" in html and "newbie" in html.lower()
            mu = Book(db).matchup(1)
            assert mu["players2"] == ["qbtwo", "newbie"]
            prop = next(m for m in Book(db).markets(1) if m["stat"] == "interceptions")
            r = await c.post(f"/admin/markets/{prop['id']}", data={"csrf": csrf, "action": "save", "line": "1.5",
                             **{f"odds_{s['id']}": "+120" for s in prop["selections"]}})
            assert "saved" in await r.text()
            assert [s["label"] for s in Book(db).market(prop["id"])["selections"]] == ["Over 1.5", "Under 1.5"]
            r = await c.post("/admin/matchups/1", data={"csrf": csrf, "action": "custom", "title": "Pick six?",
                                                       "options": "Yes | +400\nNo | -600"})
            assert "Custom bet added" in await r.text()
            r = await c.post("/admin/matchups/1", data={"csrf": csrf, "action": "prop", "player": "wrone",
                                                       "stat": "yac", "line": ""})
            assert "Yards After Catch" in await r.text()

            # bettor places a bet
            html = await (await c.get("/book/matchups/1")).text()
            sel = int(re.search(r'href="/book/bet/(\d+)"', html).group(1))
            html = await (await c.get(f"/book/bet/{sel}")).text()
            bcsrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
            odds = re.search(r'name="odds" value="(-?\d+)"', html).group(1)
            r = await c.post(f"/book/bet/{sel}", data={"csrf": bcsrf, "odds": odds, "stake": "20000"})
            assert "You only have" in await r.text()
            r = await c.post(f"/book/bet/{sel}", data={"csrf": bcsrf, "odds": odds, "stake": "250"})
            assert "Bet placed" in await r.text()
            assert "$9,750.00" in await (await c.get("/book/me")).text()
            assert "Ann" in await (await c.get("/book/leaderboard")).text()

            # the account comes back from its code in a fresh browser (e.g. after the tunnel link changed)
            code = Book(db).leaderboard()[0]["code"]
            async with TestClient(TestServer(srv.app)) as c2:
                assert "No account has that code" in await (await c2.post("/book/code", data={"code": "zzzz"})).text()
                r = await c2.post("/book/code", data={"code": code.lower().replace("-", " "), "next": "/book/me"})
                assert "Welcome back" in (html := await r.text()) and "$9,750.00" in html
            # name + password works on another device; log out works
            async with TestClient(TestServer(srv.app)) as c3:
                r = await c3.post("/book/login", data={"name": "<i>ann</i>", "password": "nope"})
                assert "Wrong name or password" in await r.text()
                r = await c3.post("/book/login", data={"name": "<i>ann</i>", "password": "pass1", "next": "/book/me"})
                html = await r.text()
                assert "Welcome back" in html and "$9,750.00" in html and "Change password" in html
                lcsrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
                r = await c3.post("/book/password", data={"csrf": lcsrf, "password": "pass2"})
                assert "Password saved" in await r.text()
                r = await c3.post("/book/logout", data={"csrf": lcsrf})
                assert "Logged out" in (html := await r.text()) and "Log in" in html
                assert Book(db).login("<i>Ann</i>", "pass2")[0] and not Book(db).login("<i>Ann</i>", "pass1")[0]
                # an old account with no password is claimed by logging in, keeping its money and bets
                old_id, _ = Book(db).create_bettor("Old Timer")
                Book(db).adjust_balance(old_id, 123)
                r = await c3.post("/book/login", data={"name": "old timer", "password": "mine", "next": "/book/me"})
                assert "its password now" in (html := await r.text()) and "$10,123.00" in html
                assert Book(db).login("Old Timer", "mine") == (Book(db).bettor(old_id), False)
                assert Book(db).login("Old Timer", "other")[0] is None

            # the game arrives by webhook and settles the matchup
            p = json.loads(SAMPLE.read_text())
            p["game_id"] = "new-one"
            p["players"] = [{"username": u, "stats": {"passing": {"yards": 200}}} for u in ("QBOne", "WrOne", "QBTwo")]
            r = await c.post(f"/webhook/{SECRET}", data=json.dumps(p))
            assert (await r.json())["status"] == "parsed"
            assert Book(db).matchup(1)["game_id"] is not None
            html = await (await c.get("/admin/matchups/1")).text()
            assert "Pick six?" in html and "Undo settlement" in html
            for path in ["/book", "/book/me", "/book/matchups/1", "/admin", "/admin/bettors"]:
                assert SECRET not in await (await c.get(path)).text(), path
            assert (await c.get("/book/matchups/99")).status == 404
    asyncio.run(run())


def test_parlays(tmp_path):
    db = _db(tmp_path)
    book = Book(db)
    mid = book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    bettor, _ = book.create_bettor("Pat")
    mk = {(m["player_key"], m["stat"]): m for m in book.markets(mid)}
    py = mk[("qbone", "pass_yards")]
    rec = mk[("wrone", "receptions")]
    tk = mk[("wrtwo", "tackles")]
    over = lambda m: next(s for s in m["selections"] if s["side"] == "over")  # noqa: E731
    under = lambda m: next(s for s in m["selections"] if s["side"] == "under")  # noqa: E731

    book.slip_add(bettor, over(py)["id"])
    book.slip_add(bettor, under(py)["id"])  # same market: swaps the pick
    assert [s["id"] for s in book.slip(bettor)] == [under(py)["id"]]
    try:
        book.place_parlay(bettor, 100)
        raise AssertionError("one pick isn't a parlay")
    except BookError:
        pass
    book.slip_add(bettor, over(py)["id"])
    book.slip_add(bettor, over(rec)["id"])
    book.slip_add(bettor, over(tk)["id"])
    seen = [(s["id"], s["odds"]) for s in book.slip(bettor)]
    try:
        book.place_parlay(bettor, 100, seen[:2])
        raise AssertionError("stale slip accepted")
    except BookError:
        pass
    pid = book.place_parlay(bettor, 100, seen)
    assert book.slip(bettor) == [] and book.bettor(bettor)["balance"] == START_BALANCE - 100
    assert book.leaderboard()[0]["at_risk"] == 100

    # leg 1 wins, leg 2 pushes (drops out), leg 3 wins -> paid at legs 1 x 3
    book.settle_market(py["id"], over(py)["id"])
    assert book.parlays_for(bettor)[0]["status"] == "pending"
    book.settle_market(rec["id"], None)
    book.settle_market(tk["id"], over(tk)["id"])
    p = book.parlays_for(bettor)[0]
    expect = round(100 * decimal_odds(over(py)["odds"]) * decimal_odds(over(tk)["odds"]), 2)
    assert p["id"] == pid and p["status"] == "won" and p["payout"] == expect
    assert book.bettor(bettor)["balance"] == round(START_BALANCE - 100 + expect, 2)

    # regrading a leg as a loss takes the winnings back
    book.unsettle_market(tk["id"])
    assert book.parlays_for(bettor)[0]["status"] == "pending"
    assert book.bettor(bettor)["balance"] == START_BALANCE - 100
    book.settle_market(tk["id"], under(tk)["id"])
    assert book.parlays_for(bettor)[0]["status"] == "lost"
    assert book.bettor(bettor)["balance"] == START_BALANCE - 100


def test_live_edit(tmp_path):
    db = _db(tmp_path)
    book = Book(db)
    mid = book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    v0 = book.version()
    bettor, _ = book.create_bettor("Lee")
    prop = next(m for m in book.markets(mid) if m["player_key"] == "wrtwo")
    book.place_bet(bettor, prop["selections"][0]["id"], 100)
    ml = book.markets(mid)[0]
    msg = book.edit_matchup(mid, "Big game", "Hawks", "Owls", ["qbone", "wrone", "newguy"], ["qbtwo"])
    assert "1 player added" in msg and "1 taken out" in msg
    mu = book.matchup(mid)
    assert (mu["title"], mu["team1"], mu["team2"]) == ("Big game", "Hawks", "Owls")
    assert [s["label"] for s in book.market(ml["id"])["selections"]] == ["Hawks", "Owls"]
    assert book.market(prop["id"])["status"] == "void" and book.bettor(bettor)["balance"] == START_BALANCE
    assert any(m["player_key"] == "newguy" for m in book.markets(mid))
    assert book.version() != v0


def test_admin_move_player(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    async def run():
        db = _db(tmp_path)
        Book(db).create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
        async with TestClient(TestServer(WebhookServer(db, SECRET).app)) as c:
            html = await (await c.post("/admin/login", data={"username": "admin", "password": PASSWORD})).text()
            csrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
            assert "Move to Team qbtwo" in await (await c.get("/admin/matchups/1")).text()
            r = await c.post("/admin/matchups/1", data={"csrf": csrf, "action": "move", "player": "wrone"})
            assert "moved wrone to team qbtwo" in (await r.text()).lower()
            mu = Book(db).matchup(1)
            assert mu["players1"] == ["qbone"] and mu["players2"] == ["qbtwo", "wrtwo", "wrone"]
            r = await c.post("/admin/matchups/1", data={"csrf": csrf, "action": "remove", "player": "qbone"})
            assert "Each team needs at least one player" in await r.text()
            r = await c.post("/admin/matchups/1", data={"csrf": csrf, "action": "remove", "player": "wrtwo"})
            assert "took wrtwo out" in (await r.text()).lower() and "wrtwo" not in Book(db).matchup(1)["players2"]
    asyncio.run(run())


def test_settles_when_stats_arrive_later(tmp_path):
    """A game first saved without its stats (score only) settles its matchup when it's saved again with them."""
    db = _db(tmp_path)
    book = Book(db)
    mid = book.create_matchup("", ["qbking_123", "speedywr"], ["billsqb", "truckstick"])
    sam, _ = book.create_bettor("Sam")
    ml = book.markets(mid)[0]
    book.place_bet(sam, ml["selections"][0]["id"], 1000)
    full = json.loads(SAMPLE.read_text())
    score_only = {**full, "players": []}
    assert ingest(db, json.dumps(score_only), "test")["status"] == "parsed"
    assert book.matchup(mid)["status"] == "open"  # no players yet: nothing to settle on
    assert ingest(db, json.dumps(full), "test")["status"] == "replaced"
    assert book.matchup(mid)["status"] == "settled"
    assert book.bettor(sam)["balance"] == round(START_BALANCE - 1000 + 1000 * decimal_odds(ml["selections"][0]["odds"]), 2)


def test_tie_and_leftovers(tmp_path):
    db = _db(tmp_path)
    book = Book(db)
    mid = book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    sam, _ = book.create_bettor("Sam")
    ml = book.markets(mid)[0]
    book.place_bet(sam, ml["selections"][0]["id"], 1000)
    custom = book.add_custom(mid, "Pick-six?", [("Yes", 400), ("No", -600)])
    prop = next(m for m in book.markets(mid) if m["kind"] == "prop")
    db.x("UPDATE book_selections SET side = 'broken' WHERE market_id = ? AND side = 'over'", (prop["id"],))
    gid, _ = db.save_game(_game(20, random.Random(2), score=(14, 14)))
    res = book.settle_from_game(mid, gid)
    # a tie pushes the winner bet even though the sides can't be told apart; a broken market doesn't stop the rest
    assert book.market(ml["id"])["status"] == "settled"
    assert book.bettor(sam)["balance"] == START_BALANCE
    assert sorted(res["left"]) == sorted([book.market(custom)["title"], prop["title"]])
    assert res["settled"] == len(book.markets(mid)) - 2
    assert book.matchup(mid)["status"] == "locked"


def test_alt_lines_round_robin_tail_and_house(tmp_path):
    db = _db(tmp_path)
    book = Book(db)
    mid = book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    ann, _ = book.create_bettor("Ann")
    bob, _ = book.create_bettor("Bob")
    mk = {(m["player_key"], m["stat"]): m for m in book.markets(mid)}
    py = mk[("qbone", "pass_yards")]

    # the slider: lines both sides of the main one, overs pay more as the line goes up
    lad = book.ladder(py)
    assert len(lad) >= 5 and any(r["line"] == py["line"] and r["over_id"] for r in lad)
    assert all(a["line"] < b["line"] and a["over"] <= b["over"] for a, b in zip(lad, lad[1:]))
    high = lad[-1]["line"]
    alt = book.alt_market(py["id"], high)
    assert book.alt_market(py["id"], high) == alt and book.alt_market(py["id"], py["line"]) == py["id"]
    assert book.market(alt)["parent_id"] == py["id"] and next(r for r in book.ladder(py) if r["line"] == high)["over_id"]
    try:
        book.alt_market(py["id"], py["line"] + 0.25)
        raise AssertionError("a line that isn't offered")
    except BookError:
        pass
    alt_over = next(s for s in book.market(alt)["selections"] if s["side"] == "over")
    book.place_bet(ann, alt_over["id"], 100)
    # another line of the same bet swaps out of the slip like any pick from that market
    book.slip_add(ann, alt_over["id"])
    book.slip_add(ann, py["selections"][1]["id"])
    assert [s["id"] for s in book.slip(ann)] == [py["selections"][1]["id"]]

    # round robin: 3 picks -> the 3-pick parlay, three 2-pick parlays and three singles, $10 each
    for key in (("wrone", "receptions"), ("wrtwo", "tackles")):
        book.slip_add(ann, mk[key]["selections"][0]["id"])
    seen = [(s["id"], s["odds"]) for s in book.slip(ann)]
    try:
        book.place_slip(ann, 10_000, seen, [1, 2, 3])
        raise AssertionError("more than the balance")
    except BookError as e:
        assert "7 bets" in str(e)
    res = book.place_slip(ann, 10, seen, [3, 2, 1], keep=True)
    assert len(res["parlays"]) == 4 and len(res["singles"]) == 3 and res["cost"] == 70
    assert len(book.slip(ann)) == 3  # kept
    assert book.bettor(ann)["balance"] == START_BALANCE - 100 - 70

    # everyone's bets, and tailing a parlay into another bettor's slip
    feed = book.feed()
    assert {d["kind"] for d in feed} == {"bet", "parlay"} and all(d["name"] == "Ann" for d in feed)
    three = next(d for d in feed if d["kind"] == "parlay" and len(d["legs"]) == 3)
    assert [s["id"] for s in book.tail(bob, "parlay", three["id"])] == [leg["selection_id"] for leg in three["legs"]]
    assert len(book.slip(bob)) == 3

    # the house: nothing settled yet, then the alt-line over loses when settled at a number
    h = book.house()
    assert h["total"]["profit"] == 0 and h["total"]["open"] == 170 and h["total"]["exposure"] > 170
    assert book.settle_value(py["id"], high - 1) == 2  # the main line and its alt line
    assert book.market(alt)["status"] == "settled" and book.house()["singles"]["profit"] >= 100
    assert book.matchup_house()[mid] == book.house()["singles"]["profit"]


def test_pick_order_and_new_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    async def run():
        db = _db(tmp_path)
        async with TestClient(TestServer(WebhookServer(db, SECRET).app)) as c:
            html = await (await c.post("/admin/login", data={"username": "admin", "password": PASSWORD})).text()
            assert "house profit/loss" in html and 'class="seg"' in html
            csrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
            # team lists keep the order players were picked in, not the table's
            await c.post("/admin/matchups", data={"csrf": csrf, "p:qbone": "1", "p:wrone": "1", "p:qbtwo": "2",
                                                  "p:wrtwo": "2", "order": "wrtwo,wrone,qbtwo,qbone"})
            mu = Book(db).matchup(1)
            assert mu["players1"] == ["wrone", "qbone"] and mu["players2"] == ["wrtwo", "qbtwo"]
            assert mu["team1"] == "Team WrOne"

            await c.post("/book/join", data={"name": "Cy", "password": "pass1"})
            html = await (await c.get("/book/matchups/1")).text()
            assert 'type="range"' in html and "data-l=" in html
            bcsrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
            py = next(m for m in Book(db).markets(1) if m["stat"] == "pass_yards" and m["player_key"] == "qbone")
            high = Book(db).ladder(py)[-1]["line"]
            r = await c.post(f"/book/alt/{py['id']}", data={"csrf": bcsrf, "line": f"{high:g}", "pick": "over:parlay",
                                                            "next": "/book/matchups/1"})
            assert f"Added Over {high:g}" in await r.text()
            r = await c.post(f"/book/alt/{py['id']}", data={"csrf": bcsrf, "line": f"{high:g}", "pick": "under:bet",
                                                            "next": "/book/matchups/1"})
            assert r.url.path.startswith("/book/bet/") and f"Under {high:g}" in await r.text()
            tk = next(m for m in Book(db).markets(1) if m["stat"] == "tackles")
            await c.post(f"/book/parlay/add/{tk['selections'][0]['id']}", data={"csrf": bcsrf, "next": "/book"})
            html = await (await c.get("/book/parlay")).text()
            assert "Each pick on its own" in html
            seen = re.search(r'name="seen" value="([^"]+)"', html).group(1)
            r = await c.post("/book/parlay", data=[("csrf", bcsrf), ("seen", seen), ("stake", "50"),
                                                   ("size", "2"), ("size", "1")])
            assert "Placed 1 parlay and 2 straight bets" in await r.text()
            html = await (await c.get("/book/feed")).text()
            assert "Cy" in html and "2-leg parlay" in html
            assert "Single bets" in await (await c.get("/book/feed?show=singles")).text()
            v = (await (await c.get("/book/version")).json())["f"]
            bk = Book(db)
            bk.place_bet(bk.create_bettor("Dee")[0], bk.markets(1)[0]["selections"][0]["id"], 5)
            assert (await (await c.get("/book/version")).json())["f"] != v
            part = await (await c.get("/book/feed", headers={"X-Requested-With": "fetch"})).text()
            assert part.startswith('<div class="card scroll">') and "Dee" in part and "<h1>" not in part
            assert "Dee" not in await (await c.get("/book/feed?view=history")).text()
            assert "Alt line" in await (await c.get("/admin/matchups/1")).text()
    asyncio.run(run())


def test_league_player_logins(tmp_path, monkeypatch):
    import config
    from nflu import players
    monkeypatch.setattr(config, "PLAYER_NAMES", {
        "Jacob": {"roblox_username": "QBOne", "password": "759822057"},
        "Nate": {"roblox_username": "WrOne", "password": ""},   # empty: uses the linked Discord ID
        "Old": "QBTwo"})                                          # older format still works
    db = _db(tmp_path)
    db.set_name("WrOne", "Nate", 4242)
    assert [p.name for p in players.players()] == ["Jacob", "Nate", "Old"]
    assert players.book_password(db, players.find("jacob")) == "759822057"
    assert players.book_password(db, players.find("Nate")) == "4242"
    assert players.book_password(db, players.find("Old")) is None
    assert players.for_discord(db, 759822057).name == "Jacob" and players.for_discord(db, 4242).name == "Nate"
    assert players.for_discord(db, 1) is None

    async def run():
        async with TestClient(TestServer(WebhookServer(db, SECRET).app)) as c:
            html = await (await c.get("/book")).text()
            assert '<option value="Jacob">' in html and '<option value="Nate">' in html and "Old" not in html
            r = await c.post("/book/join", data={"name": "jacob", "password": "mine"})
            assert "is a league player" in await r.text()
            r = await c.post("/book/login", data={"name": "nate", "password": "nope"})
            assert "Wrong name or password" in await r.text()
            r = await c.post("/book/login", data={"name": "nate", "password": "4242", "next": "/book/me"})
            html = await r.text()
            assert "Welcome back, Nate" in html and "$10,000.00" in html  # account made on first login
            r = await c.post("/book/login", data={"name": "Jacob", "password": "759822057"})
            assert "Welcome back, Jacob" in await r.text()
            assert {b["name"] for b in Book(db).leaderboard()} == {"Nate", "Jacob"}
    asyncio.run(run())


def test_delete_renumbers_and_bet_card_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    db = _db(tmp_path)
    book = Book(db)
    for _ in range(3):
        book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    zed, _ = book.create_bettor("Zed")
    third = book.markets(3)[0]
    book.place_bet(zed, book.markets(2)[0]["selections"][0]["id"], 100)
    book.place_bet(zed, third["selections"][0]["id"], 50)

    async def run():
        async with TestClient(TestServer(WebhookServer(db, SECRET).app)) as c:
            await c.post("/book/login", data={"name": "zed", "password": "pw12"})  # claims the account
            html = await (await c.get("/book/matchups/3")).text()
            assert 'id="betcard"' in html and 'class="fq"' in html and "Player props" in html
            bcsrf = re.search(r'data-csrf="(\w+)"', html).group(1)
            sid = third["selections"][1]["id"]
            d = await (await c.get(f"/book/api/sel/{sid}")).json()
            assert d["ok"] and d["open"] and d["balance"] == START_BALANCE - 150
            ajax = {"X-Requested-With": "fetch"}
            d = await (await c.post(f"/book/bet/{sid}", data={"csrf": bcsrf, "odds": "1", "stake": "10"},
                                    headers=ajax)).json()
            assert not d["ok"] and "odds changed" in d["msg"] and d["odds"] == third["selections"][1]["odds"]
            d = await (await c.post(f"/book/bet/{sid}", data={"csrf": bcsrf, "odds": str(d["odds"]), "stake": "10"},
                                    headers=ajax)).json()
            assert d["ok"] and d["balance"] == START_BALANCE - 160
            d = await (await c.post(f"/book/parlay/add/{sid}", data={"csrf": bcsrf, "next": "/book"},
                                    headers=ajax)).json()
            assert d["ok"] and d["count"] == 1

            html = await (await c.post("/admin/login", data={"username": "admin", "password": PASSWORD})).text()
            csrf = re.search(r'name="csrf" value="(\w+)"', html).group(1)
            assert "Delete matchup" in await (await c.get("/admin/matchups/2")).text()
            r = await c.post("/admin/matchups/2", data={"csrf": csrf, "action": "delete"})
            assert "Deleted matchup #2" in await r.text()
    asyncio.run(run())
    # #2's bet was refunded and removed; #3 is now #2 with its markets and bets
    assert [m["id"] for m in book.matchups()] == [2, 1]
    assert book.markets(2)[0]["id"] == third["id"]
    assert book.bettor(zed)["balance"] == START_BALANCE - 60
    assert {b["matchup_id"] for b in book.bets_for(zed)} == {2}
    assert book.create_matchup("", ["qbone"], ["qbtwo"]) == 3


def test_parlay_cart(tmp_path):
    db = _db(tmp_path)
    book = Book(db)
    for _ in range(2):
        book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    book.create_bettor("Zed")
    a, b = book.markets(1)[0]["selections"][0], book.markets(2)[0]["selections"][0]

    async def run():
        async with TestClient(TestServer(WebhookServer(db, SECRET).app)) as c:
            await c.post("/book/login", data={"name": "zed", "password": "pw12"})
            html = await (await c.get("/book")).text()
            assert 'id="cartbtn"' in html and 'id="cart"' in html
            assert 'id="cartbtn"' not in await (await c.get("/book/parlay")).text()
            csrf = re.search(r'data-csrf="(\w+)"', html).group(1)
            ajax = {"X-Requested-With": "fetch"}
            assert (await (await c.get("/book/api/slip")).json())["legs"] == []
            for s in (a, b):
                await c.post(f"/book/parlay/add/{s['id']}", data={"csrf": csrf, "next": "/book"}, headers=ajax)
            d = await (await c.get("/book/api/slip")).json()
            assert [g["id"] for g in d["legs"]] == [a["id"], b["id"]] and all(g["open"] for g in d["legs"])
            assert d["decimal"] == book.parlay_decimal([a["odds"], b["odds"]])
            r = await (await c.post("/book/parlay", data={"csrf": csrf, "stake": "", "seen": d["seen"], "size": "2"},
                                    headers=ajax)).json()
            assert not r["ok"] and "how much" in r["msg"]
            r = await (await c.post("/book/parlay", data={"csrf": csrf, "stake": "25", "seen": d["seen"], "size": "2"},
                                    headers=ajax)).json()
            assert r["ok"] and r["balance"] == START_BALANCE - 25
            assert (await (await c.get("/book/api/slip")).json())["legs"] == []
    asyncio.run(run())


def test_spread_and_game_props(tmp_path):
    from nflu.sportsbook import game_stat_value
    line = lambda **st: {"stats": st}
    # 3 TDs + a field goal = 21; 25 points means 4 after touchdowns, more than one a TD: a 2-pt try made
    sheet = [line(pass_tds=2, fg_made=1), line(rec_tds=2), line(rush_tds=1)]
    assert game_stat_value(sheet, "g:tds") == 3 and game_stat_value(sheet, "g:pat", 25) == 4
    assert game_stat_value(sheet, "g:two_pt", 25) == 1 and game_stat_value(sheet, "g:two_pt", 21) == 0
    assert game_stat_value(sheet, "g:two_pt", 23) is None and game_stat_value(sheet, "g:pat", 40) is None

    db = _db(tmp_path)
    book = Book(db)
    mid = book.create_matchup("", ["qbone", "wrone"], ["qbtwo", "wrtwo"])
    ms = book.markets(mid)
    spread = next(m for m in ms if m["kind"] == "spread")
    assert spread["line"] % 1 == 0.5 and spread["selections"][0]["label"].startswith("Team qbone ")
    tds = next(m for m in ms if m["stat"] == "g:tds")
    two = next(m for m in ms if m["kind"] == "yesno")
    assert tds["kind"] == "total" and book.ladder(tds)
    book.update_market(spread["id"], -6.5, {}, reprice=True)
    assert book.market(spread["id"])["selections"][1]["label"] == "Team qbtwo +6.5"

    g = _game(10, random.Random(1), home="Ravens", away="Bills", score=(7, 32))
    for p in g.players:
        for k in ("pass_tds", "rec_tds", "rush_tds", "def_tds"):
            p.stats.pop(k, None)
    g.players[0].stats.update(pass_tds=4)  # team 1: 24 + 8 after TDs = 32, so a 2-pt try was made
    g.players[2].stats.update(pass_tds=1)
    gid, _ = db.save_game(g)
    book.settle_from_game(mid, gid)
    sp = book.market(spread["id"])
    assert [s["outcome"] for s in sp["selections"]] == ["win", "lose"]  # won by 25, more than 6.5
    assert book.market(tds["id"])["result"].startswith("5 touchdowns")
    assert [s["outcome"] for s in book.market(two["id"])["selections"]] == ["win", "lose"]
