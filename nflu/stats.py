"""Canonical stat names, display labels, and the aliases the parser accepts."""

# canonical name -> (label, category)
STATS: dict[str, tuple[str, str]] = {
    # Passing (QB)
    "pass_completions": ("Completions", "passing"),
    "pass_attempts": ("Pass Attempts", "passing"),
    "pass_yards": ("Passing Yards", "passing"),
    "pass_tds": ("Passing TDs", "passing"),
    "interceptions": ("Interceptions Thrown", "passing"),
    "int_charged": ("INTs Charged", "passing"),  # computed: INTs this player loses points for
    "pick_sixes_thrown": ("Pick-Sixes Thrown", "passing"),
    "sacks_taken": ("Sacks Taken", "passing"),
    "longest_pass": ("Longest Pass", "passing"),
    "pass_att_lt15": ("Attempts 0-15 yd", "passing"),
    "pass_cmp_lt15": ("Completions 0-15 yd", "passing"),
    "pass_att_lt35": ("Attempts 15-35 yd", "passing"),
    "pass_cmp_lt35": ("Completions 15-35 yd", "passing"),
    "pass_att_lt50": ("Attempts 35-50 yd", "passing"),
    "pass_cmp_lt50": ("Completions 35-50 yd", "passing"),
    "pass_att_gt50": ("Attempts 50+ yd", "passing"),
    "pass_cmp_gt50": ("Completions 50+ yd", "passing"),
    # Rushing (RB)
    "rush_attempts": ("Carries", "rushing"),
    "rush_yards": ("Rushing Yards", "rushing"),
    "rush_tds": ("Rushing TDs", "rushing"),
    "rush_20plus": ("Carries 20+ yd", "rushing"),
    "rush_first_downs": ("Rushing First Downs", "rushing"),
    "longest_rush": ("Longest Rush", "rushing"),
    # Receiving (WR)
    "targets": ("Targets", "receiving"),
    "receptions": ("Receptions", "receiving"),
    "rec_yards": ("Receiving Yards", "receiving"),
    "rec_tds": ("Receiving TDs", "receiving"),
    "yac": ("Yards After Catch", "receiving"),
    "rec_first_downs": ("Receiving First Downs", "receiving"),
    "catches_in_traffic": ("Catches In Traffic", "receiving"),
    "drops": ("Drops", "receiving"),
    "ints_allowed": ("INTs Allowed (receiver)", "receiving"),
    "toss_ups": ("Toss Ups", "receiving"),
    "longest_rec": ("Longest Reception", "receiving"),
    "rec_lt15": ("Catches 0-15 yd", "receiving"),
    "rec_lt35": ("Catches 15-35 yd", "receiving"),
    "rec_lt50": ("Catches 35-50 yd", "receiving"),
    "rec_gt50": ("Catches 50+ yd", "receiving"),
    # Shared offense
    "third_down_conv": ("3rd Down Conversions", "offense"),
    "fourth_down_conv": ("4th Down Conversions", "offense"),
    "fumbles": ("Fumbles", "offense"),
    # Defense (DB + DL)
    "tackles": ("Tackles", "defense"),
    "tfl": ("Tackles For Loss", "defense"),
    "tfl_yards": ("TFL Yards", "defense"),
    "sacks": ("Sacks", "defense"),
    "sack_yards": ("Sack Yards", "defense"),
    "qb_hits": ("QB Hits", "defense"),
    "qb_pressures": ("QB Pressures", "defense"),
    "pass_rushes": ("Pass Rushes", "defense"),
    "def_ints": ("Interceptions", "defense"),
    "ints_in_traffic": ("INTs In Traffic", "defense"),
    "int_return_yards": ("INT Return Yards", "defense"),
    "longest_int_return": ("Longest INT Return", "defense"),
    "pass_deflections": ("Pass Deflections", "defense"),
    "forced_fumbles": ("Forced Fumbles", "defense"),
    "fumble_recoveries": ("Fumble Recoveries", "defense"),
    "fumble_return_yards": ("Fumble Return Yards", "defense"),
    "def_tds": ("Defensive TDs", "defense"),
    "safeties": ("Safeties", "defense"),
    "completions_allowed": ("Completions Allowed", "defense"),
    "targets_allowed": ("Targets Allowed", "defense"),
    "pass_yards_allowed": ("Pass Yards Allowed", "defense"),
    "pass_tds_allowed": ("Pass TDs Allowed", "defense"),
    "toss_ups_def": ("Toss Ups (defense)", "defense"),
    # Special teams (ST)
    "fg_made": ("FG Made", "special teams"),
    "fg_missed": ("FG Missed", "special teams"),
    "fg_yards": ("FG Yards", "special teams"),
    "longest_fg": ("Longest FG", "special teams"),
    "kick_returns": ("Kick Returns", "special teams"),
    "kick_return_yards": ("Kick Return Yards", "special teams"),
    "kick_return_tds": ("Kick Return TDs", "special teams"),
    "punt_returns": ("Punt Returns", "special teams"),
    "punt_return_yards": ("Punt Return Yards", "special teams"),
    "punt_return_tds": ("Punt Return TDs", "special teams"),
    "punt_blocks": ("Punt Blocks", "special teams"),
    "kickoffs": ("Kickoffs", "special teams"),
    "touchbacks": ("Touchbacks", "special teams"),
    "punts": ("Punts", "special teams"),
    "punt_yards": ("Punt Yards", "special teams"),
    "punts_inside_10": ("Punts Inside 10", "special teams"),
    "longest_punt": ("Longest Punt", "special teams"),
    "onside_recoveries": ("Onside Recoveries", "special teams"),
    "onside_recovery_yards": ("Onside Recovery Yards", "special teams"),
    "snaps": ("Snaps", "other"),
}

