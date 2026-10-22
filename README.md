# NFL Universe League Bot

A Discord bot for a Roblox **NFL Universe Football** league. It receives game results from the NFL Universe webhook, stores every game and stat line in a local SQLite database, scores fantasy points, maps Roblox usernames to real names, and gives your server slash commands for scores, player stats and leaderboards.

It runs entirely on your own computer. You do **not** need a domain: when the bot starts it can open a free tunnel so the game can reach it.

---

## 1. Quick start

1. **Install Python 3.10+** (python.org; on Windows tick "Add Python to PATH").
2. **Install the dependencies** in this folder:
   ```bash
   python -m venv .venv
   # Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Create the Discord bot** at <https://discord.com/developers/applications>:
   - New Application → **Bot** → *Reset Token* → copy the token.
   - **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions *Send Messages*, *Embed Links*, *Attach Files*, *Add Reactions*, *Read Message History*. Open the URL and add the bot to your server.
4. **Copy `.env.example` to `.env`** and fill in:
   - `DISCORD_TOKEN`: the token from step 3.
   - `GUILD_ID`: your server ID (turn on Developer Mode, right-click the server → Copy Server ID). This makes slash commands show up instantly.
   - The bot only re-registers slash commands with Discord when they've changed, because Discord limits how often that can happen. If commands ever go missing, set `FORCE_COMMAND_SYNC=1`, restart once, then remove it.
   - `WEBHOOK_SECRET`: a long random string. Generate one with
     `python -c "import secrets; print(secrets.token_urlsafe(24))"`
5. **Add yourself as an admin** in `config.py`:
   ```python
   ADMIN_IDS: list[int] = [
       123456789012345678,  # you
       234567890123456789,  # a co-commissioner
   ]
   ```
   (Right-click your name in Discord → Copy User ID.)
6. **Install a tunnel program** (see section 2), then run:
   ```bash
   python bot.py
   ```
   That one command starts the bot **and** the stats website (section 2b). Or just double-click `start.bat` (Windows) / run `./start.sh` (macOS/Linux): on the first run it also sets up `.venv` and installs the requirements.
   The console prints the public webhook URL (set `DM_ADMINS_WEBHOOK_URL=true` in `.env` to also have the bot DM it to every admin on startup). `/admin webhookurl` shows it again any time.
7. **Paste that URL into NFL Universe's webhook setting**, then pick a channel for results with `/admin channel`.

To test without playing a game: `python scripts/send_test_webhook.py` posts `samples/sample_game.json` to your running bot.

---

## 2. Hosting the webhook on your own computer (no domain)

The bot runs a small web server on `WEBHOOK_PORT` (default 8080). The game needs a public HTTPS address to reach it, and `TUNNEL` in `.env` decides how you get one. The bot starts and stops the tunnel itself, so it is only online while the bot is running.

| `TUNNEL=` | Account needed | URL stays the same? | Setup |
|---|---|---|---|
| `cloudflared` (default) | No | **No**, new URL every restart | Install cloudflared |
| `ngrok` | Free ngrok account | **Yes**, with your free static domain | Install ngrok, set `NGROK_AUTHTOKEN` and `NGROK_DOMAIN` |
| `none` | – | Yes | Forward the port on your router yourself, set `PUBLIC_URL` |

**Recommendation:** start with `cloudflared` to try it out. If re-pasting the URL into the game after every restart gets annoying, switch to `ngrok`, whose free plan includes one permanent domain.

### cloudflared
- Windows: `winget install --id Cloudflare.cloudflared`
- macOS: `brew install cloudflared`
- Linux: download from <https://github.com/cloudflare/cloudflared/releases>

If it isn't on your PATH, set `CLOUDFLARED_PATH` to the full path of the program.

### ngrok
1. Sign up at <https://ngrok.com> (free), install ngrok (`winget install ngrok.ngrok` / `brew install ngrok`).
2. Dashboard → *Your Authtoken* → put it in `NGROK_AUTHTOKEN`.
3. Dashboard → *Domains* → claim your free static domain (something like `yourname.ngrok-free.app`) → put it in `NGROK_DOMAIN`.
4. `TUNNEL=ngrok`. Your webhook URL is now `https://yourname.ngrok-free.app/webhook/<WEBHOOK_SECRET>` forever.

