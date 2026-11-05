"""SQLite storage for games, player stat lines, name mappings and raw webhook payloads."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config

from .fantasy import fantasy_points, stat_group
from .parser import ParsedGame

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_payloads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    remote       TEXT,
    hash         TEXT UNIQUE,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'received',   -- received | parsed | failed | duplicate | deleted | superseded
    error        TEXT,
    game_id      INTEGER,
    message_id   TEXT                                 -- Discord message it came from (feed channel)
);

CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT UNIQUE,
    season       INTEGER NOT NULL DEFAULT 0,          -- seasons.id, 0 = not in any season
    week         INTEGER,
    home_team    TEXT NOT NULL,
    away_team    TEXT NOT NULL,
    home_score   INTEGER NOT NULL DEFAULT 0,
    away_score   INTEGER NOT NULL DEFAULT 0,
    played_at    TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'webhook',     -- webhook | manual | import
    raw_id       INTEGER REFERENCES raw_payloads(id)
);

CREATE TABLE IF NOT EXISTS player_games (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    username       TEXT NOT NULL,
    username_key   TEXT NOT NULL,
    roblox_user_id TEXT,
    team           TEXT,
    position       TEXT,
    stats          TEXT NOT NULL DEFAULT '{}',
    extra          TEXT NOT NULL DEFAULT '{}',
    pos_stats      TEXT NOT NULL DEFAULT '{}',          -- {"QB": {stat: value}, "DB": {...}}
    fantasy_points REAL NOT NULL DEFAULT 0,
    UNIQUE (game_id, username_key)
);
CREATE INDEX IF NOT EXISTS idx_pg_user ON player_games(username_key);

CREATE TABLE IF NOT EXISTS names (
    username_key    TEXT PRIMARY KEY,
    roblox_username TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    discord_id      INTEGER
);

-- Admin rulings on who is at fault for an interception. charged_key NULL = excused (nobody loses points).
CREATE TABLE IF NOT EXISTS int_faults (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    thrower_key  TEXT NOT NULL,
    charged_key  TEXT,
    count        INTEGER NOT NULL,
    note         TEXT,
    created_by   INTEGER,
    created_at   TEXT NOT NULL
);

-- Players an admin removed from a game (/admin removeplayer); kept out when the game is re-read.
CREATE TABLE IF NOT EXISTS removed_players (
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    username_key TEXT NOT NULL,
    PRIMARY KEY (game_id, username_key)
);

-- League seasons. A game belongs to the season whose start..end window contains it (games.season).
-- At most one season is open (ended_at NULL). mvps = the awards worked out when it ended (JSON).
CREATE TABLE IF NOT EXISTS seasons (
    id           INTEGER PRIMARY KEY,                 -- season number: 1, 2, 3...
    name         TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    mvps         TEXT,                                -- stat awards, worked out when it ended
    vote_ends_at TEXT,                                -- MVP voting is open from ended_at until this
    vote_results TEXT                                 -- set when voting closes (JSON)
);

-- Players' MVP votes. One vote per voter per award per season (voting again replaces it).
-- weight = how much it counts, from the share of the season's games the voter played.
CREATE TABLE IF NOT EXISTS season_votes (
    season_id     INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    role          TEXT NOT NULL,                      -- MVP, QB, RB, WR, DB, DL, ST
    voter_key     TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    weight        REAL NOT NULL,
    voter_discord INTEGER,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (season_id, role, voter_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str | None) -> datetime | None:
    """ISO time from the bot, the webhook or Discord ('Z' or no zone = UTC). None if unreadable."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def season_length() -> int:
    """Games in a season (SEASON_GAMES in config.py, default 80; 0 = seasons only end by hand)."""
    return int(getattr(config, "SEASON_GAMES", 80) or 0)


def season_min_players() -> int:
    """Players a game needs to count toward a season (SEASON_MIN_PLAYERS, default 8)."""
    return int(getattr(config, "SEASON_MIN_PLAYERS", 8) or 0)


