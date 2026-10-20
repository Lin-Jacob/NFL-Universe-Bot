"""SQLite storage for games, player stat lines, name mappings and raw webhook payloads."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    season       INTEGER NOT NULL DEFAULT 1,          -- unused (no seasons); kept for older databases
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

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        self.conn.commit()

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
                cur = c.execute("INSERT INTO games(external_id, season, week, home_team, away_team, "
                                "home_score, away_score, played_at, source, raw_id) VALUES(?,1,?,?,?,?,?,?,?,?)",
                                (g.external_id, g.week, g.home_team, g.away_team,
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
            return game_id, replaced

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

    def player_lines(self, username_key: str) -> list[dict]:
        sql = ("SELECT pg.*, g.week, g.home_team, g.away_team, g.home_score, g.away_score, "
               "g.played_at FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE pg.username_key = ?")
        params: list = [username_key]
        sql += " ORDER BY g.played_at DESC, g.id DESC"
        return [self._pg(r) for r in self.q(sql, tuple(params))]

    def all_player_lines(self, week: int | None = None) -> list[dict]:
        sql = ("SELECT pg.*, g.week FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE 1=1")
        params: list = []
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
