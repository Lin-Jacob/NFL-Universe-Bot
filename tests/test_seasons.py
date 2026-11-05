import json

import pytest

import config
from nflu import analytics, awards
from nflu.db import Database
from nflu.parser import ParsedGame, ParsedPlayer


def _game(ext, when, players):
    return ParsedGame(external_id=ext, home_team="A", away_team="B", home_score=1, away_score=0, played_at=when,
                      players=[ParsedPlayer(username=u, stats={k: v for st in ps.values() for k, v in st.items()},
                                            pos_stats=ps) for u, ps in players.items()])


def _t(i):  # one game an hour, in order
    return f"2030-01-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00"


@pytest.fixture
def season_games(monkeypatch):
    monkeypatch.setattr(config, "SEASON_GAMES", 4, raising=False)
    monkeypatch.setattr(config, "SEASON_VOTE_DAYS", 3, raising=False)
    monkeypatch.setattr(config, "SEASON_AUTO_START", True, raising=False)
    monkeypatch.setattr(config, "SEASON_MIN_PLAYERS", 0, raising=False)


def test_vote_weight_curve():
    assert analytics.vote_weight(0) == 0
    assert analytics.vote_weight(0.25) == 0.5
    assert analytics.vote_weight(0.5) == 0.75
    assert analytics.vote_weight(0.75) == 1.0
    assert analytics.vote_weight(1) == 1.0
    assert 0.5 < analytics.vote_weight(0.4) < 0.75  # in between = interpolated


def test_seasons_fill_up_and_roll_over(tmp_path, season_games):
    db = Database(tmp_path / "s.db")
    ids = []
    for i in range(6):
        players = {"qb1": {"QB": {"pass_yards": 250, "pass_tds": 2}}, "db1": {"DB": {"tackles": 4, "def_ints": 1}}}
        if i == 0:
            players["qb2"] = {"QB": {"pass_yards": 2000}}  # one huge game: not enough games to win
        if i < 1:
            players["sub"] = {"WR": {"receptions": 1}}
        ids.append(db.save_game(_game(f"g{i}", _t(i), players))[0])
    # games 1-4 made Season 1, which ended by itself; games 5-6 started Season 2
    assert [db.game(g)["season"] for g in ids] == [1, 1, 1, 1, 2, 2]
    s1 = db.season(1)
    assert s1["ended_at"] and s1["vote_ends_at"] and db.active_season()["id"] == 2
    assert db.get_setting("season_announce_pending") == "1"
    race = json.loads(s1["mvps"])
    assert race["games"] == 4 and race["min_games"] == 2
    assert [r["username"] for r in race["roles"]["QB"]] == ["qb1"]
    assert race["roles"]["DB"][0]["username"] == "db1" and race["roles"]["DB"][0]["games"] == 4
    assert race["roles"]["PTS"][0]["username"] == "qb1" and race["roles"]["ST"] == []
    # season-only stats
    assert analytics.player_summary(db, "qb1", 2)["games"] == 2
    assert analytics.player_summary(db, "qb1")["games"] == 6
    assert analytics.position_leaders(db, "QB", 5, 2)[0]["games"] == 2
    # a game imported late that was played during Season 1 goes back into Season 1
    late, _ = db.save_game(_game("late", "2030-01-01T02:30:00+00:00", {"x": {"QB": {"pass_yards": 1}}}))
    assert db.game(late)["season"] == 1


def test_voting_rules_and_weights(tmp_path, season_games):
    db = Database(tmp_path / "v.db")
    for i in range(4):
        players = {"ace": {"QB": {"pass_yards": 300}}, "bob": {"WR": {"receptions": 5}}}
        if i < 2:
            players["cal"] = {"DB": {"tackles": 2}}  # plays half the season
        if i == 0:
            players["dee"] = {"DL": {"sacks": 1}}  # plays a quarter of it
        db.save_game(_game(f"g{i}", _t(i), players))
    assert db.voting_season()["id"] == 1

    def vote(voter, award, cand):
        return awards.cast(db, voter, award, cand)

    with pytest.raises(awards.VoteError, match="yourself"):
        vote("ace", "MVP", "ace")
    with pytest.raises(awards.VoteError, match="didn't play"):
        vote("nobody", "MVP", "ace")
    with pytest.raises(awards.VoteError, match="didn't play"):
        vote("ace", "MVP", "ghost")
    with pytest.raises(awards.VoteError, match="award"):
        vote("ace", "BOGUS", "bob")
    assert vote("bob", "MVP", "ace")["weight"] == 1.0
    assert vote("cal", "MVP", "bob")["weight"] == 0.75
    assert vote("dee", "MVP", "bob")["weight"] == 0.5
    assert vote("ace", "MVP", "cal")["weight"] == 1.0
    assert vote("ace", "MVP", "bob")["replaced"]  # changing a vote replaces it
    res = analytics.vote_results(db, 1)
    mvp = res["roles"]["MVP"]
    assert res["voters"] == 4
    assert (mvp[0]["username"], mvp[0]["weight"], mvp[0]["votes"]) == ("bob", 2.25, 3)
    assert (mvp[1]["username"], mvp[1]["weight"]) == ("ace", 1.0)
    db.close_voting(1, res)
    assert db.voting_season() is None
    with pytest.raises(awards.VoteError, match="No vote is open"):
        vote("bob", "MIP", "ace")


