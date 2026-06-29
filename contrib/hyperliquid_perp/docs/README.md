# Hyperliquid Perp Trading Agents — Design Docs

An optional `contrib/` module that drives the **unmodified** TradingAgents
engine against the [Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs)
perpetuals exchange. TradingAgents runs as-is and emits its usual
`PortfolioDecision` (a 5-tier rating + thesis); a thin **adapter** in this module
maps that into a `PerpTradeDecision` (a perp intent), and a deterministic risk
gate + order planner turn the intent into actual exchange actions.

> **Integration stance (Direction 2 — plugin, zero core changes):** nothing in
> `tradingagents/` is edited. Perp market context is injected by *subclassing*
> `TradingAgentsGraph` and overriding its existing extension points; the perp
> intent is produced *after* the engine by an adapter.

## Documents

**Design reference** (stable — how it works):

| Doc | Contents |
|---|---|
| [DESIGN](./DESIGN.md) | The module's data contracts: Hyperliquid API reference (inputs) and the `PerpTradeDecision` schema + order flow (output). |
| [INTEGRATION](./INTEGRATION.md) | How it drives the unmodified engine (subclass override points, `PortfolioDecision → PerpTradeDecision` mapping) and which models run which roles. |

**Specs** (actionable — what to build):

| Doc | Contents |
|---|---|
| [phase1-spec](./phase1-spec.md) | Phase 1 decisions log, config schema, secrets, setup & run, build order. |

---

## Architecture

Perp data flows up into the **unmodified** TradingAgents engine via a subclass
override; the engine's rating flows back down through an adapter, a risk gate,
and into execution. Each layer is labelled with the phase in which it is built.
Boxes marked `contrib` live entirely in this module; the engine box is upstream
code we do not edit.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Hyperliquid exchange                                                  │
│  REST + WebSocket — raw market data, fills, liquidation events         │
└──────────────────────────────────────────────────────────────────────┘
        │
[Ph 1]  Exchange adapter layer                                  (contrib)
        Wraps the HL SDK so nothing upstream sees HL directly.
        sdk_client.py · market_data.py · account.py · mapper.py · errors.py
        │
[Ph 1]  Perp domain layer                                       (contrib)
        HL raw response → clean schema. Builds PerpMarketContext + PerpPosition.
        schema.py · context_builder.py · prompt_context.py · decision.py
        │
        ▼  injected as instrument-context text (no core change)
┌──────────────────────────────────────────────────────────────────────┐
│  TradingAgents engine — UNCHANGED upstream code                        │
│  HyperliquidTradingGraph(TradingAgentsGraph)  ← contrib subclass       │
│   · overrides resolve_instrument_context() → injects perp context      │
│   · runs analysts → researchers → trader → portfolio manager           │
│   · emits PortfolioDecision: rating (Buy/Overweight/Hold/Under/Sell)    │
└──────────────────────────────────────────────────────────────────────┘
        │  final_state + signal (the 5-tier rating)
        ▼
[Ph 1/2] Decision adapter                                       (contrib)
        PortfolioDecision + PerpMarketContext + PerpPosition
        → PerpTradeDecision (intent, target_size_pct, funding_view, …)
        decision_log.py logs prompt hash · model · full decision JSON
        │
[Ph 1/2] RiskGate                                               (contrib)
        Deterministic hard gate: schema check → soft risk → hard limits →
        kill switch.
        │
        ├─ approved ─→ [Ph 2] Execution pipeline               (contrib)
        │              order_planner.py (decision → order, deterministic)
        │              paper_executor.py (fee 0.035% · slippage · SL/TP)
        │
        └─ rejected ─→ [Ph 1/2] Audit                          (contrib)
                       order_log.py (planned orders · fills · rejections ·
                       position before/after)
        │
[Ph 2/3] Reconciliation                                         (contrib)
        Before each decision: sync HL state → trusted PerpState.
        After execution: HL actual position vs planned order / fills.
        │
[Ph 3]  Phase-3 additions                                       (contrib)
        exchanges/hyperliquid/ (websocket, polling, submit/cancel)
        live_executor.py (API wallet, testnet/mainnet)
        telegram.py (fills · liquidation alerts · daily PnL)
