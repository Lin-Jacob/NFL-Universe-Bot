"""Recognizing NFL Universe's game posts in the feed channel.

Each game arrives as two messages from the NFL Universe webhook: first a lone
.csv attachment with every player's stats, then an embed titled
"<team> vs <team>" with a "Scoring History" table.
"""

import discord


def is_nflu_csv(message: discord.Message) -> bool:
    return (not message.embeds and
            any(a.filename.lower().endswith(".csv") and a.size < 2 * 1024 * 1024 for a in message.attachments))


def nflu_embed(message: discord.Message) -> dict | None:
    """The game's score embed as a dict, if this message is one."""
    for e in message.embeds:
        if " vs " in (e.title or "") and any(f.name == "Scoring History" for f in e.fields):
            return e.to_dict()
    return None
