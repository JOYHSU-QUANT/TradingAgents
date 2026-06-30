# hyperliquid_perp

A `contrib/` module that connects the TradingAgents loop to the
[Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs) perpetuals
exchange. The agents reason about market context and emit an **intent**
(`PerpTradeDecision`), not a raw order; a deterministic risk gate and order
planner turn that intent into actual exchange actions.

> Status: Phase 1 runnable — market context + the unmodified engine + a
> `PerpTradeDecision` audit log all run end to end (see [SETUP](./docs/SETUP.md));
> Phase 2+ (risk gate, order planner, execution) is still in design. See the
> roadmap for what each phase builds.

## Documentation

Full design lives in [`docs/`](./docs/README.md):

- [docs/README](./docs/README.md) — overview, architecture, project layout, roadmap.
- [SETUP](./docs/SETUP.md) — setup & run runbook (install, config, running, output, troubleshooting).
- [DESIGN](./docs/DESIGN.md) — Hyperliquid API reference + `PerpTradeDecision` schema & order flow.
- [INTEGRATION](./docs/INTEGRATION.md) — subclass override points, `PortfolioDecision → PerpTradeDecision` mapping, and model assignment.
- [phase1-spec](./docs/phase1-spec.md) — Phase 1 decisions, config schema, secrets, setup & run, build order.

## Safety

`configs/*.local.yaml` holds the public wallet address + network and is
gitignored — never commit it. **Secrets (API keys, the Phase-3 agent-wallet
private key) live in environment variables, never in any yaml.**
