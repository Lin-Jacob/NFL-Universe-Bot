"""Season awards: stat awards worked out from the numbers, and voted awards the players pick.

Stat awards (when a season ends): best at each position by the fantasy points earned there,
plus the top scorer overall. Voted awards (MVP, Most Improved and some fun ones): every league
player who played in the season votes, on Discord (/vote) or on the website (/vote). Nobody can
vote for themselves, and a vote counts more the more of the season's games the voter played
(analytics.vote_weight). Change the voted awards with SEASON_VOTE_AWARDS in config.py.
"""

from __future__ import annotations

import config

from . import analytics
from .db import Database

# key -> (title, icon). PTS = most fantasy points overall; the rest are position groups.
STAT_AWARDS: dict[str, tuple[str, str]] = {
    "PTS": ("Top Scorer", "🔥"),
    "QB": ("Best Quarterback", "🎯"),
    "RB": ("Best Running Back", "🏃"),
    "WR": ("Best Receiver", "🙌"),
    "DB": ("Best Defensive Back", "🛡️"),
    "DL": ("Best Defensive Lineman", "💥"),
    "ST": ("Best Special Teams", "🦶"),
}

DEFAULT_VOTE_AWARDS: dict[str, tuple[str, str]] = {
    "MVP": ("Most Valuable Player", "🏆"),
    "MIP": ("Most Improved Player", "📈"),
    "CLUTCH": ("Mr. Clutch", "🧊"),
    "TEAMMATE": ("Best Teammate", "🤝"),
    "HIGHLIGHT": ("Highlight Reel", "🎬"),
    "BALLHOG": ("Ball Hog", "🐷"),
    "TRASH": ("Trash Talker", "🗣️"),
}


def vote_awards() -> dict[str, tuple[str, str]]:
    """Voted awards: SEASON_VOTE_AWARDS in config.py ({"KEY": "Title"} or {"KEY": ("Title", "icon")})."""
    custom = getattr(config, "SEASON_VOTE_AWARDS", None)
    if not custom:
        return DEFAULT_VOTE_AWARDS
    return {str(k).upper(): (v if isinstance(v, (tuple, list)) else (str(v), "🏅")) for k, v in custom.items()}


class VoteError(Exception):
    """Why a vote wasn't accepted, worded for the voter."""


def voting_open(db: Database):
    """The season whose vote is open, or raise VoteError."""
    s = db.voting_season()
    if not s:
        raise VoteError("No vote is open right now. Voting opens when a season ends.")
    return s


def candidates(db: Database, season_id: int, exclude: str | None = None) -> list[tuple[str, str]]:
    """[(username_key, username)] who played in the season, minus `exclude` (the voter)."""
    _, played, names = analytics.season_roles_played(db, season_id)
    return sorted(((k, names[k]) for k in played if k != exclude), key=lambda kv: kv[1].lower())


def cast(db: Database, voter_key: str | None, award: str, candidate_key: str | None,
         voter_discord: int | None = None) -> dict:
    """Check and record one vote. Returns {season, award, title, candidate, weight, games, total, replaced}."""
    s = voting_open(db)
    awards = vote_awards()
    award = (award or "").upper()
    if award not in awards:
        raise VoteError("That isn't one of the awards.")
    if not voter_key:
        raise VoteError("Your account isn't linked to a league player, so you can't vote. "
                        "Ask an admin to link it (/admin setname with your Discord account).")
    weight, games, total = analytics.voter_weight(db, s["id"], voter_key)
    if not games:
        raise VoteError(f"Only players who played in {s['name']} can vote, and you didn't play a game in it.")
    if not candidate_key:
        raise VoteError("Couldn't find that player.")
    if candidate_key == voter_key:
        raise VoteError("You can't vote for yourself.")
    names = dict(candidates(db, s["id"]))
    if candidate_key not in names:
        raise VoteError(f"That player didn't play in {s['name']}.")
    replaced = db.cast_vote(s["id"], award, voter_key, candidate_key, weight, voter_discord)
    return {"season": s, "award": award, "title": awards[award][0], "candidate": names[candidate_key],
            "weight": weight, "games": games, "total": total, "replaced": replaced}


def my_votes(db: Database, season_id: int, voter_key: str) -> dict[str, str]:
    """{award: candidate_key} the voter has cast this season."""
    return {r["role"]: r["candidate_key"] for r in db.season_votes(season_id) if r["voter_key"] == voter_key}
