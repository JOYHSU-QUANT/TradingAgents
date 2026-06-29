# Design — API & decision contract

The module's own data contracts: what it reads from Hyperliquid (inputs) and the
`PerpTradeDecision` it produces (output). For how the decision is produced from
the engine, see [Integration](./INTEGRATION.md).

---

# Part 1 — Hyperliquid API reference

The endpoints the module consumes. See the [architecture overview](./README.md)
for where these sit in the data flow.

## Info endpoint

`https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint`

Public market reads need no authentication. Account reads require the **wallet
address** (the master account address — *not* the agent/API-wallet address).

| `type` | Purpose | Key returned fields | Auth |
|---|---|---|---|
| `metaAndAssetCtxs` | Market snapshot: mark/index price, OI, funding, volume. The single most important Phase-1 call — all market data comes from here. | `markPx`, `oraclePx`, `funding`, `openInterest`, `dayNtlVlm`, `prevDayPx`, `premium` | Public |
| `candleSnapshot` | OHLCV candle history. Body: `req:{coin, interval:"4h", startTime, endTime}` | `o`, `h`, `l`, `c`, `v`, `t`, `T` | Public |
| `fundingHistory` | Historical funding rate. Body: `coin`, `startTime` | `fundingRate`, `premium`, `time` | Public |
| `predictedFundings` | Predicted next funding rate, including per-exchange predictions for cross-venue comparison. | `nextFunding`, `nextFundingTime` | Public |
| `clearinghouseState` | Account state: margin, positions, open orders. Body: `user` — **must be the wallet address, not the agent address**. | `marginSummary`, `accountValue`, `withdrawable`, `assetPositions[].szi`, `entryPx`, `leverage`, `unrealizedPnl`, `liquidationPx`, `openOrders[]` | Wallet address |
| `userFillsByTime` | Today's fills, used to compute realized PnL. Body: `user`, `startTime=today 00:00 UTC`. | `closedPnl`, `fee`, `side`, `sz`, `px`, `time` | Wallet address |
| `userFunding` | Today's funding payments/receipts. Body: `user`, `startTime=today 00:00 UTC`; sum the `delta` yourself. | `delta`, `time` | Wallet address |
| `orderStatus` | Query a single order's current status (e.g. whether a limit filled). Body: `user`, `oid`. Used by `execution.py` to poll order state. | `status: open / filled / canceled / rejected` | Wallet address |

## Exchange endpoint

`https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint`

All actions are signed. (Phase 3 only — Phase 1/2 do not place orders.)

| `action` | Purpose | Key params | Signed |
|---|---|---|---|
| `order` | Place an order: limit / market / TP / SL. TIF: `Alo`=post-only, `Ioc`=IOC, `Gtc`=GTC. | `a`=asset index, `b`=isBuy, `p`=price, `s`=size, `r`=reduceOnly, `t.limit.tif` or `t.trigger` | Yes |
| `cancel` | Cancel an order by `oid`. Used to cancel a limit after timeout; needs the asset index and oid. | `a`=asset, `o`=oid | Yes |
| `cancelByCloid` | Cancel by client order id. Convenient when you assign your own `cloid` and want easy lookup. | `asset`, `cloid` | Yes |
| `scheduleCancel` | Dead man's switch: auto-cancel all open orders after a given time. Required by `kill_switch.py`; refresh the deadline every round so a crashed process self-protects. | `time` = ms timestamp | Yes |
| `updateLeverage` | Update leverage multiplier. Confirm leverage matches the `risk.yaml` setting before opening. | `asset`, `isCross`, `leverage` | Yes |

---

# Part 2 — Decision schema & order flow

`PerpTradeDecision` describes **intent and direction** — never a concrete order.

> **Who produces it:** in Direction 2 the LLM agents do *not* emit this schema
> directly. The unmodified TradingAgents engine emits a `PortfolioDecision`
> (a 5-tier rating + thesis); the **decision adapter** in this module maps that —
> together with the live `PerpMarketContext` and `PerpPosition` — into the
> `PerpTradeDecision` below. See [Integration](./INTEGRATION.md) for the
> field-by-field mapping. The schema here is the adapter's **output contract**.

The perp context the engine reasons over is built by `context_builder.py`: every
numeric value is annotated (e.g. funding-rate basis points, z-scores) so the
model reads context rather than re-deriving it.

## Schema — `PerpTradeDecision`

