# SIG Predictions Cup Bot

A small, honest trading bot for the Susquehanna Predictions Cup. It automates
*pricing, filtering, sizing, and order placement* through the platform's real
API. It does **not** automate "read the news and form a probability" — that
endpoint doesn't exist on this platform (I checked your `api-1.json` spec;
there is no `/news` or `/polls` route). That step is a deliberately separate,
swappable input file: `beliefs.json`.

## 1. Files

| File | What it does |
|---|---|
| `sig_client.py` | API wrapper: auth, retries, idempotency, every endpoint the bot needs |
| `models.py` | The math: Beta-Bayesian updating, log-odds stacking, Kelly, risk-adjusted Kelly |
| `risk_book.py` | Correlation/exposure caps (don't pile 10 bets into "the national environment") |
| `scanner.py` | Read-only: fetches all markets, filters to your target price band, ranks by edge |
| `beliefs_example.json` | Template for your research inputs (polls, fundamentals, external forecasts) |
| `bot.py` | CLI: `scan`, `trade` (dry-run by default), `positions`, `cancel-all` |
| `test_bot.py` | Runs the math against the guide's worked Alaska example — no API key needed |

## 2. Get an API key

1. Log in at `sig.thesuper.market`.
2. Settings → API Keys → Generate New Key.
3. Give it `read` + `trade` scope (you don't need `admin` — that's for
   platform/organization admins, not participants).
4. Copy the key immediately — it is shown once and cannot be recovered.

```bash
export SIG_API_KEY="paste-your-key-here"
pip install -r requirements.txt
python3 test_bot.py        # sanity check: no key or network needed
```

## 3. Find your tournament slug

The Predictions Cup is itself a "tournament" in this API's data model.

```bash
python3 -c "from sig_client import client_from_env; import json; print(json.dumps(client_from_env().list_tournaments(), indent=2))"
```

Look for the Midterm Elections competition and note its `slug` (you'll pass
it as `--tournament` to every command below).

## 4. Fill in `beliefs.json`

Copy `beliefs_example.json` to `beliefs.json` and add one entry per market
you've researched. `title_contains` is matched case-insensitively against the
market title the API returns, so it doesn't need to be exact — just unique
enough to identify the race. Leave `polls` empty and just fill
`fundamental_p`/`external_p` for a quick first pass; add real polls once you
have them.

**This is the file to hand me when you want a race researched** — ask me to
look into a specific race and I'll give you back a filled-in block for this
file (polls, fundamentals, an external forecast, sourced and dated), in the
exact shape the bot expects.

## 5. Scan (always safe — read-only)

```bash
python3 bot.py scan --tournament <your-slug> --beliefs beliefs.json \
    --min-price 0.30 --max-price 0.70 --top 25
```

`--min-price/--max-price` is the competitive band you described (40–60,
35–65, 30–70 are all sub-ranges of a single 0.30–0.70 band, which is the
default — narrow it with e.g. `--min-price 0.40 --max-price 0.60` if you want
only the tightest races). This step places **zero orders**. Markets inside
the band with no `beliefs.json` entry still print, labeled
`NO BELIEF ENTRY -- add to beliefs.json`, so you always know what to research
next.

## 6. Trade — dry-run first, always

```bash
# DRY RUN (default) -- shows exactly what it *would* do, places nothing
python3 bot.py trade --tournament <your-slug> --beliefs beliefs.json \
    --min-price 0.30 --max-price 0.70 --top 10

# LIVE -- requires the flag AND a typed confirmation
python3 bot.py trade --tournament <your-slug> --beliefs beliefs.json --live
```

Every run — dry or live — appends one line per decision to `run_log.jsonl`
(price, model probability, edge, sizing, and outcome), so you have a full
audit trail of what the bot did and why, even for trades it decided *not* to
make.

## 7. How risk is actually managed

Every candidate gets sized through, in order:

1. **No-trade zone** (`models.should_trade`) — edge must clear
   `k × model uncertainty + half the spread` before it's even considered.
   A market with no polls yet uses a conservative default uncertainty, so it
   won't trade on a thin/guessed belief.
2. **Half-Kelly** (`models.full_kelly` / `risk_adjusted_kelly`) — never full
   Kelly; `--full-kelly` exists but is not recommended (see the guide, §3.6).
3. **Three multiplicative discounts** (`RiskAdjustment`): model confidence
   (defaults conservative at 0.75), liquidity depth, and correlation
   exposure.
4. **Hard caps** (`risk_book.py`): no single position over `--max-single-frac`
   of bankroll (default 5%), no correlated *group* (set per-market in
   `beliefs.json`, e.g. all Alaska races share group `"AK-senate"`, or
   everything you think rides on the national environment shares one group)
   over `--max-group-frac` (default 15%), and total deployed capital capped
   at `--max-deployed-frac` (default 70%, leaving 30% in reserve — see the
   guide, §5.5, on why unspent capital has option value in a one-shot,
   month-long competition).
5. **Literal order-book depth** — the bot reads `GET /exchanges/{id}/orderbook`
   and never sizes past what's actually resting at the price it's crossing.

## 8. What's deliberately *not* automated, and why

- **News/poll ingestion.** Not an API the platform exposes. Ask me to
  research a specific race and I'll hand you a filled `beliefs.json` block.
- **Super Signal.** The spec's `GET /tournaments/{slug}/me/smart-score`
  endpoint only returns *your own* score, not other traders' aggregate
  signal, so there's no endpoint to pull a platform-wide "Super Signal"
  number from directly — if a market-level `superSignal` field shows up in
  `GET /markets` responses once the competition has more trading history,
  wire it into `beliefs.json`'s `sig_p` field.
- **Placing orders without a human in the loop.** `--live` requires an
  explicit typed confirmation every run. This is intentional for a one-shot
  competition with no way to undo a mistake.

## 9. Full detail

Every formula and every endpoint this bot uses is explained and derived from
scratch in `SIG_Predictions_Cup_Complete_Guide.pdf` — read Part 3 for the
math, Part 6 for the API.

## 10. Pushing this to GitHub without ever exposing your API key

This repo is already set up so the key can't leak by accident:

- `.gitignore` excludes `.env`, `run_log.jsonl`, and `__pycache__/`.
- `.env.example` is the only env file meant to be committed — it has no real
  key in it, just a placeholder.
- `sig_client.py` reads `SIG_API_KEY` from the environment (optionally via a
  local `.env` file through `python-dotenv`), never from a hardcoded string.

First-time push:

```bash
git init
git add .
git status   # double-check: .env should NOT appear in this list
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<your-repo>.git
git push -u origin main
```

If you ever *do* accidentally commit a real key: treat it as burned.
Immediately revoke it (Settings → API Keys on the platform) and generate a
new one — rewriting git history does not reliably scrub it from forks,
caches, or anyone who already cloned it.

## 11. Deploying so it runs continuously, for free

The bot's own philosophy (guide, Part 5.3) is **update daily, don't trade
every tick** — so you don't need an expensive always-on server. The
recommended free setup is a **scheduled** run, not a persistent one:

### Option A — GitHub Actions (recommended, free, no server to manage)

Already included: `.github/workflows/trade.yml`. It runs a read-only
`bot.py scan` hourly on weekdays, starting at 9:30 a.m. Eastern and ending
with the 3:30 p.m. Eastern scan, only during the competition window (noon
Eastern Oct 1 through noon Eastern Nov 4, 2026). The workflow handles
daylight-saving time and lets you trigger a one-off `trade` (dry-run or
`--live`) manually from the Actions tab.

Setup:
1. Push this repo to GitHub (Section 10).
2. **Settings → Secrets and variables → Actions → New repository secret**:
   add `SIG_API_KEY` with your real key. This is encrypted at rest and never
   shown in logs — it's the correct place for it, never a committed file.
3. **Settings → Secrets and variables → Actions → Variables tab → New
   repository variable**: add `SIG_TOURNAMENT_SLUG` with the slug from
   Section 3 (this one isn't secret, so a variable rather than a secret is
   fine).
4. The scheduled runs are scan-only by default (zero risk). To place real
   orders for one run: **Actions tab → SIG bot scan/trade → Run workflow →
   set "live" to true**. `bot.py`'s own `COMPETITION_START`/`COMPETITION_END`
   guard additionally refuses `--live` outside Oct 1 – Nov 4, 2026 regardless
   of what triggers it.
5. Want scheduled runs to also go live automatically (no manual click each
   time)? Edit the `schedule` step in the workflow to call `bot.py trade
   --live` instead of `scan`, and add `SIG_BOT_CONFIRM_LIVE` as a **secret**
   set to exactly `YES-I-UNDERSTAND-THIS-IS-LIVE`. Think carefully before
   doing this — it means every scheduled tick can place real orders
   unattended.

The scheduled workflow requires `SIG_API_KEY`; without that Actions secret,
the run stops with a clear configuration error before contacting the API.
Its UTC cron opens a broad window, and a New York local-time check limits
actual scans to weekday market hours across daylight-saving changes. GitHub
Actions' free tier comfortably covers this usage for a public repo, and is
generous even for a private one.

### Option B — An always-on worker (Railway, Render, Fly.io free tiers, etc.)

Use `run_loop.py` instead — it's a persistent process with its own sleep
loop and error handling, for platforms that run a long-lived process rather
than a schedule:

```bash
python3 run_loop.py --tournament <slug> --beliefs beliefs.json \
    --interval-minutes 60       # scan-only, loops forever
# add --live (plus SIG_BOT_CONFIRM_LIVE as a platform secret) to actually trade each cycle
```

Set `SIG_API_KEY` (and `SIG_BOT_CONFIRM_LIVE` if using `--live`) as that
platform's secret/environment-variable feature — every one of these hosts
has one; never put it in a Dockerfile, start-command string, or committed
config file.

### Either way

- `run_log.jsonl` is your audit trail of every decision made, win or lose —
  on GitHub Actions it's uploaded as a downloadable run artifact each time
  (Section 9's workflow already does this); on a persistent host it just
  accumulates on disk.
- Nothing here removes the `--live` safety rail from Section 6 — automation
  makes the bot run on a schedule, it doesn't make `--live` happen silently
  without you having deliberately set the confirm variable somewhere.
