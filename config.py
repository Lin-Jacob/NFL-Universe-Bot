"""
Bot settings you are expected to edit by hand.

Secrets (bot token, webhook secret, ngrok token) live in .env, not here.
"""

# ---------------------------------------------------------------------------
# Admins
# ---------------------------------------------------------------------------
# Discord user IDs allowed to run /admin commands.
# To get an ID: Discord Settings > Advanced > Developer Mode ON,
# then right-click a user > "Copy User ID".
ADMIN_IDS: list[int] = [
    123456789012345678,  # <- replace with your own Discord ID
]

# ---------------------------------------------------------------------------
# Fantasy scoring
# ---------------------------------------------------------------------------
# Points awarded per unit of each stat. Stats not listed score 0.
# Defaults: PPR offense + IDP (individual defensive player) scoring,
# because NFL Universe games have every player on the field.
FANTASY_SCORING: dict[str, float] = {
    # Passing
    "pass_yards": 0.04,        # 1 point per 25 yards
    "pass_tds": 4,
    "interceptions": -2,       # interceptions thrown
    "pass_2pt": 2,
    # Rushing
    "rush_yards": 0.1,         # 1 point per 10 yards
    "rush_tds": 6,
    "rush_2pt": 2,
    # Receiving
    "receptions": 1,           # PPR. Set to 0.5 for half-PPR or 0 for standard.
    "rec_yards": 0.1,
    "rec_tds": 6,
    "rec_2pt": 2,
    # Ball security
    "fumbles_lost": -2,
    # Defense (IDP)
    "tackles": 1,
    "sacks": 2,
    "def_ints": 3,
    "pass_deflections": 1,
    "forced_fumbles": 2,
    "fumble_recoveries": 2,
    "def_tds": 6,
    "safeties": 2,
    # Special teams
    "return_tds": 6,
    "fg_made": 3,
    "fg_missed": -1,
    "xp_made": 1,
    "xp_missed": -1,
}

# Bonus points when a single-game stat reaches a threshold.
FANTASY_BONUSES: list[tuple[str, float, float]] = [
    # (stat, threshold, bonus points)
    ("pass_yards", 300, 3),
    ("rush_yards", 100, 3),
    ("rec_yards", 100, 3),
]

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
# Season used when a webhook payload does not say which season it is.
# Admins can change it at runtime with /admin season.
DEFAULT_SEASON: int = 1

# Color of the bot's embeds.
EMBED_COLOR: int = 0x013369