The bot uses ngrok v3's `--url` flag. If you have an old ngrok that doesn't know it, run `ngrok update`.

### Security
- The webhook only accepts posts that carry `WEBHOOK_SECRET`, either at the end of the URL (`/webhook/<secret>`), in an `X-Webhook-Secret` header, or as `?secret=`. Anything else gets `401`.
- Treat the full webhook URL like a password. If it leaks, change `WEBHOOK_SECRET` and restart.
- `.env` and the `data/` folder are git-ignored. Don't share them.

### Option B: the game only accepts a Discord webhook URL
Some Roblox games only let you paste a `https://discord.com/api/webhooks/...` link. If NFL Universe is like that:
1. In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy URL. Paste **that** into the game.
2. Put that channel's ID in `FEED_CHANNEL_ID` in `.env`.
3. Developer Portal → your app → Bot → turn on **Message Content Intent**.

The bot then reads the posts from that webhook (set its ID as `NFLU_WEBHOOK_ID` in `config.py`), parses the scores and stats from the message, saves the game, and reacts ✅. You don't need a tunnel in this mode (`TUNNEL=none`).

NFL Universe posts each game as two messages: a `.csv` file with every player's stats, then a "Team A vs Team B" embed with the score. The bot pairs them up. The CSV doesn't say which team a player was on, so player stats are saved without a team. Games posted while the bot was offline can be imported with `/admin backfill`.

Only games from your own NFL Universe servers are saved. The score embed shows a **Server Id** (for example `EBA64F`). List the ones that count in `NFLU_SERVER_IDS` in `config.py`, and games from any other server (test or pickup games) are ignored. You can list as many servers as you like, and case doesn't matter. An empty list accepts every server. Ignored games aren't stored, so if you add a server later, `/admin backfill` can still import its past games. `/admin test` shows the latest game's Server Id and whether it's allowed.

## 2b. Stats website

The bot also serves a read-only stats website from the same web server, so it runs whenever the bot runs. No extra setup: start the bot and open it.

| Who | Address |
|---|---|
| You, on the computer running the bot | `http://localhost:8080/` |
| People on the same Wi-Fi / network | `http://<that computer's local IP>:8080/` (find the IP with `ipconfig` on Windows, `ipconfig getifaddr en0` on macOS). Allow Python through the firewall if Windows asks |
| Anyone on the internet | The tunnel address **without** `/webhook/...`, e.g. `https://yourname.ngrok-free.app/`. `/website` in Discord posts it |

Pages: home (latest games, fantasy and position leaders), every game with its box score and each player's points by position, every player with career totals and a game log showing how each game's points were scored, the fantasy board (points per 100 snaps), QB/RB/WR/DB/DL/ST rankings, leaders in any stat, and the scoring rules. Real names from the name mapping are shown everywhere, and times show in each visitor's own time zone. It works on phones and follows the visitor's light/dark setting.

The site can only show data, never change it, and never shows the webhook secret or raw webhook payloads, so the base URL is safe to share. Keep sharing the full `/webhook/<secret>` URL with no one but the game. Set `WEBSITE=false` in `.env` to turn the site off (the address then only answers the health check). With `cloudflared` the address changes on every restart, so use `ngrok`'s free static domain if you want a link people can bookmark.

### Sportsbook (fake money)

`/book` on the same address is a mock sportsbook. A new visitor makes an account with a name and password and starts with $10,000 of fake money. They log in with the same name and password on any device (and again after a `cloudflared` restart changes the link); the browser also remembers them until they log out. Everything (accounts, balances, bets, matchups) is saved in the bot's database (`data/nfl_universe.db`), so stopping and starting the bot loses nothing. Accounts made before passwords existed keep their money and bets: the first login with that name sets its password (claims it). Passwords are stored hashed, but this is a friends' league, not a bank. An admin can reset a forgotten password on the Bettors page.

**League player logins:** league players log in with their real name (the login box suggests names as you type) and a preset password. Set it per player in `PLAYER_NAMES` in `config.py`:

```python
PLAYER_NAMES = {
    "Jacob": {"roblox_username": "roblox_user_759822057", "password": ""},
}
```

