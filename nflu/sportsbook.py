"""
Mock sportsbook: fake-money bets on matchups an admin sets up, priced from the stored stats.

A matchup is two lineups of players (and optionally the NFL Universe team names they'll play
as). Creating one generates its markets:
    moneyline   which team wins (same model as /odds)
    total       combined score over/under, from past games' scores
    prop        any player's stat over/under (any stat on the sheet, fantasy points, or
                "any_td" = scores a touchdown), from that player's past games
    custom      anything else: the admin types the question, options and odds

Settling: when a new game arrives that has at least half of a matchup's players, the matchup
is locked and every market the game can answer is settled. Which side of the game each lineup
played on is worked out from the players (see Book.assign_sides), so the winner settles too.
Anything left (custom bets, a side it can't tell) is settled by hand on the admin page.

Parlays: a bettor collects picks (one per market) in a slip that's saved with their account, then
bets them together. The odds multiply; it pays only if every leg wins. A leg that pushes or is
voided drops out (the rest still count); if every leg drops out, the stake is returned.

Alternate lines: every over/under has a slider of other lines priced from the same model. Betting
one creates a hidden "alt" market (parent_id = the main one) that settles with the rest.

Tunable values below can be overridden by adding the same name to config.py.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import secrets
from collections import defaultdict
from datetime import datetime, timezone

import config

from . import analytics
from .db import Database
from .stats import STATS, label

log = logging.getLogger(__name__)

START_BALANCE: float = getattr(config, "BOOK_START_BALANCE", 10_000)
# Bookmaker's margin on a two-way market: 0.045 makes an even line about -110 / -110.
VIG: float = getattr(config, "BOOK_VIG", 0.045)
MIN_STAKE: float = getattr(config, "BOOK_MIN_STAKE", 1)
MAX_PARLAY_LEGS: int = getattr(config, "BOOK_MAX_PARLAY_LEGS", 10)
# Most bets one slip can place at once (singles + every parlay of the chosen sizes)
MAX_SLIP_BETS: int = getattr(config, "BOOK_MAX_SLIP_BETS", 100)
# Alternate lines offered each side of an over/under's main line (the slider), and the least
# likely side a line may have before it's left off
ALT_LINES: int = getattr(config, "BOOK_ALT_LINES", 8)
ALT_MIN_CHANCE = 0.02
# Past score totals assumed when fewer than 2 games are stored
DEFAULT_TOTAL = (28.0, 12.0)

# Extra stats you can bet on that aren't single columns of the stat sheet
DERIVED = {
    "fantasy": "Fantasy Points",
    "any_td": "Touchdowns (rush + rec + defense + returns)",
}
TD_STATS = ("rush_tds", "rec_tds", "def_tds", "kick_return_tds", "punt_return_tds")
# Props created automatically for each player in a new matchup (only stats they have recorded)
QB_PROPS = ("pass_yards", "pass_tds", "interceptions", "pass_completions")
ALL_PROPS = ("receptions", "rec_yards", "any_td", "tackles", "sacks", "def_ints", "pass_deflections")
# Stats spread like yards (normal curve) rather than counts (Poisson)
_CONTINUOUS_HINTS = ("yard", "yac", "fantasy", "longest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_bettors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,                  -- kept in the bettor's cookie
    code        TEXT UNIQUE,                           -- typed in to get the account back (new link/device)
    password    TEXT,                                  -- salt$hash (hash_password); NULL = none set yet
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL UNIQUE,
    balance     REAL NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_matchups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    team1         TEXT NOT NULL,                       -- "Team <first player>" until a game names it
    team2         TEXT NOT NULL,
    players1      TEXT NOT NULL DEFAULT '[]',          -- username keys
    players2      TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'open',        -- open | locked | settled | void
    auto_settle   INTEGER NOT NULL DEFAULT 1,
    base_game_id  INTEGER NOT NULL DEFAULT 0,          -- newest game when created; only later games settle it
    game_id       INTEGER,                             -- the game it was settled from
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_markets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    matchup_id  INTEGER NOT NULL REFERENCES book_matchups(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                         -- moneyline | total | prop | custom
    title       TEXT NOT NULL,
    player_key  TEXT,
    stat        TEXT,
    line        REAL,
    status      TEXT NOT NULL DEFAULT 'open',          -- open | locked | settled | void
    result      TEXT,                                  -- what happened, e.g. "312 Passing Yards"
    sort        INTEGER NOT NULL DEFAULT 0,
    parent_id   INTEGER                                -- alt line: the main market it's an alternative to
);
CREATE TABLE IF NOT EXISTS book_selections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   INTEGER NOT NULL REFERENCES book_markets(id) ON DELETE CASCADE,
    side        TEXT NOT NULL,                         -- over | under | team1 | team2 | custom
    label       TEXT NOT NULL,
    odds        INTEGER NOT NULL,                      -- American odds
    fair_p      REAL,                                  -- model chance, before the margin
    outcome     TEXT                                   -- NULL | win | lose | push | void
);
CREATE TABLE IF NOT EXISTS book_bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bettor_id     INTEGER NOT NULL REFERENCES book_bettors(id) ON DELETE CASCADE,
    selection_id  INTEGER NOT NULL REFERENCES book_selections(id) ON DELETE CASCADE,
    stake         REAL NOT NULL,
    odds          INTEGER NOT NULL,                    -- odds when the bet was placed
    placed_at     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',     -- pending | won | lost | push | void
    payout        REAL NOT NULL DEFAULT 0              -- returned to the bettor (stake included)
);
CREATE INDEX IF NOT EXISTS idx_book_bets_bettor ON book_bets(bettor_id);
CREATE INDEX IF NOT EXISTS idx_book_bets_sel ON book_bets(selection_id);
CREATE TABLE IF NOT EXISTS book_parlays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bettor_id   INTEGER NOT NULL REFERENCES book_bettors(id) ON DELETE CASCADE,
    stake       REAL NOT NULL,
    placed_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',       -- pending | won | lost | void
    payout      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS book_parlay_legs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id     INTEGER NOT NULL REFERENCES book_parlays(id) ON DELETE CASCADE,
    selection_id  INTEGER NOT NULL REFERENCES book_selections(id) ON DELETE CASCADE,
    odds          INTEGER NOT NULL                     -- odds when the parlay was placed
);
CREATE INDEX IF NOT EXISTS idx_book_legs_sel ON book_parlay_legs(selection_id);
-- Picks a bettor is collecting for their next parlay
CREATE TABLE IF NOT EXISTS book_slip (
    bettor_id     INTEGER NOT NULL REFERENCES book_bettors(id) ON DELETE CASCADE,
    selection_id  INTEGER NOT NULL REFERENCES book_selections(id) ON DELETE CASCADE,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (bettor_id, selection_id)
);
"""


class BookError(Exception):
    """A request the sportsbook refuses (shown to the user as-is)."""


def _check_new_password(password: str) -> None:
    if not 4 <= len(password) <= 100:
        raise BookError("Pick a password of at least 4 characters.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db: Database) -> None:
    with db.lock:
        c = db.conn
        c.executescript(SCHEMA)
        if "code" not in {r["name"] for r in c.execute("PRAGMA table_info(book_bettors)")}:
            c.execute("ALTER TABLE book_bettors ADD COLUMN code TEXT")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_book_code ON book_bettors(code)")
        if "password" not in {r["name"] for r in c.execute("PRAGMA table_info(book_bettors)")}:
            c.execute("ALTER TABLE book_bettors ADD COLUMN password TEXT")
        if "parent_id" not in {r["name"] for r in c.execute("PRAGMA table_info(book_markets)")}:
            c.execute("ALTER TABLE book_markets ADD COLUMN parent_id INTEGER")
        for r in c.execute("SELECT id FROM book_bettors WHERE code IS NULL").fetchall():
            c.execute("UPDATE book_bettors SET code = ? WHERE id = ?", (new_code(), r["id"]))
        c.commit()


_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def new_code() -> str:
    """Account code like 'K7QM-2XPA' (40 bits): enough that nobody guesses someone else's."""
    raw = "".join(secrets.choice(_CODE_CHARS) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 50_000).hex()
    return f"{salt}${digest}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    return secrets.compare_digest(hash_password(password, stored.split("$", 1)[0]), stored)


def clean_code(code: str) -> str:
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:]}"


# =============================================================================
# Odds math
# =============================================================================

def decimal_odds(american: int) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / -american


