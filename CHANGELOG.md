# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes within the 0.x line are called out explicitly.

## [Unreleased]

### Added

- **hyperliquid_perp: the running daemons say which prompt regime they are
  in, and the rows a run wrote before schema v11 can be told** (issue
  #163). The paper and live lanes log one INFO line ``prompt_regime:
  prompt_version=… context_shape=… format_fingerprint=…`` the first time a
  cycle's prompt is built through (after its payload is written), and again
  only when that triple flips mid-run (a section appearing or disappearing)
  — so a YAML edit + restart shows its bucket in journald without a
  ``validate`` run. ``export --backfill-format-fingerprint`` fills, in a
  store already at v11, the ``NULL`` ``ai_inputs.format_fingerprint`` cells
  written before the column existed, by re-digesting the
  ``format_instructions`` text each row's payload JSON recorded; only
  ``NULL`` cells on rows that carry the other two keys are written, only
  from a payload whose bytes still hash to the row's ``input_payload_hash``,
  and rows that are pre-v10 (no shape either), missing their file, unreadable
  or altered stay ``NULL`` and are counted on stderr. Deliberately not a
  migration (file I/O, tolerated absence). ``common/prompt_regime.py`` is
  the one renderer all three surfaces (``validate``, the daemon log,
  ``--context-only``) print the line through, and ``common/digest.py`` the
  one spelling of the payload digest the daemon records and the backfill
  verifies — the daemon now writes the payload as the bytes it hashed
  (``write_bytes``), where a text-mode write let Windows rewrite the
  newlines between the two.

### Changed

- **dataflows: a throttle met while a sibling tool call was in flight is no
  longer forgotten, and a spent Alpha Vantage daily quota is remembered for
  an hour** (issue #153; the #137 tail). ``ThrottleLatch`` now records when
  each key was armed, and a call that returns drops only a deadline older
  than the request it sent (a lapsed one), keeping the one a sibling thread
  armed while the request was in flight: the vendor decided the request no
  later than it was sent, so the refusal issued after that is the later
  verdict. Before, the served result cleared it and the next tool call
  re-discovered the same throttle (on yfinance, at the price of a full
  backoff ladder). At the yfinance boundary this is per attempt and dated
  once the un-hide lock is held (``yf_retry`` now holds it around each
  attempt), and the latch is consulted again once the lock is held and
  before each retry — a call that queued behind a sibling's refused attempt,
  or slept through its backoff while a sibling exhausted its ladder, is now
  refused without a request instead of paying the ladder again; the
  exhausted attempt arms before it releases the lock so the queued call
  sees it. A retry that goes out once such a stand-off has lapsed and is
  served drops that lapsed deadline rather than keeping it as in-flight.
  A re-arm never cuts a recorded stand-off short (a burst 429 met while a
  daily quota is spent keeps the quota's deadline). ``VendorRateLimitError``
  gains ``latch_ttl_s`` (default ``None`` = the shared 300s window) so a
  raise can carry its own window: Alpha Vantage's "requests per day"
  notice now raises ``AlphaVantageDailyQuotaError`` with
  ``AV_DAILY_QUOTA_LATCH_TTL_S`` (3600s), where the shared window had the
  router re-probing the spent quota — one refused request and one WARNING
  — every five minutes. The shared window itself is unchanged, and the
  router's WARNING names the window actually applied.
  ``stockstats_utils._UNHIDE_LOCK`` stays a whole-fetch lock; the
  measurement that decided it (about a second per decision cycle, #137) is
  in its comment.

- **sosovalue_common: a process-wide per-minute request budget on the
  shared key** (issue #189). The three SoSoValue modules refresh 10
  (macro), ~15 (ETF) and 16 (treasuries) requests at a time against one
  20 req/min key, and only their cache TTLs kept them apart; under the paper
  daemon's 4h cycle the 5h and 6h TTLs expire on the same cycle, and since
  the 2026-09-02 cutover every cycle ended with treasuries never building a
  cache and ETF flows stale-served behind a 429. ``_request`` now records
  every send in a sliding 60s window and, when the next send would be the
  twenty-first inside it, sleeps until the oldest ages out (inside the tool
  call, at most one window plus 2s of slack, logged at INFO); a 429 parks
  the budget for a full window so the next module's sweep in the same
  analyst turn waits instead of burning its own quota. The key check still
  runs first, so the unset-key emergency switch is never delayed. The ETF
  fund loop now drains on a 429 like the family's ``fetch_each`` (the rest
  of the listing goes to ``funds_failed``, retried on the short TTL): its
  PR #19 per-fund retry predates both the drain and the budget, and with a
  park in place it would have cost a full window per remaining fund on a
  persistently throttled key — minutes inside one analyst tool call. The
  budget is per process and spends the whole plan limit, so a second
  process on the same key (a CLI run on the box while the daemon is up)
  will 429 and park both. Tests get a fresh budget per test and a sleep
  that fails loudly.

- **hyperliquid_perp: ``--context-only`` prints its segmentation bucket as
  one ``prompt_regime:`` line** (issue #163) — ``prompt_version``,
  ``context_shape`` and ``format_fingerprint`` together, in the grammar the
  daemon logs and ``validate`` prints — instead of the separate
  ``context_shape: …`` / ``format_fingerprint: …`` lines. Same values, one
  grep handle; the lane still carries no ``|position`` token (RUNBOOK §4).
  The two causes of a missing ``Position:`` section (no books yet vs
  non-positive equity) remain distinguishable only by their WARNING wording,
  now pinned by test and documented as the accepted state (issue #161).

- **data_vendors: the SoSoValue economic-calendar and BTC-treasuries
  categories are cut over to enabled** — ``economic_calendar`` and
  ``btc_treasuries`` default to ``"sosovalue"`` instead of ``"none"``
  (cutover dated 2026-09-02; both shipped OFF pending exactly this deliberate
  flip, mirroring ``options_data``'s 2026-08-12 cutover). A default-config
  run now binds ``get_economic_calendar`` on both asset paths and
  ``get_btc_treasuries`` behind the crypto gate; with ``SOSOVALUE_API_KEY``
  unset both degrade to the ``DATA_UNAVAILABLE`` sentinel. The perp engine's
  config overlay does not pipe ``data_vendors`` through, so the first
  deployment carrying this commit changes the analyst input surface and is
  the dated segmentation point; switching either category back off is a code
  change (``"none"``), not a YAML edit.

- **hyperliquid_perp: one implementation of the venue's epoch-millisecond
  time form** (issue #157). ``common.instants`` gains ``epoch_ms`` /
  ``from_epoch_ms`` / ``delta_ms`` — integer arithmetic on ``timedelta``,
  exact by construction, round-trip pinned across every magnitude
  ``datetime`` holds — and the eleven call sites that each converted on their
  own (the market-data window ends, the l2Book and ``clearinghouseState``
  clocks, the kill switch's ``scheduleCancel`` deadline, the fill parser,
  the fill backfiller's and the reconciler's ``userFillsByTime`` windows, the
  agent-authorization expiry, the audit record's ``timestamp_ms``, the
  context's ``as_of``, the funding-history hour bucket) now call them; most
  had gone through a float (``int(dt.timestamp() * 1000)`` /
  ``fromtimestamp(ms / 1000)``), whose exactness at 2026 magnitudes was an
  accident of float formatting, not a property of the code. Values are
  byte-identical on every path a test pins. ``epoch_ms`` refuses a naive
  instant by a REQUIRED caller name (``what=``); at the two sites that had
  no guard of their own (the reconciler's cross-check window, the context's
  no-candle ``as_of``) a naive clock now raises where the float route read
  it as host-local time — unreachable in production, where every clock is
  aware UTC, but a refusal rather than a wrong window if that ever changes.
  Also recorded, not changed: the ``ExchangeMarketData`` port documents the
  whole read surface rather than one consumer's needs, and the live lane's
  ``Position:`` costs reuse the paper fill model's assumptions by the
  2026-08-31 decision (issue #161; ``PositionPricing`` is the seam a future
  ``live.assumed_costs`` goes through).

### Removed

- **dataflows: ``y_finance.get_stockstats_indicator`` and
  ``stockstats_utils.StockstatsUtils``** (issue #137). The per-date getter
  existed only to serve the windowed getter's per-day fallback loop (removed
  under Fixed below), and the class existed only to serve that getter; no
  other caller remained in the repo. ``get_stock_stats_indicators_window``
  is unchanged as the one yfinance indicator entry point.

### Fixed

- **hyperliquid_perp: a ``--db`` pointed at somebody else's SQLite database is
  refused without being written into** (issue #174; the #175 tail). "An EMPTY
  store" was decided by ``MAX(schema_migrations.version) == 0``, which is a
  fact about this project's bookkeeping rather than about the file — and
  another application's database has no such rows either. A mistyped ``--db``
  therefore read as a fresh store: the reporting commands (``validate``,
  ``export``, ``--gate-status``) wrote a ``schema_migrations`` table into that
  file on the way to refusing it, and the owning commands (``paper``,
  ``live``) skipped the refusal altogether and built all of this project's
  tables into it, after which a daemon opened its books inside someone else's
  database and the file looked like a store to whoever opened it next. EMPTY
  now means empty: a SQLite file holding no objects of its own is built in
  full, one holding this project's tables goes on to the version policies
  unchanged, and one holding anything else is refused by name, listing what it
  found. Ownership is recognised by those tables and NOT by the presence of a
  ``schema_migrations`` table, which is the same name Rails/ActiveRecord and
  golang-migrate use — a database carrying one is evidence that somebody
  migrates it, not that we do. golang-migrate in particular often leaves that
  table and nothing else, and both halves of it used to end badly from one
  typo: populated, its ``version`` was read as a schema number and the
  operator was told the store "was migrated by a NEWER build … restore a
  backup"; empty, it passed every guard and died inside the first migration on
  ``no column named applied_at``. A lone ``schema_migrations`` is now accepted
  only when it is empty AND in this project's own shape, which is the one
  state an older build of this project could have left. Two neighbouring
  mistypes are named rather than left to ``main()``'s exit-2 last resort as
  ``unable to open database file``: a ``--db`` that is a directory, and one
  whose directory does not exist or is not a directory (the latter is
  ``cannot open …`` on Windows and ``cannot read … to tell whether it is one
  of this project's stores`` on POSIX, which raises ENOTDIR for it). Either
  parent mistype reached that exit 2 only through ``--create``; every other
  command already refused it by name. A file that exists but cannot be READ is
  not in this set — ``stat`` succeeds on one, so the guard has nothing to
  refuse on, and it fails in the probe as it did before: issue #210. The
  read-only probe builds its URI itself rather than through
  ``Path.as_uri``, which rejects a relative ``--db`` outright and renders a
  Windows UNC path with an authority SQLite refuses — a store on a share
  would have stopped opening. The verdict is read from ``sqlite_master``
  before the connection is even tuned, because tuning is itself a write
  (``PRAGMA journal_mode = WAL`` rewrites the header of a database not already
  in WAL), and over a read-only connection, because opening a database
  read-write is one too: SQLite checkpoints an uncheckpointed ``-wal`` into
  the main file at last close, so a plain probe would silently perform a
  crashed foreign application's recovery for it. The refused file is therefore
  left byte-for-byte as it was in every journal and crash state; the only mark
  left anywhere is SQLite's own empty ``-shm`` / ``-wal`` pair beside a WAL
  database that had none, which its owner reclaims on its next open.
  ``stored_schema_version`` is read-only accordingly (it used to ``CREATE
  TABLE IF NOT EXISTS`` the very table it reads); ``apply_migrations``, the
  only writer, creates it. Objects SQLite makes for itself
  (``sqlite_sequence``, ``sqlite_stat*``, implicit indexes) are not evidence of
  ownership and do not make a file foreign. Also from the same review: a
  failed open no longer leaks its connection handle, which on Windows locked a
  non-database file against the ``unlink`` an operator reaches for next.

- **hyperliquid_perp: one out-of-range venue timestamp costs its own bar or
  hour, not the run** (issue #191; the #193 tail). ``Candle`` and
  ``FundingPoint`` carry the venue's stamps as bare epoch-millisecond ints,
  and nothing bounded them. A ``fundingHistory`` point stamped in nanoseconds
  — venue drift, or any integer past ``datetime``'s range — therefore reached
  ``from_epoch_ms`` in the funding-rate lookup, which decodes every point of
  a fetched window outside its own ``except ExchangeError``. The
  ``OverflowError`` that came back is neither an ``ExchangeError`` nor a
  ``ValueError``, so it slipped every handler between the wire and that
  decode: the paper engine's funding loop is ``@_fail_stop``, so the engine
  halted and the daemon exited with no shutdown export, while the
  pending-funding backfill pass — which is contractually never allowed to
  abort — aborted, and a supervised restart re-fetched the same response and
  crash-looped on it. The decodable range is now published as
  ``common.constants.MIN_EPOCH_MS`` / ``MAX_EPOCH_MS``, derived from
  ``datetime``'s own limits and held equal to what the decoder accepts by a
  drift pin, and it is enforced twice on purpose: at the wire, where the
  mapper refuses the stamp on the ``Decimal`` BEFORE ``int()`` (an
  ``"1E+999999999"`` is finite, so converting first would wedge the fetch
  materializing a billion digits) as the ``MalformedResponseError`` its
  per-element handler already drops and counts; and at the two DTOs, whose
  ``__post_init__`` now also names a non-``int`` stamp, covering the
  scripted and backtest feeds ``ports`` exists for. The poisoned hour reads
  as pending and retries; the good points in the same response still resolve.
- **hyperliquid_perp: a failing funding reader no longer reads as a corrupt
  stored row, and the market-data port's exception contract is written down**
  (issue #193). Three fixes to one confusion — which failures are the
  venue's. The funding backfill wrapped the rate lookup in the same handler
  as the stored fields, so a defect of ours (a drifted call signature, a
  naive clock) was logged as "corrupt stored row; fix it in the store" and
  sent an operator to SQLite to hunt a fault in the code; the reader now has
  its own contained lane, logged apart with its traceback and counted into
  the still-pending total, and the runbook names the line.
  ``PortSnapshotProvider.fetch`` caught every exception and collapsed it to
  an ERROR outcome, so that same drifted signature read as an exchange outage
  and left market data paused forever, one WARNING per tick, about an
  exchange that was answering; it now catches only the venue-failure family,
  which ``ports.ExchangeMarketData`` now states as the contract that reader's
  consumers share. That failure now sorts into two outcomes instead
  of one: the venue's stays ``ERROR``, while ours becomes a new ``DEFECT``
  carrying an ERROR-level traceback. Sorted rather than raised, deliberately —
  ``fetch`` returning for every failure is a property all five of its call
  sites rely on, and one of them (``engine.try_write_cycle_snapshot``, reached from the
  scheduler's terminal lane) is not fail-stop and sits in no broad handler, so
  a raise there would end the daemon after the terminal row committed, with no
  halt breadcrumb; the paper loop does not wrap its tick the way the live loop
  does, so a raise would also have traded a silent stall for a crash-loop.
  The containment covers the whole answer, not just the reader call: the
  ``PriceSnapshot`` built on the way out enforces ``received_at >=
  requested_at``, so a host clock stepped backwards mid-request (an NTP
  correction, a resumed VM) used to refuse instants nobody passed in and
  escape as a bare ``ValueError`` through that same unwrapped call site.
  Making the narrowing safe required ``mapper.map_market_snapshot`` — which
  returned its DTO's construction untranslated — to raise its
  ``MarketSnapshot`` refusals (a ``"markPx": "0"``) as
  ``MalformedResponseError`` too; a side effect is that such a response is now
  classified as a retryable malformed response and recorded with an honest
  ``error_type`` rather than escaping to the last-resort handler. A null coin
  name in the universe no longer raises a bare ``TypeError`` from inside the
  unknown-coin message itself. (``map_account_snapshot`` and the position
  mapper still return theirs untranslated; their consumers compensate by
  widening, and that is tracked separately.) The backfill pass's "never
  abort" also stopped depending on its handlers' exception lists — those lists
  are what issue #191 got through — and is now held by an outer per-event
  lane that gives every event a verdict; a stored timestamp or size of the
  wrong type reads as the corrupt row it is rather than falling through to it.
  Finally the two windowed reads no longer share one refusal wording, so a
  naive ``end`` says which read was handed it.
- **hyperliquid_perp: the live daemon no longer crash-loops on a stored
  decision response that fails to re-parse** (issue #180; the #181 tail).
  ``LiveDecisionDriver.resume_startup`` rebuilds a stranded cycle from its
  ``pending_raw_response`` before the loop's tick guard exists, and the
  re-parse ran unguarded: a poisoned row (a corrupted store, or a parser
  change that turned a check into a raise) exited the daemon at startup,
  systemd restarted it into the same deterministic parse, and the real
  position with its resting SL/TP sat unwatched between restarts. The parse
  now fails the cycle closed the way the paper lane already did (PR #178):
  the row goes ``api_failed`` with no ``error_type`` and a
  ``non-retryable:`` message, the response is cleared, and its full text is
  logged at ERROR first (the row was its only durable copy). Startup adoption
  is now contained the way an in-loop tick is: should its own ``api_failed``
  record meet a locked store (an operator's export/validate), the loop enters
  recoverable safe mode and starts anyway — exiting would hand the supervisor
  a restart that can meet the same lock, with the position and its resting
  SL/TP unwatched in between. Both branches then heal inside the loop: a
  poisoned re-parse has already armed the driver's retry lane, which retries
  just that write on each pump, and an unanswered attempt — which arms
  nothing — is re-adopted by ``pump``, which starts no new cycle until
  adoption completes (the stranded attempt still owns ``next_decision_at``,
  so starting one would re-derive its deterministic id and collide on the
  primary key every tick: the wedge adoption exists to prevent, reached
  through the containment). A store that never unlocks is not visible to
  ``validate`` — neither branch writes a terminal row while it retries — so
  the runbook names the journald lines to watch instead.
  Relatedly, a response that did not parse to a decision is no longer stored
  as resumable at all, in either lane: it is nothing to resume, and its
  preserved text is not guaranteed to re-parse to the same verdict — a
  non-str engine answer is kept as its ``repr``, which IS a str on resume and
  re-parsed into the very target the first pass refused. Two invariants that
  guard moves through now live in the repository instead of each writer:
  ``store_pending_response`` is the one writer of the resumable row (the
  paper store, the live store and the live shutdown salvage all land the
  same shape through it; ``update_decision_attempt`` refuses a string for
  that column and ``insert_decision_attempt`` refuses it outright), and a
  terminal write lands ``pending_raw_response`` as ``NULL`` whatever the
  row held — silently, so a writer that forgets the clear cannot kill a
  daemon holding a position. ``ParsedDecision`` now refuses a non-``str``
  ``raw_response`` at construction, so the store's own refusal can never be
  what a retry lane spins on.
- **dataflows: an optional category's failure no longer writes the raw
  transport message — request URL and API key included — into the prompt**
  (issue #171; the #172 and #187 tails). ``route_to_vendor``'s
  ``DATA_UNAVAILABLE: optional <category> could not be retrieved (...)``
  sentinel held ``str(first_error)`` whatever the error was, and a
  ``requests`` message quotes the request URL — FRED's ``api_key`` is a
  query parameter on it — so one connection failure wrote the key into the
  LLM context and the persisted report artifacts. The parenthesis now holds,
  for the generic lane, the vocabulary the no-data outage clause already
  used — ``could not be reached: ConnectionError``, ``answered HTTP 503``,
  and by the same rule ``answered HTTP 400`` for an answer that clause never
  quotes — read off the status and the class, never the text; and for a
  typed vendor error its message flattened and capped at
  ``MAX_UNTRUSTED_CHARS``; the full message stays in the warning log. FRED's
  400 is now a typed ``FredRequestError`` so its reason ("Bad value for
  variable series_id") keeps riding the sentinel. The no-data sentinel's
  outage clause and its detail clause are capped the same way (yfinance
  quotes the library's exception, decoded error body included, with no
  bound of its own), and the no-data lane — the one vendor-failure lane
  that logged nothing — now logs the raise at INFO so the detail's only
  other copy exists. And the nine leaf getters that
  degrade an untyped failure to one line of prose (the yfinance windowed
  indicator getter, fundamentals, the three statements, insider
  transactions, both news getters, and Alpha Vantage's indicator getter) now
  flatten and cap the message on its way in, so "one line" holds for a
  pandas message that quotes a frame — newlines and pipes included — and
  not only for ``KeyError('volume')``. The prefix and the do-not-fabricate
  tail every reader keys on are byte-identical.

- **dataflows: a daemon's global news no longer freezes after its first
  cycle** (issue #198). ``get_global_news_yfinance`` builds each
  ``yf.Search`` from the configured queries alone — no date in the request —
  and yfinance serves ``Search`` through ``YfData.cache_get``, a
  process-wide ``lru_cache`` on its singleton with no TTL and no
  invalidation. A long-lived paper or live daemon therefore contacted Yahoo
  for global news exactly once per process, and every later cycle re-read
  its first cycle's headlines until a restart (paper-BTC-3 on every boot
  segment since 2026-08-28). The getter now forgets that memo before each
  call, so every call is a fresh set of requests. The forget is
  process-wide: it also drops the day's memoized fundamentals-timeseries
  pages behind ``info`` and the statements (their keys carry today's date,
  so those were held a day at most, never for the process) and the timezone
  entries, which yfinance's persistent tz cache serves first. ``get_news``
  (``Ticker.get_news``, an uncached POST) was never affected.

- **hyperliquid_perp: the CLI's funding-rate lookup no longer reads a
  programmer error as a venue outage** (issue #157). ``rate_at`` recorded
  every exception from the funding-history read as a fetch failure — three
  WARNINGs, then an ERROR that read as an endpoint outage while every
  settlement stayed pending forever. It now records only the
  ``ExchangeError`` family as "pending"; a ``TypeError`` from a drifted call
  signature or a ``ValueError`` from a naive clock propagates out of the
  lookup with the failure counter untouched (the engine tick lets it end the
  run; the cycle-boundary backfill's corrupt-row lane still catches a
  ``ValueError`` — a follow-up). Alongside: the agent-authorization check
  refuses a ``NaN`` / ``Infinity`` / out-of-range ``validUntil`` by the same
  named ``AgentAuthorizationError`` as any other unreadable value, where
  those shapes previously escaped as a bare ``ValueError`` / ``OverflowError``
  at live start-up.

- **sosovalue_macro: the oldest print of a series no longer drops the whole
  event history** (issue #188). The live GDP (QoQ) history opens on
  2008-03-27 with ``actual`` and ``forecast`` and no ``previous`` key at all
  — the first print of a series has no prior print to carry — and the
  history parser treated that as a malformed row, failing the entire event
  on every refresh since the 2026-09-02 cutover: GDP (QoQ) vanished from
  the economic-calendar report, and the failure routed the event into
  ``events_failed``, whose 1h retry TTL re-ran the module's full 10-request
  sweep on every call against the shared 20 req/min key (the direct trigger
  for the 429s in #189). A missing or null ``previous`` on the series'
  first print now parses as the empty string, the "no figure" meaning a
  pending ``actual`` already holds. The tolerance is scoped to exactly that
  row — one row by identity, and only when the served history is complete
  (a page at ``HISTORY_LIMIT`` dropped the oldest prints, so its first row
  is mid-series): ``actual`` and ``forecast`` are still required
  everywhere, and any other row without ``previous`` still fails the event
  — a provider that renames or drops the field mid-series must surface as
  a disclosed gap, not as an empty Previous column served for a full TTL.

- **dataflows: one indicator description table, and the windowed indicator
  getter's per-day fallback loop is gone** (issue #137). The sentence each
  indicator report ends with — agent-facing prompt text — lived in two
  verbatim copies in the vendor report lanes, a function-local dict in the
  yfinance getter and a module-level one in the Alpha Vantage vendor,
  editable apart with nothing comparing them. Both vendors now read
  ``utils.INDICATOR_DESCRIPTIONS`` (byte-identical to the old copies, so
  the agent's input is unchanged), the Alpha Vantage registry is a derived
  slice that fails at import on a gap, and identity/marker tests catch a
  re-forked copy in either lane the day it appears. A third verbatim copy —
  the market analyst prompt's indicator menu — is out of these lanes and
  deliberately untouched (prompt text is deployment-segmented); it is
  tracked in issue #187. The
  windowed getter's broad handler used to fall back to a per-day loop that
  re-ran the identical fetch and calculation once per day of the window:
  after the taxonomy (#67) and transport (#116) re-raises, nothing that
  reaches it is transient, so a stockstats/pandas failure rendered a 30-row
  column of blanks under a successful-looking header. The loop — and the
  per-date getter that existed only to serve it — is deleted; an untyped
  failure now renders the same one-line prose every sibling leaf answers
  with. The un-hidden window's handling of yfinance's partial-service
  ``history`` failure (the auto/back-adjust step — the only flag-gated
  ``history`` swallow reachable at this codebase's ``repair=False`` that
  serves the data frame rather than the empty one) is pinned as deliberate:
  rows the library would have served on the wrong price basis become this
  vendor's no-data verdict instead (#38).

- **llm_clients: every LLM call can carry a completion-token cap, and perp
  runs always send one** (issue #177). The OpenAI-compatible family (including
  OpenRouter) never forwarded ``max_tokens``, and no config key could express
  it — the completion budget was left to each upstream provider's discretion.
  One OpenRouter upstream (GMICloud) substitutes the model's full context
  length for a missing cap and then rejects every request as
  input + completion > context: a deterministic HTTP 400 that stalled
  paper-BTC-3 for six consecutive cycles (~24h) with the open position riding
  SL/TP alone. ``max_tokens`` now rides the ``temperature`` pattern:
  ``DEFAULT_CONFIG["max_tokens"]`` (default ``None`` = provider default),
  ``TRADINGAGENTS_MAX_TOKENS`` env override (documented in ``.env.example``
  and the README), validated forwarding in ``_get_provider_kwargs`` (positive
  int only — forwarding 0/negative is the same deterministic-400 stall shape,
  and a typo fails naming the key instead of as a bare ``int()`` error), and
  passthrough in the OpenAI-compatible, Azure, and Google clients (Google's
  field aliases it to ``max_output_tokens``; Anthropic and Bedrock already
  forwarded it). Langchain renames the kwarg on the way out — twice — so the
  tests pin the outgoing payload, not just the constructor field:
  ``max_completion_tokens`` on Chat Completions (OpenRouter honoring it was
  verified live: ``finish_reason: length`` at exactly the cap) and
  ``max_output_tokens`` on native OpenAI's Responses branch. The perp bridge
  adds ``engine.max_completion_tokens`` (validated through the shared YAML
  coercion seam and normalised to int at load, so the value's type does not
  follow its quoting: bools, non-integral floats, junk and non-positive
  values fail closed) — and resolves the cap at daemon startup, not per
  cycle: an env value reaches the bridge unchecked, and left to
  ``build_graph`` a junk one raises outside the retry classification, so the
  scheduler would log an unclassified ``api_failed`` every cycle and hold the
  position on SL/TP alone — #177's own stall shape, reached from a typo. The
  effective cap and its source are logged once at startup (YAML can shadow an
  env var set on the host, and a cap that binds is invisible downstream). An
  unknown ``engine:`` key now warns rather than being silently ignored: for
  most keys a typo lands on a working default, for this one it reverts a
  deliberate raise. The startup refusal is an ``EngineConfigError`` (the new
  base of ``EngineImportError``), so a rejected cap takes the lane a failed
  import already had: over a live position the run degrades to
  protection-only rather than exiting — killing the process would leave the
  position with nobody watching SL/TP, worse than the stall being fixed —
  and flat it is the named exit 1. An engine whose config has no
  ``max_tokens`` key at all (a stale ``tradingagents`` shadowing the
  checkout) is refused by name rather than quietly running uncapped behind a
  log line claiming a cap. Precedence is
  YAML > ``TRADINGAGENTS_MAX_TOKENS`` > perp default 8192 — absent and null
  both fall through to a cap, so the uncapped path is unreachable from a
  perp config. The cap includes reasoning/thinking tokens; switching
  deep-think to a thinking model needs an explicit raise.

- **hyperliquid_perp: the paper lane's post-answer failures follow what is
  already durable, instead of always exiting the daemon** (issue #163).
  Issue #134 gave the stretch BEFORE the AI answers a fail-closed guard; past
  the answer everything still propagated, so a transient SQLite miss — an
  operator's ``export``/``validate`` holding the write lock — discarded a
  paid-for decision or exited over an audit commit whose plan the engine had
  already committed and armed. The two scheduler-owned persists (the §3.1
  ``pending_raw_response`` store and ``_finalize``'s audit commit) now retry
  in-process, keeping the decision and, past the gate, its cached
  ``start_plan`` registration — never re-asking the AI, never re-gating — the
  live driver's ``_PendingResponsePersistError`` /
  ``_PlanRegisteredPersistError`` split, expressed as return values because
  paper's synchronous poll loop has no safe mode. Bounded at ten failed polls
  in a row (any persist that lands clears the streak), after which the
  exception propagates after all: a fault that outlives the bound is not the
  transient lock the lane exists for, and containing it forever would wedge
  the run invisibly (the attempt row stays ``in_progress``, so the issue #50
  streak and ``validate``'s exit 4 never fire while the lease heartbeat keeps
  reporting a healthy daemon). Escalating restores the pre-#163 exit as a
  deliberate signal, and the RUNBOOK now says what a restart does with each
  lane (the store lane re-asks within its §3.1 budget; the audit lane cancels
  the committed plan and re-gates), since neither guarantee outlives the
  in-process retry.
  Re-parsing a stored response on resume now fails that cycle closed like any
  other non-retryable error — a raise there is deterministic, so propagating
  it crash-looped every supervised restart — and the response text is logged
  in full before the terminal row clears it, because ``ai_outputs`` never
  stores raw text and that row was its only durable copy. Terminal rows on
  both lanes now clear ``pending_raw_response``, so no ``api_failed`` row
  presents a consumed response as resumable state. The one post-answer step
  given no containment at all is an exception escaping the engine's
  ``start_plan``: the engine fail-stops (a partially committed plan may
  exist) and refuses every later call, so the position would sit unwatched
  inside a live-looking process — the supervisor's restart rebuilds it from
  the store. Other exits remain and are now named in the RUNBOOK rather than
  implied away: the escalation above, a failure of the terminal ``api_failed``
  write itself (no retry lane — live's ``pending_fail`` has no paper
  counterpart yet), and the cycle-boundary scheduling writes outside every
  guard.

- **dataflows: the date sentinel and the tool descriptions tell the model
  the truth about disclosure-only dates, legal omission, and refusals**
  (issues #144, #140). The shared refusal sentence told the three
  disclosure-only getters' callers their data "cannot be bounded to a point
  in time" — but Polymarket odds and the live fundamentals are never bounded
  by ``curr_date``; it only gates the as-of disclosure, and the wording
  invited retrying historical dates against a live snapshot. ``DateKind``
  gains ``"disclosure"`` ("the report cannot say whether the live {what} are
  as of that date") and that kind's retry sentence offers omission ("or omit
  it") — the model failing right now reads the sentinel, not the description.
  Derived from the kind, never a free flag: the statement lanes' date
  genuinely bounds (look-ahead filtering), so they keep the bounding sentence
  and never advertise the omission that would switch that filter off, even
  though their date-less #73 lane stays legal.
  ``get_fundamentals``'s wrapper makes ``curr_date`` optional like its three
  statement siblings — a tool-schema change the model sees (required →
  nullable-with-default), not just wording — so the omission lane it
  advertises is one its schema accepts, via explicit JSON ``null`` too
  (all four are ``str | None`` now). The fundamentals analyst's prompt
  scopes that exit: fix the format first, omission is the last resort and
  comes back live and undated. Every date-taking tool description now names
  the sentinel it can return, as its own paragraph, attached structurally
  (``tool_notes.notes_date_sentinel`` appends
  ``utils.date_sentinel_note``) instead of hand-written per wrapper — eleven
  wrappers said only "a formatted report" — and a test iterates the runtime
  tools so a dropped note turns red. The echoed value is re-capped AFTER
  ``repr`` (escape expansion grew a "capped" hostile value 4-10x, control
  characters to ~800 chars), by whole characters with balanced quotes; a
  truncated non-string echo re-closes its outermost delimiter; the refusal
  computes its echo once for the log line and the sentinel.
  ``sanitize_untrusted`` / ``MARKDOWN_CONTROL`` / ``EMPHASIS_UNDERSCORE`` /
  ``MAX_UNTRUSTED_CHARS`` are declared supported API for vendor modules, and
  deribit's private ``_MAX_UNTRUSTED_CHARS`` alias is gone.

- **hyperliquid_perp: the window an owning command opens a store in before
  it may upgrade it is declared, floored and race-safe** (issue #147).
  `paper`, `live` and a real `live-smoke` run open a populated store as-is
  and consult the run lease before migrating (issue #129); what they may
  touch in that window lived in three docstrings. `schema.LEASE_READABLE_SINCE`
  now names the floor (v3, where the lease columns arrived), a test runs the
  actual pre-lease readers and the lease write against a store built at
  every version from that floor up, and `Database` refuses an older populated
  store by name under both non-migrating policies — the owning commands and
  the reporting ones (`validate` / `export` / `--gate-status` /
  `live-smoke --dry-run`) print the same sentence and the same remedy —
  instead of letting an owning command's first lease read exit 2 as an
  `OperationalError`. `apply_migrations` re-checks
  each version inside its own `BEGIN IMMEDIATE`: two owning commands started
  against the same behind store in the same moment both saw "not applied"
  outside the lock, and the loser re-ran the winner's `ADD COLUMN` and died
  on `duplicate column name` — it now skips the version, and refuses by name
  if the winner was a newer build. RUNBOOK-live gains the pid-recycling row:
  `live`'s pre-migration lease peek holds no lease and so exempts no pid, not
  even its own, so a hard-killed `live` restarted under a recycled pid is
  refused until the lease goes stale; that refusal's message now ends with
  `(this process is pid N)` so the row can be matched after the process is
  gone (the `already being driven` prefix a supervisor may grep is unchanged).
- **dataflows: a vendor being down is one router reaction whichever vendor
  it was, and a fallback's "no data" after it is not a verdict on the
  symbol** (issues #142, #137). `route_to_vendor` let a no-data verdict
  outrank every recorded failure, so a chain whose primary answered a 5xx
  (or could not be reached) and whose fallback had no rows told the agent
  `NO_DATA_AVAILABLE ... The symbol may be invalid, delisted, not covered` —
  a false statement the agent reasons from — with the outage reduced to one
  log line. The router now remembers the first vendor that was down — the
  `VendorUnavailableError` lane, or in the generic lane an `OSError` that
  either carries no HTTP status (unreachable) or carries a 5xx or a 401/403
  (down, or refusing this client; judged by the status the exception
  carries, since yfinance's `HTTPError` is curl_cffi's, not `requests`') —
  and the sentinel, under the same prefix and whichever position the down
  vendor held in the chain, names that vendor and says to treat the symbol
  as unconfirmed rather than invalid; a no-data verdict beside a plain error,
  or beside a vendor that answered any other status, keeps the old wording.
  What the sentinel quotes of the outage is flattened (`sanitize_untrusted`)
  for the typed lane and the status or exception class alone for the generic
  lane — a requests message carries the request URL, API key included. The
  outage type was yfinance's alone: FRED, Polymarket, Farside and Alpha
  Vantage all left a 5xx to `raise_for_status()` as a `requests.HTTPError` —
  the generic lane, logged as a bug with a traceback; Polymarket's transport
  handler caught it (and a non-JSON body, which `requests` raises as a
  `RequestException` too) and returned a "network error" paragraph the
  router read as an answer. Each now maps a 5xx — and, for the JSON vendors
  FRED and Polymarket, a 2xx body that is not JSON — to
  `VendorUnavailableError` at its request boundary through the
  shared `utils.raise_for_http_status` / `utils.json_body_or_outage`; every
  4xx keeps its vendor's handling, and Farside still serves its stale cache
  from a 5xx exactly as from a network error. Separately, an
  `UnsupportedIndicatorError` — the caller's indicator name, which the tool
  wrapper renders as one line of report text — now outranks a vendor's
  failure at the verdict unless a vendor was down: on the chain
  `alpha_vantage,yfinance` with no Alpha Vantage key, a typo used to abort
  the run on the missing key.

- **hyperliquid_perp: the reconciler's fill cross-check window follows the
  backfiller it holds** (issues #149, #159, #151). The invalid-local-fill
  cross-check window was a module constant derived from
  `fill_backfill.DEFAULT_LOOKBACK_SECONDS`, equal to every `FillBackfiller`'s
  own `lookback_seconds` only by coincidence — the first wiring to pass a
  narrower lookback through would have had the cross-check call every local
  fill between the two windows "a fill the exchange denies". The window (and
  the operator warning that renders it) is now read off the bound
  backfiller's new `lookback` property at each sweep; the whole-hours refusal
  moves from import to the binding. `LiveReconciler` refuses a non-callable
  `fetch_open_orders` / `fetch_clearinghouse` / `fetch_fills` at construction
  instead of reading it as a failed exchange read every sweep, and
  `set_reconciliation_action` — the daemon's overwriting `action_taken`
  writer — accepts machine vocabulary (`MACHINE_DISPOSITIONS`) only; a
  human's disposition goes through `stamp_reconciliation_action_if_unset`.

- **hyperliquid_perp: a YAML `.nan` / `.inf` in a Decimal config field is a
  named config error, not a traceback** (issue #128). `decimal_from_yaml`
  accepted the non-finite float PyYAML produces, and the config dataclasses'
  own range checks (`risk.leverage <= 0` and the like) then raised
  `decimal.InvalidOperation` — an `ArithmeticError` the config-error lane does
  not catch, so the operator got exit 2 and a decimal traceback instead of the
  key name. The converter now refuses non-finite values as a `ValueError`
  (`config key 'leverage': expected a finite number, got nan`), so every
  downstream comparison is finite; the drift comparator's `ArithmeticError`
  clause stays as defence in depth with its test re-pointed at a parser that
  actually raises one.
- **hyperliquid_perp `paper` / `live`: the store is migrated only once no
  other process owns it** (issue #129). Both opened the store with migrate-on-open and
  reached the lease check afterwards, so a new build started by hand while the
  old daemon still ran upgraded the schema underneath it on the way to being
  refused — the ordering `live-smoke` already corrected. An existing store is
  now opened as-is; `paper` migrates inside the lease-holding block, and
  `live` — whose identity/off-coin reads and `--create` write sit between the
  open and its lock — first asks two read-only ownership questions (a fresh
  sibling lease on the wallet; a fresh lease on the run itself, via the new
  `run_lock.peek_run_lock`) and migrates once both say nobody owns the store,
  still taking the real lease later — so a held lease now refuses `live`
  before the §20.2 smoke-gate check rather than after it (a run that is both
  gate-closed and lease-held exits 1 with the lease message, not 4). The three
  commands share `cli._common._migrate_owned_store`, which also closes the
  store-wide half of the hazard: the lease is per-run but a migration rewrites
  the whole file, so when an upgrade is actually owed, a fresh lease on ANY
  other run in the store — the other network's run RUNBOOK-live §7.3 keeps in
  the same `live_trading.db`, or a paper sibling — refuses it by name (a store
  that is already current never refuses). `Database`'s deferred open now
  settles the two edge cases itself: a store migrated by a NEWER build is
  refused at open, before `paper` or `live-smoke` stamps a lease into columns
  it does not know, and an empty store (no file, or a file with no schema yet)
  has no owner, so it is built in full on the way in.
- **hyperliquid_perp: one agent-key refusal for both signing entry points**
  (issue #126). `live` and `live-smoke` each carried their own copy of the
  "agent key is not set" check — which is how #82 fixed one and missed the
  other. `cli._common._require_agent_key` now owns it, alongside
  `_require_api_key`; the two commands' messages are unchanged, and the
  `dotenv_diagnosis` suffix is appended by the helper for the network's actual
  variable so no caller can drop or misdirect it. The `MIN_VOLUME_PROFILE_WINDOW`
  comment also records why that floor stays in `common.constants` rather than
  on a `MarketDataConfig` field.
- **dataflows: `get_prediction_markets` refuses an unusable `curr_date`
  instead of dropping it** (issue #139). Its `curr_date` does not bound the
  data — Polymarket serves live odds only — and was read solely by
  `live_snapshot_note`, which degrades to `""` on a date it cannot parse, so
  `"2026/08/18"` drew a full report with no disclosure: today's odds, served
  as that date's, in the same turn every sibling tool answered
  `INVALID_CURR_DATE` (#119). The getter now refuses a supplied-but-unusable
  date with the shared sentinel before any request, as the two fundamentals
  getters — whose `curr_date` is the same kind of disclosure-only input —
  have since #89; `None` remains the no-disclosure lane, and the empty
  string, being supplied, is refused rather than treated as omitted. The
  judge is the strict one those getters use, so a datetime object or an ISO
  timestamp — which the disclosure helper's lenient reader used to accept —
  is refused too; the tool schema has always declared `yyyy-mm-dd`, and its
  description and the news analyst's hint now say the format is enforced
  and that the argument should be omitted rather than guessed. The
  StockTwits block, the helper's other unguarded caller, is deliberately
  unchanged: its `curr_date` is the graph's own `end_date`, not a model
  argument, so an unparseable one is a programming error: the helper logs
  it, the block renders without a note, and the `get_news` call beside it
  in the sentiment analyst already surfaces the same bad date as
  `INVALID_END_DATE`.
- **dataflows: a Yahoo outage page is no longer read as "no data" on the
  yfinance paths that parse the body before the status** (issue #136).
  `Ticker.history`, `get_news`, `Search` and the second
  (fundamentals-timeseries) fetch behind `info` call `.json()` on the body
  without checking the status, so a 5xx HTML page reached them as a
  `JSONDecodeError` — not the `OSError` #116 let out — and the un-hidden
  window restored it to the library's empty answer: `get_news` reported
  "No news found", `get_global_news` "No global news found", `get_stock_data`
  and `get_fundamentals` the `NO_DATA_AVAILABLE` sentinel, which outranks the
  recorded failure in `route_to_vendor`, so the fallback vendor was never
  tried. Yahoo's "Will be right back" page (yfinance's `YFDataException`)
  left the window raw and the getters' broad handler rendered it as "Error
  fetching ..." prose. Both now leave `yf_fetch_unhidden` as the new
  `VendorUnavailableError` — the vendor answered, but not with data — which
  the getters' existing `except VendorError: raise` lets out and
  `route_to_vendor` handles in a lane of its own: the chain goes on, and the
  warning carries no traceback (the generic lane reserves that for a bug).
  Yahoo answers JSON for every genuine "no data" case, so an unparsable body
  never was one; the 404 verdict (an unknown or delisted symbol) is unchanged.
  `get_global_news`'s `yf.Search` now runs inside the same window as every
  other yfinance leaf rather than plain `yf_retry` — it was the one call that
  could observe a sibling tool's un-hidden window through the process-global
  flag. The quoteSummary payload `info` had already fetched when its second
  fetch fails is still discarded by yfinance; the getter reports the outage
  rather than reaching into the library for it.
- **dataflows: an optional-category getter refuses an unusable `curr_date`
  instead of reporting the vendor down** (issue #119). The seven
  date-bounded getters behind `OPTIONAL_CATEGORIES` — Fear & Greed, both
  ETF-flow vendors, FRED macro, Deribit options, the SoSoValue economic
  calendar and BTC treasuries — now answer the shared `INVALID_CURR_DATE`
  sentinel before any request for a `curr_date` that does not parse (`""`,
  `"abc"`, `"2026/08/18"`, `None`), the verdict the core-category tools have
  given since #109/#111 (Polymarket's `curr_date`, a disclosure input rather
  than a bound, was left unchanged here — see #139 above). They used
  to raise — a bare `strptime` ValueError from four of them, a vendor-typed
  error from the other three — which `route_to_vendor`'s optional lane
  rendered as `DATA_UNAVAILABLE: optional <category> could not be
  retrieved`, so one bad argument drew "fix the date and retry" from
  `get_stock_data` and "this source is down, proceed without it" from every
  positioning and sentiment tool in the same turn. The shared sentinel now
  flattens markdown control characters in the echoed value and caps it, the
  guard Deribit and SoSoValue carried in their own messages (now the one
  definition, `utils.sanitize_untrusted`) — with one difference, since the
  echo goes back to its author: a marker at the value's edge becomes a space
  rather than vanishing, so a date refused only for a stray `_` does not
  come back looking valid; a clean value is echoed unchanged. Each refusal
  leaves one `INFO` log line naming the argument and the data it would have
  bounded — returned rather than raised, it never reaches the router's
  warning lane, so this is an operator's only trace of a model resending an
  unusable date. Two loose ends from #118 close with it (issue #120):
  `alpha_vantage_common.format_datetime_for_api` accepts only `yyyy-mm-dd`
  (its passthrough-stamp, `"%Y-%m-%d %H:%M"` and `datetime` branches were
  unreachable once both news getters refused anything else up front), and
  the verified-snapshot header and its no-rows detail print the requested
  date in ISO, so an accepted `2026/08/18` no longer sits beside a sibling
  tool's `INVALID_END_DATE` for the same string. The four fundamentals tool
  descriptions now say the sentinel is a possible answer (issue #112); the
  optional tools' descriptions are a follow-up.

- **dataflows: a yfinance transport failure is no longer read as a successful
  report** (issue #116). Every yfinance leaf — fundamentals, the three
  statements, insider transactions, both news getters and both stockstats
  indicator paths — re-raises `OSError` ahead of its broad handler, which used
  to turn a reset or a timeout into `"Error retrieving ..."` prose
  `route_to_vendor` records as an answer: the chain stopped at the vendor that
  had just failed and the agent analysed the error sentence (on the indicator
  window, after re-running the failed fetch once per day and rendering a column
  of blanks). Measured on yfinance 1.4.1: it fetches through `curl_cffi`, whose
  request exceptions escape `info`/`insider_transactions`/news unwrapped and,
  like `requests.RequestException`, subclass `OSError`, while nothing in
  yfinance's own `YFException` family does — so one clause covers both
  transport libraries and leaves the taxonomy lane and the library-bug
  degradation unchanged. The clause alone could not reach the failures
  yfinance's own scrapers swallow under `hide_exceptions` — a statement, a
  price history, an insider-filing frame or a news list answered empty,
  `info` a `None` — so `yf_fetch_statement` is now the general `yf_fetch_unhidden`
  and every one of those calls goes through it: a throttle, a reset, a
  timeout, an HTTP 5xx or a 401/403 (Yahoo refusing the client) comes out;
  an HTTP 404 (Yahoo's verdict on an unknown or delisted symbol) and
  anything else is restored to the empty answer the library would have
  given, so those still reach the no-data lane. Operator-visible
  consequence: the shipped single-vendor `yfinance` defaults now fail the
  tool call on a network failure instead of serving error prose; a fallback
  chain (`yfinance,alpha_vantage`) gets its turn. Still outside this fix,
  because yfinance parses the body before it looks at the status: a 5xx HTML
  page on the price history or on `info`'s second (fundamentals-timeseries)
  fetch reads as no data, and on both news getters as "No news found". The
  `get_indicators` tool wrapper now renders only
  the new `UnsupportedIndicatorError` as report text; `VendorNotConfiguredError`
  (a `ValueError`) and the router's own configuration errors reach the ToolNode
  as failures instead of being pasted into the market report (#117). Alpha
  Vantage's indicator descriptions are a module-level registry checked before
  any request, the shared statement filter refuses an unusable `curr_date` on an
  empty frame too, and a served statement coerces its fiscal-period labels once
  (#112, #117).

- **hyperliquid_perp live: the venue-identity fault is bounded at every
  orderStatus consumer, not only in protection** (issue #80). The consecutive
  "answer is not about our cloid" counter that PR #79 kept inside
  `ProtectionManager` now lives in a `VenueIdentityMonitor` shared by
  protection's two probe sites, the reconciler's per-order tiebreaker/settle
  reads and the kill switch's shutdown disarm cross-check, so a persistent
  misroute latches the manual `venue_identity_fault` safe mode wherever it is
  observed — the reconciler escalates after each pass, and the CLI persists the
  escalation after the §18.2 shutdown sweep so the next boot refuses to start
  instead of every shutdown blocking its disarm with only a log line. Transport
  failures stay neutral; every consumer's fail-closed verdict is unchanged, and
  its audit text now names the family (`answered unusably (venue identity
  fault)` vs `failed`). Forensics: an unreadable orderStatus answer carries its
  whole payload on the error and is written to
  `payloads/<run_id>/orderStatus-<cloid>-*.json`, matching what the order-ack
  and fill paths already keep.

### Added

- **hyperliquid_perp: the decision context carries the account's own
  position and the marginal cost of every legal move (prompt
  `phase2-target-v4`).** The 2026-08-27 `/paper-review` of paper-BTC-2 found
  the model resizing in >= 10-point jumps at the deadband's edge and
  re-adding exposure it had just cut — churn the gate's advertised thresholds
  shaped rather than stopped, from a context that never told the model where
  it stood. The daemon provider (paper and live) now reads the run's books
  (`paper/position_facts.py`) and the new pure pricer
  (`domains/perp/marginal_cost.py`) attaches a `Position:` section:
  side/size/entry/unrealized PnL, committed margin % of equity, last fill,
  holding cost per 8h at the current funding rate, and — per displayed legal
  target — the notional traded, the ROUND-TRIP cost (fee and slippage on
  both legs, from `paper_trading.execution`, 19 bps under the defaults) and
  the same restated as breakeven bps. Facts and prices only: no gate rule,
  no accumulated (sunk) cost. Flat renders one line; unusable books (no
  ledger, equity <= 0) omit the section with a WARNING. The one-shot CLI
  stays position-blind. `context_shape` gains `position` (open vs flat is
  the account's state, already on `ai_inputs.current_position_side`, not a
  shape); the format block is unchanged (same digest). Not cherry-picked to `deploy/paper` until the
  current A/B segment has its >= 8-episode review.

- **US economic calendar + corporate BTC treasuries (SoSoValue).** Two new
  news-analyst categories for crypto assets, served by the SoSoValue key
  already used for ETF flows (shared plumbing — auth, request envelope, error
  taxonomy, clock/cache-age helpers — extracted from `sosovalue.py` into
  `sosovalue_common.py`; four deltas for the ETF path: an out-of-range int now
  fails the finite-number check instead of raising, vendor text reaching a
  raised message is flattened through the shared markdown sanitizer rather than
  raw-sliced, the unset-key message names the SoSoValue-backed category
  generically instead of the ETF chain, and a cached fund carrying two rows for
  one date is now rejected at read time — previously whichever copy the file
  listed first was served as that day's flow). `economic_calendar`
  reports the scheduled US releases from `curr_date` through the next two
  weeks (today included) with consensus forecasts
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
  filing (carrying no cost or implied price, since that change spans
  everything since that filing); ETH and other recognized risk assets get the
  BTC data as a labelled
  market-wide demand proxy. Both categories are optional (sentinel
  degradation), cache on rolling snapshots with stale fallback, and **ship
  disabled** (`"none"`): the key already sits on deployed boxes, so landing
  them enabled would change a running paper deployment's analyst input
  surface with no server-side action to date the change from — flip them
  together as one deliberate, separately dated cutover, clear of the
  `options_data` one (already dated 2026-08-12), so the two input-surface
  changes stay attributable apart.
  Both reports state the semantics of what they render rather than leaving
  them to be inferred: the unit each figure carries (a payrolls actual of
  `-23` is thousands of jobs), that a surprise is actual minus forecast and
  its sign is not a directional verdict, when the snapshot was fetched even
  when it is fresh, which aggregate figures mix filed with derived values,
  and — when a section is empty — whether that is a quiet window or this
  snapshot's own coverage gap. A provider that merely reshapes its output
  (a longer calendar, a repeated calendar date, an unreadable calendar day-row,
  comma-grouped numbers, a listed company with no filings yet) degrades to a
  disclosed, counted partial rather than failing the category into a stale
  serve that expires — the calendar is parsed before any history request, so
  failing it over one bad row would discard every tracked event's figures too.
  An unreadable day-row is disclosed by the dates it cost (a row whose only
  fault was its event list still carries a usable one) and shortens the cache
  TTL, so the hole is retried in hours rather than re-served for the full
  period — to a dedicated middle value, not the shortest one, which stays
  reserved for failures that are transient by construction; a permanently
  malformed provider row on the shortest TTL would be a standing fivefold
  request amplification with no path back. The count stays the authority on how
  much was dropped: a row that lost its date too is counted but cannot be
  named, a date still rendered anywhere in the report — from the calendar or
  from a tracked event's history, which the tables draw on equally — is
  withheld rather than listed as lost, and a long list is capped so a wholesale
  contract break cannot push kilobytes of dates into the prompt.
  Coverage claims are worded so that content **this client** dropped is never
  read back as the provider's own silence: the snapshot's calendar span is
  labelled as the snapshot's rather than the provider's, and where a caveat in
  the same header already names the cause of a short calendar — a dropped row,
  or a snapshot fetched before the report's date — the report names it too
  alongside it — but only where that cause could actually account for the gap.
  A dropped row the report can place before the calendar's last dated entry, or
  a dropped event *name* that left every day-row past that entry standing,
  explains nothing about a short forward tail and is not offered as if it did;
  and the caveat naming the drop now claims each end of the rendered span
  separately, so it cannot re-raise a possibility the sentence above just
  declined. Reading an empty window as genuinely quiet
  additionally requires that the scheduled table really is empty (it is fed by
  forward-dated event histories as well as by the calendar) and that the
  snapshot is current, and it no longer extends to events this feed does not
  carry at all, which would have contradicted the standing Fed-decision caveat.
  A non-integer `look_back_days` is reported as this vendor's error class
  rather than escaping as a raw `TypeError` (an unparseable `curr_date` is
  answered by the shared refusal described above rather than raised, since
  #119), and the treasuries coverage denominator no longer
  presents a client-shrunk listing count as the provider's own.
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
  It shipped **disabled** and was **cut over to `"deribit"` on 2026-08-12**, so
  the category is on by default. Being keyless, shipping it on at merge time
  would have changed a running deployment's analyst input surface the moment the
  code landed, with no server-side action to date the change from; the dated
  cutover is that action. The perp engine's config overlay carries a fixed key
  list and does not pipe `data_vendors` through, so for that deployment this
  default is the live value and switching the category back off is a code change.
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
  Vendor error text and the caller-supplied `asset` are flattened before they
  are interpolated — whitespace collapsed and mid-line markdown markers
  removed; an unusable `curr_date` is refused with the shared
  `INVALID_CURR_DATE` sentinel before any of this runs (#119) —
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

### Changed

- **hyperliquid_perp: the output-format block no longer tells the model the
  gate's threshold numbers (prompt `phase2-target-v5`).** The second half of
  the marginal-cost plan (PR-B; PR-A was `phase2-target-v4`). The 2026-08-27
  `/paper-review` of paper-BTC-2 put 27 of 48 confidences at exactly 0.72 —
  `resize_min_confidence` plus 0.02 — and every approved rebalance exactly
  one `rebalance_deadband_pct` from the last: rendered as numbers, the three
  thresholds (`min_confidence` included) were anchors the model steered to,
  and the gate rejected 1 of 48 while the churn it exists to stop went on.
  `decision_format_instructions` now states the three rules qualitatively
  ("too low is rejected", "only marginally away creates no order", "a resize
  that would actually trade is held to a higher confidence bar") and renders
  no threshold value; the legal grid and the effective ceiling still render,
  and the RiskGate enforces the numbers unchanged. Consequences for the
  segmentation keys: `format_fingerprint` follows what the model reads, so a
  `decision:` threshold edit no longer moves it (the run-id rule for strategy
  parameters still applies); `PROMPT_VERSION` moves to `phase2-target-v5`,
  so the next `deploy/paper` push starts a new `prompt_version` segment —
  time it after the v4 segment has its >= 8-episode review (phase2-spec §2.4
  records why v5-segment confidences cannot be pooled with v2–v4).

- **hyperliquid_perp: the context guards and the no-decision policy now live
  below the engines** (issue #122; pure relocation, no behaviour change). The
  decision cadence `CYCLE_INTERVAL` and the store's timestamp decoder
  `parse_instant` moved from `paper/scheduler.py` to `common/constants.py`
  and the new `common/instants.py` (the scheduler still re-exports both), so
  the freshness guard derives its "3 x the 4h decision cycle" ceiling from
  the shared constant instead of drift-locking a hand-written copy, and
  `paper/no_decision.py` — the issue-#50 escalation policy both engines and
  both validators consume — moved to `common/no_decision.py`, where importing
  it no longer loads the paper engine (its logger is now
  `contrib.hyperliquid_perp.common.no_decision`). The four pre-LLM context
  guards moved from `engine_bridge.py` to `domains/perp/context_guards.py`
  as public names (`context_refusal`, `context_refusal_message`,
  `warmup_threshold`, `UNUSABLE_CONTEXT_ERROR`), with `indicator_names` /
  `DEFAULT_INDICATORS` beside the vocabulary they resolve in
  `indicator_vocab.py`; `engine_bridge` keeps no re-exports, so tests patch
  `context_guards.warmup_threshold` directly. A shared `whole_hours_label`
  replaces the two hand-rolled whole-hours label guards, `schema.parse_interval`
  resolves a candle interval once for both `interval_to_ms` and the context's
  constructor, and `tests/common/test_layering.py` pins the guard family's and
  the policy's load-time import closures to `domains.perp` + `common`. The
  freshness tests' exchange-clock branch now also runs with the host two
  hours behind the exchange, pinning that skew changes the refusal's wording
  and never its verdict.
- **A vendor's rate limit is now discovered once per cycle by the router, for
  every vendor** (issue #114). `route_to_vendor`'s rate-limit lane is the one
  point every vendor's throttle passes through, so it now remembers a vendor
  that has just raised `VendorRateLimitError` for a short window and skips it
  in its turn without contacting it — the chain goes on in its configured
  order, and a chain with nothing else to say degrades exactly as it would
  have after being refused again (the throttle for a core category, the
  `DATA_UNAVAILABLE` sentinel for an optional one). Alpha Vantage's per-key
  daily quota, Deribit, and any vendor registered later are covered without a
  per-family copy. The latch mechanism and its window move to the new
  `dataflows.throttle` module; the router holds one latch keyed by vendor
  name, and **the yfinance latch PR #113 put in `yf_retry` stays its own**,
  for two reasons: the indicator path reaches that boundary only after the
  OHLCV cache `load_ohlcv` reads first, so a symbol whose bars are on disk is
  still served while Yahoo is being stood off from (a router-level skip would
  refuse it in front of the cache), and the verification snapshot builder
  calls into it without routing. Only a raised throttle arms the router's latch — a vendor that
  renders a partial throttle into its report (Deribit, when not every request
  was refused) is still contacted next time — and a `VendorRateLimitError`
  subclass can say its raise is not about the client's standing
  (`latches_vendor = False`): `YFinanceRateLimitError` does, because the
  standing-off is done behind its cache, and `SoSoValueRateLimitError` does,
  because it reaches the router only when the throttled call also had no
  usable cache, while its sibling tools answer the same throttle with a
  stale-cache report that a latch would have turned into `DATA_UNAVAILABLE`.
  When a chain ends on throttles alone, one actually met outranks a latch
  skip whatever the chain order, since it carries the vendor's own detail.
  Alpha Vantage's "premium endpoint" notice is no longer classified as a rate
  limit: it says the key lacks an entitlement it will not gain by waiting, so
  it now raises `AlphaVantageNotConfiguredError` like an invalid key (the
  router still moves to the next vendor; the notice stops being described as
  transient, and stops keeping every free endpoint unasked for the window).
  The daily-quota notice, which also mentions the premium plans, stays a rate
  limit, and any other premium-flavoured refusal still raises the entitlement
  verdict rather than returning to the caller as data. Separately, a failure
  `yf_fetch_unhidden` restores to the library's empty answer (a 404, an
  expected library condition, a scraper bug) no longer reaches `yf_retry`
  looking like data: it travels as an internal signal, so it neither clears
  a throttle latch a sibling thread had just armed nor makes `yf_retry`
  inspect return values. Cost only — what any caller gets is unchanged.
- **hyperliquid_perp: the remaining duplicated live constants are derived or
  drift-locked** (issues #102, #122). No behaviour changes. The exchange
  minimum order value moves to `common.constants.EXCHANGE_MIN_ORDER_NOTIONAL_USDC`
  (`live.config` re-exports it) and is now the paper `min_notional_usdc`
  default rather than a second `10`; `live.config.DEFAULT_SCHEDULE_CANCEL_SECONDS`
  names the kill-switch default and validation's fallback derives from it; the
  smoke suite's per-test floor is one `live.smoke.SMOKE_MIN_KILL_SWITCH_DEADLINE`
  shared by the dataclass default and the CLI (pinned `>=` the daemon default,
  not equal to it by rule); the reconciler's fill cross-check window and its
  operator warning derive from the backfiller's lookback; the equity-tolerance
  tests derive their fixtures from the constants and pin the bound on both
  legs; `LEGAL_NETWORKS` is locked to the SDK client's URL table; and
  `configs/hyperliquid.example.yaml` plus the RUNBOOK's threshold sentences
  are pinned to the constants they restate. `RetryableDecisionError` now
  checks `error_type` against `ERROR_TYPES` at construction, so a producer's
  typo fails on the raise instead of at the repository write boundary.
- **hyperliquid_perp: the reconcile vocabulary guard's two tails** (issue
  #104). No behaviour changes on the happy path. The orphan back-fill's
  `local_row_backfilled` stamp — the one case the sweep builds after its write
  — is now checked at import with the three case-less stamps, so an
  unclassified rename refuses to start the daemon instead of leaving an orders
  row with no case row explaining it. The disposition AST scan now covers
  every module under `live/` and resolves positional `ReconciliationCase` and
  stamp-writer arguments against the real signatures, not only the
  `action_taken=` keyword in `reconcile.py`.

- **Breaking for direct callers of `alpha_vantage_common.format_datetime_for_api`**
  (issue #120): it accepts only a `yyyy-mm-dd` string and raises `ValueError`
  for the `"YYYYMMDDTHHMM"` passthrough, the `"%Y-%m-%d %H:%M"` string and
  the `datetime` object it used to read. The routed news tools never sent
  those (both refuse anything else first), so only a direct caller notices.

- **Hyperliquid: the freshness guard's two bounds now describe a healthy
  feed.** Stale side (issue #92): the three-decision-cycle ceiling (12h) no
  longer clamps the candle-age limit below one bar. With
  `candle_interval: 1d` the newest CLOSED bar ages from zero to 24h across
  every day, so the 12h cap refused every cycle from 12:00 UTC on with the
  exchange healthy and the clocks agreeing — and since PR #91 counted them
  as a stalled feed toward `validate`'s exit 4. The limit is now never
  below one bar: where the clamp would put it there it becomes one bar plus
  one decision cycle (28h for 1d, labelled `one 1d bar plus one 4h decision
  cycle` in the refusal), so a daily feed that misses a boundary is refused
  once its newest closed bar is over 28h old — the first or second cycle
  after the miss (consecutive cycles are over 4h apart, so at most one lands
  inside the grace window). The 12h cap
  remains for an interval over one cycle and up to three, which no
  configurable interval is (4h is exactly one cycle and takes the uncapped
  `3 x 4h`), so its `capped at` label is not printed by any shipped
  configuration; the host-clock fallback shares the stale limit as its
  future tolerance, so its 1d future bound widened from 12h to 28h with it
  (pinned by a test). Future side (issue #93): on the exchange-clock path a
  candle closing more than sixty seconds AFTER the exchange's clock was
  refused, instead of being tolerated up to the stale limit — the
  still-forming bar a host running AHEAD pulled through `get_candles`'
  host-cut window. Superseded within this same unreleased span by issue
  #124 (next entry), which moves the window cut itself onto the exchange's
  clock so no live fetch can produce such a bar; the tolerance, its
  three-way cause wording and the "candles before the clock" read order
  went with it. Tests (the #92 half): a healthy 1d feed passes every cycle
  of a day and its last second on both clocks, a three-day-old daily feed
  is still refused and a 27h-old one is not, the limit sits between one
  bar and the uncapped bound for every interval.

- **Hyperliquid: the candle and funding windows are cut at the exchange's
  clock, not the host's** (issue #124, **behaviour change**).
  `ExchangeMarketData.get_candles` / `get_funding_history` take a required,
  keyword-only, tz-aware `end` and read no host clock at all;
  `_build_context` reads the exchange's clock (public `l2Book` `time`)
  FIRST and hands that same reading to both fetches (the order is pinned by
  a test, the other way round from #93 — a clock read after the fetch would
  pass a bar that closed during it while the response carried its OHLCV as
  captured before the close). Consequences an operator should know: a host
  running AHEAD no longer admits the forming bar — the context's `as_of` is
  the previous CLOSED bar's close, the guard passes and the cycle decides,
  where #93 refused the cycle and blamed the clock; a host running BEHIND
  no longer truncates either window (the newest funding settlements were
  silently missing from the z-score sample before). The freshness guard's
  exchange-clock path therefore has ONE stale cause ("the feed stopped
  advancing" — the host's clock cannot have truncated a window it did not
  cut; the skew is carried as information) and NO lead tolerance: any
  candle closing after the exchange's clock is a context no live fetch
  produced (replay, hand-built, or candles and clock from different
  fetches). The skew WARNING still fires past one minute and now says what
  a steady offset does reach — the durable record's host-side stamps and
  the settlement hour funding accrual asks for — not the market data, and
  not the rolling decision cadence. `cli._provider`'s funding-rate lookup
  is the one windowed read that deliberately passes the host clock (it
  looks up a past hour; a miss is `pending`, never a wrong rate). The
  residual trust is now on the `l2Book` stamp alone (RUNBOOK §7 says so): a
  venue stamp that regressed would look exactly like a stalled feed. One
  accepted edge: a bar closing in the sub-second gap between the clock
  read and the candle fetch sits outside the window for that cycle, so
  `as_of` — and with it the analysts' `trade_date` — is one bar older;
  with `candle_interval: 1d` at a UTC midnight that is the previous day.
  Tests: the acceptance fixture (4h bars, exchange 3h30 into the forming
  bar, host 90 minutes ahead) returns the previous closed bar and the guard
  passes; the cut is inclusive at a bar's close; both windows end at the
  clock handed in; a naive `end` is refused before any request; `end` has
  no default on the port or the reader; the stale verdict names the feed
  under every skew; any lead — including one inside the old minute — reads
  as a non-live context under every pairing; the module carries no lead
  tolerance.
- **Hyperliquid: the market-context freshness guard and the no-decision
  escalation policy each have their own module.** The exchange-clock rewrite
  (PR #91) had grown the freshness guard to ~200 lines inside
  `engine_bridge`, whose job is composing exchange reads into engine inputs;
  it now lives in `domains/perp/freshness.py` (`freshness_refusal`,
  `ContextRefusal`, the age-limit and clock-skew helpers), SDK-free and
  persistence-free, with `engine_bridge._context_refusal` calling it as the
  last of the four pre-LLM guards. `interval_to_ms` moved beside the
  `CandleInterval` enum in `domains/perp/schema.py` to make that possible
  (**breaking for direct importers:** `exchanges.hyperliquid.market_data`
  no longer exports it). The issue-#50 no-decision policy — threshold,
  recency window, `TrailingFailureStreaks`, the store query,
  `no_decision_shortfall` and the per-cycle `note_cycle_outcome` the two
  running loops call — moved from `paper/validation.py`, a read-only
  acceptance validator by contract, to `paper/no_decision.py`
  (**breaking for direct importers:** `paper.validation` no longer exports
  those five names). The §6.2 `decision_attempts.error_type` vocabulary is
  now defined once in `common.constants.ERROR_TYPES` (public), re-exported
  by `persistence.repository` as the storage vocabulary; `_vocab._ERROR_TYPES`
  is gone. One behaviour change rides along: `ContextRefusal` is a frozen
  dataclass that validates its class against that registry at construction,
  so a misspelt class raises a `ValueError` where the guard is built — in
  the one-shot entry points too — instead of out of the repository on the
  daemon's first refused cycle (the paper daemon still aborts, only earlier;
  the live driver's `_start` fail-closed branch now records it as an
  unclassified `api_failed` instead of arming `pending_fail` and re-raising
  on every pump).
  Two of issue #94's proposals were reviewed
  and deliberately not taken: guarding the `LiveValidationReport`
  streak/shortfall pairing (whether a streak is current depends on `now`,
  which is not a report field — the paper report already records the same
  decision), and withholding `no_decision_shortfall` when the newest cycle
  is stamped after the validator's clock (the live validator reads `now`
  before its store query, so a daemon finalizing a cycle in between produces
  exactly that stamp, and the run must not pass the gate; a test now pins
  the future side as reported). Tests: the freshness bound, floor,
  ceiling and age-format tests now run under both measuring clocks (all but
  the two age bounds ran on the host-clock fallback alone, which production
  never takes), plus the sixty-second cause-wording floor, the
  unpaired-reading branches, the sole-cause skew boundary and the two
  `PerpMarketContext` host-reading rules (issue #94).
- **Breaking for direct callers of two Alpha Vantage getters.** `get_stock`
  refuses a slash-separated or time-suffixed `end_date` it used to serve, and
  `get_news` refuses the intraday `"YYYY-MM-DD HH:MM"`, `"YYYYMMDDTHHMM"` and
  `datetime` forms; both answer the shared `INVALID_*_DATE` sentinel instead.
  The routed tools only ever send `yyyy-mm-dd`, so nothing routed is affected.
  Detail in the Fixed entry on unusable dates below.
- **The configured article count now reaches both news vendors.**
  `news_article_limit` and `global_news_article_limit` are what the tool
  wrappers document as the source of those defaults, and were read by the
  yfinance getters only. Two things this does not equalise: how much comes
  *back* (yfinance fetches that many and filters the window client-side, Alpha
  Vantage filters server-side), and a count set above either vendor's own
  request ceiling. The Alpha Vantage siblings carried literals instead: global news
  asked for 50 articles over a hard-coded 7-day window, and ticker news sent no
  `limit` at all, leaving the endpoint's own default of 50. Both now read the
  same config keys the yfinance getters read. **Article counts change for Alpha
  Vantage users:** global news 50 → 10 (`global_news_article_limit`) and ticker
  news 50 → 20 (`news_article_limit`), so prompts get shorter and cheaper;
  raise either key to restore the old volume. An explicit `look_back_days` or
  `limit` argument still outranks the config. The shipped `news_data` default is
  a single-vendor yfinance chain, so a deployment that has not switched vendors
  is unaffected.

- **One yfinance throttle is now discovered once per cycle, not once per tool.**
  `yf_retry` is the single boundary every yfinance network call goes through,
  and its 2+4+8s backoff ladder ran independently per call. A 429 is Yahoo
  refusing this client rather than one endpoint, so every yfinance-first tool
  queued behind the first one re-discovered the same refusal: each slept
  through a ladder of its own and each made four more attempts against a host
  already turning the client away, learning nothing the first had not already
  established. An exhausted ladder now records a short deadline; while it
  stands, `yf_retry` raises the taxonomy's `VendorRateLimitError` immediately
  without contacting Yahoo, and any answered request drops it. **The call that
  discovers the throttle still pays the full ladder** — the shipped defaults
  give the four yfinance categories a single-vendor chain, so there is no
  fallback vendor to hand a brief throttle to, and lowering `max_retries` would
  have traded resilience away rather than removing waste. The window is far
  shorter than the perp scheduler's cycle interval, so the next cycle always
  re-probes Yahoo. Nothing but an exhausted throttle records a deadline, and no
  caller sees a new failure shape: the same error type is raised from the same
  function, only sooner and fewer times. Operator-visible effect: a throttled
  cycle logs one backoff sequence instead of one per tool. Separately, the test
  that checks each routed yfinance implementation lets a rate limit propagate
  now derives its leaf list from `VENDOR_METHODS` instead of a hand-written
  third list — which had already drifted, leaving `get_stock_data` unchecked.

- **SoSoValue cache-read plumbing deduplicated.** The remaining two
  near-verbatim copies the earlier extraction left behind now live once in
  `sosovalue_common.py`: the cache-file read preamble every `_read_cache`
  opens with (`_read_cache_preamble`, with `_cache_rejecter` owning the
  rejection-log format) and the macro/treasuries dated-history-row shape
  skeleton (`_valid_dated_rows`; the ETF module's `summary_rows` deliberately
  stay on their own strictly-ascending check), plus the shared
  non-negative-count predicate (`_is_non_negative_int`). Behaviour, log
  wording, and rendered reports are unchanged (verified byte-identical
  against fixed cache fixtures).

- **Vendor plumbing deduplicated; a disabled news category is no longer
  advertised.** The SoSoValue vendor family's three near-verbatim copies of the
  cache/TTL/stale-fallback discipline and the two twin per-item sweep loops
  (429 drain + network breaker) now live once in `sosovalue_common.py`
  (`load_rolling_snapshot`, `fetch_each`); each module keeps only its own TTL
  policy and payload shape, and its logger, raised messages and cache formats
  are unchanged; log wording is too, except the 429-drain line's bare
  "histories" noun, which now matches the breaker line's per-module noun
  ("event histories" / "company histories"). The ETF module's fund loop
  deliberately keeps its older per-item-retry rate-limit semantics rather
  than adopting the drain.
  The four-way `_classify_asset` copy (farside / sosovalue / treasuries /
  deribit) became `symbol_utils.classify_crypto_asset`, and the family's
  printable-ASCII text gates and STALE caveat line are shared
  (`_is_safe_text`, `_stale_caveat`). The news analyst's optional-tool
  registration is table-driven (`OPTIONAL_NEWS_TOOLS`), and the news ToolNode
  registers from the same table, so a category can no longer be bound in one
  place and forgotten in the other. One behaviour change rides along: setting
  `macro_data` or `prediction_markets` to `"none"` now removes both the tool
  binding *and* its sentence from the analyst prompt — previously both stayed,
  and the model could spend a tool call only to receive the disabled sentinel.
  Default-config prompts and bindings are byte-identical.

- **The perp decision prompt ships a schema, not a worked example.** The
  output-format contract used to carry a complete, *valid* `maintain_current`
  example (`confidence: 0.55`), and models returned it as their answer: on the
  `paper-BTC` run, 117 of 159 outputs (74%) reproduced its four decision fields
  verbatim, every output carrying exactly 0.55 was one of them, and the run went
  21 days without a fill — the position was frozen because no *target* was ever
  proposed, not because risk gates rejected one. The four typed fields now hold
  type-illegal placeholders (`"<set_target|maintain_current>"`, `"<0.0-1.0>"`,
  …), with `requested_target_margin_pct`'s bounds rendered from the live config
  so they still cannot drift; `rationale` and `key_risks` keep
  legal-string placeholders, since only the typed fields decide whether an
  output is a directional order. An echo keeps landing on the same harmless
  `maintain_current` — the parser fails closed — but is now tagged rather than
  counted as a decision. Which tag depends on the echo: a whole-block echo that
  keeps the block's quoting fails on `decision_mode`, the earliest of the four
  the parser coerces; a partial echo fails on whichever **typed** placeholder it
  kept (one that kept only `rationale` or `key_risks` parses cleanly, by
  design); and an echo that also unquotes the numeric fields stops parsing as
  JSON and lands on `invalid_output`. None of these tags is exclusive to echoing — a
  hallucinated `decision_mode` records identically — so read them as a proxy for
  it, not as proof. The `requested_target_margin_pct` and `confidence`
  placeholders are quoted only so the block stays valid JSON, so the contract
  spells out that those two are written as bare JSON numbers and `null` as the
  JSON literal, while every genuinely-string field keeps its quotes; no
  placeholder offers `|null` inside its quotes, because that is an invitation to
  substitute in place and leave them — the one mistake that costs a real
  proposal rather than an echo. `PROMPT_VERSION` moves to `phase2-target-v3` so
  `ai_inputs.prompt_version` splits before/after when measuring whether the
  proposal rate recovered; a test pins the stamp to a fingerprint of the
  rendered block, since the two live in different modules and nothing makes the
  constant track the text, so a prompt edit that forgot the bump would merge the
  two populations silently.

  **Expect more unparseable cycles while echoing persists** — that is
  previously-hidden echoing becoming visible, not a new defect — and note where
  that lands. No validator has a threshold on `invalid_output_count`, but these
  cycles used to parse as valid `maintain_current` and therefore counted toward
  the live validator's ≥30 `cycle_count` gate, which admits `completed` only; a
  live acceptance run will now need proportionally more cycles. The paper
  validator still counts them toward its own gate — which is the half worth
  saying out loud: its `cycle_count` cannot tell 30 decisions from 30
  unparseable outputs, and unlike the live report it carries no
  `invalid_output_count` to separate them, so read `order_count` and the
  exported `decision_attempts` statuses before trusting `phase3_ready`. The
  one-shot `python -m contrib.hyperliquid_perp.main` path also exits **3**
  (documented in SETUP.md as the model-drift alarm) on an echo that previously
  exited 0; the paper and live daemons do not go through that path.

  Each such cycle now stores `risk_action = invalid_fail_closed` — documented in
  `phase2-data.md` as the model-drift alarm — plus `risk_reason` /
  `decision_reason = invalid_decision_mode`, `confidence = NULL` and an empty
  `key_risks`, where the same echo previously stored `approved` /
  `maintain_current` / `0.55`. The `decision_mode` column still reads
  `maintain_current` for these rows, so grouping by it shows no change at all
  across the boundary unless `risk_action` is filtered too. Expect the alarm
  value in bulk at first. The NULL
  confidence also biases any before/after comparison of the confidence
  distribution: the old echo's `0.55` spike disappears whether or not the model
  changed, so judge the change on the proposal rate and render the NULL bucket
  explicitly if the distribution is plotted at all. The proposal rate needs one
  correction of its own: a fail-closed row stores `requested_target_margin_pct`
  as NULL whatever the model asked for, so the cycles that prove a numeric
  margin was nevertheless supplied must be netted back in, or a recovered model
  reads as one that never proposed. Those are the six tags that can only follow
  a margin the model actually supplied as a figure: `margin_off_step_grid` and
  `margin_out_of_range` (the value itself was rejected), plus
  `flat_with_nonzero_margin`, `directional_side_with_zero_margin` and
  `set_target_without_confidence` — all five sitting past the
  `set_target_without_margin` guard, which is what makes a null margin unable to
  reach them (one of them, `margin_off_step_grid`, is additionally emitted
  inside the coercion block for a non-integral number) — and
  `margin_quoted_number`, the one member that fails *before* the coercion.
  "Anything tagged after the coercion" is the wrong rule — a null margin skips
  the coercion rather than failing it, so
  `invalid_key_risks` and `missing_rationale` are reachable with nothing
  proposed. `margin_not_numeric` and `confidence_not_numeric` are likewise out:
  they now carry only the strings that are not figures at all — an echoed
  placeholder, or a `maintain_current` that quoted its `"null"`. So is
  `confidence_quoted_number`: margin is coerced first and a null margin skips
  that block, so it is reachable with nothing proposed.
- **A quoted figure is tagged apart from an echoed placeholder.** A string in
  `requested_target_margin_pct` / `confidence` is still refused and the cycle is
  still fail-closed — the safety semantics are unchanged — but the tag now says
  which refusal it was: `margin_quoted_number` / `confidence_quoted_number` when
  the string parses as a finite number, `margin_not_numeric` /
  `confidence_not_numeric` otherwise. One tag previously covered a leftover
  placeholder, a quoted `"null"` and a figure typed as `"35"`, and because a
  fail-closed row stores the margin as NULL and the raw response is not
  persisted, the three were indistinguishable afterwards — on the single metric
  this prompt change is judged by. Like `margin_off_step_grid`,
  `margin_quoted_number` is emitted inside the coercion block and reads no
  `decision_mode`, so a `maintain_current` that quoted its margin as `"0"`
  reaches it too: the netted rate counts cycles where the model supplied a
  figure, which is what the proposal rate has always proxied.
- **The Rules preamble no longer advertises maintain_current as the cost of a
  violation.** It said violations are "discarded and treated as
  maintain_current", ten lines under a new sentence saying the same cycle is
  "recorded as a model-format failure" — one page, two answers, and the one it
  gave first is a zero-cost no-op for a model whose failure mode is exactly
  preferring no-ops. The wording is now the same in both places; the discard
  behaviour itself is unchanged.

### Fixed

- **`hyperliquid_perp`: a frozen DTO can no longer contradict the values it
  says it was derived from.** `PerpMarketContext.day_change_pct` is a float
  ratio stored beside the two prices it comes from, and nothing checked them
  against each other: a hand-built or replayed context could render
  "24h change: 40.00%" over a mark and a previous-day price that give 1.7%,
  with every existing guard passing. The rule now lives in
  `schema.derive_day_change_pct` — `context_builder` fills the field with it
  and the DTO checks against it (relative tolerance, so a dust `prevDayPx`
  under a real mark still builds) — and the `None` case is the rule's own:
  `None` exactly when `prev_day_price` is `0` (no 24h reference yet), a value
  otherwise; `prev_day_price` also gains the snapshot's `>= 0` guard.
  `VolumeProfile` gets the four guards its own
  comment listed as definable but unwritten: `shape` is re-derived from the
  three stored fractions with the producer's rule — a profile labelling itself
  `P` with its POC at 6% of the range, which rendered as one
  self-contradicting block, is refused naming the letter its numbers give —
  `candle_count` is pinned to the producer's window floor and `bucket_count`
  to its grid (they admitted 1–11 and anything but 24), and the two volume
  shares get the floors the value-area walk guarantees (`1 / bucket_count`
  for the POC bucket, `VALUE_AREA_FRACTION` for the area). To make that
  possible without a circular import, the shape rule now lives in
  `schema.derive_profile_shape` (called by `classify_shape`, so producer and
  DTO share one definition) and the profile's thresholds and grid
  (`VOLUME_PROFILE_BUCKET_COUNT`, `VALUE_AREA_FRACTION`,
  `THIN_VALUE_AREA_RATIO`, `POC_UPPER_BAND`/`POC_LOWER_BAND`,
  `RANGE_MIDPOINT`) moved to `common/constants.py` beside the window floor;
  `volume_profile` imports the grid (as `BUCKET_COUNT`) and
  `VALUE_AREA_FRACTION` from there, and the four shape thresholds now exist
  only in `common.constants`, read by `schema.derive_profile_shape`. No
  producer output changes — every guard is a floor the producer already
  honoured — but a context or profile built any other way now has to agree
  with itself (issue #100).
- **`hyperliquid_perp`: the run-segmentation signal was wrong in both
  directions.** (1) Resuming a run whose YAML had gained a key at its documented
  default — `market_data.volume_profile_window_candles: 0` copied from the
  newer example config — reported config drift and stamped the
  `config_drift = "drift"` breadcrumb the paper review reads as a regime break,
  although nothing about the run had changed: the drift check compared each
  block whole, and `_DRIFT_KEYS_ADDED_LATER` only covered a block missing
  entirely. Blocks with a typed parser (`RiskConfig`, `DecisionConfig`,
  `MarketDataConfig`, `PaperExecutionConfig` for the resume-effective
  `execution` sub-block) are now compared PARSED rather than as raw YAML — an
  absent key, a `null` block, an empty block and one spelling out every
  default all parse to the same object, and a YAML `"30"` against `30`, or
  `0.7` against `Decimal("0.7")`, is no longer a difference; a side the parser
  refuses (a retired key in an old genesis) falls back to the raw comparison
  so drift is never hidden behind the exception. The same key at a live value
  (`30`) still drifts; `engine`/`indicators` carry no declared defaults and
  keep the raw whole-block comparison (issue #98). (2) A config-only edit that
  changes the prompt's *shape* — switching the volume profile on, editing the
  `indicators` list — needs no deploy and so never bumped `PROMPT_VERSION`,
  and `GROUP BY prompt_version` merged two prompt regimes into one bucket.
  Schema v10 adds `ai_inputs.context_shape` (also in the payload JSON and the
  CSV export, after `input_payload_hash`): the rendered context's section
  structure as one string — `price|market|funding|indicators(rsi_14,…)|
  volume_profile` — covering section headers and indicator rows only, never
  the numbers inside labels or the per-cycle `Mid:`/`Premium:` lines — so it
  changes when a section is added or removed, or the indicator list is
  reordered (a different prompt), and not when a value does. The segmentation
  key is now `(prompt_version, context_shape)`; the RUNBOOK's hand rule for
  the YAML case is retired (issue #97).
- **A date the model cannot be held to is now refused the same way by either
  vendor of the routed news, OHLCV and indicator tools.** The parity the entry
  below established for the four fundamentals getters stopped there; the same
  three inputs (`""`, `"abc"`, `"2026/08/18"`) still got a different answer per
  vendor from `get_news`, `get_global_news` and `get_stock_data`, and which
  vendor `data_vendors` selected is not something the agent can see.
  (`get_indicators` did not diverge — both vendors raised the same `strptime`
  error and its tool wrapper served that one message — but it was the raw
  parser message, with no tag and no retry instruction, beside sibling tools
  answering the shared sentence in the same turn; it now answers it too.) For
  ticker news,
  yfinance parsed the dates inside its broad `except` and came back with an
  "Error fetching news" string the router serves as a successful report, while
  Alpha Vantage raised a bare `ValueError` the router re-raised into the tool
  node. For global news, yfinance's "No global news found for {curr_date}"
  early exit ran *before* the date was parsed, so an unusable date with a quiet
  feed came back as `No global news found for abc` — a coverage claim about a
  day that was never named — and one with a busy feed fell into the same error
  string; Alpha Vantage raised. For OHLCV, yfinance raised a bare `ValueError`
  (a crash: `core_stock_apis` is not an optional category), Alpha Vantage never
  checked `end_date` at all beyond the range filter's `pd.to_datetime` — so
  `""` and `"abc"` became typed no-data, and **`"2026/08/18"` was parsed and
  served real rows** as if it were the ISO date. All eight getters now judge
  the date through the one function in `utils` the fundamentals getters use
  and answer its sentence before any request is made; the sentence names the
  argument being refused (`INVALID_START_DATE`/`INVALID_END_DATE` for the two
  window-bounded tools, `INVALID_CURR_DATE` as before) and what it was meant to
  bound — a window "cannot be resolved" rather than "cannot be bounded to a
  point in time" — and the fundamentals wording is unchanged byte for byte. A
  window names only its first unusable bound. The empty string is refused as
  supplied and unusable on all four tools, as it is for fundamentals, and so is
  `None`: none of the four has a date-less lane to keep. The routed tools
  declare their dates as required strings, so a model cannot send `None` — it
  reaches a getter only from a direct caller, and there it used to be a bare
  `TypeError` from `strptime` on every vendor; the refusal also forecloses what
  dropping that `strptime` without a gate would have opened, yfinance's
  `history(start=None)` answering its default trailing month under a header
  naming a historical end date (measured on an intermediate draft of this
  change, never shipped). The judgement runs after the indicator name and
  before the symbol on these tools, unlike the statement getters, because here
  the dates shape the request rather than filter its result. For
  `get_indicators` this is also a contract change, not only a wording one: a
  refused date used to leave the getter as a `ValueError`, which let
  `route_to_vendor` try the next vendor before the tool wrapper stringified
  it, and now ends the chain as the first vendor's answer — legitimate for a
  caller mistake both vendors would refuse identically, and distinct from the
  vendor-failure strings the entry below stops from ending a chain. An
  unsupported indicator name still raises first, on both vendors, in its own
  untagged register: that verdict is true whatever the date, and turning it
  into a returned sentinel would end the chain on a vendor that merely lacks
  the endpoint. The argument tags are a closed set (`curr_date`,
  `start_date`, `end_date`), and whether a date bounds a point or a window is
  stated by the caller rather than inferred from the argument's name. The
  direct-call verification snapshot
  tool, which carried a third hand-written copy of the sentinel with different
  wording and no "Do not fabricate values", now serves the shared sentence too
  — while keeping its own looser `pd.to_datetime` parse on purpose, since it
  compares a real `Timestamp` numerically rather than a normalised string
  lexically against vendor date fields; a test pins that `"2026/08/18"` is
  still accepted there so the difference cannot be "cleaned up" silently.
  **This narrows what two Alpha Vantage getters accept** — called out under
  Changed as a breaking change for direct callers — in the same way the entry
  below narrowed yfinance's fundamentals: the OHLCV getter refuses a
  slash-separated or time-suffixed date it used to serve, and the ticker-news
  getter refuses the intraday `"YYYY-MM-DD HH:MM"`, `"YYYYMMDDTHHMM"` and
  `datetime` forms `format_datetime_for_api` used to read (#120 later removed
  them from that helper too) — the routed tool only ever sends `yyyy-mm-dd`,
  so this reaches direct callers alone. Both answer with a
  retry instruction rather than an error.
- **Two more vendor failures stop arriving as reports the agent can analyse.**
  `route_to_vendor` never inspects a returned string, so any getter that answers
  prose instead of raising ends the vendor chain and hands the agent that prose
  as data. Two getters still did. The Alpha Vantage indicator getter returned
  five such sentences — a blank or header-only CSV, a CSV missing its `time`
  column, one missing the indicator's own value column, a window the vendor had
  no rows in (reported *inside* a well-formed `## RSI values from … to …` report
  carrying no error wording at all), and `vwma`, which Alpha Vantage has no
  endpoint for and which said so in prose while the yfinance vendor serving the
  same routed tool computes it from OHLCV and was never asked. All five now
  raise `NoMarketDataError`, the lane the same vendor's daily bars getter has
  taken since #30: another configured vendor gets its turn, and a chain with
  only this one emits the router's no-data sentinel with the reason attached.
  The yfinance statement getters answered `"Error retrieving balance sheet for
  AAPL: …"` on fiscal-period columns carrying a timezone, in two distinct ways
  (both measured on pandas 2.3.3): a tz-aware index will not compare against the
  naive cutoff, and labels mixing a tz-aware timestamp with a naive value of
  another type will not coerce as one index at all. Every statement-side reader
  of those labels — the look-ahead filter, the "did any column carry a fiscal
  period" measurement, and the freshness note — now parses them one at a time
  and zone-free through one shared helper — the same zone-free reading the
  OHLCV path already takes — and the filter relabels the survivors so the
  rendered CSV header does not depend on the vendor build. Separately, an indicator registered as supported with no
  request definition or no CSV column mapping now raises before making a
  request: the column-mapping case used to pay for one first, the
  request-definition case never made one, and both used to `return` an "Error: …"
  string that the router recorded as a successful answer. (The agent still reads
  a message rather than seeing the run abort — the indicator tool wrapper
  catches `ValueError` and appends it to the report — but on a multi-vendor
  chain the next vendor now gets its turn.) The
  indicator dispatch is a registry rather than an elif ladder; tests assert set
  equality between it and the supported list in both directions, and pin each
  indicator's request against a table transcribed from the ladder it replaced.

- **A curr_date the model cannot be held to is now refused the same way by
  either fundamentals vendor.** The same routed statement tool used to take
  opposite lanes on the same argument depending on which vendor `data_vendors`
  selected — something the agent cannot see, so the two answers were not
  interpretable as one contract. Three inputs diverged, not just the empty
  string the report started from: `""` was read by yfinance as "no bound
  requested" (unfiltered reports plus a wall-clock note) and by Alpha Vantage as
  "supplied and unusable" (the `INVALID_CURR_DATE` sentinel); `"abc"` fell into
  yfinance's broad `except` and came back as an error string the router reads as
  a successful answer; and `"2026/08/18"` was filtered by yfinance, because
  pandas parses it, while Alpha Vantage rejected it. yfinance's overview getter
  was the quiet one — on all three it served today's ratios with the
  live-snapshot disclosure switched off, which is the exact failure that
  disclosure exists to prevent (a warning log was its only trace, and only on
  two of the three: an empty string left none at all). Both vendors now decide
  "is this curr_date usable?" through one function in `utils` and answer with
  one sentence from another, so a refinement to that judgement reaches both
  rather than one; a test pins that they are the same objects, not two copies.
  **This narrows what yfinance accepts:** the rule is now `strptime`'s, so a
  non-ISO date it used to parse and serve filtered data for — `"2026/08/18"`,
  `"Aug 18 2026"`, an ISO string with a time suffix — is refused instead.
  Non-zero-padded `"2026-8-18"` still works. Nothing in the repo supplies such
  a date; only a model's own tool call can. An omitted `curr_date`
  (`None`) still takes the date-less fallback lane on both vendors, unchanged.
  Vendor emptiness is judged first, matching the Alpha Vantage order: an unknown
  symbol reaches the router's no-data lane either way rather than one vendor
  answering about the date instead. The shared `filter_financials_by_date` now
  tests for `None` rather than falsiness and raises on a bound it cannot use —
  unreachable in production now that the getters answer first, so it stands as
  the contract for a direct caller. Also pinned: the date-less statement
  fallback's 180/181-day bound, which both vendors share and neither had a
  boundary test for. Two yfinance no-data reasons are now distinct where one
  string used to cover both, since the router splices the reason into what the
  agent reads: a frame that empties with no column label on or before
  `curr_date` says so and names the date (correct point-in-time behaviour on any
  backtest older than the vendor's window, so it stays quiet), while a frame
  whose labels are not dates at all — a vendor schema break, which would
  otherwise report every ticker as an uncovered symbol — says that instead and
  is logged. Alpha Vantage already drew that line, and its coverage reasons
  already name `curr_date`; only its schema-break reason omits it, which is left
  to a follow-up.
- **Alpha Vantage getters no longer hand a failure, or a key the vendor wrote,
  to the agent as data.** Three same-family gaps left by the two entries below:
  (1) only HTTP 429 was classified at the request boundary, so any other status
  — an Alpha Vantage 503 outage, say — reached the *indicator* getter's broad
  `except` and came back as `Error retrieving rsi data: 503 Server Error`, a
  string the router reads as a successful answer: the chain stopped at the
  vendor that had just gone down and the agent analysed the error prose as an
  indicator report. That getter now re-raises every `requests` exception
  (statuses, connection resets and timeouts alike) — which is what the other
  Alpha Vantage getters, none of which carries a broad `except`, already did —
  so the router hands the next vendor its turn, or raises on a single-vendor
  chain, exactly as it does for a 429. (2) The two *news* getters served the
  API body raw, so an empty feed arrived as empty JSON while the yfinance
  vendor behind the same routed tool answered in prose, and a vendor-written
  `_freshness_note` key reached the agent looking like a system-issued
  freshness statement with no real disclosure beside it to contradict it. Both
  now answer an empty feed in the yfinance sibling's voice ("No news found for
  AAPL between 2026-06-01 and 2026-06-05") and drop a vendor-supplied note key
  on every served path — the two rules the insider getter in the same module
  took in the entry below. An empty feed riding beside an unclassified
  Information/Note still passes through, so the vendor's own explanation is not
  discarded; the empty verdict reads the `feed` list alone, and the documented
  `items` count plays no part in it. (3) `_make_api_request` and both news
  getters still advertised `dict` return types neither had produced; all three
  now say `str`.
- **Freshness disclosure now covers the rest of the dataflows family.** Three
  same-family gaps left after the fundamentals disclosures below: (1) the
  Alpha Vantage insider-transactions getter served the API body with no
  staleness note, while the yfinance vendor behind the same routed tool flags
  a long-dead filing stream — it now attaches the family's `_freshness_note`
  when the newest `transaction_date` trails the wall clock by more than the
  insider bound, which both vendors now import from a single definition in
  `utils` (the note carrier and its guards — envelope bodies and non-JSON
  bodies are never dressed in a disclosure, vendor-written note keys are
  always stripped on the annotated paths — moved to `alpha_vantage_common`
  for every Alpha Vantage module to share). An empty insider stream also
  answers in one voice now: the Alpha Vantage getter returns the same
  "no insider transactions reported" prose the yfinance vendor uses instead
  of raw `{"data": []}` JSON — unless an unclassified vendor notice rides
  beside the empty list, in which case the body passes through so the
  vendor's own explanation is not discarded. (2) When the model omitted `curr_date` on a statement
  tool, both vendors switched off look-ahead filtering *and* the staleness
  note together, silently; the filter stays off — with no point-in-time bound
  there is nothing to filter against, and pretending otherwise would fake a
  protection — but the note now falls back to judging against the wall clock
  (the insider path's design), the degraded mode is logged, and the
  fundamentals analyst's prompt now tells the model to always pass
  `curr_date`. (3) The OHLCV staleness bound existed as two per-vendor copies
  pinned equal by a test; it now has a single definition in `utils` (stdlib-
  only, so the pure-requests Alpha Vantage module imports it without dragging
  in yfinance/stockstats), with the bounds' values pinned by test.
- **Vendor throttles and rejections now reach the router instead of reading as
  successful answers.** Three same-family gaps around the indicator fix below:
  (1) an exhausted Yahoo Finance 429 re-raised yfinance's own
  `YFRateLimitError` — a type outside the vendor-error taxonomy — so every
  yfinance leaf's broad `except` turned it into `"Error retrieving ..."` prose
  and the router never opened its rate-limit lane; `yf_retry` (the one boundary
  every yfinance network call goes through) now maps exhaustion to
  `VendorRateLimitError`, and all nine yfinance leaves re-raise the taxonomy
  (`except VendorError: raise`, the same shape as the indicator fix) — this is
  the default vendor for every category it serves, so the gap was on the
  every-day path. Two of those leaves needed more than the mapping, because
  yfinance itself swallows the 429 before our boundary can see it (verified on
  1.4.1, the pinned floor): the OHLCV cache now fetches through
  `Ticker.history` (which re-raises throttles) instead of `yf.download` (whose
  per-ticker worker stores the error and answers an empty frame — a throttle
  that read as "no rows"), and the three statement getters go through a
  wrapper that briefly un-hides yfinance's hidden-exception mode — under a
  lock, since parallel tool execution shares the process-global flag — re-
  raising only the throttle and restoring every other failure to the
  swallowed-empty answer the library gives today. (2) An Alpha Vantage HTTP
  429 became a bare
  `requests.HTTPError` at `raise_for_status()` before the body-notice
  classification could see it; a 429 status now raises
  `AlphaVantageRateLimitError` (naming any `Retry-After` the response carried),
  while other 4xx/5xx keep their `HTTPError` behaviour. (3) An Alpha Vantage
  `{"Error Message": ...}` rejection envelope was returned to callers as if it
  were data — news and insider tools served it verbatim and the statement
  filter re-serialized it — so the router never fell back; the shared request
  boundary now raises `NoMarketDataError` with the vendor's wording in the
  detail (so a parameter mistake stays distinguishable in the no-data
  sentinel), a non-object JSON body — no shape this vendor serves data in —
  is classified the same way, and the envelope key list has a single
  definition in `alpha_vantage_common`. The direct-call verification snapshot
  tool keeps a
  throttle transient rather than flattening the newly-typed error into its
  permanent-sounding no-data sentinel.
- **An Alpha Vantage rate limit no longer reads as a successful indicator
  report.** The indicator getter re-raised only the missing-key error and
  caught everything else, returning
  `"Error retrieving <indicator> data: ..."`, which the router reads as a
  successful answer — so once the free tier's daily quota was spent, the
  rate-limit lane never opened and a configured fallback vendor never got its
  turn; the market analyst was handed that prose instead of indicator values.
  The whole vendor-error taxonomy now propagates from that path (caught as the
  base type rather than one leaf at a time), so a chain with a fallback vendor
  moves on to it and a single-vendor chain surfaces a rate limit as a failure
  instead of as a report. That covers the *typed* lane only: the getter's other
  error returns (an unimplemented indicator, an unmapped CSV column, an empty
  range) are still strings the router reads as answers, and an untyped failure
  still degrades to one. An Alpha Vantage indicator config without a fallback
  vendor is therefore a poor fit for the free tier, whose 25-call daily quota
  makes exhaustion routine rather than exceptional.
- **Alpha Vantage fundamentals now carry the same freshness disclosures as the
  yfinance ones.** `get_fundamentals` (the OVERVIEW snapshot) discloses that
  its values are live as of the fetch when the analysis date sits behind the
  wall clock, and the three statement tools disclose a stalled filing stream —
  previously the honesty of a routed fundamentals tool depended on which vendor
  `data_vendors` happened to select, and an Alpha Vantage deployment ran a
  backtest with today's market cap and P/E presented as that date's. The lag
  bound (180 days for quarterlies, 550 for annuals — a fiscal period plus a
  generous filing window in each case) is now shared by both vendors instead of
  living in one of them. Alpha Vantage
  answers in JSON, so the disclosure rides in a `_freshness_note` key rather
  than a header line. Both disclosures need the analysis date, so they fire only
  when the caller supplies `curr_date` — which also remains what turns on the
  look-ahead filter. An Alpha Vantage statement response carries the annual and
  quarterly lists together; whenever a `curr_date` is supplied, only the
  requested cadence is served, so the agent does not receive an unjudged second
  list (a years-old annual balance sheet alongside a current quarterly one) with
  no disclosure attached. Without a `curr_date` the vendor body still passes
  through whole and unfiltered, as it always has.
  A payload with no fundamentals in it — an unknown symbol's `{}`, or no report
  of the requested cadence within the point-in-time bound — now raises
  `NoMarketDataError` like the yfinance path instead of rendering as a
  successful report, so the router falls back or emits its no-data sentinel; an
  unparseable `curr_date` gets the same `INVALID_CURR_DATE` answer from all four
  fundamentals tools. Classifying Alpha Vantage's error envelopes belongs at the
  request boundary and is left to a follow-up.
- **Farside catch-up: cache boundary and caveats aligned with the family's
  decided semantics.** The Farside cache validator now mirrors the invariants
  its parser enforces live (an `asset` echo so a copied/renamed cache file
  cannot serve another asset's flows; strictly-ascending unique dates so a
  newest-first or duplicated-date file cannot mis-date the report or
  double-count a day; the per-row Total-vs-issuer-sum cross-check, same
  tolerances as the parser). Its wording adopts the SoSoValue-side decided
  fixes: the over-cap message names both causes an unknown age collapses to
  ("unparseable **or future-dated** fetch date"), the data-lag cause no
  longer equates snapshot age with row lag on a stale serve ("the stale
  snapshot above may itself be missing newer filings") nor overclaims the
  site's frontier on a fresh one ("no newer filing is visible as of
  curr_date"), the STALE header rides the family's shared template with
  Farside's own cause set, and a table carrying a "not yet posted" cell that
  the Latest line does not explain (an unposted row older than the latest
  day) now gets a legend — previously only an unposted *latest* day was ever
  explained. Farside also adopts the shared cache-read preamble and
  rejecter factory.

- **SoSoValue ETF disclosure gates widened to the facts they hedge.** Four
  disclosures were gated on conditions narrower than what makes them true:
  the revision/restatement caveats now carry a provenance note on **every**
  cache serve (a TTL-fresh serve replays the last fetch's diff — previously
  only a stale serve said so, so repeat calls within one TTL window read as
  repeated revision events); the "no fund reported a flow" scope word hedges
  to "no fetched fund" for **all three** coverage gaps (failed histories,
  a failed fund list, dropped listing entries — previously only failed
  histories); the all-flat-but-material-aggregate breadth verdict hedges on
  dropped listing entries like it already did on failed histories; and a
  table carrying "not yet posted" cells on rows older than the latest day
  now has a legend (the latest day's cell is already explained by the
  Latest line, so it does not re-trigger it). The breadth concentration shares
  ride the treasuries module's near-100 truncation band (now shared as
  `_concentration_share_str`), so a 99.5% top-3 share can no longer print
  as "100%" beside a leaders line showing more funds.

- **The daemon's pre-LLM context guards have a working test again.** When the
  live loop's `on_blocking_read` callback was added to `main._build_context`,
  `test_build_input_refuses_untradeable_indicators`'s two-argument stand-in was
  not updated, so every one of its four cases raised `TypeError` at the call
  site instead of exercising the warm-up / dead-indicator guards it targets —
  and had been doing so since the callback landed. The stand-in now accepts the
  keyword.

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