An empty `password` means the player's Discord ID, once an admin has linked their account with `/admin setname`. The first login creates their account with the starting money. In Discord, `/password` shows a player their own name and password, and only they can see the reply. Nobody else can sign up with a league player's name. The older `"Jacob": "roblox_user_759822057"` format still works; those players just have no preset password.

**Parlays:** the **+** next to any pick adds it to the bettor's parlay slip (`/book/parlay`, saved with their account). Two to ten picks, one per bet; the odds multiply and it pays only if every pick wins. A pick that pushes or is voided drops out and the rest still count. Parlays settle by themselves as their picks settle, and undoing a pick's settlement re-grades them.

**Parlay cart:** the 🧾 Parlay button in the bottom corner of every sportsbook page opens the slip like a shopping cart. Tapping **+** (or a slider's + parlay) adds the pick without leaving the page and opens the cart; tap it again, or ✕ in the cart, to take it out. The cart shows each pick with its odds, the combined parlay odds, and what your stake pays as you type, and **Place parlay** bets it right there. It stays open as you move between pages. Round robins and straight bets from the slip are on the full slip page (link in the cart).

**Injury insurance:** if a player leaves a game hurt, the admin matchup page's **🚑 Injury insurance** button handles it the way the big books do: single bets on that player's stats are voided (stakes returned), and a parlay with one of their picks carries on without it, re-priced at the odds of the picks left (all of it back if nothing else is left). The matchup page tells bettors it happened.

**Boosts and promos** (admin **Promos** page, bettors' **🎁 Promos** page):
- **🔥 Odds boosts:** better odds on one pick (set on the matchup's admin page), with an optional max bet. Boosted picks are single bets only and show the old odds crossed out.
- **🚀 Parlay boost:** every parlay automatically wins more the more picks it has (3+ picks +10%, 4+ +15%, 5+ +20%, 6+ +25%, 8+ +30%, 10+ +40%; `BOOK_PARLAY_BOOSTS` in config.py, `{}` turns it off). A pick that pushes or is voided drops out of the count.
- **Profit boosts** (+X% winnings) and **no-sweat bets** (stake back if it loses), given to one bettor or everyone, optionally only for bets up to a max stake. Bettors pick one in the bet card, bet slip, cart or parlay slip; each is used once.
- **Bonus money** to every bettor at once.

**No inside betting:** a league player's account can't bet on their own stats (over or under) in a game they play in. Those picks show a 🔒 and can't go in a slip, a parlay or a tail. They can still bet on either team, the game's totals and game props, other players' stats, and any game they're not in, and nobody else is limited. The account is matched to a player by name (`PLAYER_NAMES` or `/admin setname`); **Plays as** on the admin Bettors page sets it by hand or marks an account as not a player. If a bet slips through (placed before this, or before the admin added the player to the lineup), the admin matchup page lists it under **Inside bets** with a **Void (refund)** button. More rules can be turned on in `config.py`: `BOOK_BLOCK_BET_AGAINST_SELF = True` (no betting on the other team's winner or spread), `BOOK_BLOCK_OWN_GAME_PROPS = True` (no totals or game props of their game), `BOOK_BLOCK_TEAMMATE_PROPS = True` (no teammates' stats); `BOOK_BLOCK_OWN_PROPS = False` turns the main rule off.

**Bet card:** tapping any odds opens a small bet card on the same page instead of a new one. Drag it anywhere by its top bar (it remembers where). It has quick stake buttons (+$10 … Max), shows what the bet pays as you type, places the bet or adds the pick to your parlay slip, and updates your balance on the spot. Esc or ✕ closes it.

**Search and filters:** the sportsbook home page can search games by title, team or player and filter Open / Betting closed. Each matchup page has a search box (e.g. "passing yards" or a player's name) and filters for Game, Player props and Specials, plus a team dropdown and a player dropdown (it lists the chosen team's players, or everyone when no team is picked) to see one player's bets. Both teams are always listed with their players, under their in-game names once a game has named them.

**Deleting a matchup:** **Delete matchup** on its admin page refunds open bets on it, removes it and its bets, and renumbers the matchups after it so there's no gap.

**Alt lines (the slider):** every over/under and the spread on a matchup page has a big slider through other lines, priced from the same model (a higher line pays more on the over; a bigger handicap pays more on the team giving it, e.g. Team Jacob −10.5). Picking a line bets it alone or adds it to the slip like any pick. Betting a new line creates a hidden "alt" market under the main one that settles with the game. On the admin page, alt lines show under their bet once someone has picked one. **Settle at number** settles a bet and all its alt lines from what the stat came to.

**Several bets from one slip:** the slip can place the full parlay, round robins (every smaller parlay, e.g. all 2-pick parlays from 4 picks) and each pick as a straight bet, all at once, with the stake on each bet. Up to `BOOK_MAX_SLIP_BETS` (100) bets at once. Tick **Keep these picks** to bet the same picks again another way.

**Everyone's bets:** `/book/feed` is a live feed of everyone's open bets. New bets appear on their own (highlighted for a moment) without reloading. Filter it by parlays or single bets. **Tail** copies a bet's still-open picks at today's odds: a parlay goes into your slip, and a single bet opens its bet slip. The **Bet history** tab lists settled bets with how they did. On the **Leaderboard**, tap a bettor's name to see all their bets (open ones first, with Tail) and their record.

**House profit/loss:** the top of `/admin` shows what the book has won or lost on settled bets (singles and parlays), the money in open bets, and what it would owe if every open bet won. Each matchup row shows the profit/loss on its single bets. Money handed out on the Bettors page isn't counted.

**Picking teams:** the admin's player chart has a big Out / Team 1 / Team 2 switch per player. Above it, each team is listed in the order you picked the players, and that order is saved; the first pick names the team. Each team also has an **Add a player** dropdown that puts someone straight on that team (players already picked are greyed out), and a ✕ next to each name takes them off.

**Admin page:** `/admin`, username `admin`, password `ADMIN_PASSWORD` from `.env` (add the line `ADMIN_PASSWORD=...` and restart; without it the admin page stays off). There you:

1. **Create a matchup:** mark each player Team 1 / Team 2 (or type names of players with no games yet). Teams are called "Team <first player>" until the game comes in. Odds are generated from the stored stats right away:
   - **Winner**, from the same model as `/odds`.
   - **Spread**, e.g. Team Jacob −3.5 / Team Chris +3.5. The margin is spread out like past games' final-score margins and centred where the winner odds put it. Settles from the final score once the bot knows which side each team played.
   - **Add newer bet types** (admin matchup page): a game made before an update gets any kinds of bets it's missing. Bets already on it don't change.
   - **Total points**, over/under from past games' combined scores.
   - **Game props**, over/unders on the whole game from past games' stat sheets: touchdowns, field goals made, longest field goal, points after touchdowns (extra points + 2-pt conversions), sacks, turnovers, punts, defensive and return TDs, and safeties. Only stats some past game has recorded are offered, and each has a slider. Also **A 2-point conversion is made** (yes/no). The stat sheet has no extra points or 2-pt conversions, so those two are worked out from the final score minus 6 a TD, 3 a field goal and 2 a safety; when that can't tell (the sheet is missing someone's scoring, or one 2-pt try looks the same as two kicks) the bet is voided and stakes are returned.
   - **Exact numbers** for low counts (touchdowns, field goals, turnovers, sacks): 0, 1, 2 … or "N or more", priced from past games.
   - **Team bets**: each team's touchdowns (over/under with a slider), **Both teams score a touchdown** (yes/no), **Winning margin** (each team by 1-6, 7-13 or 14+, or a tie), and **Most fantasy points** among the players in the game. Team touchdowns come from each lineup's own stat lines, so they settle from the final stats; the margin settles from the final score once the bot knows which side each team played. Changing the lineups re-prices these, and replaces Most fantasy points (bets on the old one are refunded). Also **each team's points** (over/under with a slider; settles from the final score once the bot knows which side each team played).
   - **Total points range** (e.g. 0-20, 21-35, 36-49, 50+, set around past games' totals), and **Any defensive or return touchdown** / **Any safety** (yes/no, offered once a past game has had one).
   - **Player head-to-heads**: for passing, rushing and receiving yards and fantasy points, each team's best player at it against the other's. Who has more wins; a tie pushes; if either doesn't play the bet is voided.
   - **Milestones**: 250+ / 300+ / 350+ passing yards, 75+ / 100+ rushing or receiving yards, 6+ / 8+ catches, 2+ / 3+ touchdowns and so on. Each step is its own yes bet, offered when the player has a fair shot at it. Players can't bet on their own head-to-heads or milestones, and injury insurance voids them like props. The admin's **Settle at number** works on milestones and ranges.
   - **Player props**, over/under for each player's fantasy points and the stats they actually record (passing yards, TDs, interceptions thrown, completions, receptions, receiving yards, touchdowns, tackles, sacks, interceptions...). Lines come from each player's per-game average (scaled to a full game, pulled toward the league average when they have few games), priced with about a -110/-110 margin.
2. **Add more:** an over/under on **any** stat on the sheet for any player in the matchup, or a custom bet with your own question, options and odds.
3. **Edit live:** change any line or odds (Auto-price re-prices a line from the stats), close or reopen betting per market or for the whole matchup, void (refunds), delete. **Edit matchup** changes the title, team names and lineups at any time before it settles: players taken out have their props voided, added players get props, and the winner odds are re-priced. Every change takes effect immediately with no restart, and bettors' open pages refresh themselves within a few seconds (if someone is typing a stake they get a "reload" banner instead). Bets already placed keep their odds.
4. **Settle:** when a game arrives with at least half the matchup's players, it settles itself: props and the total from the game's stats, and the winner by working out which side each lineup played from its players (their team if the game reports it, otherwise which lineup's touchdowns, field goals and safeties fit each final score). The teams then take the game's team names. A player who didn't play has their props voided. Anything left (custom bets, a winner it couldn't tell, e.g. a 0-0 game) you settle by hand; "Settle from this game" does the same for a game you pick, and "Undo settlement" reverses payouts if you got one wrong.
5. **Bettors:** add or take away money, or delete someone.

Optional tuning in `config.py`: `BOOK_START_BALANCE`, `BOOK_VIG` (margin, 0.045 = -110 lines), `BOOK_MIN_STAKE`.

---

## 3. Webhook payload format

I couldn't find public documentation for the NFL Universe webhook's JSON, so the parser accepts many shapes and **every payload is stored raw before parsing**. If the real format turns out to be different, nothing is lost: look at what arrived, adjust the parser, then re-parse.

The shape it handles best (also in `samples/sample_game.json`):

```json
{
  "game_id": "unique-id-per-game",
  "week": 3,
  "timestamp": "2026-10-15T19:00:00Z",
  "home": { "name": "Ravens", "score": 28 },
  "away": { "name": "Bills",  "score": 21 },
  "players": [
    { "username": "QBKing_123", "team": "Ravens", "position": "QB",
      "stats": { "passing": { "completions": 18, "attempts": 25, "yards": 312, "tds": 3, "ints": 1 },
                 "rushing": { "carries": 4, "yards": 22, "tds": 0 } } }
  ]
}
```

It also understands:
- payloads wrapped in `data`, `game`, `payload`, `match` or `result`;
- `home_team` / `away_team` / `home_score` / `away_score`, or `"score": "28-21"`;
- a `teams` list (`[{name, score, players: [...]}, ...]`) or players keyed by username (`{"QBKing_123": {...}}`);
- flat stats on the player (`"Pass Yds": 312`, `"passing_tds": 3`, `"Comp/Att": "18/25"`). Key matching ignores case, spaces and punctuation, and knows common abbreviations (`yds`, `td`, `int`, `tkl`, `pd`, `ff` …);
- Discord-style messages (`content` + `embeds`) with a score like `Ravens 24 - 17 Bills` and stat lines like `Pass Yds: 250`.

Notes:
- A game with the same `game_id` posted again **replaces** the earlier one instead of duplicating it. An exactly identical payload is ignored.
- Numeric stats it doesn't recognise are kept in an `extra` column and listed in `/admin rawlog`, so you can add aliases for them in `nflu/stats.py`.
- Flat `interceptions` / `int` means interceptions **thrown**. Use `def_ints` or put it under `"defense": {...}` for interceptions caught.

### When a payload doesn't parse
1. `/admin rawlog` lists recent deliveries with their status and errors.
2. `/admin raw raw_id:<n>` downloads the exact JSON that arrived.
3. Add the new key names to `nflu/stats.py` (or adjust `nflu/parser.py`), restart, then `/admin reparse raw_id:<n>`.

---

## 4. Commands

**Everyone**

| Command | What it does |
|---|---|
| `/games` | Every game played, newest first, with the date and time (in each viewer's time zone) and top player. ◀ ▶ buttons page through long lists |
| `/inspect_game [game] [view] [player]` | One game (latest if empty). **Simple** (default): the box score, with ◀ ▶ flipping to older / newer games. **Complex**: a stat card per player showing the matchup, fantasy points, rank in the game, snaps and every stat they recorded grouped by position (QB/RB/WR/DB/DL/ST) with points per group, with ◀ ▶ flipping between players (best first). `player` starts the cards on that player |
| `/player [name] [view]` | **Simple** (default): career stats, fantasy totals, best game. **Complex**: a career card with every stat combined over all games, grouped by position (QB/RB/WR/DB/DL/ST), each shown as total · per-game average (longest plays show best · average), plus points per group, snaps, points per 100 snaps and best game. ◀ ▶ flips between players in `/fantasy` order, starting on `name` (or #1 if empty) |
| `/me` | Your own stats (once an admin links your Discord account) |
| `/password` | Your sportsbook login name and password. Only you can see the reply |
| `/gamelog name [count]` | A player's recent games |
| `/compare player1 player2` | Side-by-side stats |
| `/leaders stat [per_game]` | Top 10 in any stat, or fantasy points |
| `/fantasy` | Fantasy leaderboard ranked by points per 100 snaps, so players who joined late aren't penalized. Players under `FANTASY_MIN_SNAPS` are listed separately |
| `/stats [position]` | Position leaderboards: QB (Passing), RB (Rushing), WR (Receiving), DB (Cornerback), DL (DLine), ST (Special Teams), ranked by fantasy points earned at that position |
| `/points name [game_id]` | How a player's fantasy points were scored |
| `/scoring` | Fantasy scoring rules |
| `/odds team1 team2` | Win chances for two lineups. List each team's players separated by commas (real names or Roblox usernames, with autocomplete). Shows each team's win %, moneyline and decimal odds, projected fantasy points, and each player's rating. See "Matchup odds" below |
| `/names`, `/whois query` | Who's who |
| `/asknfl prompt [public]` | **Owner only** (`ASKNFL_USER_IDS` in `config.py`). Ask Claude a question about the league's stats. It runs the Claude Code CLI (`claude`) on this PC with your own Claude subscription. See "Ask the stats (/asknfl)" below |
| `/help` | Command overview |

Anywhere a player name is asked for you can type a mapped real name, a Roblox username, or an @mention, and it autocompletes.

**Admins only** (Discord IDs in `ADMIN_IDS`):

| Command | What it does |
|---|---|
| `/admin setname roblox_username display_name [discord_user]` | Show a real name instead of the Roblox username; linking a Discord user enables `/me` |
| `/admin removename`, `/admin unmapped` | Remove a mapping / list usernames with no name yet |
| `/admin addgame`, `/admin editgame`, `/admin deletegame` | Record, fix or delete games by hand (**master admin only**, `MASTER_ADMIN_IDS` in `config.py`) |
| `/admin setstat game_id roblox_username stat value [position]`, `/admin removeplayer` | Fix one stat on one player (0 removes it), or remove a player from a game (**master admin only**) |
| `/admin import file` | Import a game from a `.json` attachment (same format as the webhook) |
| `/admin test` | Dry run: reads the latest NFL Universe game post in the feed channel and shows the teams, score and every player's points, plus whether it's saved yet. Also reports who sent the channel's last message, and warns if a game post came from a webhook other than `NFLU_WEBHOOK_ID`. Saves nothing |
| `/admin backfill` | Import every past NFL Universe game post in the feed channel (posts by `NFLU_WEBHOOK_ID` in `config.py`). Messages already saved are skipped by message ID, so it is safe to re-run |
| `/admin intfault game thrower [at_fault] [count] [note]` | Move an interception's −2 to the player at fault, or excuse it (leave `at_fault` empty) |
| `/admin intfaults [game]`, `/admin removeintfault fault_id` | List / undo interception rulings |
| `/admin rawlog`, `/admin raw`, `/admin reparse` | Inspect and re-process raw webhook deliveries |
| `/admin reparseall` | Re-read every saved game with the current parser (run after a bot update) |
| `/admin channel [channel]` | Where final scores are posted automatically |
| `/admin announce game_id` | Re-post a game's result |
| `/admin recalc` | Reload `config.py` (scoring and admins) and recalculate all fantasy points |
| `/admin webhookurl` | Show the current webhook URL |
| `/admin admins` | List admins |

---

## 5. Fantasy scoring

Scoring is set **per position group** in `FANTASY_SCORING` in `config.py`. Each stat counts toward the group whose section of NFL Universe's stat sheet it came from:

| Group | Stat sheet section | Defaults |
|---|---|---|
| QB | Passing | 1 pt / 25 yds, 4 per TD, −2 per interception charged |
| RB | Rushing | 1 pt / 10 yds, 6 per TD |
| WR | Receiving | 1 per reception, 1 pt / 10 yds, 6 per TD, −2 per interception an admin moved onto them |
| DB | Cornerback | tackle 1, INT 3, pass break-up 1, pick-six 6 |
| DL | DLine | tackle 1, sack 2, swat 1, forced fumble 2, fumble recovery 2, fumble TD 6, safety 2 |
| ST | Special Teams | FG made 3, **FG missed −2**, kick/punt return TD 6 |

Bonuses: +3 for 300 passing yards, 100 rushing yards or 100 receiving yards in one game.

Every column on the stat sheet is listed in its group, including drops, yards after catch, first downs, 3rd/4th down conversions, QB hits, pressures, tackles for loss, coverage allowed, punts inside the 10 and onside recoveries. Most start at 0. Give any of them points (or a negative number to subtract), then restart or run `/admin recalc`. Because groups are separate, the same stat can be worth different amounts. For example, set DL `tackles` to 2 and leave DB `tackles` at 1.

The penalties are interceptions charged (−2) and missed field goals (−2). Negative yardage never costs points. The stat sheet has no extra points or 2-point conversions, so those can't be scored. Averages and percentages (yards per carry, FG %) aren't stored, because they're worked out from other columns.

`/scoring` shows every value by group, zeros included, and `/points` shows which group each point came from. `/admin setstat` takes an optional `position` for stats that exist in more than one group, such as tackles.

### Points per snap

`/fantasy` ranks players by fantasy points per 100 snaps, not by total points, so someone who joined a game late (fewer snaps) isn't penalized for it. Snaps come from NFL Universe's stat sheet: every section's Snaps column added up, which is the number of plays the player was on the field. A full game is about 130–145 snaps, so "per 100 snaps" reads like a game's score.

Players with fewer than `FANTASY_MIN_SNAPS` total snaps (default 50) are listed under "Not enough snaps yet" instead of ranked, so a few lucky plays can't top the board. Change both settings in `config.py`. `/player` and `/compare` also show points per 100 snaps.

### Interception faults

Every interception thrown costs the QB −2 (`int_charged` in `config.py`). When a pick wasn't the QB's fault, an admin can rule on it:

- `/admin intfault game thrower at_fault` moves the −2 to the player at fault, for example the receiver who tipped it. The QB gets the points back.
- `/admin intfault game thrower` with `at_fault` left empty excuses it, so nobody loses points.
- `count` applies the ruling to several of that QB's interceptions at once.
- `/admin intfaults [game]` lists rulings, and `/admin removeintfault fault_id` undoes one.

Rulings are stored separately from the game data, so they survive `/admin reparseall`. Deleting a game deletes its rulings. In `/stats`, a QB's own interceptions count against QB, and ones moved onto a player count against WR.

Change any of it in `config.py` (`FANTASY_SCORING`, `FANTASY_BONUSES`), then restart or run `/admin recalc` to rescore every stored game.

### Player names

To show a real name instead of a Roblox username, add it to `PLAYER_NAMES` in `config.py`:

```python
PLAYER_NAMES = {
    "Jacob": "roblox_user_759822057",   # real name: IGN
}
```

Restart the bot or run `/admin recalc`. Commands like `/player` then accept either the name or the IGN. `/admin setname` does the same thing from Discord, and can also link a Discord account for `/me`.

### Matchup odds (/odds)

`/odds team1:"Chris, Gabe, Jacob" team2:"DJ, Ethan, Stewie"` estimates which lineup wins:

1. **Each team is just a list of players**, and everyone keeps their **usual role**. A player counts as a QB if at least `ODDS_QB_SHARE` (40%) of their offense snaps were at QB. That's currently Stewie, Gabe and DJ, worked out from the stats rather than a fixed list. Everyone else is a receiver.
2. **The QBs throw, one at a time:** a team passes at its best QB's level, and any other QBs on it catch instead. **Receivers only produce as well as their QB throws**: their output is scaled by the QB's quality, from 20% to 150% of average. Three great receivers with no QB are therefore an underdog, not a favorite. The embed warns when a team has no QB.
3. **Everyone plays defense and special teams**, rated per snap on their own history there.
4. **Unproven roles:** a team with no QB passes at `ODDS_UNPROVEN_ROLE` (default 0.5) of an average QB, and a role someone has never played is rated at that fraction of the league average.
5. **Few games, cautious rating:** each role rating is pulled toward the league average as if the player had `ODDS_PRIOR_GAMES` extra average games there. A player with no games at all gets the unproven rating everywhere.
6. **Win chance:** players vary from game to game, so the bot works out the chance that one team's projected total beats the other's, using each player's consistency (`ODDS_FULL_GAME_SNAPS` and `ODDS_MIN_GAME_SNAPS` set what counts as a full game). That's shown as a percentage, a moneyline (−150 = bet 150 to win 100) and decimal odds. Each player is listed with their role (🎯 QB or 🙌 WR) and projected points.

It predicts from fantasy output, not past results between these lineups. The game doesn't record who was on which team, so there's no win/loss history to learn from.

### Ask the stats (/asknfl)

`/asknfl prompt:"Who has the most passing yards?"` asks Claude about the league's stats. The answer is visible only to you unless you add `public:True`.

- **Who can use it:** only the IDs in `ASKNFL_USER_IDS` in `config.py` (default: just the owner). It runs the Claude Code CLI on this PC, signed in with your own Claude subscription (`claude` must be installed and signed in). Anthropic's terms don't allow routing other people's requests through a Pro/Max login, so don't add other people. Letting others use it would need an Anthropic API key instead.
- **Read-only:** the bot reads the stats through a read-only database connection, which SQLite itself refuses to write to, and gives Claude a text copy of them. Claude has **no tools at all**, so it can't run SQL, read or write files, or run commands. It can't change anything even if asked.
- **Prompt-injection safeguards:**
  - Player and team names come from the game, so they're reduced to safe characters and passed as data that Claude is told never to treat as instructions.
  - Nothing variable goes on the command line.
  - Claude runs without your Claude Code settings, plugins, hooks, MCP servers or saved sessions, in an empty temporary folder.
- **Stats only:** off-topic questions (general knowledge, real NFL, coding, chit-chat) are rejected.
- **Model:** `ASKNFL_MODEL` in `config.py` (`""` = your subscription's default). It handles one question at a time, with a 3-minute limit. If `claude` isn't on PATH, set `CLAUDE_PATH` in `.env`.

---

## 6. Files

```
bot.py                     entry point: Discord bot + webhook server + website + tunnel
start.bat, start.sh        one-click start (sets up .venv on first run)
config.py                  ADMIN_IDS, fantasy scoring, player names, NFL Universe webhook ID   <- edit me
.env.example               copy to .env and fill in                     <- edit me
nflu/parser.py             webhook JSON -> game + player stats
nflu/stats.py              stat names and the aliases the parser accepts
nflu/db.py                 SQLite tables: games, player_games, names, raw_payloads, settings
nflu/fantasy.py            fantasy point calculation
nflu/analytics.py          totals and leaderboards
nflu/commands.py           public slash commands
nflu/admin.py              /admin slash commands
nflu/webhook.py            aiohttp webhook server
nflu/site.py               read-only stats website (same server)
nflu/sportsbook.py         sportsbook: markets, odds from stats, bets, settling
nflu/book.py               sportsbook pages and the /admin page
nflu/tunnel.py             starts cloudflared / ngrok
samples/sample_game.json   example payload
scripts/send_test_webhook.py
tests/test_bot.py          run with:  pip install pytest && python -m pytest
```

The database lives at `data/nfl_universe.db`. Back it up by copying that file while the bot is stopped.
