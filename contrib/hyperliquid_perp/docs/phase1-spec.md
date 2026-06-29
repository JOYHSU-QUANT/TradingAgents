# Phase 1 spec — build & run

The actionable spec the implementation is written against. Phase 1 =
**read HL data → build `PerpMarketContext` → run the unmodified engine →
adapter → `PerpTradeDecision` → log**. No RiskGate, no orders (those are
Phase 2+).

## Decisions log

| # | Decision | Status |
|---|---|---|
| 1 | `network`, `wallet_address`, `coins` are config, not hardcoded. **Phase 1 reads mainnet (read-only)** + single BTC; testnet is reserved for Phase 3 order testing. Rationale: testnet's only benefit (no real money on bad orders) doesn't apply until Phase 3 — Phase 1/2 place no real orders, and mainnet gives realistic funding/OI/candles so the 30-day z-score actually has data. The wallet read is public/read-only, so pointing at a mainnet address is zero-risk. | ✅ confirmed |
| 2 | Technical indicators computed by `context_builder.py` from HL candles (reuse `stockstats_utils.py`, no new dep). | ✅ confirmed |
| 3 | Funding z-score window = 30 days. | ✅ confirmed |
| 4 | Indicator set = `rsi_14, ema_20, ema_50, atr_14, macd`. | ✅ confirmed |
| 5 | Engine via OpenRouter; deep = `anthropic/claude-sonnet-4-6`, quick = `deepseek/deepseek-chat`. | ✅ confirmed |
| 6 | `selected_analysts = [market, social, news]` (drop fundamentals for perp). | ✅ confirmed |
| 7 | Adapter: **rebalance to a bounded per-tier target** — size up/down toward the rating's target, capped (no unbounded pyramiding); deadband; `no_direct_flip`. See [INTEGRATION](./INTEGRATION.md#rebalance-to-target--intent). | ✅ confirmed |
| 8 | `last_decision` / local `PerpState` persistence **deferred to Phase 2 (not yet implemented)**; first round `last_decision = null`. | ✅ confirmed |
| 9 | Secrets only via env vars; `*.local.yaml` holds the public wallet address + network, never a private key. | ✅ confirmed |
| 10 | `--context-only` dev mode: build & print `PerpMarketContext`, skip the agents (cheap iteration, no API key). | ✅ proposed |
| 11 | Shorting enabled (`allow_short: true`), **tier-symmetric** (revised): bearish tiers mirror bullish ones — `sell → −20` (full short), `underweight → −10` (mild short). Long side unchanged (`buy 20 / overweight 10`). `allow_short: false` forces every negative target to flat. | ✅ revised |

## Config

`configs/hyperliquid.example.yaml` (committed; copy to `hyperliquid.local.yaml`
and fill in — the `.local.yaml` is gitignored):

```yaml
network: mainnet              # Phase 1/2 = mainnet (read-only); testnet reserved for Phase 3 order testing
wallet_address: "0xYOUR..."   # read-only mainnet address; for clearinghouseState (position/margin). Real value lives in the gitignored .local.yaml, never here.
coins: [BTC]                  # single coin for Phase 1; add more later

market_data:
  candle_interval: "4h"
  candle_lookback: 200             # candles for indicator warm-up
  funding_zscore_window_days: 30

indicators: [rsi_14, ema_20, ema_50, atr_14, macd]   # computed by context_builder

engine:
  llm_provider: openrouter
  deep_think_llm: "anthropic/claude-sonnet-4-6"
  quick_think_llm: "deepseek/deepseek-chat"
  selected_analysts: [market, social, news]

adapter:
  target_size_pct:             # rating tier → target % of net value (signed: +long / −short)
    buy: 20                    # full long conviction
    overweight: 10             # partial long
    underweight: -10           # mild short, mirrors overweight (gated by allow_short)
    sell: -20                  # full short
  allow_short: true            # bearish tiers (Sell/Underweight) may short; false forces them to flat
  rebalance_deadband_pct: 2    # |target - current| within this → hold (avoid churn)
  no_direct_flip: true         # opposite-sign target closes first; no one-step flip
  entry_band_pct: 0.5          # entry_zone = entry_price ± this %
  confidence:                  # rating tier → confidence
    full: 0.8                  # Buy / Sell
    partial: 0.6               # Overweight / Underweight
    hold: 0.4
```

