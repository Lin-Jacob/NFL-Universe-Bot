import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nflu import analytics
from nflu.db import Database
from nflu.fantasy import fantasy_points
from nflu.parser import ParseError, parse_payload
from nflu.webhook import WebhookServer, ingest

SAMPLE = Path(__file__).parent.parent / "samples" / "sample_game.json"
SECRET = "test-secret-123456"


def test_native_sample():
    g = parse_payload(json.loads(SAMPLE.read_text()))
    assert (g.home_team, g.home_score, g.away_team, g.away_score) == ("Ravens", 28, "Bills", 21)
    assert g.external_id == "a1b2c3-server-job-id" and g.week == 3
    qb = next(p for p in g.players if p.username == "QBKing_123")
    assert qb.stats == {"pass_completions": 18, "pass_attempts": 25, "pass_yards": 312, "pass_tds": 3,
                        "interceptions": 1, "rush_attempts": 4, "rush_yards": 22}
    cb = next(p for p in g.players if p.username == "LockdownCB")
    assert cb.stats["def_ints"] == 1 and cb.stats["def_tds"] == 1
    assert not g.unknown_stats


def test_flat_and_teams_list():
    g = parse_payload({"data": {"matchId": 9, "teams": [
        {"name": "Jets", "score": 7, "players": [{"Player": "A", "Pass Yds": "150", "Pass TD": 1, "Comp/Att": "10/20"}]},
        {"name": "Pats", "score": 10, "isHome": True, "players": {"B": {"Rush Yds": 80, "Tackles": 3, "Weird": 5}}},
    ]}})
    assert g.home_team == "Pats" and g.away_team == "Jets" and g.home_score == 10
    a = next(p for p in g.players if p.username == "A")
    assert a.stats["pass_yards"] == 150 and a.stats["pass_completions"] == 10 and a.stats["pass_attempts"] == 20
    b = next(p for p in g.players if p.username == "B")
    assert b.team == "Pats" and b.stats["rush_yards"] == 80 and b.extra == {"Weird": 5}
    assert g.unknown_stats == {"Weird"}


def test_home_away_strings_and_score_string():
    g = parse_payload({"home_team": "A", "away_team": "B", "score": "14-3",
                       "players": [{"name": "x", "team": "A", "passing_yards": 100, "rushing_tds": 2}]})
    assert (g.home_score, g.away_score) == (14, 3)
    assert g.players[0].stats == {"pass_yards": 100, "rush_tds": 2}


def test_discord_embed_style():
    g = parse_payload({"embeds": [{"title": "Final: Ravens 24 - 17 Bills", "fields": [
        {"name": "QBKing_123", "value": "Pass Yds: 250\nPass TDs: 2\nINT: 1"},
        {"name": "Notes", "value": "GG"}]}]})
    assert (g.home_team, g.home_score, g.away_score, g.away_team) == ("Ravens", 24, 17, "Bills")
    assert g.players[0].stats == {"pass_yards": 250, "pass_tds": 2, "interceptions": 1}


def test_garbage_raises():
    with pytest.raises(ParseError):
        parse_payload({"hello": "world"})
    with pytest.raises(ParseError):
        parse_payload([1, 2])


def test_fantasy_points():
    # 312*.04 + 3*4 - 2 + 22*.1 + 300yd bonus 3 = 12.48+12-2+2.2+3
    assert fantasy_points({"pass_yards": 312, "pass_tds": 3, "interceptions": 1, "rush_yards": 22}) == 27.68
    assert fantasy_points({"receptions": 7, "rec_yards": 141, "rec_tds": 2}) == 7 + 14.1 + 12 + 3