# Always shown in /player and /compare; other stats show once they're worth points in config.py
CORE_DISPLAY = {
    "pass_completions", "pass_attempts", "pass_yards", "pass_tds", "interceptions", "sacks_taken",
    "rush_attempts", "rush_yards", "rush_tds", "targets", "receptions", "rec_yards", "rec_tds", "fumbles",
    "tackles", "sacks", "def_ints", "pass_deflections", "forced_fumbles", "fumble_recoveries", "def_tds",
    "safeties", "fg_made", "fg_missed", "kick_return_tds", "punt_return_tds", "snaps",
}

# Longest-play stats combine across games by taking the best, not by adding up
MAX_STATS = {k for k in STATS if k.startswith("longest_")}

# Positions for /stats: code -> (section title, CSV section it comes from)
POSITIONS: dict[str, tuple[str, str]] = {
    "QB": ("Quarterbacks", "Passing"),
    "RB": ("Running backs", "Rushing"),
    "WR": ("Receivers", "Receiving"),
    "DB": ("Defensive backs", "Cornerback"),
    "DL": ("Defensive line", "DLine"),
    "ST": ("Special teams", "SpecialTeams"),
}

CATEGORY_ORDER = ["passing", "rushing", "receiving", "offense", "defense", "special teams"]


def _norm(key: str) -> str:
    """Lowercase and strip everything but letters/digits so 'Pass Yds' == 'passyds'."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


# Flat aliases: normalized key -> canonical stat.
_FLAT_ALIASES: dict[str, list[str]] = {
    "pass_completions": ["completions", "comp", "cmp", "passcompletions", "passcomp", "completed"],
    "pass_attempts": ["passattempts", "passatt", "att", "attempts", "passes", "throws"],
    "pass_yards": ["passyards", "passyds", "passingyards", "passingyds", "pyds", "yardspassing"],
    "pass_tds": ["passtds", "passtd", "passingtds", "passingtouchdowns", "passtouchdowns", "ptd", "tdpass"],
    "interceptions": ["interceptions", "ints", "int", "interceptionsthrown", "intsthrown", "passints", "passint", "picks"],
    "sacks_taken": ["sackstaken", "timessacked", "sacked"],
    "pass_2pt": ["pass2pt", "passing2pt", "pass2pts"],
    "rush_attempts": ["rushattempts", "rushatt", "carries", "rushes", "car"],
    "rush_yards": ["rushyards", "rushyds", "rushingyards", "rushingyds", "ryds"],
    "rush_tds": ["rushtds", "rushtd", "rushingtds", "rushingtouchdowns", "rushtouchdowns"],
    "rush_2pt": ["rush2pt", "rushing2pt"],
    "targets": ["targets", "tgts", "tgt"],
    "receptions": ["receptions", "rec", "catches", "recs"],
    "rec_yards": ["recyards", "recyds", "receivingyards", "receivingyds", "catchyards"],
    "rec_tds": ["rectds", "rectd", "receivingtds", "receivingtouchdowns", "rectouchdowns"],
    "rec_2pt": ["rec2pt", "receiving2pt"],
    "fumbles": ["fumbles", "fum"],
    "fumbles_lost": ["fumbleslost", "fumlost", "lostfumbles", "fl"],
    "tackles": ["tackles", "tkl", "tck", "tkls", "totaltackles"],
    "sacks": ["sacks", "sck", "defsacks"],
    "def_ints": ["defints", "defint", "definterceptions", "interceptionscaught", "intscaught", "intcaught", "defensiveinterceptions"],
    "pass_deflections": ["passdeflections", "deflections", "pd", "pbu", "passesdefended", "swats", "breakups"],
    "forced_fumbles": ["forcedfumbles", "ff", "fumblesforced"],
    "fumble_recoveries": ["fumblerecoveries", "fr", "fumblesrecovered", "recoveries"],
    "def_tds": ["deftds", "deftd", "defensivetds", "defensivetouchdowns", "pick6", "pick6s"],
    "safeties": ["safeties", "safety", "sfty"],
    "return_yards": ["returnyards", "returnyds", "kryds", "pryds", "retyds"],
    "return_tds": ["returntds", "returntd", "krtd", "prtd", "rettd"],
    "fg_made": ["fgmade", "fgm", "fieldgoalsmade", "fieldgoals", "fg"],
    "fg_missed": ["fgmissed", "fgmiss", "fieldgoalsmissed"],
    "xp_made": ["xpmade", "xpm", "extrapointsmade", "pat", "patmade", "xp"],
    "xp_missed": ["xpmissed", "xpmiss", "patmissed"],
    "punts": ["punts"],
}

# Nested aliases: e.g. {"passing": {"yards": 200}} -> pass_yards
_CATEGORY_ALIASES = {
    "passing": "pass", "pass": "pass", "qb": "pass", "throwing": "pass",
    "rushing": "rush", "rush": "rush", "running": "rush",
    "receiving": "rec", "rec": "rec", "catching": "rec", "receiver": "rec",
    "defense": "def", "defence": "def", "def": "def", "defensive": "def",
    "kicking": "kick", "kick": "kick", "kicker": "kick",
    "returns": "ret", "return": "ret", "specialteams": "ret",
}
_NESTED: dict[tuple[str, str], str] = {
    ("pass", "yards"): "pass_yards", ("pass", "yds"): "pass_yards",
    ("pass", "tds"): "pass_tds", ("pass", "td"): "pass_tds", ("pass", "touchdowns"): "pass_tds",
    ("pass", "ints"): "interceptions", ("pass", "int"): "interceptions", ("pass", "interceptions"): "interceptions",
    ("pass", "completions"): "pass_completions", ("pass", "comp"): "pass_completions", ("pass", "cmp"): "pass_completions",
    ("pass", "attempts"): "pass_attempts", ("pass", "att"): "pass_attempts",
    ("pass", "sacked"): "sacks_taken", ("pass", "sacks"): "sacks_taken",
    ("rush", "yards"): "rush_yards", ("rush", "yds"): "rush_yards",
    ("rush", "tds"): "rush_tds", ("rush", "td"): "rush_tds", ("rush", "touchdowns"): "rush_tds",
    ("rush", "attempts"): "rush_attempts", ("rush", "att"): "rush_attempts", ("rush", "carries"): "rush_attempts",
    ("rec", "yards"): "rec_yards", ("rec", "yds"): "rec_yards",
    ("rec", "tds"): "rec_tds", ("rec", "td"): "rec_tds", ("rec", "touchdowns"): "rec_tds",
    ("rec", "receptions"): "receptions", ("rec", "catches"): "receptions", ("rec", "rec"): "receptions",
    ("rec", "targets"): "targets",
    ("def", "tackles"): "tackles", ("def", "sacks"): "sacks",
    ("def", "ints"): "def_ints", ("def", "int"): "def_ints", ("def", "interceptions"): "def_ints",
    ("def", "tds"): "def_tds", ("def", "td"): "def_tds", ("def", "touchdowns"): "def_tds",
    ("def", "deflections"): "pass_deflections", ("def", "pd"): "pass_deflections",
    ("def", "forcedfumbles"): "forced_fumbles", ("def", "ff"): "forced_fumbles",
    ("def", "fumblerecoveries"): "fumble_recoveries", ("def", "fr"): "fumble_recoveries",
    ("def", "safeties"): "safeties",
    ("kick", "fgmade"): "fg_made", ("kick", "fgm"): "fg_made", ("kick", "fgmissed"): "fg_missed",
    ("kick", "xpmade"): "xp_made", ("kick", "xpmissed"): "xp_missed", ("kick", "punts"): "punts",
    ("ret", "yards"): "return_yards", ("ret", "yds"): "return_yards",
    ("ret", "tds"): "return_tds", ("ret", "td"): "return_tds",
}

ALIASES: dict[str, str] = {}
for canon, names in _FLAT_ALIASES.items():
    ALIASES[_norm(canon)] = canon
    for n in names:
        ALIASES[_norm(n)] = canon


def canonical_stat(key: str, category: str | None = None) -> str | None:
    """Map a raw stat key (optionally inside a category object) to a canonical stat name."""
    k = _norm(key)
    if category:
        cat = _CATEGORY_ALIASES.get(_norm(category))
        if cat and (cat, k) in _NESTED:
            return _NESTED[(cat, k)]
        if cat:
            # e.g. category "passing" + key "passing_yards"
            hit = ALIASES.get(cat + k) or ALIASES.get(k)
            return hit
    return ALIASES.get(k)


def is_category(key: str) -> bool:
    return _norm(key) in _CATEGORY_ALIASES


def label(stat: str) -> str:
    return STATS.get(stat, (stat.replace("_", " ").title(), "other"))[0]


def stat_choices() -> list[str]:
    return list(STATS.keys())
