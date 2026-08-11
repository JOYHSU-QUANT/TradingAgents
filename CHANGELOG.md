# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes within the 0.x line are called out explicitly.

## [Unreleased]

### Added

- **US economic calendar + corporate BTC treasuries (SoSoValue).** Two new
  news-analyst categories for crypto assets, served by the SoSoValue key
  already used for ETF flows (shared plumbing — auth, request envelope, error
  taxonomy, clock/cache-age helpers — extracted from `sosovalue.py` into
  `sosovalue_common.py`, semantics unchanged except that an out-of-range int
  now fails the finite-number check instead of raising). `economic_calendar`
  reports the next two weeks' scheduled US releases with consensus forecasts
  and the trailing window's releases with actual-vs-forecast surprises, for a
  live-verified whitelist of high-signal events (the API has no importance
  field, so the whitelist is the importance filter; calendar names outside it
  appear name-only). Scheduled rows never render an actual and released
  figures appear only on or before `curr_date` (lookahead-safe); surprises
  are computed only when actual and forecast share a unit; the report flags
  that the feed carries no Fed rate-decision events at all, and frames event
  risk as a regime/risk modifier rather than a directional signal.
  `btc_treasuries` reports combined and top-5 holdings across the 15 largest
  tracked corporate holders plus the window's disclosed changes — buys and
  disposals (numeric strings normalized at the parse boundary; the API's
  `avg_btc_cost` field is live-verified unusable and never rendered), with
  holdings-only disclosures rendered as the implied change from the prior
  filing; ETH and other recognized risk assets get the BTC data as a labelled
  market-wide demand proxy. Both categories are optional (sentinel
  degradation), cache on rolling snapshots with stale fallback, and **ship
  disabled** (`"none"`): the key already sits on deployed boxes, so landing
  them enabled would change a running paper deployment's analyst input
  surface with no server-side action to date the change from — flip them as
  a deliberate cutover, ideally alongside the `options_data` one.
  Both reports state the semantics of what they render rather than leaving
  them to be inferred: the unit each figure carries (a payrolls actual of
  `-23` is thousands of jobs), that a surprise is actual minus forecast and
  its sign is not a directional verdict, when the snapshot was fetched even
  when it is fresh, which aggregate figures mix filed with derived values,
  and — when a section is empty — whether that is a quiet window or this
  snapshot's own coverage gap. A provider that merely reshapes its output
  (a longer calendar, a repeated calendar date, comma-grouped numbers, a
  listed company with no filings yet) degrades to a disclosed, counted
  partial rather than failing the category into a stale serve that expires.