def implied(american: int) -> float:
    return 1 / decimal_odds(american)


def to_american(q: float) -> int:
    """American odds for an implied chance q (margin already included), rounded to 5."""
    q = min(max(q, 0.01), 0.99)
    raw = -100 * q / (1 - q) if q >= 0.5 else 100 * (1 - q) / q
    out = int(5 * round(raw / 5))
    if -100 < out < 100:  # +100 and -100 are the same price; nothing lives in between
        out = 100 if raw > 0 else -100
    return out


def price(fair: list[float]) -> list[int]:
    """Odds for each outcome's model chance, with the book's margin spread over them."""
    return [to_american(p * (1 + VIG)) for p in fair]


def american_from_decimal(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else -round(100 / (dec - 1))


def fmt_odds(american: int) -> str:
    return f"{american:+d}"


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _poisson_cdf(k: int, lam: float) -> float:
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(total, 1.0)


def p_over(model: dict, line: float) -> float:
    """Chance the stat finishes above the line (lines end in .5, so no ties)."""
    if model["dist"] == "poisson":
        return 1 - _poisson_cdf(math.floor(line), max(model["mean"], 1e-6))
    sd = max(model["sd"], 1e-6)
    return 1 - _phi((line - model["mean"]) / sd)


def default_line(model: dict) -> float:
    """The .5 line closest to an even bet."""
    if model["dist"] == "poisson":
        return min((k + 0.5 for k in range(0, 60)), key=lambda ln: abs(p_over(model, ln) - 0.5))
    return math.floor(model["mean"]) + 0.5


def alt_lines(model: dict, main: float) -> list[float]:
    """The slider's lines for an over/under: the main line plus up to ALT_LINES each side, a whole
    number of steps apart (1 for counts, a round number near a third of the spread for yards), so they
    all end in .5 like the main one. Lines where one side is nearly certain are left off."""
    if model["dist"] == "poisson":
        step = 1.0
    else:
        step = next((x for x in (1, 2, 5, 10, 15, 20, 25, 50, 100) if x >= model["sd"] / 3), 100.0)
    out = []
    for k in range(-ALT_LINES, ALT_LINES + 1):
        line = main + k * step
        if line < 0 or (k and not ALT_MIN_CHANCE <= p_over(model, line) <= 1 - ALT_MIN_CHANCE):
            continue
        out.append(line)
    return out


def stat_label(stat: str) -> str:
    return DERIVED.get(stat) or label(stat)


def bettable_stats() -> list[tuple[str, str]]:
    """[(stat key, label)] for every stat a prop can be on."""
    return list(DERIVED.items()) + [(k, lbl) for k, (lbl, _) in STATS.items() if k != "snaps"]


def stat_value(line: dict, stat: str) -> float:
    """A player's number for one stat in one game (stored stat line from Database.game_players)."""
    if stat == "fantasy":
        return line["fantasy_points"] or 0
    if stat == "any_td":
        return sum(line["stats"].get(s) or 0 for s in TD_STATS)
    return line["stats"].get(stat) or 0


# =============================================================================
# Models from past games
# =============================================================================

class Models:
    """Per-game distributions of any stat for any player, built once per request from every stored
    game. A game is scaled to a full game (config.ODDS_FULL_GAME_SNAPS snaps) when its snap count is
    known, so a late joiner's half game counts as a whole one; games under ODDS_MIN_GAME_SNAPS snaps
    are skipped. Each player's average is pulled toward the average of everyone who has recorded the
    stat by ODDS_PRIOR_GAMES games, so one big game doesn't set the line."""

    def __init__(self, db: Database):
        self.db = db
        self.samples: dict[str, list[dict]] = defaultdict(list)  # key -> [{"scale": x, "line": line}]
        for ln in db.all_player_lines():
            snaps = ln["extra"].get("Snaps") or 0
            if snaps and snaps < config.ODDS_MIN_GAME_SNAPS:
                continue
            scale = config.ODDS_FULL_GAME_SNAPS / snaps if snaps else 1.0
            self.samples[ln["username_key"]].append({"scale": scale, "line": ln})
        self._league: dict[str, tuple[float, float]] = {}

    def _values(self, key: str, stat: str) -> list[float]:
        return [stat_value(s["line"], stat) * s["scale"] for s in self.samples.get(key, [])]

    def _league_stats(self, stat: str) -> tuple[float, float]:
        """(mean, pooled game-to-game sd) over players who have recorded this stat at least once."""
        if stat not in self._league:
            per = [vs for k in self.samples if any(vs := self._values(k, stat))]
            every = [v for vs in per for v in vs]
            mean = sum(every) / len(every) if every else 0.0
            dev = [(v - sum(vs) / len(vs)) ** 2 for vs in per if len(vs) > 1 for v in vs]
            dof = sum(len(vs) - 1 for vs in per if len(vs) > 1)
            if dof:
                sd = (sum(dev) / dof) ** 0.5
            elif len(every) > 1:
                sd = (sum((v - mean) ** 2 for v in every) / (len(every) - 1)) ** 0.5
            else:
                sd = max(mean, 1.0) ** 0.5
            self._league[stat] = (mean, sd)
        return self._league[stat]

    def stat(self, key: str, stat: str) -> dict:
        """{"mean", "sd", "dist": "normal" | "poisson", "games", "raw"} for one player's stat."""
        vals = self._values(key, stat)
        lmean, lsd = self._league_stats(stat)
        prior = config.ODDS_PRIOR_GAMES
        n = len(vals)
        raw = sum(vals) / n if n else 0.0
        if n and not any(vals):
            lmean = 0.0  # has played but never recorded it: don't invent production
        mean = (n * raw + prior * lmean) / (n + prior)
        own_var = sum((v - raw) ** 2 for v in vals) / (n - 1) if n > 1 else lsd ** 2
        var = (prior * lsd ** 2 + max(n - 1, 0) * own_var) / (prior + max(n - 1, 0))
        continuous = any(h in stat for h in _CONTINUOUS_HINTS) or mean >= 10
        return {"mean": mean, "sd": max(var ** 0.5, 0.5), "dist": "normal" if continuous else "poisson",
                "games": n, "raw": raw}

    def has_recorded(self, key: str, stat: str) -> bool:
        return any(self._values(key, stat))

    def is_qb(self, key: str, rr: dict) -> bool:
        return bool(rr.get(key, {}).get("is_qb"))


def moneyline_chances(db: Database, keys1: list[str], keys2: list[str]) -> tuple[float, float, dict]:
    """Win chances for two lineups (same model as /odds), plus the projections."""
    ratings, league = analytics.player_ratings(db)
    if not league["games"]:
        return 0.5, 0.5, {}
    rr, roles_league = analytics.role_ratings(db)
    proj = [analytics.lineup_projection(keys, rr, roles_league) for keys in (keys1, keys2)]
    var = sum(ratings.get(k, league)["sd"] ** 2 for k in keys1 + keys2)
    p1, p2 = analytics.win_odds(proj[0]["total"], proj[1]["total"], var)
    return p1, p2, {"team1": proj[0]["total"], "team2": proj[1]["total"]}


def total_model(db: Database) -> dict:
    """Combined final score of past games, as a normal curve."""
    totals = [r["t"] for r in db.q("SELECT home_score + away_score t FROM games")]
    if len(totals) < 2:
        mean, sd = DEFAULT_TOTAL
    else:
        mean = sum(totals) / len(totals)
        sd = max((sum((t - mean) ** 2 for t in totals) / (len(totals) - 1)) ** 0.5, 3.0)
    return {"mean": mean, "sd": sd, "dist": "normal", "games": len(totals)}


# =============================================================================
# Storage
# =============================================================================

class Book:
    def __init__(self, db: Database):
        self.db = db
        ensure_schema(db)

    # ------------------------------------------------------------------ bettors
    def bettor_by_token(self, token: str | None):
        if not token:
            return None
        return self.db.one("SELECT * FROM book_bettors WHERE token = ?", (token,))

    def bettor(self, bettor_id: int):
        return self.db.one("SELECT * FROM book_bettors WHERE id = ?", (bettor_id,))

    def bettor_by_code(self, code: str):
        return self.db.one("SELECT * FROM book_bettors WHERE code = ?", (clean_code(code),))

    def create_bettor(self, name: str, password: str | None = None) -> tuple[int, str]:
        name = " ".join(name.split())
        if not 1 <= len(name) <= 24:
            raise BookError("Pick a name between 1 and 24 characters.")
        if password is not None:
            _check_new_password(password)
        if self.db.one("SELECT 1 FROM book_bettors WHERE name_key = ?", (name.lower(),)):
            raise BookError(f"Someone already goes by {name}. Pick another name, or log in if it's you.")
        token = secrets.token_urlsafe(32)
        cur = self.db.x("INSERT INTO book_bettors(token, code, name, name_key, balance, created_at, password) "
                        "VALUES(?,?,?,?,?,?,?)", (token, new_code(), name, name.lower(), START_BALANCE, _now(),
                                                  hash_password(password) if password is not None else None))
        return cur.lastrowid, token

    def set_password(self, bettor_id: int, password: str) -> None:
        _check_new_password(password)
        self.db.x("UPDATE book_bettors SET password = ? WHERE id = ?", (hash_password(password), bettor_id))

    def login(self, name: str, password: str, preset: str | None = None) -> tuple[object, bool]:
        """(bettor, claimed) for this name and password, or (None, False). An account made before
        passwords existed is claimed by the first person to log in to it: their password becomes its
        password (on purpose: this is a friends' league, not a bank).

        preset: for a league player (config.PLAYER_NAMES), the password set for them there. Typing it
        logs in to the account with their name, making it (with the starting money) the first time;
        the account's own password works too, and it's never claimed by anyone else."""
        name = " ".join(name.split())
        b = self.db.one("SELECT * FROM book_bettors WHERE name_key = ?", (name.lower(),))
        if preset:
            if secrets.compare_digest(password.encode(), preset.encode()):
                if not b:
                    self.create_bettor(name, secrets.token_urlsafe(16))
                    b = self.db.one("SELECT * FROM book_bettors WHERE name_key = ?", (name.lower(),))
                return b, False
            return (b, False) if b and check_password(password, b["password"]) else (None, False)
        if b and b["password"] is None:
            self.set_password(b["id"], password)
            return self.bettor(b["id"]), True
        return (b, False) if b and check_password(password, b["password"]) else (None, False)

    def adjust_balance(self, bettor_id: int, amount: float) -> None:
        self.db.x("UPDATE book_bettors SET balance = round(balance + ?, 2) WHERE id = ?", (amount, bettor_id))

    def delete_bettor(self, bettor_id: int) -> None:
        self.db.x("DELETE FROM book_bettors WHERE id = ?", (bettor_id,))

    def leaderboard(self) -> list[dict]:
        """Every bettor with money in open bets and their record, singles and parlays together."""
        rows = self.db.q(
            "WITH x AS (SELECT bettor_id, stake, status FROM book_bets "
            "           UNION ALL SELECT bettor_id, stake, status FROM book_parlays) "
            "SELECT b.*, "
            "COALESCE(SUM(CASE WHEN x.status = 'pending' THEN x.stake END), 0) AS at_risk, "
            "COUNT(x.bettor_id) AS bets, "
            "COALESCE(SUM(x.status = 'won'), 0) AS won, COALESCE(SUM(x.status = 'lost'), 0) AS lost "
            "FROM book_bettors b LEFT JOIN x ON x.bettor_id = b.id GROUP BY b.id")
        out = [dict(r, worth=round(r["balance"] + r["at_risk"], 2)) for r in rows]
        out.sort(key=lambda r: (r["worth"], r["balance"]), reverse=True)
        return out

    # ----------------------------------------------------------------- matchups
    def matchups(self, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM book_matchups"
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        rows = self.db.q(sql + " ORDER BY id DESC", statuses or ())
        return [self._mu(r) for r in rows]

    def matchup(self, matchup_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM book_matchups WHERE id = ?", (matchup_id,))
        return self._mu(r) if r else None

    @staticmethod
    def _mu(r) -> dict:
        d = dict(r)
        d["players1"], d["players2"] = json.loads(d["players1"]), json.loads(d["players2"])
        return d

    def markets(self, matchup_id: int) -> list[dict]:
        out = []
        for m in self.db.q("SELECT * FROM book_markets WHERE matchup_id = ? ORDER BY sort, id", (matchup_id,)):
            d = dict(m)
            d["selections"] = [dict(s) for s in self.db.q(
                "SELECT s.*, (SELECT COUNT(*) FROM book_bets b WHERE b.selection_id = s.id) bets, "
                "(SELECT COALESCE(SUM(stake), 0) FROM book_bets b WHERE b.selection_id = s.id) handle "
                "FROM book_selections s WHERE market_id = ? ORDER BY id", (m["id"],))]
            out.append(d)
        return out

    def market(self, market_id: int) -> dict | None:
        m = self.db.one("SELECT * FROM book_markets WHERE id = ?", (market_id,))
        if not m:
            return None
        d = dict(m)
        d["selections"] = [dict(s) for s in self.db.q("SELECT * FROM book_selections WHERE market_id = ? "
                                                      "ORDER BY id", (market_id,))]
        return d

    def selection(self, sel_id: int) -> dict | None:
        s = self.db.one("SELECT s.*, m.matchup_id, m.title AS market_title, m.status AS market_status, "
                        "m.line, m.kind, m.player_key, m.stat, m.parent_id, mu.status AS matchup_status, mu.title AS matchup_title "
                        "FROM book_selections s JOIN book_markets m ON m.id = s.market_id "
                        "JOIN book_matchups mu ON mu.id = m.matchup_id WHERE s.id = ?", (sel_id,))
        return dict(s) if s else None

    def create_matchup(self, title: str, keys1: list[str], keys2: list[str], names=lambda k: k) -> int:
        """Two lineups. Teams are called "Team <first player>" until the game that settles it names them."""
        if not keys1 or not keys2:
            raise BookError("Each team needs at least one player.")
        if set(keys1) & set(keys2):
            raise BookError("A player can't be on both teams.")
        team1, team2 = f"Team {names(keys1[0])}", f"Team {names(keys2[0])}"
        title = title.strip() or f"{team1} vs {team2}"
        base = self.db.one("SELECT COALESCE(MAX(id), 0) n FROM games")["n"]
        cur = self.db.x("INSERT INTO book_matchups(title, team1, team2, players1, players2, base_game_id, "
                        "created_at) VALUES(?,?,?,?,?,?,?)",
                        (title, team1, team2, json.dumps(keys1), json.dumps(keys2), base, _now()))
        mid = cur.lastrowid
        self.generate_markets(mid, names)
        return mid

    def _add_market(self, matchup_id: int, kind: str, title: str, sels: list[tuple[str, str, int, float | None]],
                    *, player_key: str | None = None, stat: str | None = None, line: float | None = None,
                    sort: int = 0) -> int:
        with self.db.lock:
            c = self.db.conn
            mid = c.execute("INSERT INTO book_markets(matchup_id, kind, title, player_key, stat, line, sort) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (matchup_id, kind, title, player_key, stat, line, sort)).lastrowid
            for side, lbl, odds, fair in sels:
                c.execute("INSERT INTO book_selections(market_id, side, label, odds, fair_p) VALUES(?,?,?,?,?)",
                          (mid, side, lbl, odds, fair))
            c.commit()
            return mid

    def generate_markets(self, matchup_id: int, names=lambda k: k) -> None:
        """Moneyline, game total, and the usual props for each player (stats they have recorded)."""
        mu = self.matchup(matchup_id)
        p1, p2, _ = moneyline_chances(self.db, mu["players1"], mu["players2"])
        o1, o2 = price([p1, p2])
        self._add_market(matchup_id, "moneyline", "Winner",
                         [("team1", mu["team1"], o1, p1), ("team2", mu["team2"], o2, p2)], sort=0)
        tm = total_model(self.db)
        line = default_line(tm)
        self._add_total(matchup_id, tm, line)
        self._player_props(matchup_id, mu["players1"] + mu["players2"], names)

    def _player_props(self, matchup_id: int, keys: list[str], names=lambda k: k, first_sort: int = 10) -> None:
        """The usual props for these players: fantasy points plus the stats each has recorded."""
        models = Models(self.db)
        rr, _ = analytics.role_ratings(self.db)
        for i, key in enumerate(keys):
            stats = ["fantasy"] + [s for s in (QB_PROPS if models.is_qb(key, rr) else ()) + ALL_PROPS
                                   if models.has_recorded(key, s)]
            for stat in stats:
                self.add_prop(matchup_id, key, stat, names=names, models=models, sort=first_sort + i)

    def edit_matchup(self, matchup_id: int, title: str, team1: str, team2: str, keys1: list[str],
                     keys2: list[str], names=lambda k: k) -> str:
        """Change a matchup while it's live. Players taken out have their props voided (stakes back),
        players added get the usual props, and the winner odds are re-priced for the new lineups
        (bets already placed keep their odds). Returns a summary of what changed."""
        mu = self.matchup(matchup_id)
        if not mu:
            raise BookError("No such matchup.")
        if mu["status"] in ("settled", "void"):
            raise BookError("This matchup is already settled.")
        if not keys1 or not keys2:
            raise BookError("Each team needs at least one player.")
        if set(keys1) & set(keys2):
            raise BookError("A player can't be on both teams.")
        team1, team2 = " ".join(team1.split()) or mu["team1"], " ".join(team2.split()) or mu["team2"]
        title = " ".join(title.split()) or mu["title"]
        old, new = set(mu["players1"]) | set(mu["players2"]), set(keys1) | set(keys2)
        removed, added = old - new, [k for k in keys1 + keys2 if k not in old]
        moved = (set(mu["players1"]) & set(keys2)) | (set(mu["players2"]) & set(keys1))
        with self.db.lock:
            c = self.db.conn
            c.execute("UPDATE book_matchups SET title = ?, team1 = ?, team2 = ?, players1 = ?, players2 = ? "
                      "WHERE id = ?", (title, team1, team2, json.dumps(keys1), json.dumps(keys2), matchup_id))
            for side, name in (("team1", team1), ("team2", team2)):
                c.execute("UPDATE book_selections SET label = ? WHERE side = ? AND market_id IN "
                          "(SELECT id FROM book_markets WHERE matchup_id = ? AND kind = 'moneyline')",
                          (name, side, matchup_id))
            c.commit()
        for m in self.db.q("SELECT id FROM book_markets WHERE matchup_id = ? AND kind = 'prop' AND player_key IN "
                           f"({','.join('?' * len(removed))}) AND status IN ('open', 'locked')",
                           (matchup_id, *removed)) if removed else []:
            self.void_market(m["id"], "player taken out of the matchup")
        if added:
            first = self.db.one("SELECT COALESCE(MAX(sort), 9) + 1 n FROM book_markets WHERE matchup_id = ? "
                                "AND sort < 90", (matchup_id,))["n"]
            self._player_props(matchup_id, added, names, first_sort=first)
        if removed or added or moved:
            for m in self.db.q("SELECT id FROM book_markets WHERE matchup_id = ? AND kind = 'moneyline' "
                               "AND status IN ('open', 'locked')", (matchup_id,)):
                self.update_market(m["id"], None, {}, reprice=True)
        parts = [f"{len(added)} player{'s' * (len(added) != 1)} added"] if added else []
        parts += [f"{len(removed)} taken out (their props voided)"] if removed else []
        parts += [f"{len(moved)} switched teams"] if moved else []
        return "Matchup saved" + (": " + ", ".join(parts) + ", winner odds re-priced." if parts else ".")

    def version(self) -> str:
        """Changes whenever anything a bettor sees on the sportsbook changes (odds, lines, status...),
        so open pages know to refresh. A bettor opening an alt line doesn't count: its odds come from
        the same model the slider already shows."""
        row = self.db.one(
            "SELECT (SELECT group_concat(id || ':' || odds || ':' || label || ':' || COALESCE(outcome, ''), ',') "
            "        FROM book_selections WHERE market_id IN (SELECT id FROM book_markets WHERE parent_id IS NULL)) a, "
            "       (SELECT group_concat(id || ':' || status || ':' || COALESCE(line, '') || ':' || title, ',') "
            "        FROM book_markets WHERE parent_id IS NULL) b, "
            "       (SELECT group_concat(id || ':' || status || ':' || title || ':' || team1 || ':' || team2 || ':' "
            "        || players1 || players2, ',') FROM book_matchups) c")
        return hashlib.sha256("|".join(str(v) for v in tuple(row)).encode()).hexdigest()[:16]

    def feed_version(self) -> str:
        """Changes whenever a bet or parlay is placed, settled or removed, for the live bets feed."""
        row = self.db.one("SELECT (SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) || ':' || SUM(status = 'pending') "
                          "FROM book_bets) a, (SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) || ':' || "
                          "SUM(status = 'pending') FROM book_parlays) b")
        return f"{row['a']}|{row['b']}"

    def _add_total(self, matchup_id: int, model: dict, line: float) -> int:
        po = p_over(model, line)
        oo, ou = price([po, 1 - po])
        return self._add_market(matchup_id, "total", "Total points (both teams)",
                                [("over", f"Over {line:g}", oo, po), ("under", f"Under {line:g}", ou, 1 - po)],
                                line=line, sort=1)

    def add_prop(self, matchup_id: int, key: str, stat: str, line: float | None = None, names=lambda k: k,
                 models: Models | None = None, sort: int = 50) -> int:
        if stat not in DERIVED and stat not in STATS:
            raise BookError(f"Unknown stat {stat}.")
        model = (models or Models(self.db)).stat(key, stat)
        if line is None:
            line = default_line(model)
        po = p_over(model, line)
        oo, ou = price([po, 1 - po])
        return self._add_market(matchup_id, "prop", f"{names(key)}: {stat_label(stat)}",
                                [("over", f"Over {line:g}", oo, po), ("under", f"Under {line:g}", ou, 1 - po)],
                                player_key=key, stat=stat, line=line, sort=sort)

    def add_custom(self, matchup_id: int, title: str, options: list[tuple[str, int]]) -> int:
        title = title.strip()
        if not title or len(options) < 2:
            raise BookError("A custom bet needs a question and at least two options.")
        return self._add_market(matchup_id, "custom", title, [("custom", lbl, odds, None) for lbl, odds in options],
                                sort=90)

    # ----------------------------------------------------------- alt lines
    def ladder(self, market: dict, models: Models | None = None, alts: list[dict] | None = None) -> list[dict]:
        """The slider for an over/under: [{"line", "over", "under", "over_id", "under_id"}] lowest line
        first. The main line has the market's own odds; an alt line someone already bet has its stored
        odds; the rest are priced from the model now. *_id is the selection when the line exists."""
        model = self.model_for(market, models)
        if model is None:
            return []
        have = {market["line"]: market}
        for a in alts if alts is not None else self._alts(market["id"]):
            have.setdefault(a["line"], a)
        out = []
        for line in sorted(set(alt_lines(model, market["line"])) | set(have)):
            row = {"line": line, "over_id": None, "under_id": None}
            if line in have:
                for sel in have[line]["selections"]:
                    row[sel["side"]], row[sel["side"] + "_id"] = sel["odds"], sel["id"]
            else:
                po = p_over(model, line)
                row["over"], row["under"] = price([po, 1 - po])
            out.append(row)
        return out

    def _alts(self, market_id: int) -> list[dict]:
        return [m for r in self.db.q("SELECT id FROM book_markets WHERE parent_id = ?", (market_id,))
                if (m := self.market(r["id"]))]

    def alt_market(self, market_id: int, line: float) -> int:
        """The market for one of an over/under's alternate lines, made (priced from the model) the first
        time someone picks that line. The main line is the market itself."""
        m = self.market(market_id)
        if not m or m["kind"] not in ("prop", "total") or m["parent_id"]:
            raise BookError("That bet doesn't have other lines.")
        mu = self.matchup(m["matchup_id"])
        if m["status"] != "open" or mu["status"] != "open":
            raise BookError("Betting on this is closed.")
        if line == m["line"]:
            return m["id"]
        model = self.model_for(m)
        if line not in alt_lines(model, m["line"]):
            raise BookError("That line isn't offered.")
        with self.db.lock:
            row = self.db.one("SELECT id FROM book_markets WHERE parent_id = ? AND line = ?", (m["id"], line))
            if row:
                return row["id"]
            po = p_over(model, line)
            oo, ou = price([po, 1 - po])
            aid = self._add_market(m["matchup_id"], m["kind"], m["title"],
                                   [("over", f"Over {line:g}", oo, po), ("under", f"Under {line:g}", ou, 1 - po)],
                                   player_key=m["player_key"], stat=m["stat"], line=line, sort=m["sort"])
            self.db.x("UPDATE book_markets SET parent_id = ? WHERE id = ?", (m["id"], aid))
            return aid

    def model_for(self, market: dict, models: Models | None = None) -> dict | None:
        if market["kind"] == "total":
            return total_model(self.db)
        if market["kind"] == "prop":
            return (models or Models(self.db)).stat(market["player_key"], market["stat"])
        return None

    def update_market(self, market_id: int, line: float | None, odds: dict[int, int], reprice: bool) -> None:
        """Change a market's line and/or odds. reprice=True sets the odds from the model at the (new) line."""
        m = self.market(market_id)
        if not m:
            raise BookError("No such market.")
        if m["status"] in ("settled", "void"):
            raise BookError("This market is already settled.")
        with self.db.lock:
            c = self.db.conn
            if line is not None and m["kind"] in ("total", "prop"):  # a whole-number line can push
                c.execute("UPDATE book_markets SET line = ? WHERE id = ?", (line, market_id))
                for s in m["selections"]:
                    word = "Over" if s["side"] == "over" else "Under"
                    c.execute("UPDATE book_selections SET label = ? WHERE id = ?", (f"{word} {line:g}", s["id"]))
                m["line"] = line
            if reprice and (model := self.model_for(m)):
                po = p_over(model, m["line"])
                oo, ou = price([po, 1 - po])
                for s in m["selections"]:
                    over = s["side"] == "over"
                    c.execute("UPDATE book_selections SET odds = ?, fair_p = ? WHERE id = ?",
                              (oo if over else ou, po if over else 1 - po, s["id"]))
            elif reprice and m["kind"] == "moneyline":
                mu = self.matchup(m["matchup_id"])
                p1, p2, _ = moneyline_chances(self.db, mu["players1"], mu["players2"])
                o1, o2 = price([p1, p2])
                for s in m["selections"]:
                    first = s["side"] == "team1"
                    c.execute("UPDATE book_selections SET odds = ?, fair_p = ? WHERE id = ?",
                              (o1 if first else o2, p1 if first else p2, s["id"]))
            else:
                for sid, o in odds.items():
                    if o is None or -100 < o < 100:
                        raise BookError("Odds must be +100 or more, or -100 or less (e.g. -110, +150).")
                    c.execute("UPDATE book_selections SET odds = ? WHERE id = ? AND market_id = ?",
                              (o, sid, market_id))
            c.commit()

    def set_market_status(self, market_id: int, status: str) -> None:
        if status not in ("open", "locked"):
            raise BookError("Bad status.")
        self.db.x("UPDATE book_markets SET status = ? WHERE (id = ? OR parent_id = ?) AND status IN ('open', 'locked')",
                  (status, market_id, market_id))

    def delete_market(self, market_id: int) -> None:
        """Remove a market and its alt lines, refunding any bets on them."""
        self.void_market(market_id, "removed")
        self.db.x("DELETE FROM book_markets WHERE id = ? OR parent_id = ?", (market_id, market_id))

    def set_matchup_status(self, matchup_id: int, status: str) -> None:
        if status not in ("open", "locked"):
            raise BookError("Bad status.")
        self.db.x("UPDATE book_matchups SET status = ? WHERE id = ? AND status IN ('open', 'locked')",
                  (status, matchup_id))

    def set_auto_settle(self, matchup_id: int, on: bool) -> None:
        self.db.x("UPDATE book_matchups SET auto_settle = ? WHERE id = ?", (int(on), matchup_id))

    def void_matchup(self, matchup_id: int) -> None:
        for m in self.db.q("SELECT id FROM book_markets WHERE matchup_id = ?", (matchup_id,)):
            self.void_market(m["id"], "matchup voided")
        self.db.x("UPDATE book_matchups SET status = 'void' WHERE id = ?", (matchup_id,))

    def delete_matchup(self, matchup_id: int) -> None:
        """Delete a matchup (open bets on it are refunded first, then its markets and bets go with it)
        and renumber the matchups after it so the numbers stay 1, 2, 3... with no gap."""
        self.void_matchup(matchup_id)
        with self.db.lock:
            c = self.db.conn
            c.execute("DELETE FROM book_matchups WHERE id = ?", (matchup_id,))
            c.execute("PRAGMA defer_foreign_keys = ON")  # markets point at the old numbers mid-way
            ids = [r["id"] for r in c.execute("SELECT id FROM book_matchups ORDER BY id")]
            for new, old in enumerate(ids, start=1):
                if new != old:
                    c.execute("UPDATE book_markets SET matchup_id = ? WHERE matchup_id = ?", (new, old))
                    c.execute("UPDATE book_matchups SET id = ? WHERE id = ?", (new, old))
            c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'book_matchups'", (len(ids),))
            c.commit()

    # --------------------------------------------------------------------- bets
    def place_bet(self, bettor_id: int, sel_id: int, stake: float, seen_odds: int | None = None) -> int:
        stake = round(stake, 2)
        if not stake >= MIN_STAKE:
            raise BookError(f"The smallest bet is ${MIN_STAKE:,.0f}.")
        with self.db.lock:
            c = self.db.conn
            s = self.selection(sel_id)
            if not s:
                raise BookError("That bet doesn't exist anymore.")
            if s["matchup_status"] != "open" or s["market_status"] != "open":
                raise BookError("Betting on this is closed.")
            if seen_odds is not None and seen_odds != s["odds"]:
                raise BookError(f"The odds changed to {fmt_odds(s['odds'])}. Check them and bet again.")
            b = c.execute("SELECT balance FROM book_bettors WHERE id = ?", (bettor_id,)).fetchone()
            if not b or b["balance"] + 1e-9 < stake:
                raise BookError(f"You only have ${b['balance'] if b else 0:,.2f}.")
            c.execute("UPDATE book_bettors SET balance = round(balance - ?, 2) WHERE id = ?", (stake, bettor_id))
            bid = c.execute("INSERT INTO book_bets(bettor_id, selection_id, stake, odds, placed_at) "
                            "VALUES(?,?,?,?,?)", (bettor_id, sel_id, stake, s["odds"], _now())).lastrowid
            c.commit()
            return bid

    def bets_for(self, bettor_id: int) -> list[dict]:
        return [dict(r) for r in self.db.q(
            "SELECT b.*, s.label, m.title AS market_title, m.result, m.kind, m.player_key, m.stat, mu.title AS matchup_title, mu.id AS matchup_id "
            "FROM book_bets b JOIN book_selections s ON s.id = b.selection_id "
            "JOIN book_markets m ON m.id = s.market_id JOIN book_matchups mu ON mu.id = m.matchup_id "
            "WHERE b.bettor_id = ? ORDER BY b.id DESC", (bettor_id,))]

    # ------------------------------------------------------------------ parlays
    def slip(self, bettor_id: int) -> list[dict]:
        """The picks in a bettor's parlay slip, oldest first, with their current odds and status."""
        ids = [r["selection_id"] for r in self.db.q("SELECT selection_id FROM book_slip WHERE bettor_id = ? "
                                                    "ORDER BY added_at, rowid", (bettor_id,))]
        return [s for sid in ids if (s := self.selection(sid))]

    def slip_add(self, bettor_id: int, sel_id: int) -> None:
        """Add a pick to the slip. Another pick from the same market (or another line of it) is swapped
        out for it."""
        s = self.selection(sel_id)
        if not s:
            raise BookError("That bet doesn't exist anymore.")
        if s["matchup_status"] != "open" or s["market_status"] != "open":
            raise BookError("Betting on this is closed.")
        with self.db.lock:
            c = self.db.conn
            c.execute("DELETE FROM book_slip WHERE bettor_id = ? AND selection_id IN "
                      "(SELECT s.id FROM book_selections s JOIN book_markets m ON m.id = s.market_id "
                      "WHERE COALESCE(m.parent_id, m.id) = ? AND s.id != ?)",
                      (bettor_id, s["parent_id"] or s["market_id"], sel_id))
            n = c.execute("SELECT COUNT(*) FROM book_slip WHERE bettor_id = ? AND selection_id != ?",
                          (bettor_id, sel_id)).fetchone()[0]
            if n >= MAX_PARLAY_LEGS:
                c.rollback()
                raise BookError(f"A parlay can have at most {MAX_PARLAY_LEGS} picks.")
            c.execute("INSERT OR IGNORE INTO book_slip(bettor_id, selection_id, added_at) VALUES(?,?,?)",
                      (bettor_id, sel_id, _now()))
            c.commit()

    def slip_remove(self, bettor_id: int, sel_id: int | None = None) -> None:
        """Take one pick out of the slip, or empty it (sel_id None)."""
        if sel_id is None:
            self.db.x("DELETE FROM book_slip WHERE bettor_id = ?", (bettor_id,))
        else:
            self.db.x("DELETE FROM book_slip WHERE bettor_id = ? AND selection_id = ?", (bettor_id, sel_id))

    @staticmethod
    def parlay_decimal(odds: list[int]) -> float:
        return math.prod(decimal_odds(o) for o in odds)

    def place_parlay(self, bettor_id: int, stake: float, seen: list[tuple[int, int]] | None = None) -> int:
        """Bet the whole slip as one parlay (see place_slip)."""
        n = len(self.slip(bettor_id))
        return self.place_slip(bettor_id, stake, seen, [max(n, 2)])["parlays"][0]

    @staticmethod
    def slip_bets(n_legs: int, sizes: list[int]) -> int:
        """How many bets a slip of n_legs places for these sizes (1 = each pick alone, k = every k-pick parlay)."""
        return sum(math.comb(n_legs, k) for k in set(sizes) if 1 <= k <= n_legs)

    def place_slip(self, bettor_id: int, stake: float, seen: list[tuple[int, int]] | None = None,
                   sizes: list[int] | None = None, keep: bool = False) -> dict:
        """Bet the slip, stake on each bet: sizes [n] = one parlay of every pick; add 1 for each pick
        on its own, or any k for every k-pick parlay from the slip (a round robin), all at once.
        seen = [(selection id, odds)] the bettor was shown; if any pick or its odds changed since,
        nothing is placed so they can look again. keep=True leaves the picks in the slip.
        Returns {"singles": [bet ids], "parlays": [parlay ids], "cost": total staked}."""
        stake = round(stake, 2)
        if not stake >= MIN_STAKE:
            raise BookError(f"The smallest bet is ${MIN_STAKE:,.0f}.")
        with self.db.lock:
            c = self.db.conn
            legs = self.slip(bettor_id)
            if not legs:
                raise BookError("Your slip is empty. Add picks from the games.")
            if any(k >= 2 for k in sizes or [2]) and len(legs) < 2:
                raise BookError("A parlay needs at least 2 picks. Add more from the games.")
            sizes = sorted({k for k in (sizes or [len(legs)]) if 1 <= k <= len(legs)})
            if not sizes:
                raise BookError("Pick what to bet: the parlay, a round robin or each pick alone.")
            closed = [s["label"] for s in legs if s["matchup_status"] != "open" or s["market_status"] != "open"]
            if closed:
                raise BookError("Betting closed on " + ", ".join(closed) + ". Remove it to place your bets.")
            if seen is not None and sorted(seen) != sorted((s["id"], s["odds"]) for s in legs):
                raise BookError("Your picks or their odds changed. Check the slip and bet again.")
            n_bets = self.slip_bets(len(legs), sizes)
            if n_bets > MAX_SLIP_BETS:
                raise BookError(f"That's {n_bets} bets; the most at once is {MAX_SLIP_BETS}. Pick fewer sizes.")
            cost = round(stake * n_bets, 2)
            b = c.execute("SELECT balance FROM book_bettors WHERE id = ?", (bettor_id,)).fetchone()
            if not b or b["balance"] + 1e-9 < cost:
                raise BookError(f"{n_bets} bets of ${stake:,.2f} cost ${cost:,.2f}; you only have "
                                f"${b['balance'] if b else 0:,.2f}.")
            c.execute("UPDATE book_bettors SET balance = round(balance - ?, 2) WHERE id = ?", (cost, bettor_id))
            now, singles, parlays = _now(), [], []
            for k in sizes:
                for combo in itertools.combinations(legs, k):
                    if k == 1:
                        singles.append(c.execute("INSERT INTO book_bets(bettor_id, selection_id, stake, odds, "
                                                 "placed_at) VALUES(?,?,?,?,?)",
                                                 (bettor_id, combo[0]["id"], stake, combo[0]["odds"], now)).lastrowid)
                        continue
                    pid = c.execute("INSERT INTO book_parlays(bettor_id, stake, placed_at) VALUES(?,?,?)",
                                    (bettor_id, stake, now)).lastrowid
                    for s in combo:
                        c.execute("INSERT INTO book_parlay_legs(parlay_id, selection_id, odds) VALUES(?,?,?)",
                                  (pid, s["id"], s["odds"]))
                    parlays.append(pid)
            if not keep:
                c.execute("DELETE FROM book_slip WHERE bettor_id = ?", (bettor_id,))
            c.commit()
            return {"singles": singles, "parlays": parlays, "cost": cost}

    def tail(self, bettor_id: int, kind: str, bet_id: int) -> list[dict]:
        """Load someone's bet (kind "bet") or parlay into this bettor's slip, replacing what was in it.
        Returns the picks that are still open (the ones loaded)."""
        if kind == "parlay":
            ids = [r["selection_id"] for r in self.db.q("SELECT selection_id FROM book_parlay_legs WHERE parlay_id = ? "
                                                        "ORDER BY id", (bet_id,))]
        else:
            row = self.db.one("SELECT selection_id FROM book_bets WHERE id = ?", (bet_id,))
            ids = [row["selection_id"]] if row else []
        if not ids:
            raise BookError("That bet doesn't exist anymore.")
        legs = [s for sid in ids if (s := self.selection(sid))
                and s["matchup_status"] == "open" and s["market_status"] == "open"]
        if not legs:
            raise BookError("Betting has closed on every pick in that bet.")
        with self.db.lock:
            c = self.db.conn
            c.execute("DELETE FROM book_slip WHERE bettor_id = ?", (bettor_id,))
            for s in legs:
                c.execute("INSERT OR IGNORE INTO book_slip(bettor_id, selection_id, added_at) VALUES(?,?,?)",
                          (bettor_id, s["id"], _now()))
            c.commit()
        return legs

    def feed(self, limit: int = 60, pending: bool | None = None) -> list[dict]:
        """Everyone's latest bets and parlays, newest first: {"kind": "bet" | "parlay", "id", "name",
        "bettor_id", "stake", "status", "payout", "placed_at", "decimal", "legs": [...]}; a leg has the
        pick, its odds when placed and whether betting on it is still open. pending=True keeps only
        open bets, False only settled ones."""
        only = "" if pending is None else f"WHERE status {'=' if pending else '!='} 'pending' "
        leg_sql = ("SELECT {odds} AS odds, s.id AS selection_id, s.label, s.outcome, m.title AS market_title, "
                   "m.result, m.kind, m.player_key, m.stat, m.status AS market_status, mu.status AS matchup_status, "
                   "mu.title AS matchup_title, mu.id AS matchup_id FROM {src} JOIN book_selections s "
                   "ON s.id = {sel} JOIN book_markets m ON m.id = s.market_id "
                   "JOIN book_matchups mu ON mu.id = m.matchup_id WHERE {where} ORDER BY {order}")
        out = []
        for r in self.db.q("SELECT b.*, u.name FROM book_bets b JOIN book_bettors u ON u.id = b.bettor_id "
                           + only.replace("status", "b.status") + "ORDER BY b.id DESC LIMIT ?", (limit,)):
            legs = self.db.q(leg_sql.format(odds="b.odds", src="book_bets b", sel="b.selection_id",
                                            where="b.id = ?", order="b.id"), (r["id"],))
            out.append(dict(r, kind="bet", legs=[dict(x) for x in legs], decimal=decimal_odds(r["odds"])))
        for r in self.db.q("SELECT p.*, u.name FROM book_parlays p JOIN book_bettors u ON u.id = p.bettor_id "
                           + only.replace("status", "p.status") + "ORDER BY p.id DESC LIMIT ?", (limit,)):
            legs = [dict(x) for x in self.db.q(leg_sql.format(odds="l.odds", src="book_parlay_legs l",
                                                              sel="l.selection_id", where="l.parlay_id = ?",
                                                              order="l.id"), (r["id"],))]
            out.append(dict(r, kind="parlay", legs=legs,
                            decimal=self.parlay_decimal([leg["odds"] for leg in legs]) if legs else 1.0))
        out.sort(key=lambda d: (d["placed_at"], d["kind"] == "parlay", d["id"]), reverse=True)
        return out[:limit]

    def house(self) -> dict:
        """The book's side of every bet: profit = stakes kept minus winnings paid on settled bets (money
        handed out on the Bettors page isn't counted); open = stakes on bets not settled yet; exposure =
        what the book would pay out if every open bet won. Per kind ("singles", "parlays") and overall."""
        out = {}
        for kind, table in (("singles", "book_bets"), ("parlays", "book_parlays")):
            r = self.db.one(f"SELECT COALESCE(SUM(CASE WHEN status != 'pending' THEN stake - payout END), 0) profit, "
                            f"COALESCE(SUM(CASE WHEN status = 'pending' THEN stake END), 0) open, "
                            f"COUNT(*) n, COALESCE(SUM(stake), 0) handle FROM {table}")
            out[kind] = dict(r)
        out["singles"]["exposure"] = sum(round(b["stake"] * decimal_odds(b["odds"]), 2) for b in self.db.q(
            "SELECT stake, odds FROM book_bets WHERE status = 'pending'"))
        out["parlays"]["exposure"] = 0.0
        for p in self.db.q("SELECT id, stake FROM book_parlays WHERE status = 'pending'"):
            legs = self.db.q("SELECT l.odds, s.outcome FROM book_parlay_legs l JOIN book_selections s "
                             "ON s.id = l.selection_id WHERE l.parlay_id = ?", (p["id"],))
            out["parlays"]["exposure"] += round(p["stake"] * self.parlay_decimal(
                [leg["odds"] for leg in legs if leg["outcome"] in (None, "win")]), 2)
        out["total"] = {k: round(out["singles"][k] + out["parlays"][k], 2)
                        for k in ("profit", "open", "n", "handle", "exposure")}
        return out

    def matchup_house(self) -> dict[int, float]:
        """Matchup id -> the book's profit on its settled single bets (parlays span games, so they're
        only in the overall figure)."""
        return {r["mid"]: r["profit"] for r in self.db.q(
            "SELECT m.matchup_id mid, COALESCE(SUM(CASE WHEN b.status != 'pending' THEN b.stake - b.payout END), 0) "
            "profit FROM book_bets b JOIN book_selections s ON s.id = b.selection_id "
            "JOIN book_markets m ON m.id = s.market_id GROUP BY m.matchup_id")}

    def parlays_for(self, bettor_id: int) -> list[dict]:
        out = []
        for p in self.db.q("SELECT * FROM book_parlays WHERE bettor_id = ? ORDER BY id DESC", (bettor_id,)):
            d = dict(p)
            d["legs"] = [dict(r) for r in self.db.q(
                "SELECT l.odds, s.label, s.outcome, m.title AS market_title, m.result, m.kind, m.player_key, "
                "m.stat, mu.title AS matchup_title, mu.id AS matchup_id FROM book_parlay_legs l "
                "JOIN book_selections s ON s.id = l.selection_id JOIN book_markets m ON m.id = s.market_id "
                "JOIN book_matchups mu ON mu.id = m.matchup_id WHERE l.parlay_id = ? ORDER BY l.id", (p["id"],))]
            d["decimal"] = self.parlay_decimal([leg["odds"] for leg in d["legs"]]) if d["legs"] else 1.0
            out.append(d)
        return out

    def _regrade_parlays(self, c, sel_ids: list[int]) -> None:
        """Re-work out every parlay with a leg on these selections from its legs' outcomes, and move the
        difference between the old and new payout to or from the bettor (caller holds the lock).

        Lost as soon as one leg loses; won once every leg is decided, paying the stake times the odds of
        the winning legs (pushed/voided legs drop out); void (stake back) if no leg won or lost."""
        if not sel_ids:
            return
        marks = ",".join("?" * len(sel_ids))
        for p in c.execute(f"SELECT DISTINCT p.* FROM book_parlays p JOIN book_parlay_legs l ON l.parlay_id = p.id "
                           f"WHERE l.selection_id IN ({marks})", sel_ids).fetchall():
            legs = c.execute("SELECT l.odds, s.outcome FROM book_parlay_legs l JOIN book_selections s "
                             "ON s.id = l.selection_id WHERE l.parlay_id = ?", (p["id"],)).fetchall()
            outcomes = [leg["outcome"] for leg in legs]
            if "lose" in outcomes:
                status, payout = "lost", 0.0
            elif None in outcomes:
                status, payout = "pending", 0.0
            elif "win" in outcomes:
                status = "won"
                payout = round(p["stake"] * self.parlay_decimal([leg["odds"] for leg in legs
                                                                 if leg["outcome"] == "win"]), 2)
            else:
                status, payout = "void", p["stake"]
            if (status, payout) != (p["status"], p["payout"]):
                c.execute("UPDATE book_parlays SET status = ?, payout = ? WHERE id = ?", (status, payout, p["id"]))
                c.execute("UPDATE book_bettors SET balance = round(balance + ?, 2) WHERE id = ?",
                          (round(payout - p["payout"], 2), p["bettor_id"]))

    # --------------------------------------------------------------- settlement
    def _grade(self, market_id: int, outcomes: dict[int, str], result: str | None, status: str = "settled") -> None:
        """Set each selection's outcome and pay its bets (caller checked the market isn't settled)."""
        pay = {"win": None, "push": 1.0, "void": 1.0, "lose": 0.0}
        bet_status = {"win": "won", "push": "push", "void": "void", "lose": "lost"}
        with self.db.lock:
            c = self.db.conn
            for sid, outcome in outcomes.items():
                c.execute("UPDATE book_selections SET outcome = ? WHERE id = ?", (outcome, sid))
                for bet in c.execute("SELECT * FROM book_bets WHERE selection_id = ? AND status = 'pending'",
                                     (sid,)).fetchall():
                    mult = decimal_odds(bet["odds"]) if outcome == "win" else pay[outcome]
                    payout = round(bet["stake"] * mult, 2)
                    c.execute("UPDATE book_bets SET status = ?, payout = ? WHERE id = ?",
                              (bet_status[outcome], payout, bet["id"]))
                    if payout:
                        c.execute("UPDATE book_bettors SET balance = round(balance + ?, 2) WHERE id = ?",
                                  (payout, bet["bettor_id"]))
            self._regrade_parlays(c, list(outcomes))
            c.execute("UPDATE book_markets SET status = ?, result = ? WHERE id = ?", (status, result, market_id))
            c.commit()
        self._maybe_finish(market_id)

    def _maybe_finish(self, market_id: int) -> None:
        """Mark the matchup settled once none of its markets are left to settle."""
        row = self.db.one("SELECT matchup_id FROM book_markets WHERE id = ?", (market_id,))
        if not row:
            return
        left = self.db.one("SELECT COUNT(*) n FROM book_markets WHERE matchup_id = ? AND status IN ('open', 'locked')",
                           (row["matchup_id"],))["n"]
        if not left:
            self.db.x("UPDATE book_matchups SET status = 'settled' WHERE id = ? AND status IN ('open', 'locked')",
                      (row["matchup_id"],))

    def settle_market(self, market_id: int, winner_sel_id: int | None, result: str | None = None) -> None:
        """Settle by hand: winner_sel_id wins, the others lose. None = push (everyone gets their stake back)."""
        m = self.market(market_id)
        if not m:
            raise BookError("No such market.")
        if m["status"] in ("settled", "void"):
            raise BookError("Already settled. Undo the settlement first.")
        ids = [s["id"] for s in m["selections"]]
        if winner_sel_id is not None and winner_sel_id not in ids:
            raise BookError("That option isn't part of this market.")
        outcomes = {sid: ("push" if winner_sel_id is None else "win" if sid == winner_sel_id else "lose")
                    for sid in ids}
        won = next((s["label"] for s in m["selections"] if s["id"] == winner_sel_id), "Push")
        self._grade(market_id, outcomes, result or f"Settled by admin: {won}")

    def void_market(self, market_id: int, why: str = "voided") -> None:
        m = self.market(market_id)
        if not m:
            return
        if m["status"] == "settled":
            self.unsettle_market(market_id)
        elif m["status"] == "void":
            return
        self._grade(market_id, {s["id"]: "void" for s in m["selections"]}, f"Void ({why}), stakes returned",
                    status="void")
        for a in self.db.q("SELECT id FROM book_markets WHERE parent_id = ?", (market_id,)):
            self.void_market(a["id"], why)

    def settle_value(self, market_id: int, value: float) -> int:
        """Settle an over/under and all its alt lines by hand from what the stat came to. Returns how
        many markets were settled."""
        m = self.market(market_id)
        if not m or m["kind"] not in ("prop", "total"):
            raise BookError("Only over/unders settle from a number.")
        n = 0
        for x in [m] if m["parent_id"] else [m] + self._alts(m["id"]):
            if x["status"] in ("settled", "void"):
                continue
            over = "win" if value > x["line"] else "lose" if value < x["line"] else "push"
            under = {"win": "lose", "lose": "win", "push": "push"}[over]
            sels = {sel["side"]: sel["id"] for sel in x["selections"]}
            self._grade(x["id"], {sels["over"]: over, sels["under"]: under}, f"{value:g} (settled by admin)")
            n += 1
        return n

    def unsettle_market(self, market_id: int) -> None:
        """Undo a settlement: take back what was paid and put the bets back to pending."""
        with self.db.lock:
            c = self.db.conn
            for bet in c.execute("SELECT b.* FROM book_bets b JOIN book_selections s ON s.id = b.selection_id "
                                 "WHERE s.market_id = ? AND b.status != 'pending'", (market_id,)).fetchall():
                if bet["payout"]:
                    c.execute("UPDATE book_bettors SET balance = round(balance - ?, 2) WHERE id = ?",
                              (bet["payout"], bet["bettor_id"]))
                c.execute("UPDATE book_bets SET status = 'pending', payout = 0 WHERE id = ?", (bet["id"],))
            c.execute("UPDATE book_selections SET outcome = NULL WHERE market_id = ?", (market_id,))
            self._regrade_parlays(c, [r["id"] for r in c.execute("SELECT id FROM book_selections WHERE market_id = ?",
                                                                 (market_id,)).fetchall()])
            c.execute("UPDATE book_markets SET status = 'locked', result = NULL WHERE id = ?", (market_id,))
            c.execute("UPDATE book_matchups SET status = 'locked' WHERE id = (SELECT matchup_id FROM book_markets "
                      "WHERE id = ?) AND status = 'settled'", (market_id,))
            c.commit()

    @staticmethod
    def _points_scored(lines: list[dict]) -> float:
        """Points a lineup's own stats account for: 6 a touchdown (passing TDs cover the catches, so
        receiving TDs aren't counted again), 3 a field goal, 2 a safety. Extra points aren't on the sheet."""
        tds = ("pass_tds", "rush_tds", "def_tds", "kick_return_tds", "punt_return_tds")
        return sum(6 * sum(ln["stats"].get(s) or 0 for s in tds) + 3 * (ln["stats"].get("fg_made") or 0)
                   + 2 * (ln["stats"].get("safeties") or 0) for ln in lines)

    def assign_sides(self, mu: dict, game, lines: dict[str, dict]) -> bool | None:
        """Which side of the game each lineup played on, from its players: True = team 1 was the game's
        first-listed (home) team, False = the second, None = can't tell.

        Uses the players' team when the game reports it, otherwise matches each lineup's touchdowns,
        field goals and safeties against the two final scores (the closer fit wins)."""
        h, a = game["home_team"].strip().lower(), game["away_team"].strip().lower()
        sides = [[lines[k] for k in mu[p] if k in lines] for p in ("players1", "players2")]
        teams = []
        for side in sides:
            named = [str(ln["team"]).strip().lower() for ln in side if ln.get("team")]
            teams.append(max(set(named), key=named.count) if named else None)
        if teams[0] in (h, a) and teams[1] in (h, a) and teams[0] != teams[1]:
            return teams[0] == h
        p1, p2 = (self._points_scored(side) for side in sides)
        hs, as_ = game["home_score"], game["away_score"]
        home_fit, away_fit = abs(p1 - hs) + abs(p2 - as_), abs(p1 - as_) + abs(p2 - hs)
        if home_fit == away_fit:
            return None
        return home_fit < away_fit

    def settle_from_game(self, matchup_id: int, game_id: int) -> dict:
        """Settle every market a game's results can answer. Returns {"settled": n, "left": [titles]}."""
        mu = self.matchup(matchup_id)
        game = self.db.game(game_id)
        if not mu or not game:
            raise BookError("No such matchup or game.")
        if mu["status"] == "open":
            self.set_matchup_status(matchup_id, "locked")
        self.db.x("UPDATE book_matchups SET game_id = ? WHERE id = ?", (game_id, matchup_id))
        lines = {p["username_key"]: p for p in self.db.game_players(game_id)}
        scores = None
        first_home = self.assign_sides(mu, game, lines)
        if first_home is not None:
            # The lineups take the game's team names from here on
            t1, t2 = (game["home_team"], game["away_team"]) if first_home else (game["away_team"], game["home_team"])
            scores = (game["home_score"], game["away_score"]) if first_home else (game["away_score"], game["home_score"])
            with self.db.lock:
                c = self.db.conn
                c.execute("UPDATE book_matchups SET team1 = ?, team2 = ? WHERE id = ?", (t1, t2, matchup_id))
                for side, name in (("team1", t1), ("team2", t2)):
                    # "Ravens (Team Jacob)", so bettors still recognise the lineup they picked
                    c.execute("UPDATE book_selections SET label = ? || ' (' || label || ')' WHERE side = ? "
                              "AND label LIKE 'Team %' AND market_id IN (SELECT id FROM book_markets "
                              "WHERE matchup_id = ? AND kind = 'moneyline')", (name, side, matchup_id))
                c.commit()
            mu = self.matchup(matchup_id)
        settled, left = 0, []
        for m in self.markets(matchup_id):
            if m["status"] in ("settled", "void"):
                continue
            try:
                done = self._settle_one(m, game, game_id, lines, scores, mu)
            except Exception:  # one bad market mustn't stop the rest from paying out
                log.exception("Couldn't settle market %s from game %s", m["id"], game_id)
                done = False
            if done:
                settled += 1
            else:
                left.append(m["title"])
        return {"settled": settled, "left": left}

    def _settle_one(self, m: dict, game, game_id: int, lines: dict[str, dict], scores, mu: dict) -> bool:
        """Grade one market from a game. False if the game can't answer it (settle it by hand)."""
        sels = {s["side"]: s["id"] for s in m["selections"]}
        if m["kind"] in ("prop", "total"):
            if m["kind"] == "total":
                value, what = game["home_score"] + game["away_score"], "points"
            else:
                ln = lines.get(m["player_key"])
                if ln is None:
                    self._grade(m["id"], {sid: "void" for sid in sels.values()},
                                f"Void: didn't play in game #{game_id}, stakes returned", status="void")
                    return True
                value, what = round(stat_value(ln, m["stat"]), 2), stat_label(m["stat"])
            over = "win" if value > m["line"] else "lose" if value < m["line"] else "push"
            under = {"win": "lose", "lose": "win", "push": "push"}[over]
            self._grade(m["id"], {sels["over"]: over, sels["under"]: under},
                        f"{value:g} {what} in game #{game_id}")
            return True
        if m["kind"] == "moneyline" and not scores and game["home_score"] == game["away_score"]:
            # A tie is a push whichever side each lineup was on
            self._grade(m["id"], {sid: "push" for sid in sels.values()},
                        f"Tie {game['home_score']} – {game['away_score']} (game #{game_id}), stakes returned")
            return True
        if m["kind"] == "moneyline" and scores:
            s1, s2 = scores
            o1 = "win" if s1 > s2 else "lose" if s1 < s2 else "push"
            o2 = {"win": "lose", "lose": "win", "push": "push"}[o1]
            self._grade(m["id"], {sels["team1"]: o1, sels["team2"]: o2},
                        f"{mu['team1']} {s1} – {s2} {mu['team2']} (game #{game_id})")
            return True
        return False

    def on_game_saved(self, game_id: int) -> list[int]:
        """Auto-settle matchups a newly saved game belongs to: at least half of the matchup's players
        are in it, and it arrived after the matchup was created. Returns the matchup ids settled."""
        keys = {p["username_key"] for p in self.db.game_players(game_id)}
        done = []
        for mu in self.matchups(("open", "locked")):
            # A matchup already tied to this game is tried again: the game may have been saved first
            # without its stats CSV, or have markets left over from last time
            if not mu["auto_settle"] or mu["game_id"] not in (None, 0, game_id) or game_id <= mu["base_game_id"]:
                continue
            players = set(mu["players1"]) | set(mu["players2"])
            if players and len(players & keys) * 2 >= len(players):
                self.settle_from_game(mu["id"], game_id)
                done.append(mu["id"])
        return done
