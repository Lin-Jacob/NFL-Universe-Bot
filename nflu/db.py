"""SQLite storage for games, player stat lines, name mappings and raw webhook payloads."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fantasy import fantasy_points
from .parser import ParsedGame

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_payloads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    remote       TEXT,
    hash         TEXT UNIQUE,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'received',   -- received | parsed | failed | duplicate
    error        TEXT,
    game_id      INTEGER
);

CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT UNIQUE,
    season       INTEGER NOT NULL,
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

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

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

    def current_season(self, fallback: int) -> int:
        return int(self.get_setting("season", str(fallback)))

    # ------------------------------------------------------------ raw payloads
    def store_raw(self, body: str, remote: str | None, h: str) -> tuple[int, bool]:
        """Store a raw payload. Returns (raw_id, is_duplicate)."""
        existing = self.one("SELECT id FROM raw_payloads WHERE hash = ?", (h,))
        if existing:
            return existing["id"], True
        cur = self.x("INSERT INTO raw_payloads(received_at, remote, hash, body) VALUES(?,?,?,?)",
                     (_now(), remote, h, body))
        return cur.lastrowid, False

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
    def save_game(self, g: ParsedGame, *, season: int, raw_id: int | None = None,
                  source: str = "webhook") -> tuple[int, bool]:
        """Insert (or replace, if the external id is already known) a game. Returns (game_id, replaced)."""
        with self.lock:
            c = self.conn
            replaced = False
            existing = None
            if g.external_id:
                existing = c.execute("SELECT id FROM games WHERE external_id = ?", (g.external_id,)).fetchone()
            season_val = g.season or season
            played = g.played_at or _now()
            if existing:
                game_id = existing["id"]
                replaced = True
                c.execute("UPDATE games SET season=?, week=?, home_team=?, away_team=?, home_score=?, "
                          "away_score=?, played_at=?, source=?, raw_id=? WHERE id=?",
                          (season_val, g.week, g.home_team, g.away_team, g.home_score, g.away_score,
                           played, source, raw_id, game_id))
                c.execute("DELETE FROM player_games WHERE game_id = ?", (game_id,))
            else:
                cur = c.execute("INSERT INTO games(external_id, season, week, home_team, away_team, "
                                "home_score, away_score, played_at, source, raw_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (g.external_id, season_val, g.week, g.home_team, g.away_team,
                                 g.home_score, g.away_score, played, source, raw_id))
                game_id = cur.lastrowid
            for p in g.players:
                c.execute("INSERT INTO player_games(game_id, username, username_key, roblox_user_id, team, "
                          "position, stats, extra, fantasy_points) VALUES(?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(game_id, username_key) DO UPDATE SET stats=excluded.stats, "
                          "extra=excluded.extra, fantasy_points=excluded.fantasy_points",
                          (game_id, p.username, p.username.lower(), p.user_id, p.team, p.position,
                           json.dumps(p.stats), json.dumps(p.extra), fantasy_points(p.stats)))
            c.commit()
            return game_id, replaced

    def delete_game(self, game_id: int) -> bool:
        return self.x("DELETE FROM games WHERE id = ?", (game_id,)).rowcount > 0

    def update_game(self, game_id: int, **fields: Any) -> bool:
        allowed = {"season", "week", "home_team", "away_team", "home_score", "away_score"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return False
        sql = "UPDATE games SET " + ", ".join(f"{k} = ?" for k in sets) + " WHERE id = ?"
        return self.x(sql, (*sets.values(), game_id)).rowcount > 0

    def game(self, game_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM games WHERE id = ?", (game_id,))

    def recent_games(self, limit: int = 10, season: int | None = None,
                     team: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM games WHERE 1=1", []
        if season is not None:
            sql += " AND season = ?"
            params.append(season)
        if team:
            sql += " AND (lower(home_team) = lower(?) OR lower(away_team) = lower(?))"
            params += [team, team]
        sql += " ORDER BY played_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return self.q(sql, tuple(params))

    def game_players(self, game_id: int) -> list[dict]:
        rows = self.q("SELECT * FROM player_games WHERE game_id = ? ORDER BY fantasy_points DESC", (game_id,))
        return [self._pg(r) for r in rows]

    @staticmethod
    def _pg(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["stats"] = json.loads(d["stats"] or "{}")
        d["extra"] = json.loads(d["extra"] or "{}")
        return d

    def set_player_stat(self, game_id: int, username: str, stat: str, value: float,
                        team: str | None = None) -> float:
        """Set one stat on one player's line (creating the line if needed). Returns new fantasy points."""
        key = username.lower()
        with self.lock:
            row = self.conn.execute("SELECT * FROM player_games WHERE game_id=? AND username_key=?",
                                    (game_id, key)).fetchone()
            stats = json.loads(row["stats"]) if row else {}
            if value == 0:
                stats.pop(stat, None)
            else:
                stats[stat] = value
            fp = fantasy_points(stats)
            if row:
                self.conn.execute("UPDATE player_games SET stats=?, fantasy_points=? WHERE id=?",
                                  (json.dumps(stats), fp, row["id"]))
            else:
                self.conn.execute("INSERT INTO player_games(game_id, username, username_key, team, stats, "
                                  "fantasy_points) VALUES(?,?,?,?,?,?)",
                                  (game_id, username, key, team, json.dumps(stats), fp))
            self.conn.commit()
            return fp

    def remove_player_from_game(self, game_id: int, username: str) -> bool:
        return self.x("DELETE FROM player_games WHERE game_id=? AND username_key=?",
                      (game_id, username.lower())).rowcount > 0

    def recompute_fantasy(self) -> int:
        """Recalculate every stored fantasy score (run after editing config.FANTASY_SCORING)."""
        with self.lock:
            rows = self.conn.execute("SELECT id, stats FROM player_games").fetchall()
            for r in rows:
                self.conn.execute("UPDATE player_games SET fantasy_points=? WHERE id=?",
                                  (fantasy_points(json.loads(r["stats"])), r["id"]))
            self.conn.commit()
            return len(rows)

    def seasons(self) -> list[int]:
        return [r[0] for r in self.q("SELECT DISTINCT season FROM games ORDER BY season")]

    # ---------------------------------------------------------------- players
    def resolve_player(self, query: str) -> str | None:
        """Find a player's username_key from a display name, Roblox username, or Discord mention/ID."""
        q = query.strip()
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
        sql = ("SELECT pg.*, g.season, g.week, g.home_team, g.away_team, g.home_score, g.away_score, "
               "g.played_at FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE pg.username_key = ?")
        params: list = [username_key]
        if season is not None:
            sql += " AND g.season = ?"
            params.append(season)
        sql += " ORDER BY g.played_at DESC, g.id DESC"
        return [self._pg(r) for r in self.q(sql, tuple(params))]

    def all_player_lines(self, season: int | None = None, week: int | None = None) -> list[dict]:
        sql = ("SELECT pg.*, g.season, g.week FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE 1=1")
        params: list = []
        if season is not None:
            sql += " AND g.season = ?"
            params.append(season)
        if week is not None:
            sql += " AND g.week = ?"
            params.append(week)
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