## Secrets & keys

| Secret | Where | Phase |
|---|---|---|
| `OPENROUTER_API_KEY` | env var (one key, all OpenRouter models) | 1 — only needed to run the full engine; **not** needed for `--context-only` |
| `HYPERLIQUID_AGENT_KEY` | env var | 3 only — an **agent/API wallet** key (can trade, cannot withdraw), never the master key |

- The private key never goes in any yaml or committed file. `.gitignore` must
  cover `.env` and `*.local.yaml`.
- Phase 1 needs **no private key** — reads only.

## Setup & run

```bash
# 1. install (HL SDK added to requirements)
pip install -r requirements.txt

# 2. configure
cp contrib/hyperliquid_perp/configs/hyperliquid.example.yaml \
   contrib/hyperliquid_perp/configs/hyperliquid.local.yaml
#   then edit: network, wallet_address, coins

# 3. dev loop — build context only, no LLM calls, no key needed
python -m contrib.hyperliquid_perp.main --context-only --coin BTC

# 4. full Phase 1 run — needs the engine key
export OPENROUTER_API_KEY=sk-or-...
python -m contrib.hyperliquid_perp.main --coin BTC
```

Expected output of the full run: a `PerpTradeDecision` JSON written by
`audit/decision_log.py` (prompt hash · model · full decision · timestamp) under
the results dir, plus the decision printed to stdout.

## Phase 1 build order

1. `ports.py` — `ExchangeMarketData` / `ExchangeAccount` interfaces.
2. `exchanges/hyperliquid/` — `sdk_client`, `market_data`, `account`, `mapper`, `errors`.
3. `domains/perp/schema.py` — `PerpMarketContext`, `PerpPosition`, `AccountSnapshot`.
4. `domains/perp/context_builder.py` — market + account → context; computes indicators + funding z-score.
5. `domains/perp/prompt_context.py` — renders context to text (neutral placeholder wording; private funding framing dropped in later).
6. `domains/perp/decision.py` — `PerpTradeDecision` schema.
7. `integration/trading_graph.py` — `HyperliquidTradingGraph` subclass.
8. `integration/decision_adapter.py` — `PortfolioDecision` → `PerpTradeDecision`.
9. `audit/decision_log.py` — write the decision JSON.
10. `main.py` — wire it together; `--context-only` flag.

## Deferred — not yet implemented

Tracked so they are not silently forgotten:

- **State persistence (decision #8):** no local `PerpState`. `last_decision` is
  always `null` in Phase 1; carrying the previous round's decision/position
  across runs lands in Phase 2 (with reconciliation).
- **Sub-tier sizing:** target size has only 5 levels (the rating tiers). Finer
  sizing by parsing the trader's `position_sizing` text is a later refinement.
- **Shorting:** enabled (`allow_short: true`), tier-symmetric — `Sell` is a full
  short (`−20`) and `Underweight` a mild one (`−10`); `allow_short: false` forces
  both to flat. Because `no_direct_flip: true`, a long→short swing closes to flat
  first and re-enters short the next round (never flips in one step).

## Open items still on you

- The `prompt_context.py` funding wording (your alpha) — I scaffold a neutral
  placeholder; you drop in the real phrasing.
- Tier numbers and shorting are now settled (decision #11, revised to
  tier-symmetric): `buy 20 / overweight 10 / underweight −10 / sell −20`,
  `allow_short: true`. Revisit the magnitudes once paper-trading (Phase 2) shows
  real fill/funding behaviour.
