"""Recognizing NFL Universe's game posts in the feed channel.

Each game arrives as two messages from the NFL Universe webhook: first a lone
.csv attachment with every player's stats, then an embed titled
"<team> vs <team>" with a "Scoring History" table.
"""

import discord

import config


def is_nflu_csv(message: discord.Message) -> bool:
    return (not message.embeds and
            any(a.filename.lower().endswith(".csv") and a.size < 2 * 1024 * 1024 for a in message.attachments))


def nflu_server_id(embed: dict) -> str | None:
    """The 'Server Id' field of the game's score embed (e.g. 'EBA64F')."""
    for f in embed.get("fields") or []:
        if str(f.get("name", "")).strip().lower() == "server id":
            return str(f.get("value", "")).strip().strip("`") or None
    return None


def server_allowed(server_id: str | None) -> bool:
    """True if config.NFLU_SERVER_IDS is empty or contains this server ID (case-insensitive)."""
    allowed = {s.strip().upper() for s in config.NFLU_SERVER_IDS}
    return not allowed or (server_id or "").upper() in allowed


PAIR_WINDOW = 30  # seconds: a game's CSV and score message are posted within this of each other


def nearest_csv(score: discord.Message, candidates: list[discord.Message]) -> discord.Message | None:
    """The CSV message from the same sender posted closest to this score message, before *or* after it.
    (The game usually sends the CSV first, but not always: both arrive in the same second.)"""
    best = None
    for m in candidates:
        if m.id == score.id or m.author.id != score.author.id or not is_nflu_csv(m):
            continue
        gap = abs((m.created_at - score.created_at).total_seconds())
        if gap <= PAIR_WINDOW and (best is None or gap < best[0]):
            best = (gap, m)
    return best[1] if best else None


def nflu_embed(message: discord.Message) -> dict | None:
    """The game's score embed as a dict, if this message is one."""
    for e in message.embeds:
        if " vs " in (e.title or "") and any(f.name == "Scoring History" for f in e.fields):
            return e.to_dict()
    return None
