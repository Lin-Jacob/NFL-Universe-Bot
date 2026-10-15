"""Canonical stat names, display labels, and the aliases the parser accepts."""

# canonical name -> (label, category)
STATS: dict[str, tuple[str, str]] = {
    "pass_completions": ("Completions", "passing"),
    "pass_attempts": ("Pass Attempts", "passing"),
    "pass_yards": ("Passing Yards", "passing"),
    "pass_tds": ("Passing TDs", "passing"),
    "interceptions": ("Interceptions Thrown", "passing"),
    "sacks_taken": ("Sacks Taken", "passing"),
    "pass_2pt": ("Passing 2PT", "passing"),
    "rush_attempts": ("Carries", "rushing"),
    "rush_yards": ("Rushing Yards", "rushing"),
    "rush_tds": ("Rushing TDs", "rushing"),
    "rush_2pt": ("Rushing 2PT", "rushing"),
    "targets": ("Targets", "receiving"),
    "receptions": ("Receptions", "receiving"),
    "rec_yards": ("Receiving Yards", "receiving"),
    "rec_tds": ("Receiving TDs", "receiving"),
    "rec_2pt": ("Receiving 2PT", "receiving"),
    "fumbles": ("Fumbles", "ball security"),
    "fumbles_lost": ("Fumbles Lost", "ball security"),
    "tackles": ("Tackles", "defense"),
    "sacks": ("Sacks", "defense"),
    "def_ints": ("Interceptions", "defense"),
    "pass_deflections": ("Pass Deflections", "defense"),
    "forced_fumbles": ("Forced Fumbles", "defense"),
    "fumble_recoveries": ("Fumble Recoveries", "defense"),
    "def_tds": ("Defensive TDs", "defense"),
    "safeties": ("Safeties", "defense"),
    "return_yards": ("Return Yards", "special teams"),
    "return_tds": ("Return TDs", "special teams"),
    "fg_made": ("FG Made", "kicking"),
    "fg_missed": ("FG Missed", "kicking"),
    "xp_made": ("XP Made", "kicking"),
    "xp_missed": ("XP Missed", "kicking"),
    "punts": ("Punts", "kicking"),
}

CATEGORY_ORDER = ["passing", "rushing", "receiving", "ball security", "defense",
                  "special teams", "kicking"]


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