- **Crypto options-implied volatility (Deribit).** A new keyless `options_data`
  category, bound to the **market** analyst for crypto assets only (vol regime
  is a technical read, so it sits alongside the indicators rather than with the
  flows/sentiment tools on the news analyst). The report carries Deribit's DVOL
  index — latest reading (always with an as-of date), 30-day min/max and a
  365-day percentile — plus ATM (50-delta) implied vol, the 25-delta
  call and put vols, and the 25-delta risk reversal (RR25 = call IV − put IV),
  read for one expiry inside a **bounded band** around 30 days: within
  `MAX_TENOR_DISTANCE_DAYS` (±15) of the target and never inside the 7-day
  pin-noise floor. Wing vols come from an undiscounted Black-76 forward delta (Deribit
  quotes `interest_rate: 0.0` on every listed option), interpolated between two
  **strike-adjacent** listed contracts, and each wing is rendered with the two
  strikes behind it. Two guards protect that number, because one collapsed or
  stale `mark_iv` corrupts both a contract's IV and its delta. Ordering by strike
  rather than by delta stops a bad quote pairing with a non-neighbour; and the
  bracket must additionally sit in a stretch where delta falls with strike, as it
  does on any well-formed smile. The second guard is not redundant: strike
  adjacency alone leaves the wing at the mercy of a bad quote sitting *inside* the
  bracket it legitimately borders, which printed exactly the strikes a reader
  would expect and so had no visible symptom at all — a 61000 put collapsing to
  0.5% IV moved RR25 from −4.74 to **+2.33**, reporting the opposite volatility
  regime. A point the chain cannot bracket, or whose local smile is not monotone,
  is reported `n/a` rather than extrapolated or guessed.
  It **ships disabled** (`"options_data": "none"`). Being keyless, shipping it on
  would change a running deployment's analyst input surface the moment the code
  landed, with no server-side action to date the change from; enabling it is a
  deliberate cutover.
  The DVOL history is date-filtered to `curr_date` (bounded server-side and again
  on the parsed rows) and lookahead-safe to the day; the candle dated today (or
  later, when the fetch crossed UTC midnight) is
  still open, so the report calls the series "readings" throughout and labels that
  one rather than passing an intraday level off as a settled close. The chain
  endpoint takes no date, so it is **withheld for a `curr_date` earlier than
  today** and said to be withheld — quoting the current chain on a past date is
  future information, and a prose warning is not an auditable guard. A `curr_date`
  up to `MAX_FUTURE_DAYS` (1) *later* than the UTC clock is served (callers derive
  it from a local clock, so east of UTC it routinely runs hours ahead; the live
  chain is then never later than the analysis date — predating it outright, or
  falling inside it when the clock reaches that date mid-run) with a note saying
  which of the two holds, and
  the feed is never called late against a date that has not arrived. Further ahead
  than that is a mistyped argument rather than a timezone — `curr_date` arrives
  from an LLM tool call — so the chain is withheld again, with its own note. The
  chain also re-reads the clock immediately before fetching rather than reusing the
  one taken before the DVOL half, so the printed snapshot instant is when the chain
  fetch begins, and a run whose DVOL half timed out across UTC midnight cannot
  serve day D+1's chain for `curr_date` = D.
  The min/max range and the percentile are computed over **different windows** —
  30 days and 365 — and printed as separate lines, each naming its own span and
  its own sample count. A percentile is a claim about the volatility *regime*, and
  a month cannot support one: through a sustained high-vol stretch every reading in
  the window is high, so a 30-day percentile sits mid-range even at a multi-year
  extreme, muting the signal exactly when volatility is what matters. The count
  travels into the closing sentence too, because "the 365-day window" on its own
  reads as one observation per day and that clause is what gets quoted alone.
  A percentile is stated only when its window holds at least 10 daily readings:
  the latest reading is itself in the sample, so a percentile over n of them
  cannot read below 100/n, and a stalled feed would otherwise always report its
  one surviving observation as the top of its own range. The min/max range needs
  two readings for the same reason: at one, the min and the max are both the level
  already printed above, and "min 62.30 / max 62.30" is the strongest possible
  claim about the regime — a month of pinned volatility — manufactured from a
  single observation, with no lag note beside it because a feed that stopped for
  four weeks and resumed today is not late. A window with no readings at all
  reports no range either. The closing sentence restates the
  figures and defines them — it deliberately does not characterise them, since it
  is re-read verbatim by the research and risk agents downstream and a negative
  RR25 is the resting state of crypto options rather than news.
  Only contracts carrying open interest enter the smile: the two guards above
  defend the wing against a bad quote that *inverts* the smile, and nothing else
  defended it against one that merely sits there being wrong. An unheld strike is
  where a stale or purely modelled mark lives, and such a quote can be perfectly
  monotone with its neighbours — passing both guards and printing the very strikes
  a reader expects. If the eligible expiry nearest 30 days cannot be used — it
  fails to bracket both wings, or it carries no usable forward — the
  next eligible one is used and the report says so rather than repeating that this
  is "the expiry closest to 30 days"; a labelled neighbouring skew is a better
  input to a risk debate than an all-`n/a` section, and the tenor is printed both
  in the section and in the closing sentence. Both branches of that sentence name
  the same exclusions, so stepping to the second candidate no longer drops the only
  line that states them. The fallback is **one step only**, and it can only land on
  another expiry inside the band. Nothing outside the band is read at all: a risk
  reversal is not tenor-invariant — the 25-delta strikes on a 300-day expiry sit
  nowhere near the 30-day ones, and every downstream agent reads this number as a
  ~30-day figure — so a thinned book whose only qualifying expiry is 96 days out
  now yields **no skew** instead of a 96-day RR25 rendered under a 30-day heading
  with `is_fallback` false, corrected only by a number a downstream summary drops.
  That sentence also states when a risk reversal is **not** in the report and
  why — withheld by policy, a chain that yielded no usable surface, or wings the
  chain does not supply — instead of simply omitting the clause. That sentence is
  the one line a downstream summary keeps, so an absence marked only by a missing clause
  was precisely the signal that did not survive the hop: a backtest, a proxied
  asset and a chain outage each produced a closing sentence differing from a
  healthy report's only by something that was not there. A chain that was
  attempted and failed now also carries its own italic header caveat, which the
  three withheld-by-policy states already had and an outage did not.
  **The same treatment now covers the DVOL half and the chain degradations that
  change what the printed figures mean.** The closing sentence states the DVOL
  **level** unconditionally rather than only as the base of a percentile — gating
  the whole clause on the percentile made an outage, a window too thin to rank,
  and a feed stalled past a year all fall silent identically, while the body
  above carried a usable level throughout — and names the half's absence with its
  cause when it failed, alongside a matching italic header caveat whose only
  previous trace was *subtractive* (a dropped clause on the source bullet). The
  closing sentence also now carries a fallback expiry and a missing ATM point, and — where a risk
  reversal is printed, that being the case where the line states a wing vol to
  qualify — each 25Δ wing interpolated across a bracket wider than
  10% of the forward, both of them when both are that wide: each changes which
  quantity the figures describe, and each was disclosed only in the body a
  summary drops. Sentences that named a cause the code does not support were
  corrected — "the options chain could not be read" for the several causes that
  are a successful 200; "the newest reading Deribit has published at all" and "the
  index has not printed since" on a historical date, asserted about a live feed
  from a series this module deliberately truncated at `curr_date`. Counts are
  labelled **usable** and readings rejected as non-positive are disclosed by
  count, so a window this module partially emptied is no longer described as a
  sparse calendar window; the too-few-to-rank floor is floored rather than
  rounded, which at six readings had claimed a bound the figure could breach.
  A DVOL candle is now also rejected when its **low is not positive, or its open
  or close falls outside its own high/low range**, disclosed as its own count
  with the newest date it reached. The positivity term is keyed off the low, which
  the ordering terms beside it extend to all four prices: previously only the
  close was sign-checked, so a candle carrying an open of `-5` and a low of `-10`
  beside a plausible close published untouched.
  The row had been guarded on one side only: a close of `0.0` was refused as
  broken while a close of `3000.0` inside a 39/41 candle became the headline
  level, the 30-day maximum and the percentile basis with no caveat anywhere.
  This is also the only check that sees a **reordered candle** — a permuted row
  passes the length-and-finiteness shape guard, and the day's low is then read as
  its close on every row indefinitely. The **latest reading is labelled usable**
  in the headline and in the `_Reading:_` line for the reason the counts already
  were: a rejected candle can be dated later, and the rejection notes print that
  date.
  A DVOL reading older than **`MAX_DVOL_STALENESS_DAYS` (14)** is now withheld
  rather than served with a caveat, measured from the earlier of the analysis date
  and the clock — the same reference the rendered lag note uses, so the refusal and
  the report can never disagree at the boundary. The previous ceiling was
  whatever the fetch happened to span (375 days, a figure that moved as a side
  effect of widening the percentile window), so a feed stalled for weeks headlined
  a weeks-old print and ranked it against a window that still held enough readings.
  A **continuation cursor** in the DVOL response is now disclosed in the report
  instead of only logged, so a sample shortened by a truncated fetch is not read as
  the index publishing sparsely.
  Chain rows are now matched against the **requested currency**. The book-summary
  payload identifies its underlying only inside the instrument names, and the
  parser discarded that segment — so a misrouted or mis-served response rendered
  one currency's forward and smile under another's heading with no log line, no
  caveat and nothing downstream to catch it (the analyst prompt forbids
  reconciling the forward against spot). The check also turns away the linear
  `BTC_USDC-…` names, which otherwise parse and interleave a second,
  differently-margined book into one smile. The **Expiry used** line now states
  how many contracts Deribit lists for the selected expiry, so a thin smile can be
  distinguished from a chain this module's own open-interest policy thinned.
  `MAX_DATA_LAG_DAYS` drops from 2 to **1**, so a lag of two days — a whole
  missing day on a 24/7 daily index — now raises the italic stall note and carries
  its age into the summarisable line instead of passing as ordinary. A missing 25Δ
  wing now states WHICH guard refused it: a thin book, or a bracket rejected
  because delta rises with strike across it, which is a suspect quote rather than
  a market fact. When neither candidate expiry brackets both wings the one
  carrying more of the three smile points is used, ties going to the nearer
  expiry. `_is_finite_number` answers `False` for an out-of-range JSON integer
  instead of raising `OverflowError`, which had cost all six chain figures to a
  single row in defiance of `parse_chain`'s skip-don't-fail contract. The
  both-halves-failed raises now read the same `withheld_mid_run` classification
  the rendered sites do, and carry the caller's own symbol plus the fact that a
  proxied asset has no chain on ANY date — a SOL backtest had read as withheld
  for the date, implying a live date would serve it.
  Vendor error text and both caller-supplied arguments (`asset` and `curr_date`)
  are flattened before they are interpolated — whitespace collapsed and mid-line
  markdown markers removed —
  because the report is assembled into an LLM prompt and a fragment carrying line
  breaks could otherwise open a forged heading or a second `_Reading:_` line
  above the real one. The flattening is applied where the fragment enters the
  message rather than only where it renders, since the router hands an optional
  category's failure to the model as `DATA_UNAVAILABLE: ... ({error})`.
  Either half can fail without costing the other (any exception, not a fixed
  allowlist); losing both degrades the optional category to the no-data sentinel —
  as does losing DVOL alone on a date, or for a proxied asset, where the chain is
  withheld by design — and
  a throttle on every request actually made re-raises as `VendorRateLimitError` so
  the router keeps its rate-limit lane — including a lone DVOL 429 on a call where
  the chain was never attempted. This vendor reads chains for BTC and ETH, so
  another recognized crypto risk asset (SOL, XRP, ...) is served BTC's **DVOL
  level** as a market-wide crypto-vol proxy — labelled in the heading and named
  again in the closing sentence, so the framing survives whichever line a
  downstream summary keeps.
  The 25Δ skew is **withheld** for such an asset: a market-wide vol *level* is a
  defensible stand-in, but a risk reversal measures demand for downside in one
  specific underlying and does not transfer, and a caveat that has to survive every
  summarisation hop is not a substitute for not printing the number. A
  stablecoin or unrecognized symbol gets a no-signal note, the same classification
  farside applies to ETF flows. No rendered line claims Deribit itself lists
  nothing for those symbols — nothing at runtime checks that. Uncached by design
  (two GETs per call, one where the chain is withheld, and both halves
  freshness-sensitive), with one retry on transient faults only; a JSON-RPC error
  or any other 4xx is deterministic and raises immediately rather than being slept
  on and reported as unreachable, and an HTTP 429 raises the shared
  `VendorRateLimitError`. The crypto prompt paragraph carries three read-guards
  for comparisons the report cannot support on its own: the forward is Deribit's
  forward for the selected expiry and is expected to differ from spot, so it is
  not reconciled against the verified snapshot; DVOL and ATM IV share a unit but
  not a construction (a model-free 30-day index across the whole strike range
  versus one 50Δ point on one expiry), so the gap between them is neither a term
  structure nor a volatility risk premium; and RR25 has nothing to be ranked
  against at all — and neither has ATM IV nor the wing vols, since Deribit
  publishes no chain history, so each may be reported by level (RR25 by sign,
  magnitude and tenor) but never called elevated or extreme. Each exists
  because an agent asked for actionable insight will otherwise fill the vacuum
  with a regime claim the tool output does not support.
  The stock path's tools and prompt are unchanged.

