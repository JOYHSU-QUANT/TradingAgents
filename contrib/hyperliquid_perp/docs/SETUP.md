# Setup & run (Phase 1)

A practical runbook: how to install, configure, and run. For **why** it is
designed this way, see [phase1-spec](./phase1-spec.md); this page is just the
"do this" steps.

Run every command from the repo root, `TradingAgents/`.

---

## 1. Requirements

- Python 3.10+
- Hyperliquid public market data needs no key; reading the account/position
  needs a **public wallet address** (read-only, never a private key).
- `OPENROUTER_API_KEY` is only needed for a full engine run; `--context-only`
  does not need it.

## 2. Install

```bash
pip install -r requirements.txt
```

This installs the core engine (`langchain-*` / `langgraph`, etc.) plus this
module's own deps, `hyperliquid-python-sdk` and `PyYAML`.

Verify the install:

```bash
python -m pytest contrib/hyperliquid_perp/tests/ -q
```

## 3. Configure

Copy the example into a `*.local.yaml` (the `.local.yaml` is gitignored, so it
**never enters version control**):

```bash
cp contrib/hyperliquid_perp/configs/hyperliquid.example.yaml \
   contrib/hyperliquid_perp/configs/hyperliquid.local.yaml
```

Then edit `hyperliquid.local.yaml`. Key fields:

| Field | Meaning |
|---|---|
| `network` | `mainnet` (Phase 1/2 read mainnet read-only; testnet is reserved for Phase 3). |
| `network_timeout_s` | HTTP timeout (seconds) per Hyperliquid request; default `30`. A stalled read fails loud instead of hanging the run forever. |
| `wallet_address` | Read-only mainnet address, used only to read position/margin. **Leaving the `0xYOUR...` placeholder = treated as unset**, and the run assumes a flat account. |
| `coins` | Single coin for Phase 1, default `[BTC]`. |
| `market_data` | `candle_interval` (4h; must be one of `1m`/`5m`/`15m`/`1h`/`4h`/`1d`) / `candle_lookback` (200) / `funding_zscore_window_days` (30). |
| `indicators` | `rsi_14, ema_20, ema_50, atr_14, macd`, computed by `context_builder`. |
| `engine` | `llm_provider: openrouter`, `deep_think_llm`, `quick_think_llm`, `selected_analysts: [market, social, news]`. |
| `adapter` | rating→target tier numbers, `deadband`, `no_direct_flip`, `allow_short`, `entry_band_pct`, `confidence` (per-tier, each in `[0, 1]`). |

> When no `*.local.yaml` exists the loader falls back to
> `hyperliquid.example.yaml`, so `--context-only` works without copying anything.

### Secrets (decision #9: env vars only, never in any yaml)

| Variable | When needed |
|---|---|
| `OPENROUTER_API_KEY` | For a full engine run. One key covers every OpenRouter model. |
| `HYPERLIQUID_AGENT_KEY` | **Phase 3 only** — not needed in Phase 1. |

```bash
export OPENROUTER_API_KEY=sk-or-...
```

Both `*.local.yaml` and `.env` are gitignored. **A private key must never go in
any yaml or any file that enters version control.**

## 4. Run

### A. Context only (no key, dev loop)

Connects to mainnet, reads data, computes indicators and the funding z-score,
and prints the `PerpMarketContext`. No LLM call, no cost.

```bash
python -m contrib.hyperliquid_perp.main --context-only --coin BTC
```

If `wallet_address` is a real address, it also prints the current position
(or `flat`).

### B. Full Phase 1 round (key required)

Build context → inject the **unmodified** TradingAgents engine → engine emits a
5-tier rating → the adapter maps it to a `PerpTradeDecision` → write the audit
log → print to stdout.

```bash
export OPENROUTER_API_KEY=sk-or-...
python -m contrib.hyperliquid_perp.main --coin BTC
```

Without `OPENROUTER_API_KEY` it fails cleanly **before any network call** and
points you at `--context-only`.

Common flags:

| Flag | Default | Meaning |
|---|---|---|
| `--coin BTC` | first of config `coins` | Coin to analyze. |
| `--context-only` | off | Build context only, skip the engine. |
| `--config PATH` | `*.local.yaml` → example | Use a different config YAML. |

## 5. Where output goes

A full round writes the decision as JSON:

```
<results_dir>/perp_decisions/BTC_<YYYYmmdd_HHMMSS_fff>.json
```

`results_dir` comes from the engine's `DEFAULT_CONFIG` (default
`~/.tradingagents/logs`, overridable via `TRADINGAGENTS_RESULTS_DIR`). File
contents (synthetic example):

```json
{
  "schema_version": 2,
  "coin": "BTC",
  "timestamp": "2026-06-27T17:45:00+00:00",
  "timestamp_ms": 1782668700000,
  "prompt_hash": "sha256:9f2a…",
  "models": {
    "provider": "openrouter",
    "deep": "anthropic/claude-sonnet-4-6",
    "quick": "deepseek/deepseek-chat"
  },
  "rating": "Buy",
  "rating_source": "explicit",
  "decision": {
    "intent": "open_long",
    "confidence": 0.8,
    "target_size_pct": 20.0,
    "entry_zone": { "low": "62685.0", "high": "63315.0" },
    "invalidation_price": "61800.0",
    "urgency": "low",
    "rationale": "…",
    "key_risks": ["…"],
    "market_regime": "trending",
    "funding_view": "neutral"
  }
}
```

`prompt_hash` is the sha256 of the perp context text the engine read; together
with `models` and `timestamp` it lets you reconstruct and post-mortem any single
decision. The `decision` fields are defined in
[DESIGN](./DESIGN.md#schema--perptradedecision).

> Phase 1 stops at "write log + print decision". **It places no orders and runs
> no RiskGate** — that is Phase 2+.

## 6. Tests

```bash
# All module tests (pure functions — no key, no network)
python -m pytest contrib/hyperliquid_perp/tests/ -q

# Lint
python -m ruff check contrib/hyperliquid_perp/
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: langchain` (or similar) | Core deps not installed → `pip install -r requirements.txt`. |
| `OPENROUTER_API_KEY is not set …` | A full run needs a key; `export` it, or use `--context-only`. |
| Position always shows `flat` / no account read | `wallet_address` is still the `0xYOUR...` placeholder; set a real read-only address. |
| `config not found …` | Copy `hyperliquid.example.yaml` to `hyperliquid.local.yaml`. |
| Want a cheap acceptance run first | Temporarily point `engine.deep_think_llm` / `quick_think_llm` at a cheap/free OpenRouter model to confirm the pipeline emits a `PerpTradeDecision` and writes a log, then switch back. |
