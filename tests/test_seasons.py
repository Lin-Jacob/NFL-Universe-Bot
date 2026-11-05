import json

from nflu import analytics
from nflu.db import Database
from nflu.parser import ParsedGame, ParsedPlayer


def _game(ext, when, players):
    return ParsedGame(external_id=ext, home_team="A", away_team="B", home_score=1, away_score=0, played_at=when,
                      players=[ParsedPlayer(username=u, stats={k: v for st in ps.values() for k, v in st.items()},
                                            pos_stats=ps) for u, ps in players.items()])


def test_season_tagging_and_mvps(tmp_path):
    db = Database(tmp_path / "s.db")
    gid0, _ = db.save_game(_game("old", "2020-01-01T00:00:00Z", {"qb1": {"QB": {"pass_yards": 500}}}))
    assert db.game(gid0)["season"] == 0  # no season yet

    s1 = db.start_season(None, include_past_games=True)
    assert s1["id"] == 1 and s1["name"] == "Season 1" and db.game(gid0)["season"] == 1
    try:
        db.start_season("dup")
        raise AssertionError("two open seasons")
    except ValueError:
        pass

    # three more games in season 1: qb1 throws, qb2 has one huge game only, db1 defends every game
    for i in range(3):
        players = {"qb1": {"QB": {"pass_yards": 250, "pass_tds": 2}}, "db1": {"DB": {"tackles": 4, "def_ints": 1}}}
        if i == 0:
            players["qb2"] = {"QB": {"pass_yards": 2000}}
        gid, _ = db.save_game(_game(f"g{i}", None, players))
        assert db.game(gid)["season"] == 1

    race = analytics.season_mvps(db, 1)
    assert race["games"] == 4 and race["min_games"] == 2
    assert race["roles"]["QB"][0]["username"] == "qb1"          # qb2 played once: not eligible
    assert [r["username"] for r in race["roles"]["QB"]] == ["qb1"]
    assert race["roles"]["DB"][0]["username"] == "db1" and race["roles"]["DB"][0]["games"] == 3
    assert race["roles"]["MVP"][0]["username"] == "qb1"
    assert race["roles"]["ST"] == []

    db.end_season(1, race)
    ended = db.season(1)
    assert ended["ended_at"] and json.loads(ended["mvps"])["roles"]["QB"][0]["username"] == "qb1"
    assert db.active_season() is None

    # a game after the season ends belongs to none; season 2 picks up from there
    late, _ = db.save_game(_game("late", None, {"qb1": {"QB": {"pass_yards": 10}}}))
    assert db.game(late)["season"] == 0
    s2 = db.start_season("Fall league")
    assert s2["id"] == 2 and db.game(late)["season"] == 0
    assert db.set_game_season(late, 2) and db.game(late)["season"] == 2
    # a backfilled game played during season 1 still lands in season 1
    db.x("UPDATE seasons SET started_at = '2019-06-01T00:00:00+00:00', ended_at = '2021-01-01T00:00:00+00:00' "
         "WHERE id = 1")  # everything above ran within the same second
    back, _ = db.save_game(_game("back", "2020-05-05T12:00:00.000Z", {"x": {"QB": {"pass_yards": 1}}}))
    assert db.game(back)["season"] == 1

    db.end_season(2, analytics.season_mvps(db, 2))
    db.reopen_season(2)
    assert db.active_season()["id"] == 2 and db.season(2)["mvps"] is None


def test_old_databases_start_off_season(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    gid, _ = db.save_game(_game("x", None, {"p": {"QB": {"pass_yards": 1}}}))
    db.conn.execute("UPDATE games SET season = 1")  # the old, unused default
    db.conn.execute("DELETE FROM settings WHERE key = 'seasons_v1'")
    db.conn.commit()
    db.conn.close()
    assert Database(path).game(gid)["season"] == 0
