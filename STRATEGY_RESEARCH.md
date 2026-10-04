# Predictions Cup 2026 — Research, Tests, Strategy

Prepared Oct 4, 2026 (about 31 days left). Labels used everywhere:
**[VERIFIED]** = read from an official source or your own files. **[INFERRED]** = my reasoning from verified facts. **[UNVERIFIED]** = a guess you must test before trusting.

I could not log in, hit the live API, or read the API reference (the site blocks automated access to `/api/v1/docs`). So nothing below was tested against live order books. What I did test is the math, your code, your run log, and the leaderboard numbers (Section 4).

---

## 0. TL;DR

1. **Only the top 3 get money** ($30,000 / $5,000 / $2,500). 4th and 5th place get nothing. So the goal is "maximize the chance of finishing top 3", not "maximize average profit". [VERIFIED]
2. **The leaderboard today is provisional.** Final rank = SUSQie balance *after every market has resolved*. Trading stops Nov 4, 12:00pm ET. [VERIFIED] The +874k leaders could still lose it, and you could still pass them.
3. **No unlevered directional betting can reach 7–8x.** My simulation: even if every one of 40–100 bets has a real edge and you go all-in, P(≥750k) is 0.00%. So the leaders are using something structural: arbitrage with capital netting, market making, or leverage-like mechanics. Your own order responses contain `collateralSavings`, `guaranteedPayoutFloorAfter`, `outstandingAdvanceAfter`, which strongly suggests the platform nets guaranteed payouts. [INFERRED, must be confirmed from the API spec]
4. **Your current bot cannot win, by design.** Its caps (3% per position, 70% deployed) cap the best case near 205k even if every bet wins. Its beliefs for KS, WI-01, VA-01, MI-10 are placeholder numbers ("Starter estimate"), not research. Three of the five live orders in your log were made on them. [VERIFIED from your files]
5. **Best path:** (a) scan for riskless inconsistencies every few seconds (R+D pairs, multi-outcome sums, the platform's own `/relationships/constraints`), (b) add passive market making where spreads are wide, (c) keep directional bets only where you have real research, (d) in the last days, decide risk on purpose using the top-3 payoff shape (Section 7).
6. **No strategy here is "full proof".** I can't see what the leaders do. Section 5 gives four 10-minute tests that will show you.

---

## 1. Competition facts [VERIFIED — predictionscup.com/rules, sig.thesuper.market docs & changelog]

| Item | Fact |
|---|---|
| Window | Oct 1, 12:00pm ET → Nov 4, 12:00pm ET. All trades must be *received* before then. |
| Start balance | 100,000 SUSQies. No monetary value. |
| Ranking | Final SUSQie balance after **resolution of all markets**. Must make ≥1 trade to get a rank. Live board = "account value", dynamic and provisional. |
| Prizes | 1st up to $30,000, 2nd up to $5,000, 3rd up to $2,500. One prize per person. Ties split the pooled prize. |
| Who can win | Enrolled university students, age of majority, valid tax ID. India is eligible except Andhra Pradesh, Telangana, Nagaland, Sikkim, Puducherry. |
| Tax | Non-US winners: 30% withholding (so $30k becomes about $21k before any Indian tax). Winners need ID, student ID, W-8 paperwork within days. |
| Bots | Allowed, unlimited bots, **one account only**. Multiple accounts = disqualification. All bot activity counts as yours. |
| Integrity | No trading on material non-public information. Sponsor can void/reverse trades, adjust balances, disqualify for "unsportsmanlike" conduct. |
| Market maker | Sponsor "may act as a market maker". Order books show everyone's resting orders. |
| Rate limit | One shared budget per account: **100 reads + 30 writes per minute** (all keys combined). Bulk prices endpoint takes up to 100 ids. DMM accounts get 600/200. |
| Data freshness | REST prices up to 2s old, order books up to 1s old. Polling faster wastes budget. Realtime channels need a token from `POST /realtime/token`; late batches are dropped, so refetch via REST every 1–2 minutes. |
| Free signal | Each market has a free "Super Signal" tab, refreshed nightly from this competition's own trading. |
| Scripts | Use the API only. Scraping the website endpoints may be blocked. |
| Markets | 237 open markets (mostly "Will the Republican/Democratic Party win X?" for Senate, Governor, House). No "mention" markets. |

Note: "8–9 lakh" on the leaderboard is **SUSQies, not rupees**. The only real money is the three prizes above.

---

## 2. What the leaderboard screenshot actually says

About 54 hours after open (Oct 4, 03:11 IST):

| User | Trades | P&L | P&L per trade | Trades/min |
|---|---|---|---|---|
| sunny-acorn-8216 | 2,916 | +874.2k | ~300 | 0.9 |
| kind-brook-6861 | 2,019 | +747.8k | ~370 | 0.6 |
| bright-finch-0400 | 13,271 | +743.7k | ~56 | 4.0 |
| steady-finch-4087 | 1,451 | +641.5k | ~442 | 0.4 |
| nimble-beacon-4953 | 1,798 | +640.8k | ~356 | 0.55 |

What this tells us:
- **Nobody is rate-limited** (limit is 30 writes/min). They are limited by opportunities or capital, not speed.
- **Two styles.** One account (bright-finch) does 13k small trades at ~$56 each: that looks like a grinding bot (spread capture or small arbs). The others do ~$300–440 per trade with far fewer trades: larger sizes, bigger gaps, or concentrated bets.
- All five are at 6.4–8.7x in 2 days. A 5% edge on a few thousand shares per trade fits these numbers. A 1–2% edge does not (Test 5).
- **What it can't tell us:** whether the gains are locked-in arbitrage or mark-to-market on open positions. Open positions can lose value before settlement.

---

## 3. Audit of your current bot [VERIFIED from your files]

1. **Fake beliefs were traded.** `beliefs.json` has "Starter estimate" entries for Kansas, WI-01, VA-01, MI-10 with no polls and no research. The live log shows real orders on MI-10 and WI-01 at 18:34 UTC on Oct 1 (before the "verified" gate existed). Those edges (12–18 pts, Class E) are model error, not alpha.
2. **Double exposure in MI-10.** The bot bought *R YES* (196 sh) and *D NO* (1,201 sh). Both pay only if R wins, so that is the same bet twice. (The newer `_keep_best_per_group` prevents this going forward.)
3. **Logged live orders cost only ~2,026 SUSQies total** (450 + 1,027 + 68 + 426 + 55). Your account value is 92,974, which is -7,026. The log cannot explain that loss. There must be other trades (manual, or earlier versions). Run `python bot.py positions` and look.
4. **Sizing makes winning impossible.** 3% per position, 15% per group, 70% deployed, half-Kelly, and a 0.75 model-confidence haircut are good for survival, not for a top-3 finish.
5. **No latency path.** `scan` polls REST on a schedule. The platform offers realtime channels. The leaders' $56/trade bot almost certainly reacts in seconds.
6. **Missing files.** README mentions `.github/workflows/trade.yml` but it was not in your upload.
7. **Good parts to keep:** the API client (retries, idempotency, tick snapping, ORDER_STATUS_UNKNOWN handling), the audit log, the `consistency`, `snapshot`, `leaderboard`, `unwind` commands, competition-window guard, `verified` gate, one-candidate-per-race rule.

---

## 4. Tests I ran (offline, reproducible — `tests.py`)

| # | Test | Result | Meaning |
|---|---|---|---|
| 1 | Leaderboard P&L per trade | $56–442/trade, 0.4–4 trades/min | Leaders are not rate-limited; two distinct styles |
| 2 | R+D "Yes" prices from the markets page | RI Gov 110, NH Senate 104, RI Senate 103, MA Senate 102, NH-01 102, MA Gov 101, AK 100, NH Gov 100 | Overround exists on the *displayed* price. **Unverified** whether bids (what you can sell/NO into) show the same gap. |
| 3 | Does your belief-based logic make money if the market is already efficient and beliefs are noisy? | Belief noise sd 6 pts: EV -1.25 pt/share, ROI -2.5%. sd 10 pts: EV -0.44 pt, ROI -0.9% | Guessed beliefs lose slightly (spread + noise). Not a disaster, but never a way to catch leaders |
| 4 | Max result of an unlevered directional portfolio (40–100 independent bets, real edge of +4 to +7 pts, all-in) | Mean ~120–140k, **P(≥750k) = 0.00%** | You cannot get leader-size returns with plain buy-and-hold bets |
| 5 | Locked-arb size needed for 750k | 1pt × 5,000 sh = $50/set → 15,000 sets. 5pt × 5,000 sh = $250/set → 3,000 sets | Matches leaders only if gaps ~5pt at several thousand shares, or capital is reused/levered |
| 6 | Toy model of a 3-prize contest | Only a base near the leaders makes a final gamble matter | Variance is only useful *after* you have a base (Section 7). Toy model, not a forecast |

What these tests do NOT prove: that arbitrage is available at size right now. That needs live bid/ask data.

---

## 5. How leaders might be doing it, and how to find out in one evening

| Hypothesis | Evidence for | Evidence against | Cheap test |
|---|---|---|---|
| **H1: Riskless inconsistency arbitrage + capital netting** | Order responses contain collateral/advance/guaranteed-floor fields; two-outcome R/D markets with overround; platform exposes `/relationships/constraints` with `suggestedCorrectiveTrades` | Unknown size of gaps | Run `python bot.py consistency`. Then buy 10 shares of NO on both R and D of one race and read `collateralSavings` and `netBuyingPowerImpact` in the response. |
| **H2: Market making / spread capture** | bright-finch: 13k trades at ~$56 | Needs inventory control; 30 writes/min budget | Run `snapshot` every minute for a few hours: how many markets have spreads ≥ 3pt? How often do bid/ask change? |
| **H3: Concentrated directional bets, marked to market** | $300–440/trade | Test 4: can't reach 7x unlevered; marks can reverse | Open leaderboard profiles of the top 5 ("traded markets" is shown on profiles). Do they hold many different markets or a few? |
| **H4: Moving thin prices to flatter the mark** | Possible if account value uses last price | Final rank is after settlement, and sponsor can void trades or disqualify for unsportsmanlike conduct | **Don't do this.** Mentioned only so you can recognise it on the board. |

Also run `python bot.py leaderboard --period 1d --top 20`. It prints volume, trades, win-rate, ROI for the top accounts. A win rate above ~90% with small volume per trade points to H1/H2. A win rate near 55% with large volume per trade points to H3.

**Biggest single unknown:** how buying power works (do guaranteed positions free capital?). Upload `api-1.json` (your README says you had it before) and have Cursor read it. That one fact decides how aggressive you can be.

---

## 6. Strategy stack (ordered by evidence and safety)

### S1. Consistency arbitrage (riskless when it is real)
- **R/D pair (two-party race):** if `bestBid_R + bestBid_D > 1`, buy NO on both. Cost = `2 − bidR − bidD`. Payout is ≥ 1 in every outcome (2 if a third party wins). Profit ≥ `bidR + bidD − 1`. Your `find_complement_gaps` already computes this.
- **YES+YES** (`1 − askR − askD > 0`) is **not riskless** (pays 0 if a third party wins). Treat as a bet, not an arb.
- **Multi-outcome markets (3+ options):** only if the set is exhaustive (check `/relationships`, `isExhaustive`).
- **Engine constraints:** poll `/relationships/constraints?violationsOnly=true&minViolation=0.01`. Use `suggestedCorrectiveTrades`. Verify each leg against the live order book before sending.
- **Size = min(depth on every leg, capital rule, 30-writes/min budget).** Send legs in one batch (`/orders/batch`, up to 50). Check whether a batch counts as 1 write or N.
- **Execution risk:** data is up to 2s stale; one leg can fill and the other not. Always re-read the book right before sending, send both legs together, and have a "leg-failed" repair routine (sell back or complete the hedge).

### S2. Passive market making (where the spread pays)
- Pick markets with spread ≥ 3pt and some two-way flow. Post a bid and an ask around your fair value, inside the spread, small size.
- Inventory limits per market and per race group; skew quotes when inventory builds; pull quotes on news days.
- Writes budget: cancel-replace burns writes. Re-quote only when your price is off by more than 1 tick or inventory changed. Use `cancel-all` as the emergency brake.
- Reality check: the sponsor may also quote; you compete with them and with other bots. Test with tiny size first.

### S3. Longshot / favorite bias (small, steady, but needs time)
- Very cheap YES in safe races (e.g., R in RI Governor at 16%, MA Senate R at 7%) can be overpriced because retail likes lottery tickets. Selling them (buying NO) earns a few points per month with low variance *if* those races are truly safe.
- Return is only ~5–10% on money tied up until settlement. Useful as a base, not as a way to 8x. [INFERRED]

### S4. Directional bets from real research (keep, but demoted)
- Only for races you have researched with ≥2 independent sources (poll margin → win probability, forecaster, fundamentals), dated within 10 days, `verified: true`.
- Remember the Alaska example in your own files: market 33.5% vs sources at 43–48%. That one is the type worth trading, and the price has since moved to ~37% (markets page), a small sign the research was right. One data point proves nothing.
- Never combine "R YES" and "D NO" on the same race.
- Prefer races where something changes soon (polls drop, debate, rating change).

### S5. Event-driven repricing (semi-manual)
- When a major poll/rating lands, reprice your fair value, then trade stale quotes within seconds. Humans can't read feeds that fast, but the API data itself is 2s stale for everyone, so the advantage is mostly "knowing the right new price".
- You need a feed: the platform has "news feeds" for some markets on the website; the API has none for news. This stays manual or needs an outside poll source.

### S6. Final-phase convexity (Section 7)

**What I'm deliberately not recommending:** multiple accounts, trading on non-public information, wash/self-trades or price pushing, scraping website endpoints. All are disqualification or prize-forfeiture risks, and "at any cost" does not include them.

---

## 7. Risk policy built for a top-3 payoff

- Prize shape: $30k / $5k / $2.5k, then nothing. Finishing 4th = 50th = last. So expected profit is the wrong target; **probability of top 3** is.
- Phase A (now → ~Oct 20): build a base with S1/S2/S3. Keep gross risk modest. Goal: get close to the leader pack with low variance. Reaching 300k+ safely changes everything.
- Phase B (Oct 20 → Nov 1): add S4/S5 on researched races. Stop trading anything you can't explain.
- Phase C (Nov 1 → Nov 4 noon): look at the live leaderboard. If you are inside or near the top 3, **lower** variance and lock hedges. If you are far behind, the only way to a prize is variance (a concentrated, correlated bet such as the national environment). Make that choice knowingly, in writing, with a number you're willing to lose. Do not drift into it.
- Settlement timing: some races may certify late; "all markets resolved" may be after Nov 4. Hold cash for that wait.
- Correlation: races move together (national wave). Group them (`group` field) and cap the group.
- Kill switches: daily loss limit, per-race and per-group caps, global "stop trading" flag, auto-stop if API errors spike or positions don't reconcile.

---

## 8. Deployment: GitHub + "free, automatic" — what is really true

**GitHub Actions `schedule` is the wrong tool for this strategy.** [VERIFIED by several sources, 2026]
- Minimum interval is 5 minutes; delays of 5–30+ minutes are common, and runs can be dropped. At 100 reads/min and 2s data, seconds matter for arbitrage.
- Max 6 hours per job.
- Free minutes: unlimited on public repos; only ~2,000 min/month on private repos (about 33 hours), not enough for 24/7.
- A **public** repo exposes your code. Keep `beliefs.json`, logs, and keys out of git (use encrypted secrets).
- I could not verify GitHub's current terms about using Actions as a general always-on runner. Read their Terms before relying on it.

**Workable options, best first:**
1. **Your own PC/laptop running `run_loop.py`** (disable sleep, plugged in). Lowest latency to debug, zero cost. Weak point: power/internet.
2. **GitHub Actions "chained long job" as a free backup:** one job that loops for ~5h45m, then re-triggers itself with `workflow_dispatch` (events from `GITHUB_TOKEN` don't start new runs, except `workflow_dispatch`/`repository_dispatch`), plus a staggered cron (e.g., `23 */6 * * *`) as a safety net, and `concurrency` so only one copy runs. Only one runner may trade at a time.
3. **Oracle Cloud Always Free VM:** best free 24/7 box, but needs a credit/debit card (no prepaid), the allowance was cut to 2 OCPU/12 GB in June 2026, and "out of capacity" is common.
4. **Render / Railway free tiers:** sleep or trial-limited; not reliable for a trading loop.

Secrets: `SIG_API_KEY`, `SIG_BOT_CONFIRM_LIVE`, `BELIEFS_JSON` as repo secrets. Never commit `.env` (your `_gitignore` already excludes it; remember the real filenames must be `.env`, `.env.example`, `.gitignore`, not `_env`, etc.).

---

## 9. What I could not verify (be honest with yourself)

- Anything live: bid/ask depth, real spread sizes, whether overround exists on executable prices.
- How collateral netting and "advance" credit work in practice and whether account value uses last-trade or mid marks.
- Whether a batch of N orders costs 1 or N writes.
- Whether the sponsor's market maker keeps quoting through news.
- What the top accounts actually hold.
- Election win probabilities by race. My attempt to read a public Senate forecast returned no numbers, so I did **not** produce new belief estimates. Don't treat any probability in this file as researched.

---

## 10. 48-hour action plan

1. Run `python bot.py positions` and `python bot.py leaderboard --period 1d --top 20`. Save output.
2. Run `python bot.py consistency`. Note gaps ≥ 1pt on **bids**.
3. Run `snapshot` every minute for 3–6 hours (cron or loop) to learn spread/gap frequency and depth.
4. One tiny experiment (≤ 20 shares per leg): NO on both R and D of one race; read the collateral fields. Write down what it says about buying power.
5. Give Cursor `CURSOR_PROMPT.md` and `api-1.json`. Build Phase 0 (recorder) and Phase 1 (arb scanner, dry-run) first.
6. Review/unwind positions on invented beliefs (WI-01, MI-10) unless you can research them.
7. Go live only for arbitrage at tiny size first; scale after 24h of clean reconciliation.