def _iso(value: str | None) -> str:
    """A time normalized to the bot's own ISO format (UTC), so stored season times compare cleanly."""
    t = _parse_time(value)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else _now()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.lock = threading.RLock()
        self._first_season_from_past_games()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(raw_payloads)")}
        if "message_id" not in cols:
            self.conn.execute("ALTER TABLE raw_payloads ADD COLUMN message_id TEXT")
            # Feed messages stored before this column existed have it in 'remote'
            self.conn.execute("UPDATE raw_payloads SET message_id = substr(remote, 14) "
                              "WHERE remote LIKE 'discord-feed:%'")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_msg ON raw_payloads(message_id)")
        pg_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(player_games)")}
        if "pos_stats" not in pg_cols:
            # Filled for existing games by /admin reparseall
            self.conn.execute("ALTER TABLE player_games ADD COLUMN pos_stats TEXT NOT NULL DEFAULT '{}'")
        s_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(seasons)")}
        for col in ("vote_ends_at", "vote_results"):
            if col not in s_cols:
                self.conn.execute(f"ALTER TABLE seasons ADD COLUMN {col} TEXT")
        if not self.conn.execute("SELECT 1 FROM settings WHERE key = 'seasons_v1'").fetchone():
            # games.season used to be an unused column fixed at 1: start everyone off-season
            # (/seasonadmin start include_past_games pulls them into the first season)
            if not self.conn.execute("SELECT 1 FROM seasons").fetchone():
                self.conn.execute("UPDATE games SET season = 0")
            self.conn.execute("INSERT INTO settings(key, value) VALUES('seasons_v1', '1')")
        self.conn.commit()

    def _first_season_from_past_games(self) -> None:
        """Once, when seasons are new: the games already played (with SEASON_MIN_PLAYERS+ players)
        become Season 1. If Season 1 was already started by a new game, the earlier games join it.
        More than SEASON_GAMES of them carry on into Season 2 and so on. Runs one time only."""
        if self.get_setting("seasons_past_filed") or not getattr(config, "SEASON_FILE_PAST_GAMES", True):
            return
        rows = self.seasons()
        if not rows:
            if self.one("SELECT 1 FROM games WHERE season = 0"):
                self.start_season("Season 1", include_past_games=True)
        elif len(rows) == 1 and rows[0]["ended_at"] is None:
            first = rows[0]
            start = _parse_time(first["started_at"])
            earlier = [g for g in self.q("SELECT id, played_at FROM games WHERE season = 0 ORDER BY played_at, id")
                       if self.counts_for_season(g["id"])
                       and (t := _parse_time(g["played_at"])) is not None and t < start]
            if earlier:
                self.x("UPDATE seasons SET started_at = ? WHERE id = ?", (_iso(earlier[0]["played_at"]), first["id"]))
                for g in earlier:
                    if self.active_season() is None:  # filled up: the rest go into the next season
                        cur = self.start_season(started_at=g["played_at"])
                    else:
                        cur = self.active_season()
                    self.set_game_season(g["id"], cur["id"])
                    self._end_if_full(cur["id"], g["played_at"])
        self.set_setting("seasons_past_filed", "1")

    # ------------------------------------------------------------------ utils
    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        self.x("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ------------------------------------------------------------ raw payloads
    def store_raw(self, body: str, remote: str | None, h: str,
                  message_id: int | None = None) -> tuple[int, bool]:
        """Store a raw payload. Returns (raw_id, is_duplicate)."""
        existing = self.one("SELECT id FROM raw_payloads WHERE hash = ?", (h,))
        if existing:
            return existing["id"], True
        cur = self.x("INSERT INTO raw_payloads(received_at, remote, hash, body, message_id) VALUES(?,?,?,?,?)",
                     (_now(), remote, h, body, str(message_id) if message_id else None))
        return cur.lastrowid, False

    def has_message(self, message_id: int) -> bool:
        """True if this Discord message was already stored."""
        return self.one("SELECT 1 FROM raw_payloads WHERE message_id = ?", (str(message_id),)) is not None

    def raws_for_message(self, message_id: int) -> list[sqlite3.Row]:
        """Stored payloads from this Discord message that still count (not superseded by a later save)."""
        return self.q("SELECT id, status, body FROM raw_payloads WHERE message_id = ? AND status != 'superseded' "
                      "ORDER BY id DESC", (str(message_id),))

    def set_raw_status(self, raw_id: int, status: str, error: str | None = None,
                       game_id: int | None = None) -> None:
        self.x("UPDATE raw_payloads SET status = ?, error = ?, game_id = ? WHERE id = ?",
               (status, error, game_id, raw_id))

    def raw(self, raw_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM raw_payloads WHERE id = ?", (raw_id,))

    def recent_raw(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.q("SELECT id, received_at, remote, status, error, game_id, length(body) AS size "
                      "FROM raw_payloads ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ games
    def save_game(self, g: ParsedGame, *, raw_id: int | None = None, source: str = "webhook") -> tuple[int, bool]:
        """Insert (or replace, if the external id is already known) a game. Returns (game_id, replaced)."""
        with self.lock:
            c = self.conn
            replaced = False
            existing = None
            if g.external_id:
                existing = c.execute("SELECT id FROM games WHERE external_id = ?", (g.external_id,)).fetchone()
            played = g.played_at or _now()
            if existing:
                game_id = existing["id"]
                replaced = True
                c.execute("UPDATE games SET week=?, home_team=?, away_team=?, home_score=?, "
                          "away_score=?, played_at=?, source=?, raw_id=? WHERE id=?",
                          (g.week, g.home_team, g.away_team, g.home_score, g.away_score,
                           played, source, raw_id, game_id))
                c.execute("DELETE FROM player_games WHERE game_id = ?", (game_id,))
            else:
                season = 0  # decided below, once we know how many players it had
                cur = c.execute("INSERT INTO games(external_id, season, week, home_team, away_team, "
                                "home_score, away_score, played_at, source, raw_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (g.external_id, season, g.week, g.home_team, g.away_team,
                                 g.home_score, g.away_score, played, source, raw_id))
                game_id = cur.lastrowid
            removed = {r[0] for r in c.execute("SELECT username_key FROM removed_players WHERE game_id = ?",
                                                (game_id,))}
            for p in (p for p in g.players if p.username.lower() not in removed):
                c.execute("INSERT INTO player_games(game_id, username, username_key, roblox_user_id, team, "
                          "position, stats, extra, pos_stats, fantasy_points) VALUES(?,?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(game_id, username_key) DO UPDATE SET stats=excluded.stats, "
                          "extra=excluded.extra, pos_stats=excluded.pos_stats, "
                          "fantasy_points=excluded.fantasy_points",
                          (game_id, p.username, p.username.lower(), p.user_id, p.team, p.position,
                           json.dumps(p.stats), json.dumps(p.extra), json.dumps(p.pos_stats),
                           fantasy_points(p.stats, p.pos_stats)))
            self._rescore_game(game_id)
            c.commit()
            self._place_in_season(game_id)
            return game_id, replaced

    def _place_in_season(self, game_id: int) -> None:
        """Put a game in its season if it counts (SEASON_MIN_PLAYERS or more players), or take it out
        if it no longer does. Games that don't count stay saved and in every all-time stat and log.
        A game already in a season stays in it (so /seasonadmin move sticks)."""
        g = self.game(game_id)
        if not g:
            return
        if not self.counts_for_season(game_id):
            if g["season"]:
                self.x("UPDATE games SET season = 0 WHERE id = ?", (game_id,))
            return
        if g["season"]:
            return
        season = self._season_for_new_game(g["played_at"])
        if season:
            self.x("UPDATE games SET season = ? WHERE id = ?", (season, game_id))
            self._end_if_full(season, g["played_at"])

    def player_count(self, game_id: int) -> int:
        return self.one("SELECT count(*) n FROM player_games WHERE game_id = ?", (game_id,))["n"]

    def counts_for_season(self, game_id: int) -> bool:
        return self.player_count(game_id) >= season_min_players()

    def delete_game(self, game_id: int) -> bool:
        """Delete a game, its stat lines and rulings. Its raw payload is kept (so /admin backfill still
        knows the message) but marked 'deleted' so /admin reparseall doesn't bring the game back."""
        with self.lock:
            self.conn.execute("UPDATE raw_payloads SET status = 'deleted', game_id = NULL WHERE game_id = ?",
                              (game_id,))
            ok = self.conn.execute("DELETE FROM games WHERE id = ?", (game_id,)).rowcount > 0
            self.conn.commit()
            return ok

    def update_game(self, game_id: int, **fields: Any) -> bool:
        allowed = {"week", "home_team", "away_team", "home_score", "away_score"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return False
        sql = "UPDATE games SET " + ", ".join(f"{k} = ?" for k in sets) + " WHERE id = ?"
        return self.x(sql, (*sets.values(), game_id)).rowcount > 0

    def game(self, game_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM games WHERE id = ?", (game_id,))

    def recent_games(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM games ORDER BY played_at DESC, id DESC LIMIT ?", (limit,))

    def game_players(self, game_id: int) -> list[dict]:
        rows = self.q("SELECT * FROM player_games WHERE game_id = ? ORDER BY fantasy_points DESC", (game_id,))
        return [self._pg(r) for r in rows]

    @staticmethod
    def _pg(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["stats"] = json.loads(d["stats"] or "{}")
        d["extra"] = json.loads(d["extra"] or "{}")
        d["pos_stats"] = json.loads(d.get("pos_stats") or "{}")
        return d

    def set_player_stat(self, game_id: int, username: str, stat: str, value: float,
                        group: str | None = None) -> float:
        """Set one stat on one player's line (creating the line if needed). Returns new fantasy points.
        On lines with position info the stat is set in `group` (default: the first group that scores
        it), and the flat total becomes the sum over groups."""
        key = username.lower()
        with self.lock:
            row = self.conn.execute("SELECT * FROM player_games WHERE game_id=? AND username_key=?",
                                    (game_id, key)).fetchone()
            stats = json.loads(row["stats"]) if row else {}
            pos = json.loads(row["pos_stats"] or "{}") if row else {}
            if pos:
                g = pos.setdefault(group or stat_group(stat) or "QB", {})
                if value == 0:
                    g.pop(stat, None)
                else:
                    g[stat] = value
                pos = {k: v for k, v in pos.items() if v}
                total = sum(st.get(stat, 0) for st in pos.values())
                value = total
            if value == 0:
                stats.pop(stat, None)
            else:
                stats[stat] = value
            if row:
                self.conn.execute("UPDATE player_games SET stats=?, pos_stats=? WHERE id=?",
                                  (json.dumps(stats), json.dumps(pos), row["id"]))
            else:
                self.conn.execute("INSERT INTO player_games(game_id, username, username_key, stats) "
                                  "VALUES(?,?,?,?)", (game_id, username, key, json.dumps(stats)))
            self._rescore_game(game_id)
            self.conn.commit()
            self._place_in_season(game_id)  # a hand-entered game counts once it has enough players
            return self.conn.execute("SELECT fantasy_points FROM player_games WHERE game_id=? AND username_key=?",
                                     (game_id, key)).fetchone()["fantasy_points"]

    def remove_player_from_game(self, game_id: int, username: str) -> bool:
        """Remove a player's line from a game. Remembered, so re-reading the game doesn't add them back."""
        key = username.lower()
        with self.lock:
            ok = self.conn.execute("DELETE FROM player_games WHERE game_id=? AND username_key=?",
                                   (game_id, key)).rowcount > 0
            if ok:
                self.conn.execute("INSERT OR IGNORE INTO removed_players(game_id, username_key) VALUES(?,?)",
                                  (game_id, key))
            self.conn.commit()
            if ok:
                self._place_in_season(game_id)  # under SEASON_MIN_PLAYERS now: no longer counts
            return ok

    def recompute_fantasy(self) -> int:
        """Recalculate every stored fantasy score (run after editing config.FANTASY_SCORING)."""
        with self.lock:
            for r in self.conn.execute("SELECT id FROM games").fetchall():
                self._rescore_game(r["id"])
            self.conn.commit()
            return self.conn.execute("SELECT count(*) FROM player_games").fetchone()[0]

    def _rescore_game(self, game_id: int) -> None:
        """Recompute 'int_charged' and fantasy points for every line in one game (caller holds the lock).

        int_charged = INTs this player threw, minus ones an admin moved to someone else or excused,
        plus ones an admin moved onto them. It is what the interception penalty is scored on. Own
        INTs count toward QB in /stats, INTs moved onto a player count toward WR."""
        c = self.conn
        faults = c.execute("SELECT thrower_key, charged_key, count FROM int_faults WHERE game_id = ?",
                           (game_id,)).fetchall()
        for r in c.execute("SELECT id, username_key, stats, pos_stats FROM player_games WHERE game_id = ?",
                           (game_id,)).fetchall():
            stats, pos = json.loads(r["stats"]), json.loads(r["pos_stats"] or "{}")
            moved = sum(f["count"] for f in faults if f["thrower_key"] == r["username_key"])
            taken = sum(f["count"] for f in faults if f["charged_key"] == r["username_key"])
            own = max(0, int(stats.get("interceptions", 0)) - moved)
            stats.pop("int_charged", None)
            for st in pos.values():
                st.pop("int_charged", None)
            if own + taken:
                stats["int_charged"] = own + taken
            if pos:  # lines without position info (hand-entered) are scored from the flat stats
                if own:
                    pos.setdefault("QB", {})["int_charged"] = own
                if taken:
                    pos.setdefault("WR", {})["int_charged"] = taken
                pos = {k: v for k, v in pos.items() if v}
            c.execute("UPDATE player_games SET stats=?, pos_stats=?, fantasy_points=? WHERE id=?",
                      (json.dumps(stats), json.dumps(pos), fantasy_points(stats, pos), r["id"]))

    # ------------------------------------------------------- interception faults
    def int_fault_free(self, game_id: int, thrower_key: str) -> int:
        """How many of this player's INTs in this game are still charged to them (not yet ruled on)."""
        row = self.one("SELECT stats FROM player_games WHERE game_id=? AND username_key=?", (game_id, thrower_key))
        thrown = int(json.loads(row["stats"]).get("interceptions", 0)) if row else 0
        ruled = self.one("SELECT COALESCE(SUM(count), 0) n FROM int_faults WHERE game_id=? AND thrower_key=?",
                         (game_id, thrower_key))["n"]
        return max(0, thrown - ruled)

    def add_int_fault(self, game_id: int, thrower_key: str, charged_key: str | None, count: int,
                      note: str | None, admin_id: int) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO int_faults(game_id, thrower_key, charged_key, count, note, "
                                    "created_by, created_at) VALUES(?,?,?,?,?,?,?)",
                                    (game_id, thrower_key, charged_key, count, note, admin_id, _now()))
            self._rescore_game(game_id)
            self.conn.commit()
            return cur.lastrowid

    def int_faults(self, game_id: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM int_faults" + (" WHERE game_id = ?" if game_id is not None else "") + " ORDER BY id"
        return self.q(sql, (game_id,) if game_id is not None else ())

    def remove_int_fault(self, fault_id: int) -> int | None:
        """Undo a ruling. Returns its game id, or None if there was no such ruling."""
        with self.lock:
            row = self.conn.execute("SELECT game_id FROM int_faults WHERE id = ?", (fault_id,)).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM int_faults WHERE id = ?", (fault_id,))
            self._rescore_game(row["game_id"])
            self.conn.commit()
            return row["game_id"]

    # ---------------------------------------------------------------- seasons
    def seasons(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM seasons ORDER BY id")

    def season(self, season_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM seasons WHERE id = ?", (season_id,))

    def active_season(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM seasons WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")

    def latest_season(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM seasons ORDER BY id DESC LIMIT 1")

    def season_for(self, played_at: str) -> int:
        """The season a game played at this time belongs to (0 = none). A game played before the open
        season started (e.g. one imported late by /admin backfill) goes in whichever season it fell in."""
        t = _parse_time(played_at)
        for s in self.q("SELECT * FROM seasons ORDER BY id DESC"):
            start, end = _parse_time(s["started_at"]), _parse_time(s["ended_at"]) if s["ended_at"] else None
            if t is None:  # no usable time: the open season, if any
                return s["id"] if end is None else 0
            if start <= t and (end is None or t < end):
                return s["id"]
        return 0

    def start_season(self, name: str | None = None, include_past_games: bool = False,
                     started_at: str | None = None) -> sqlite3.Row:
        """Open the next season. include_past_games files every game not in a season yet, oldest
        first, into this season and (once it has SEASON_GAMES games) the seasons after it."""
        with self.lock:
            if self.active_season():
                raise ValueError("a season is already in progress")
            sid = (self.one("SELECT COALESCE(MAX(id), 0) n FROM seasons")["n"]) + 1
            loose = [g for g in self.q("SELECT id, played_at FROM games WHERE season = 0 ORDER BY played_at, id")
                     if self.counts_for_season(g["id"])] if include_past_games else []
            start = loose[0]["played_at"] if loose else (started_at or _now())
            self.conn.execute("INSERT INTO seasons(id, name, started_at) VALUES(?,?,?)",
                              (sid, (name or "").strip() or f"Season {sid}", _iso(start)))
            self.conn.commit()
            first = sid
            for g in loose:
                cur = self.active_season()
                if cur is None:  # the last one filled up: the next game opens a new one
                    cur = self.start_season(started_at=g["played_at"])
                self.conn.execute("UPDATE games SET season = ? WHERE id = ?", (cur["id"], g["id"]))
                self.conn.commit()
                self._end_if_full(cur["id"], g["played_at"])
            return self.season(first)

    def _season_for_new_game(self, played_at: str) -> int:
        """Season for a newly saved game. With no season open, a game played after the last one
        ended starts the next season (unless SEASON_AUTO_START is False)."""
        sid = self.season_for(played_at)
        if sid or self.active_season() or not getattr(config, "SEASON_AUTO_START", True):
            return sid
        last = self.latest_season()
        t = _parse_time(played_at)
        if last and t and last["ended_at"] and t < _parse_time(last["ended_at"]):
            return 0  # an old game imported late, from before the last season ended
        return self.start_season(started_at=played_at)["id"]

    def _end_if_full(self, season_id: int, played_at: str | None) -> None:
        """End the open season once it has SEASON_GAMES games, and flag it for the bot to announce."""
        limit = season_length()
        s = self.season(season_id)
        if not limit or not s or s["ended_at"] or self.season_game_count(season_id) < limit:
            return
        from .analytics import season_mvps  # local: analytics imports this module
        # Ends just after its last game, so games played after that (even ones imported in a batch
        # by /admin backfill) go into the next season
        times = [_parse_time(r["played_at"]) for r in self.q("SELECT played_at FROM games WHERE season = ?",
                                                              (season_id,))]
        t = max(filter(None, times + [_parse_time(played_at)]), default=None) or _parse_time(_now())
        self.end_season(season_id, season_mvps(self, season_id), ended_at=(t + timedelta(seconds=1)).isoformat(
            timespec="seconds"))
        self.set_setting("season_announce_pending", str(season_id))

    def end_season(self, season_id: int, mvps: dict, ended_at: str | None = None) -> None:
        """Close a season with its stat awards, and open MVP voting for SEASON_VOTE_DAYS days."""
        end = ended_at or _now()
        days = float(getattr(config, "SEASON_VOTE_DAYS", 3))
        start = max(_parse_time(end), _parse_time(_now()))  # a season filed from old games still gets a vote
        vote_end = (start + timedelta(days=days)).isoformat(timespec="seconds")
        self.x("UPDATE seasons SET ended_at = ?, mvps = ?, vote_ends_at = ?, vote_results = NULL WHERE id = ?",
               (end, json.dumps(mvps), vote_end, season_id))

    def reopen_season(self, season_id: int) -> None:
        if self.active_season():
            raise ValueError("another season is in progress")
        with self.lock:
            self.conn.execute("UPDATE seasons SET ended_at = NULL, mvps = NULL, vote_ends_at = NULL, "
                              "vote_results = NULL WHERE id = ?", (season_id,))
            self.conn.execute("DELETE FROM season_votes WHERE season_id = ?", (season_id,))
            self.conn.commit()

    # ------------------------------------------------------------ MVP voting
    def voting_season(self) -> sqlite3.Row | None:
        """The season whose MVP vote is open right now, if any."""
        return self.one("SELECT * FROM seasons WHERE ended_at IS NOT NULL AND vote_results IS NULL "
                        "AND vote_ends_at IS NOT NULL ORDER BY id DESC LIMIT 1")

    def votes_due(self) -> list[sqlite3.Row]:
        """Open votes whose time is up."""
        now = _parse_time(_now())
        return [s for s in self.q("SELECT * FROM seasons WHERE ended_at IS NOT NULL AND vote_results IS NULL "
                                  "AND vote_ends_at IS NOT NULL") if _parse_time(s["vote_ends_at"]) <= now]

    def cast_vote(self, season_id: int, role: str, voter_key: str, candidate_key: str, weight: float,
                  voter_discord: int | None = None) -> bool:
        """Record a vote. Returns True if it replaced the voter's earlier vote for that award."""
        with self.lock:
            had = self.conn.execute("SELECT 1 FROM season_votes WHERE season_id=? AND role=? AND voter_key=?",
                                    (season_id, role, voter_key)).fetchone() is not None
            self.conn.execute("INSERT INTO season_votes(season_id, role, voter_key, candidate_key, weight, "
                              "voter_discord, created_at) VALUES(?,?,?,?,?,?,?) "
                              "ON CONFLICT(season_id, role, voter_key) DO UPDATE SET "
                              "candidate_key=excluded.candidate_key, weight=excluded.weight, "
                              "created_at=excluded.created_at",
                              (season_id, role, voter_key, candidate_key, weight, voter_discord, _now()))
            self.conn.commit()
            return had

    def season_votes(self, season_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM season_votes WHERE season_id = ? ORDER BY role, created_at", (season_id,))

    def close_voting(self, season_id: int, results: dict) -> None:
        self.x("UPDATE seasons SET vote_results = ?, vote_ends_at = COALESCE(vote_ends_at, ?) WHERE id = ?",
               (json.dumps(results), _now(), season_id))

    def set_game_season(self, game_id: int, season_id: int) -> bool:
        return self.x("UPDATE games SET season = ? WHERE id = ?", (season_id, game_id)).rowcount > 0

    def season_game_count(self, season_id: int) -> int:
        return self.one("SELECT count(*) n FROM games WHERE season = ?", (season_id,))["n"]

    def season_progress(self, season_id: int) -> tuple[int, int, float]:
        """(games played, games in a season, % complete). The total is 0 when seasons have no set
        length; a season ended early shows the share it reached."""
        n, total = self.season_game_count(season_id), season_length()
        return n, total, (min(100.0, n / total * 100) if total else 0.0)

    # ---------------------------------------------------------------- players
    def resolve_player(self, query: str) -> str | None:
        """Find a player's username_key from a display name, Roblox username, or Discord mention/ID.
        Also accepts the forms the bot itself shows: "Name (username)" and "Name - username"."""
        q = query.strip()
        for left, right in ((q[:q.rfind("(")], q[q.rfind("(") + 1:-1]) if q.endswith(")") and "(" in q else (None, None),
                            q.rsplit(" - ", 1) if " - " in q else (None, None)):
            if right and right.strip():  # try the username part first, then the name part
                found = self.resolve_player(right) or (self.resolve_player(left) if left and left.strip() else None)
                if found:
                    return found
        digits = q.strip("<@!>")
        if digits.isdigit():
            row = self.one("SELECT username_key FROM names WHERE discord_id = ?", (int(digits),))
            if row:
                return row["username_key"]
        row = self.one("SELECT username_key FROM names WHERE lower(display_name) = lower(?) "
                       "OR username_key = lower(?)", (q, q))
        if row:
            return row["username_key"]
        row = self.one("SELECT username_key FROM player_games WHERE username_key = lower(?) LIMIT 1", (q,))
        if row:
            return row["username_key"]
        # partial match as a last resort
        row = self.one("SELECT username_key FROM names WHERE lower(display_name) LIKE lower(?) LIMIT 1",
                       (f"%{q}%",))
        if row:
            return row["username_key"]
        row = self.one("SELECT username_key FROM player_games WHERE username_key LIKE lower(?) LIMIT 1",
                       (f"%{q}%",))
        return row["username_key"] if row else None

    def player_lines(self, username_key: str, season: int | None = None) -> list[dict]:
        sql = ("SELECT pg.*, g.week, g.season, g.home_team, g.away_team, g.home_score, g.away_score, "
               "g.played_at FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE pg.username_key = ?")
        params: list = [username_key]
        if season is not None:
            sql += " AND g.season = ?"
            params.append(season)
        sql += " ORDER BY g.played_at DESC, g.id DESC"
        return [self._pg(r) for r in self.q(sql, tuple(params))]

    def all_player_lines(self, week: int | None = None, season: int | None = None) -> list[dict]:
        sql = ("SELECT pg.*, g.week, g.season FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE 1=1")
        params: list = []
        if week is not None:
            sql += " AND g.week = ?"
            params.append(week)
        if season is not None:
            sql += " AND g.season = ?"
            params.append(season)
        return [self._pg(r) for r in self.q(sql, tuple(params))]

    def known_usernames(self) -> list[str]:
        return [r[0] for r in self.q("SELECT DISTINCT username FROM player_games ORDER BY lower(username)")]

    # ------------------------------------------------------------------ names
    def set_name(self, roblox_username: str, display_name: str, discord_id: int | None = None) -> None:
        self.x("INSERT INTO names(username_key, roblox_username, display_name, discord_id) VALUES(?,?,?,?) "
               "ON CONFLICT(username_key) DO UPDATE SET display_name=excluded.display_name, "
               "roblox_username=excluded.roblox_username, "
               "discord_id=COALESCE(excluded.discord_id, names.discord_id)",
               (roblox_username.lower(), roblox_username, display_name, discord_id))

    def remove_name(self, roblox_username: str) -> bool:
        return self.x("DELETE FROM names WHERE username_key = ?", (roblox_username.lower(),)).rowcount > 0

    def names(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM names ORDER BY lower(display_name)")

    def name_map(self) -> dict[str, str]:
        return {r["username_key"]: r["display_name"] for r in self.q("SELECT username_key, display_name FROM names")}

    def name_for_discord(self, discord_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM names WHERE discord_id = ?", (discord_id,))