def test_db_roundtrip_and_analytics(tmp_path):
    db = Database(tmp_path / "t.db")
    body = SAMPLE.read_text()
    r1 = ingest(db, body, "test", 1)
    assert r1["status"] == "parsed" and r1["players"] == 6
    assert ingest(db, body, "test", 1)["status"] == "duplicate"
    # same game id, changed score -> replaces, not duplicates
    p = json.loads(body); p["home"]["score"] = 35
    r2 = ingest(db, json.dumps(p), "test", 1)
    assert r2["status"] == "replaced" and r2["game_id"] == r1["game_id"]
    assert len(db.recent_games()) == 1 and db.game(r1["game_id"])["home_score"] == 35

    db.set_name("QBKing_123", "Jacob", 42)
    assert db.resolve_player("jacob") == "qbking_123"
    assert db.resolve_player("<@42>") == "qbking_123"
    assert db.resolve_player("speedy") == "speedywr"

    s = analytics.player_summary(db, "qbking_123")
    assert s["games"] == 1 and s["wins"] == 1 and s["totals"]["pass_yards"] == 312
    assert analytics.leaders(db, "pass_yards")[0][0] == "QBKing_123"
    st = analytics.standings(db, 1)
    assert st[0]["team"] == "Ravens" and st[0]["w"] == 1 and st[1]["l"] == 1

    fp = db.set_player_stat(r1["game_id"], "SpeedyWR", "rec_tds", 3)
    assert fp == 7 + 14.1 + 18 + 3
    assert db.recompute_fantasy() == 6

    # failed payload is kept for reparse
    bad = ingest(db, '{"nothing": 1}', "test", 1)
    assert bad["status"] == "failed" and db.raw(bad["raw_id"])["body"] == '{"nothing": 1}'


def test_webhook_http(tmp_path):
    async def run():
        db = Database(tmp_path / "w.db")
        got = []

        async def on_game(gid, game, replaced):
            got.append((gid, replaced))

        srv = WebhookServer(db, SECRET, 1, on_game=on_game)
        async with TestClient(TestServer(srv.app)) as c:
            assert (await c.get("/health")).status == 200
            assert (await c.post("/webhook/wrong", data=SAMPLE.read_text())).status == 401
            assert (await c.post("/webhook", data=SAMPLE.read_text())).status == 401
            r = await c.post(f"/webhook/{SECRET}", data=SAMPLE.read_text())
            assert r.status == 200 and (await r.json())["status"] == "parsed"
            r = await c.post("/webhook", data="{}", headers={"X-Webhook-Secret": SECRET})
            assert (await r.json())["status"] == "failed"
            r = await c.post(f"/api/webhooks/123/{SECRET}",
                             json={"embeds": [{"title": "Jets 3 - 0 Pats"}]})
            assert (await r.json())["status"] == "parsed"
        assert len(got) == 2
    asyncio.run(run())


def test_website_pages(tmp_path):
    async def run():
        db = Database(tmp_path / "s.db")
        ingest(db, SAMPLE.read_text(), "test")
        db.set_name("QBKing_123", "<b>Jake</b>")  # names are escaped, not rendered as HTML
        srv = WebhookServer(db, SECRET)
        async with TestClient(TestServer(srv.app)) as c:
            for path in ["/", "/games", "/games/1", "/players", "/players?q=jake", "/players/qbking_123",
                         "/fantasy", "/positions", "/positions/qb", "/leaders", "/leaders?stat=pass_yards&per_game=1",
                         "/leaders?stat=longest_pass", "/scoring"]:
                r = await c.get(path)
                html = await r.text()
                assert r.status == 200, path
                assert SECRET not in html and "<b>Jake</b>" not in html, path
            assert "&lt;b&gt;Jake&lt;/b&gt;" in await (await c.get("/players/qbking_123")).text()
            assert "Ravens" in await (await c.get("/games/1")).text()
            for path in ["/games/99", "/games/x", "/players/nobody_here", "/positions/zz"]:
                assert (await c.get(path)).status == 404, path
            assert (await c.post(f"/webhook/{SECRET}", data=SAMPLE.read_text())).status == 200
        off = WebhookServer(db, SECRET, site=False)
        async with TestClient(TestServer(off.app)) as c:
            assert (await (await c.get("/")).json())["ok"] is True
            assert (await c.get("/games")).status == 404
    asyncio.run(run())