- **SoSoValue is now the primary crypto spot-ETF flow vendor.** farside.co.uk
  has served a Cloudflare JS challenge to non-browser clients since 2026-07-27
  (zero successful fetches since), so `crypto_etf_flows` now routes
  `"sosovalue,farside"`: BTC/ETH US spot-ETF daily net flows come from the
  SoSoValue OpenAPI (free Demo key, `SOSOVALUE_API_KEY`), with Farside kept as
  a keyless fallback in case it ever unblocks. The report mirrors the Farside
  shape (US$m units, latest day / window cumulative / streak / leaders /
  recent-days table) and adds a since-launch cumulative, a fund breadth line
  (how many of the funds that filed moved together, how concentrated the
  flow was), revision caveats whenever any still-visible day's figure
  changed since the previous snapshot (issuers file over the US evening, so
  fresh days firm up in place), a restatement caveat when the since-launch
  cumulative shifted because a day *older* than the API's servable window was
  restated (invisible to the daily revision diff, which only covers visible
  days), and a reconciliation caveat when a fully-filed
  latest day's fund filings fail to sum to the aggregate — the sign that the
  fund listing itself is missing a fund (e.g. a newly launched ETF).
  Classification and rendering share one granularity: a flow below the
  report's 0.1 US$m render tick (which would display as `+0.0`) is not
  counted as a flow session, streak member, or leader, so the report never
  calls a figure it displays as zero a flow event. The
  aggregate endpoint alone decides vendor
  success — per-fund history failures (including a 200-with-empty-history
  response, which would otherwise silently wipe the fund's cached rows) and
  an empty fund listing degrade to a disclosed incomplete/absent
  breakdown, retried on a shorter 1-hour TTL; three consecutive
  transport-level history failures trip a circuit breaker that skips the
  remaining funds into the same disclosed path, so a hanging network costs
  a bounded number of timeouts instead of one per listed fund (API-level
  failures such as 429s still give every fund its own try) — and the cache discipline
  otherwise matches Farside (rolling per-asset file, 6h TTL, stale fallback
  capped at 14 days, failures never cached). Unsetting
  `SOSOVALUE_API_KEY` is the emergency-disable switch: the next call falls
  through the chain with no code change; the key itself is stripped of
  surrounding whitespace (Windows env-file CRLF), rejected without being
  echoed when it cannot travel in an HTTP header, and redacted from
  server-echoed error bodies — which are themselves length-bounded before
  they reach a raised message — so it can never leak into logs or report
  text.
- A vendor chain exhausted by nothing but rate limits (e.g. a single-vendor
  `crypto_etf_flows` override hitting an uncached 429) now degrades to the
  same no-data sentinel as any other optional-category failure instead of
  aborting the call with a bare `RuntimeError`; a real error elsewhere in
  the chain still takes precedence in the surfaced message.
- A mis-typed vendor name in an explicit comma chain is no longer dropped
  silently when a sibling name is valid: the router logs a warning naming
  the dropped vendor(s) while the survivors serve the call (an all-unknown
  chain still raises).

- **Crypto spot-ETF flows and Fear & Greed vendors.** Two keyless news-analyst
  data sources, bound only for crypto assets: BTC/ETH US spot-ETF daily net
  flows scraped from Farside (`crypto_etf_flows` / `get_etf_flows`, one rolling
  cache file per asset refreshed at most once every 6 hours, with a stale-snapshot
  fallback capped at 14 days) and the alternative.me Crypto Fear & Greed Index
  (`crypto_sentiment` / `get_fear_greed`, uncached, one retry on a transient
  failure). Both are lookahead-safe, honour a trailing `look_back_days` window,
  and degrade to a no-data sentinel when unreachable. A recognized crypto risk
  asset without its own spot ETF (SOL, XRP, ...) gets BTC flows as a market-wide
  proxy, marked as such in the report heading; a stablecoin or unrecognized symbol
  gets a no-signal note; the stock path is unchanged.
- Both reports disclose data staleness separately from fetch failure: a vendor
  that is reachable but has stopped publishing gets a data-lag caveat instead of
  being presented as current.
- **`"none"` disables a data category.** Setting a `data_vendors` (or
  `tool_vendors`) entry to `"none"` switches that category off: an optional
  category returns the no-data sentinel without opening a connection, and the
  analyst stops binding the tool entirely. Core categories reject it loudly.
  Previously a keyless vendor could only be stopped by editing code, having no
  API key to unset.

### Fixed

- **Perp runs no longer lose the target-JSON contract to structured output.**
  The Hyperliquid Phase 2 target contract is injected as prompt text and only
  survives in the decision agents' free-text answers; with a model whose
  `with_structured_output` succeeds, the Portfolio Manager's rendered markdown
  carried no JSON and every paper cycle fail-closed as `invalid_output`. A new
  `structured_output` engine config key (default `True`) forces the free-text
  path for the gated agents when `False` (Portfolio Manager, Research
  Manager, Trader; the Sentiment Analyst is exempt), and
  `contrib/hyperliquid_perp` defaults it to `False`, keeping
  `engine.structured_output: true` as an explicit escape hatch. The perp
  config loader rejects non-bool values for the key at load time (a quoted
  `"false"` would otherwise read truthy and silently re-enable structured
  output). On the library side a `structured_output` stored as `None` counts
  as unset (default on) rather than a falsy "off"; the perp overlay maps
  unset to `False`. Arming the escape hatch is loud:
  the perp engine-config build warns on both channels (log + stderr) when the
  effective value is `true`, since every cycle would otherwise fail-close with
  only the *absence* of the gate-off INFO lines as a trace.
- **A non-bool `structured_output` also fails loud at agent construction.**
  The library-side gate previously folded any non-`None` value through
  truthiness: a quoted `"false"` injected by an embedding caller that does not
  go through the perp loader would silently keep structured output enabled —
  the exact inversion the loader check exists to prevent. `bind_structured`
  now raises `ValueError` for non-bool non-`None` values, mirroring the
  loader's `bool_from_yaml` contract at the gate every config-gated caller
  passes (exempt agents never read the key).
- **Perp config loader rejects a scalar `indicators:` value.** Same hazard
  class as `coins: BTC`: `indicators: rsi_14` would `list()`-explode into
  per-character names, collapsing the warm-up threshold to 0 and emptying the
  all-dead-indicator guard, so the daemon would reason over an all-`None`
  indicator block forever.
- **Perp config loader validates `indicators:` element names.** List shape
  alone still let a typo'd name (`rsi14`) load as a permanently-`None`
  indicator: an unknown name contributes 0 to the warm-up threshold and the
  all-dead guard's known-names filter excludes it, so every guard passed and
  the LLM context carried a dead row forever. Unknown names are now rejected
  at load, naming the offender and the supported set.
- **Perp config loader rejects a scalar `engine.selected_analysts`.** A bare
  string (`selected_analysts: market`) would `list()`-explode per-character
  into bogus analyst keys that only detonate deep inside `build_graph` — in
  the daemon, an endless retry ladder on a pure config typo, never a named
  config error.
- **A non-empty `indicators:` list must include `atr_14`, `ema_20` and
  `ema_50`.** Such a config can never trade — `classify_regime` fabricates a
  calm RANGING when *any* of the three is missing (a live ATR with dead EMAs
  hid a trending market just as silently as a dead ATR hid a volatile one) —
  and now fails at load instead of leaving the daemon in an endless 4-hourly
  `api_failed` ladder. The trio lives in one `REGIME_INDICATORS` tuple shared
  by the loader and the runtime guard so the two rules cannot drift. An
  explicit empty list keeps its documented "no indicators" meaning.
- **The paper/live daemon now applies the one-shot path's indicator guards.**
  A fully-dead known-indicator set (broken stockstats) or missing/dead regime
  indicators made `classify_regime` silently report a fabricated-calm RANGING
  regime; the one-shot path refused loudly (exit 1) but the daemon's
  `build_input` traded on it. All three pre-LLM context guards (warm-up,
  dead set, missing/dead `atr_14`/`ema_20`/`ema_50`) now live in one ordered
  shared helper (`main._context_refusal_error`) and the daemon rides them down
  the reviewed retry ladder as recurring `api_failed` cycles — no AI spend —
  until the indicator engine or `indicators:` config is fixed.
- **`--context-only` renders the full refusal diagnosis and exits 4 on a
  degraded context.** The diagnostic loop warned on under-warm only; a
  fully-dead indicator set or dead/missing regime indicators rendered as a
  clean-looking context (fabricated-calm RANGING) — precisely where an
  operator investigating a RUNBOOK refusal would look. It now runs the same
  shared guard as the trading paths, warns on both channels while still
  rendering, and exits 4 (the repo's probe convention: command succeeded,
  state degraded) instead of 0, so a keyless deploy preflight can gate on the
  exit code rather than parsing stderr.
- **A zero-price candle can no longer silently force the RANGING regime.**
  `Candle` accepted a `0/0/0/0` bar (OHLC ordering holds vacuously), and a
  zero close flowed into `classify_regime`'s `price <= 0` branch — the same
  silent-RANGING failure mode as a dead indicator, with no guard covering it.
  `Candle` now requires strictly positive prices (parity with
  `MarketSnapshot`), so a broken-feed zero bar is dropped per-bar by
  `map_candles` like any other malformed bar instead of poisoning the
  EMA/ATR series and the regime.
- **`max_recur_limit: None` no longer silently shrinks the recursion budget.**
  A stored `None` survived `.get("max_recur_limit", 100)`, was dropped by
  langchain's `ensure_config`, and left LangGraph running at its own default
  (25) instead of the documented 100; `None` now means "unset, use 100".
- **Alpha Vantage fundamentals look-ahead guard now takes effect.**
  `get_balance_sheet` / `get_cashflow` / `get_income_statement` drop fiscal
  reports dated after `curr_date`; the filter previously ran against the API's
  raw JSON *string* return (never a dict) and silently no-op'd, so future-dated
  reports leaked into point-in-time runs. A malformed `curr_date` now returns an
  `INVALID_CURR_DATE` sentinel instead of silently serving unfiltered data.

## [0.3.0] — 2026-06-22

Stabilization and extensibility release: a CI gate, a unified verified
data-access contract, a provider and data-vendor registry, and a maintenance
sweep that hardened config precedence, the model catalog, data resilience, and
structured output.

### Added

- **CI gate.** GitHub Actions runs the pytest suite across Python 3.10-3.13,
  strict `ruff`, and a clean-install smoke that imports the package and CLI to
  catch undeclared dependencies. (#994, #197)
- **Provider registry.** OpenAI-compatible providers register as a single spec,
  and a generic `openai_compatible` endpoint covers vLLM, LM Studio, and relays.
  Adds NVIDIA NIM, Kimi, Groq, Mistral, and a native Amazon Bedrock client.
- **Macro and prediction-market vendors.** FRED macro indicators and Polymarket
  event probabilities, surfaced to the news and macro analysts.
- **Programmatic report output.** `TradingAgentsGraph.save_reports()` writes the
  same report tree the CLI produces, for headless and API runs. (#1037)
- **Env-configurable reasoning depth** via `TRADINGAGENTS_OPENAI_REASONING_EFFORT`,
  `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`, and `TRADINGAGENTS_ANTHROPIC_EFFORT`,
  each gated to the models that accept it.

### Changed

- **Verified data-access contract.** Symbol normalization on every vendor path
  (identity, returns, CLI, news); the configured vendor list is the exact
  resolution chain with no silent fallback to unselected vendors; a typed
  `VendorError` taxonomy; look-ahead-safe news windows; stale-OHLCV rejection;
  inclusive yfinance date ranges.
- **Config precedence.** An explicit `TRADINGAGENTS_*` value or CLI flag now wins
  over interactive defaults for debate and risk round counts,
  `--checkpoint / --no-checkpoint`, and the Docker provider profile; invalid
  boolean env values fail loudly. (#975, #976, #977)
- **Current-generation model catalog.** Refreshed provider lineups; retired
  `gpt-4.1`, Claude Sonnet 4.5, and the Gemini 2.5 line.
- **Optional vendors degrade** instead of aborting a run: a failed macro or
  prediction-market lookup returns a no-data sentinel.
- **Analyst prompts lead with the current date** so tool-call date ranges anchor
  to the run date rather than the model's training cutoff. (#836)

### Fixed

- **Instrument identity.** Deterministic ticker-to-company resolution prevents
  wrong-company hallucination, and a verified market-data snapshot grounds price
  and indicator claims. (#814, #830)
- **Social and market data sources.** Reddit RSS-first with 429 backoff,
  StockTwits transport hardening, and Alpha Vantage timeout plus
  key-versus-rate-limit handling.
- **Structured output.** Local OpenAI-compatible servers no longer reject
  object-form `tool_choice`; a thinking model that returns no parsed result falls
  back to free text; null-ish strings in optional price fields coerce to `None`.
  (#1038, #1051, #1057)

### Removed

- The no-op `analyst_concurrency_limit` config knob; parallel analyst execution
  is planned for a later release. (#979)
- The unused committed `uv.lock`. (#1030)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

[@CadeYu](https://github.com/CadeYu), [@Zavianx](https://github.com/Zavianx), [@weijianz-opc](https://github.com/weijianz-opc), [@naltun](https://github.com/naltun), [@brahmasky](https://github.com/brahmasky), [@nik2208](https://github.com/nik2208), [@thieucong98](https://github.com/thieucong98), [@Derekko-web](https://github.com/Derekko-web), [@LukiPrince](https://github.com/LukiPrince), [@Eddieargenal](https://github.com/Eddieargenal), [@Ghraven](https://github.com/Ghraven), [@ms32035](https://github.com/ms32035), [@yting27](https://github.com/yting27), [@nyxst4ck](https://github.com/nyxst4ck), [@KenCheung-AIxFinance](https://github.com/KenCheung-AIxFinance), [@yangyusheng2n](https://github.com/yangyusheng2n), [@fareloj](https://github.com/fareloj), [@haosenwang1018](https://github.com/haosenwang1018), [@octo-patch](https://github.com/octo-patch), [@seifenk](https://github.com/seifenk), [@CaoYuhaoCarl](https://github.com/CaoYuhaoCarl), [@mihailnica10](https://github.com/mihailnica10), [@Dado-hash](https://github.com/Dado-hash), [@Handsomemikezzz](https://github.com/Handsomemikezzz), [@ydhawesome](https://github.com/ydhawesome), [@macd2](https://github.com/macd2), [@AyushKar2005](https://github.com/AyushKar2005), [@wildhuman](https://github.com/wildhuman), [@robert23kim](https://github.com/robert23kim), [@bngness](https://github.com/bngness), [@tedix-rodrigo](https://github.com/tedix-rodrigo), [@malaccan](https://github.com/malaccan), [@rfalken78](https://github.com/rfalken78), [@dengli1971-droid](https://github.com/dengli1971-droid), [@proofconcept39](https://github.com/proofconcept39), [@prasta1](https://github.com/prasta1), [@liximin](https://github.com/liximin), [@jeffhuen](https://github.com/jeffhuen), [@mazar](https://github.com/mazar), [@soyangelromero](https://github.com/soyangelromero), [@CNQQC](https://github.com/CNQQC), [@dovetaill](https://github.com/dovetaill), [@fperdigon](https://github.com/fperdigon), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@RSXLX](https://github.com/RSXLX).

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
