"""
The league's players from config.PLAYER_NAMES, in either format:

    PLAYER_NAMES = {"Jacob": "roblox_user_759822057"}                       # older: name -> Roblox username
    PLAYER_NAMES = {"Jacob": {"roblox_username": "roblox_user_759822057",   # newer, with a sportsbook
                              "password": ""}}                              # password

A player's sportsbook password is the "password" set there, or, when that's empty, their Discord
ID (once an admin has linked it with /admin setname). /password shows a player their own.
"""

from __future__ import annotations

from typing import NamedTuple

import config

from .db import Database


class Player(NamedTuple):
    name: str             # real name, e.g. "Jacob"
    roblox_username: str
    password: str         # from config.py; "" = use their Discord ID


def players() -> list[Player]:
    out = []
    for name, v in (getattr(config, "PLAYER_NAMES", None) or {}).items():
        if isinstance(v, dict):
            ign, pw = v.get("roblox_username") or "", v.get("password")
        else:
            ign, pw = v, ""
        if str(name).strip() and str(ign or "").strip():
            out.append(Player(str(name).strip(), str(ign).strip(), str(pw if pw is not None else "").strip()))
    return out


def find(name: str) -> Player | None:
    key = " ".join(str(name).split()).lower()
    return next((p for p in players() if p.name.lower() == key), None)


def book_password(db: Database, p: Player) -> str | None:
    """The player's sportsbook password: the one in config.py, else their linked Discord ID, else None."""
    if p.password:
        return p.password
    row = db.one("SELECT discord_id FROM names WHERE username_key = ?", (p.roblox_username.lower(),))
    return str(row["discord_id"]) if row and row["discord_id"] else None


def for_discord(db: Database, discord_id: int) -> Player | None:
    """The player a Discord account belongs to: linked by /admin setname, or with that ID as their password."""
    row = db.name_for_discord(discord_id)
    for p in players():
        if p.password == str(discord_id) or (row and p.roblox_username.lower() == row["username_key"]):
            return p
    return None