```

**Key invariant:** `tradingagents/` is never edited. The only non-deterministic
part is the engine, which we drive through a subclass (context in) and read as a
`PortfolioDecision` (rating out). Everything from the adapter onward —
rating→intent mapping, sizing, SL/TP, leverage, final order params — is
deterministic and lives in this module's adapter + RiskGate + OrderPlanner.

---

## Project layout

`tradingagents/` is **not edited** — the whole integration lives under
`contrib/hyperliquid_perp/`, with `integration/` holding the subclass and adapter
that bridge to the engine.

```
TradingAgents/
├── tradingagents/                       # UNCHANGED upstream engine
├── examples/
└── contrib/
    └── hyperliquid_perp/
        ├── exchanges/
        ├── domains/
        ├── integration/                 # bridge to the unmodified engine
        │   ├── trading_graph.py         #   HyperliquidTradingGraph subclass
        │   └── decision_adapter.py      #   PortfolioDecision → PerpTradeDecision
        ├── risk/
        ├── execution/
        ├── audit/
        ├── notifications/
        ├── configs/
        │   ├── hyperliquid.example.yaml
        │   ├── risk.example.yaml
        │   ├── hyperliquid.local.yaml   🔒 gitignored
        │   └── risk.local.yaml          🔒 gitignored
        ├── docs/
        ├── ports.py
        ├── main.py
        ├── .gitignore
        └── README.md
```

`🔒 gitignored` files hold the public wallet address + network only. **Secrets
(API keys, the Phase-3 agent-wallet private key) live in environment variables,
never in any yaml.** See [phase1-spec](./phase1-spec.md#secrets--keys).

## Implementation phases

Legend: ✅ open-source · ⚠️ framework open-source, specifics kept private · 🔒 gitignored.

### Phase 1 — runnable, no real orders

| File | Status | Notes |
|---|---|---|
| `ports.py` | ✅ | `ExchangeMarketData` / `ExchangeAccount` interface definitions — write this first. |
| `exchanges/hyperliquid/sdk_client.py` | ✅ | Official SDK init, testnet/mainnet config loading. |
| `exchanges/hyperliquid/market_data.py` | ✅ | SDK Info → market snapshot. |
| `exchanges/hyperliquid/account.py` | ✅ | SDK Info → account / position snapshot. |
| `exchanges/hyperliquid/mapper.py` | ✅ | SDK raw response → internal schema. |
| `exchanges/hyperliquid/errors.py` | ✅ | SDK error → domain error. |
| `domains/perp/schema.py` | ✅ | `PerpMarketContext` · `PerpPosition` · `AccountSnapshot`. |
| `domains/perp/context_builder.py` | ✅ | `market_data + account → PerpMarketContext`; computes indicators + funding z-score. |
| `domains/perp/prompt_context.py` | ⚠️ | Structure open; the exact wording is kept private (funding-rate framing is your alpha). |
| `domains/perp/decision.py` | ✅ | `PerpTradeDecision` schema — intent, not order. |
| `integration/trading_graph.py` | ✅ | `HyperliquidTradingGraph(TradingAgentsGraph)` — overrides `resolve_instrument_context()`. No core edits. |
| `integration/decision_adapter.py` | ✅ | Maps `PortfolioDecision` + `PerpMarketContext` + `PerpPosition` → `PerpTradeDecision`. |
| `audit/decision_log.py` | ✅ | prompt hash · model · full decision JSON · timestamp. |
| `configs/hyperliquid.example.yaml` | ✅ | Format example. |
| `configs/hyperliquid.local.yaml` | 🔒 | network + wallet address (public). |

### Phase 2 — paper-trading validation

| File | Status | Notes |
|---|---|---|
| `risk/perp_risk_gate.py` | ⚠️ | Framework open; params read from `risk.local.yaml` (max leverage · max notional · stop loss %). |
| `risk/hard_limits.py` | ⚠️ | Framework open; trigger conditions kept (daily loss · stale data · liquidation → freeze). |
| `risk/kill_switch.py` | ⚠️ | Framework open; thresholds kept (orphan order → cancel all · halt conditions). |
| `execution/order_planner.py` | ✅ | `PerpTradeDecision → ExchangeOrderRequest`, deterministic. |
| `execution/paper_executor.py` | ✅ | Simulates taker fee 0.035% · funding · slippage · SL/TP triggers. |
| `audit/order_log.py` | ✅ | planned orders · paper fills · RiskGate rejections · position before/after. |
| `configs/risk.example.yaml` | ✅ | Format example. |
| `configs/risk.local.yaml` | 🔒 | Actual risk-control parameters. |

### Phase 3 — live execution

| File | Status | Notes |
|---|---|---|
| `exchanges/hyperliquid/execution.py` | ✅ | SDK Exchange → submit / cancel. |
| `exchanges/hyperliquid/websocket.py` | ✅ | SDK WebSocket wrapper / callbacks. |
| `execution/live_executor.py` | ✅ | Wraps `HyperliquidExecution`. |
| `execution/reconciliation.py` | ✅ | HL actual state vs local `PerpState`. |
| `notifications/telegram.py` | ✅ | Fills · liquidation alerts · daily PnL. |