| Field | Type | Description | Why it exists |
|---|---|---|---|
| `intent` | enum | `hold` / `open_long` / `open_short` / `reduce` / `close` | Core decision, one of five — no fuzzy answers allowed. |
| `confidence` | float 0–1 | Confidence in this decision. | RiskGate can force `confidence < 0.6 → hold`. |
| `target_size_pct` | float 0–100 | Target position as a % of account net value. | Gives direction + ratio; the actual size is computed by RiskGate + OrderPlanner. |
| `entry_zone` | object \| null | Suggested entry range `{low, high}` (decimal strings); `null` means "around market". | Reference for the OrderPlanner's limit price — not a hard command. |
| `invalidation_price` | string \| null | Breaking/crossing this level means the thesis is wrong → exit. Decimal string (exchange-native precision). | The logic behind the stop loss — not the exact SL trigger price. |
| `urgency` | enum | `low` / `medium` / `high` | Lets OrderPlanner choose limit vs market. |
| `rationale` | string | 2–4 sentences explaining the decision. | Required audit-log field for post-mortems. |
| `key_risks` | string[] | 1–3 main risks. | Gives the Risk Manager something concrete to review. |
| `market_regime` | enum | `trending` / `ranging` / `volatile` | Used by the Reflection agent to judge whether the strategy fit the regime. |
| `funding_view` | enum | `favorable` / `neutral` / `headwind` | Makes the funding judgement explicit rather than leaving it to interpret numbers. |

### Not in the decision — decided downstream

| Not a decision output | Decided by |
|---|---|
| Exact order size | RiskGate + OrderPlanner |
| Exact SL / TP price | RiskGate + OrderPlanner |
| Leverage multiplier | RiskGate |
| Final order params | OrderPlanner |

## Examples

**Hold** — keep the current position, change nothing:

```json
{
  "intent": "hold",
  "confidence": 0.72,
  "target_size_pct": null,
  "entry_zone": null,
  "invalidation_price": "61800.0",
  "urgency": "low",
  "rationale": "Uptrend structure intact with price above EMA20 and EMA50. RSI at 64 is elevated but not overbought. However, funding z-score at +1.42 indicates moderately crowded long positioning. OI rising with price suggests leveraged buildup. Prefer to hold existing position rather than add exposure at current funding levels.",
  "key_risks": [
    "Funding rate trending higher — carry cost increasing for longs",
    "OI buildup could unwind quickly if price stalls near resistance at 64,500",
    "Daily realized PnL already -120 USDC, approaching loss threshold"
  ],
  "market_regime": "trending",
  "funding_view": "headwind"
}
```

**Open long** — `target_size_pct` is a target % of net value; real size comes from RiskGate:

```json
{
  "intent": "open_long",
  "confidence": 0.81,
  "target_size_pct": 15,
  "entry_zone": { "low": "63000.0", "high": "63400.0" },
  "invalidation_price": "61800.0",
  "urgency": "low",
  "rationale": "Price broke above prior resistance at 62,800 with expanding volume. EMA structure bullish, RSI has room to run. Funding at z-score +0.4 is near neutral — no significant carry headwind. OI rising confirms new longs entering rather than short covering.",
  "key_risks": [
    "Resistance at 64,500 may cap near-term upside",
    "Macro risk: FOMC in 2 days could increase volatility"
  ],
  "market_regime": "trending",
  "funding_view": "neutral"
}
```

**Close** — `target_size_pct: 0` means fully flat:

```json
{
  "intent": "close",
  "confidence": 0.91,
  "target_size_pct": 0,
  "entry_zone": null,
  "invalidation_price": null,
  "urgency": "high",
  "rationale": "Price broke below key support at 61,800 — the defined invalidation level for this long position. Structure has shifted bearish with lower low confirmed. Funding turning negative suggests shorts gaining edge. Exit to protect remaining capital.",
  "key_risks": [
    "Slippage risk on close given expanding volatility"
  ],
  "market_regime": "volatile",
  "funding_view": "favorable"
}
```

## Order flow

How `RiskGate` and `OrderPlanner` turn each intent into deterministic action
(Phase 2+):

```
PerpTradeDecision (adapter output)
│
├─ intent = "hold"
│   └─ Do nothing. Store in local store, wait for the next round.
│
├─ intent = "open_long" / "open_short"
│   └─ RiskGate.check()
│       ├─ confidence < 0.6                 → reject, force hold
│       ├─ target_size_pct > max_notional   → clamp to limit
│       ├─ daily_loss_remaining <= 0        → reject
│       └─ passed → OrderPlanner
│           ├─ urgency = "low"   → limit order, TIF = Alo
│           ├─ urgency = "high"  → market order
│           └─ entry_zone set    → limit price = midpoint of the zone
│
├─ intent = "reduce"
│   └─ target_size_pct decides how much to cut
│       e.g. currently 15%, target = 8% → sell half
│
└─ intent = "close"
    ├─ urgency = "high" → market close
    └─ urgency = "low"  → limit close, wait for fill
```