def test_include_past_games_splits_into_seasons(tmp_path, season_games, monkeypatch):
    monkeypatch.setattr(config, "SEASON_AUTO_START", False, raising=False)
    db = Database(tmp_path / "p.db")
    for i in range(6):
        db.save_game(_game(f"g{i}", _t(i), {"p": {"QB": {"pass_yards": 10}}}))
    assert db.one("SELECT count(*) n FROM games WHERE season = 0")["n"] == 6
    db.start_season("First", include_past_games=True)
    assert [db.season_game_count(n) for n in (1, 2)] == [4, 2]
    assert db.season(1)["name"] == "First" and db.season(1)["ended_at"] and db.active_season()["id"] == 2


def test_manual_end_reopen_and_move(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEASON_GAMES", 0, raising=False)  # manual seasons only
    monkeypatch.setattr(config, "SEASON_MIN_PLAYERS", 0, raising=False)
    db = Database(tmp_path / "m.db")
    g, _ = db.save_game(_game("a", None, {"p": {"QB": {"pass_yards": 10}}}))
    assert db.game(g)["season"] == 1  # the first game starts a season
    with pytest.raises(ValueError):
        db.start_season("dup")
    db.end_season(1, analytics.season_mvps(db, 1))
    db.reopen_season(1)
    assert db.active_season()["id"] == 1 and db.season(1)["mvps"] is None
    db.end_season(1, analytics.season_mvps(db, 1))
    s2 = db.start_season("Fall")
    assert db.set_game_season(g, 2) and db.game(g)["season"] == 2 and s2["name"] == "Fall"


def test_old_databases_start_off_season(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    gid, _ = db.save_game(_game("x", None, {"p": {"QB": {"pass_yards": 1}}}))
    db.conn.execute("DELETE FROM seasons")
    db.conn.execute("UPDATE games SET season = 1")  # the old, unused default
    db.conn.execute("DELETE FROM settings WHERE key = 'seasons_v1'")
    db.conn.commit()
    db.conn.close()
    assert Database(path).game(gid)["season"] == 0


def test_small_games_dont_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEASON_GAMES", 3, raising=False)
    monkeypatch.setattr(config, "SEASON_MIN_PLAYERS", 8, raising=False)
    db = Database(tmp_path / "small.db")
    full = {f"p{i}": {"WR": {"receptions": 1}} for i in range(8)}
    small = {f"p{i}": {"WR": {"receptions": 1}} for i in range(5)}
    a, _ = db.save_game(_game("a", _t(0), full))
    b, _ = db.save_game(_game("b", _t(1), small))
    assert db.game(a)["season"] == 1 and db.game(b)["season"] == 0  # kept, but not in the season
    assert db.season_progress(1)[:2] == (1, 3) and round(db.season_progress(1)[2]) == 33
    assert analytics.player_summary(db, "p0")["games"] == 2 and analytics.player_summary(db, "p0", 1)["games"] == 1
    # saved first without its stat sheet, then again with it: counts once it has the players
    c, _ = db.save_game(_game("c", _t(2), {}))
    assert db.game(c)["season"] == 0
    db.save_game(_game("c", _t(2), full))
    assert db.game(c)["season"] == 1
    # removing players so it's under 8 takes it out of the season
    db.remove_player_from_game(c, "p0")
    assert db.game(c)["season"] == 0
    db.save_game(_game("d", _t(3), full))
    db.save_game(_game("e", _t(4), full))
    assert db.season(1)["ended_at"] and db.season_game_count(1) == 3  # small games didn't fill it


def test_past_games_become_season_1(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEASON_GAMES", 80, raising=False)
    monkeypatch.setattr(config, "SEASON_MIN_PLAYERS", 8, raising=False)
    full = {f"p{i}": {"WR": {"receptions": 1}} for i in range(8)}
    small = {f"p{i}": {"WR": {"receptions": 1}} for i in range(3)}

    def old_db(path, n_games):
        db = Database(path)
        for i in range(n_games):
            db.save_game(_game(f"g{i}", _t(i), small if i == 1 else full))
        # as if these were saved before seasons existed
        db.conn.execute("DELETE FROM seasons")
        db.conn.execute("UPDATE games SET season = 0")
        db.conn.execute("DELETE FROM settings WHERE key = 'seasons_past_filed'")
        db.conn.commit()
        return db

    db = old_db(tmp_path / "a.db", 5)
    db.conn.close()
    db = Database(tmp_path / "a.db")  # the bot starting after the update
    assert db.season(1)["name"] == "Season 1" and db.season_game_count(1) == 4  # the 3-player game isn't in it
    assert db.active_season()["id"] == 1

    # Season 1 already started by a game played after the update: the earlier games join it
    db = old_db(tmp_path / "b.db", 3)
    db.save_game(_game("new", "2031-01-01T00:00:00+00:00", full))
    assert db.season_game_count(1) == 1
    db.conn.close()
    db = Database(tmp_path / "b.db")
    assert db.season_game_count(1) == 3 and len(db.seasons()) == 1
    assert db.season(1)["started_at"].startswith("2030-01-01T00")
