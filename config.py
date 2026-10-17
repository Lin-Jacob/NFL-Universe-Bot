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
    532388408945803275,  # Jacob
    198569752388567041, # DJ
    301482258307743745, # Chris
]

# Master admins: the only ones who can change game data by hand with
# /admin addgame, editgame, deletegame, setstat and removeplayer.
MASTER_ADMIN_IDS: list[int] = [
    532388408945803275,  # Jacob
]

# ---------------------------------------------------------------------------
# Fantasy scoring
# ---------------------------------------------------------------------------
# Points per unit of each stat, separately for each position group. A stat counts
# toward the group whose section of NFL Universe's stat sheet it came from:
#   QB = Passing, RB = Rushing, WR = Receiving, DB = Cornerback, DL = DLine,
#   ST = Special Teams.
# So the same stat can be worth different points by group (e.g. make DL tackles
# worth more than DB tackles). Every stat on the sheet is listed; 0 = not scored.
# Negative numbers subtract points. A negative stat value (e.g. -3 rushing yards)
# never costs points unless its weight is negative.
# The sheet has no extra points or 2-point conversions, so those can't be scored.
FANTASY_SCORING: dict[str, dict[str, float]] = {
    "QB": {
        "pass_yards": 0.04,          # 1 point per 25 yards
        "pass_tds": 4,
        # Interceptions thrown that the QB is charged for. Every INT is charged to
        # the QB unless an admin moves it to the player at fault or excuses it
        # (/admin intfault). Keep it negative so it subtracts.
        "int_charged": -2,
        "interceptions": 0,          # raw INTs thrown (use int_charged for the penalty)
        "pick_sixes_thrown": 0,
        "pass_completions": 0,
        "pass_attempts": 0,
        "sacks_taken": 0,
        "fumbles": 0,
        "third_down_conv": 0,
        "fourth_down_conv": 0,
        "longest_pass": 0,
        "pass_att_lt15": 0,          # attempts 0-15 yards downfield
        "pass_cmp_lt15": 0,
        "pass_att_lt35": 0,          # 15-35 yards
        "pass_cmp_lt35": 0,
        "pass_att_lt50": 0,          # 35-50 yards
        "pass_cmp_lt50": 0,
        "pass_att_gt50": 0,          # 50+ yards
        "pass_cmp_gt50": 0,
        "snaps": 0,
    },
    "RB": {
        "rush_yards": 0.1,           # 1 point per 10 yards
        "rush_tds": 6,
        "rush_attempts": 0,
        "rush_20plus": 0,            # carries over 20 yards
        "rush_first_downs": 0,
        "third_down_conv": 0,
        "fourth_down_conv": 0,
        "fumbles": 0,
        "longest_rush": 0,
        "snaps": 0,
    },
    "WR": {
        "receptions": 1,             # PPR. 0.5 for half-PPR, 0 for standard.
        "rec_yards": 0.1,
        "rec_tds": 6,
        # Interceptions an admin ruled were this receiver's fault (/admin intfault)
        "int_charged": -2,
        "targets": 0,
        "yac": 0,                    # yards after catch
        "rec_first_downs": 0,
        "catches_in_traffic": 0,
        "drops": 0,
        "ints_allowed": 0,           # the game's own count of picks on this receiver
        "toss_ups": 0,
        "fumbles": 0,
        "third_down_conv": 0,
        "fourth_down_conv": 0,
        "longest_rec": 0,
        "rec_lt15": 0,               # catches 0-15 yards
        "rec_lt35": 0,               # 15-35
        "rec_lt50": 0,               # 35-50
        "rec_gt50": 0,               # 50+
        "snaps": 0,
    },
    "DB": {
        "tackles": 1,
        "def_ints": 3,
        "pass_deflections": 1,       # pass break-ups
        "def_tds": 6,                # pick-sixes
        "tfl": 0,                    # tackles for loss
        "tfl_yards": 0,
        "ints_in_traffic": 0,
        "int_return_yards": 0,
        "longest_int_return": 0,
        "completions_allowed": 0,
        "targets_allowed": 0,
        "pass_yards_allowed": 0,
        "pass_tds_allowed": 0,
        "toss_ups_def": 0,
        "snaps": 0,
    },
    "DL": {
        "tackles": 1,
        "sacks": 2,
        "pass_deflections": 1,       # swats
        "forced_fumbles": 2,
        "fumble_recoveries": 2,
        "def_tds": 6,                # fumble return TDs
        "safeties": 2,
        "tfl": 0,
        "tfl_yards": 0,
        "pass_rushes": 0,
        "sack_yards": 0,
        "qb_hits": 0,
        "qb_pressures": 0,
        "fumble_return_yards": 0,
        "snaps": 0,
    },
    "ST": {
        "fg_made": 0.5,
        "fg_missed": -3,             # missed field goal
        "kick_return_tds": 6,
        "punt_return_tds": 6,
        "fg_yards": 0,
        "longest_fg": 0,
        "kick_returns": 0,
        "kick_return_yards": 0,
        "punt_returns": 0,
        "punt_return_yards": 0,
        "punt_blocks": 0,
        "kickoffs": 0,
        "touchbacks": 0,
        "punts": 0,
        "punt_yards": 0,
        "punts_inside_10": 0,
        "longest_punt": 0,
        "onside_recoveries": 0,
        "onside_recovery_yards": 0,
        "snaps": 0,
    },
}

# /fantasy ranks by points per snap, so players who joined a game late (fewer
# snaps) aren't penalized. Shown as points per FANTASY_RATE_SNAPS snaps; a full
# game is roughly 130-145 snaps, so per 100 reads like a game's score.
FANTASY_RATE_SNAPS: int = 100
# Players with fewer total snaps than this are listed under "not enough snaps
# yet" instead of ranked, so a few lucky snaps can't top the board.
FANTASY_MIN_SNAPS: int = 0

# Bonus points when a single-game stat reaches a threshold.
FANTASY_BONUSES: list[tuple[str, float, float]] = [
    # (stat, threshold, bonus points)
    ("pass_yards", 300, 3),
    ("rush_yards", 100, 3),
    ("rec_yards", 100, 3),
]

# ---------------------------------------------------------------------------
# Player names
# ---------------------------------------------------------------------------
# Real name -> Roblox username (IGN). The bot shows the real name everywhere
# and accepts either one in commands like /player. Applied when the bot starts
# (or on /admin recalc). Overrides the same IGN set with /admin setname.
PLAYER_NAMES: dict[str, str] = {
    "Jacob": "roblox_user_759822057",
}

# ---------------------------------------------------------------------------
# NFL Universe feed
# ---------------------------------------------------------------------------
# Discord ID of the "NFL Universe Football" webhook that posts game results.
# /admin backfill imports every message this ID posted in the feed channel.
# If you delete and recreate the webhook, its ID changes: right-click the
# author name on a game post > "Copy User ID" and paste it here.
NFLU_WEBHOOK_ID: int = 1560835578579394611

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
# Color of the bot's embeds.
EMBED_COLOR: int = 0x013369
