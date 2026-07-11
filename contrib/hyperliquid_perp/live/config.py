"""Typed view of the YAML ``live:`` block (phase3-spec §3–§5).

Mirrors the ``risk:`` / ``paper_trading:`` config pattern (``config_overrides``
+ per-field converters, absent/blank keys fall back to the field default
declared once on the dataclass), with every §4 gate and safety limit validated
at construction so a config mistake is a named startup failure, never a
runtime surprise. Also home to the §5 notional-cap math
(:func:`compute_notional_caps`) — pure, no I/O, all-Decimal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from ..config import LEGAL_NETWORKS
from ..domains.perp.config_coercion import (
    bool_from_yaml,
    config_overrides,
    decimal_from_yaml,
    int_from_yaml,
)
from ..domains.perp.risk_gate import MarginMode, RiskConfig

__all__ = [
    "EXCHANGE_MIN_ORDER_NOTIONAL_USDC",
    "ExecutionMode",
    "ExecutionStyle",
    "KillSwitchConfig",
    "LiveConfig",
    "LiveExecutionConfig",
    "LiveProtectionConfig",
    "LiveSafetyConfig",
    "LiveWebsocketConfig",
    "NotionalCaps",
    "RefreshFailedPolicy",
    "ShutdownPolicy",
    "TpFailureMode",
    "compute_notional_caps",
    "validate_live_risk_consistency",
]

# Hyperliquid's minimum order value. §5 rule 4: an effective notional cap below
# this means no order can ever be placed — startup must fail (or enter safe
# mode) rather than run a bot that is structurally unable to trade.
EXCHANGE_MIN_ORDER_NOTIONAL_USDC = Decimal("10")

# cloid_logical joins its segments with "_" (§8.2), so a prefix containing one
# would corrupt every parse of the id; keep it strictly alphanumeric.
_OWNER_PREFIX_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")


class ExecutionMode(str, Enum):
    """The §3 execution modes. ``MAINNET_LIVE`` exists in the vocabulary but is
    rejected at config load — Phase 3 v1 does not enable it (§22)."""

    PAPER = "paper"
    TESTNET_LIVE = "testnet_live"
    MAINNET_TINY = "mainnet_tiny"
    MAINNET_LIVE = "mainnet_live"


# The three policy vocabularies below are single-member today, but PR 2/5
# dispatch on them — enums (matching RiskAction/MarginMode/DecisionMode) give
# those dispatch sites exhaustiveness and typo protection that validated
# strings would not.


class ExecutionStyle(str, Enum):
    """``live.execution.default_style`` — §9's only v1 style (native TWAP is
    out of scope, §25 #3); a future style lands as an explicit member here."""

    SLICED_TWAP = "sliced_twap"


class TpFailureMode(str, Enum):
    """``live.protection.tp_failure_mode`` — the only §17 TP-failure policy."""

    DEGRADED_PROTECTION = "degraded_protection"


class RefreshFailedPolicy(str, Enum):
    """``live.kill_switch.on_refresh_failed`` — the only §18 policy."""

    SAFE_MODE = "safe_mode"


class ShutdownPolicy(str, Enum):
    """``live.kill_switch.on_shutdown`` — the only §18 policy."""

    CANCEL_BOT_OWNED_OPEN_ORDERS = "cancel_bot_owned_open_orders"


def _coerce_enum(obj: object, attr: str, enum_cls: type[Enum], *, key: str, expected: str) -> None:
    """Normalise a YAML string field to its enum member on a frozen dataclass.

    One home for the ``object.__setattr__`` + named-error + ``from None``
    discipline every policy-vocabulary field needs, so the six call sites
    cannot drift (a missed normalisation would leave a str/enum-mixed field).
    """
    try:
        object.__setattr__(obj, attr, enum_cls(getattr(obj, attr)))
    except ValueError:
        raise ValueError(f"{key} must be {expected}, got {getattr(obj, attr)!r}") from None


def _parse_allowed_symbols(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"expected a list of symbols, got {raw!r}")
    symbols: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"each symbol must be a non-empty string, got {item!r}")
        symbols.append(item.strip().upper())
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"duplicate symbol(s) in {symbols}")
    return tuple(symbols)


@dataclass(frozen=True)
class LiveSafetyConfig:
    """``live.safety`` — the §4 hard limits (defaults are the §4 example values)."""

    single_symbol_only: bool = True
    allowed_symbols: tuple[str, ...] = ("BTC",)
    leverage: Decimal = Decimal(1)
    margin_mode: MarginMode = MarginMode.CROSS
    max_target_margin_pct: int = 60
    max_notional_usdc: Decimal = Decimal(100)
    absolute_notional_ceiling: Decimal = Decimal(500)
    max_open_orders: int = 5
    max_daily_loss_pct: Decimal = Decimal(2)
    max_consecutive_loss_count: int = 3

    def __post_init__(self) -> None:
        if not self.allowed_symbols:
            raise ValueError("live.safety.allowed_symbols must not be empty")
        if self.single_symbol_only and len(self.allowed_symbols) != 1:
            raise ValueError(
                f"live.safety.single_symbol_only is true but allowed_symbols has "
                f"{len(self.allowed_symbols)} entries: {list(self.allowed_symbols)}"
            )
        # Leverage > 1 is explicitly out of scope for Phase 3 v1 (§25 #6): the
        # live sizing/liquidation paths only exist for 1x, so any other value
        # would run unvalidated math with real money.
        if self.leverage != 1:
            raise ValueError(
                f"live.safety.leverage must be 1 (Phase 3 v1 is 1x only), got {self.leverage}"
            )
        _coerce_enum(
            self,
            "margin_mode",
            MarginMode,
            key="live.safety.margin_mode",
            expected="'cross' (isolated is out of scope, §25)",
        )
        if not 0 < self.max_target_margin_pct <= 100:
            raise ValueError(
                f"live.safety.max_target_margin_pct must be in (0, 100], "
                f"got {self.max_target_margin_pct}"
            )
        if self.max_notional_usdc <= 0:
            raise ValueError(
                f"live.safety.max_notional_usdc must be > 0, got {self.max_notional_usdc}"
            )
        if self.absolute_notional_ceiling <= 0:
            raise ValueError(
                f"live.safety.absolute_notional_ceiling must be > 0, "
                f"got {self.absolute_notional_ceiling}"
            )
        # §5 rule 5: the config-load-time ceiling check. Fail, never clamp —
        # a silently clamped cap would let a fat-fingered max_notional_usdc
        # pass unremarked and hide that the operator's intent was rejected.
        if self.max_notional_usdc > self.absolute_notional_ceiling:
            raise ValueError(
                f"live.safety.max_notional_usdc ({self.max_notional_usdc}) exceeds "
                f"absolute_notional_ceiling ({self.absolute_notional_ceiling}) — "
                "refusing to start (§5 rule 5; the ceiling is not a clamp)"
            )
        if self.max_open_orders <= 0:
            raise ValueError(f"live.safety.max_open_orders must be > 0, got {self.max_open_orders}")
        if not 0 < self.max_daily_loss_pct <= 100:
            raise ValueError(
                f"live.safety.max_daily_loss_pct must be in (0, 100], got {self.max_daily_loss_pct}"
            )
        if self.max_consecutive_loss_count <= 0:
            raise ValueError(
                f"live.safety.max_consecutive_loss_count must be > 0, "
                f"got {self.max_consecutive_loss_count}"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> LiveSafetyConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "single_symbol_only": bool_from_yaml,
                    "allowed_symbols": _parse_allowed_symbols,
                    "leverage": decimal_from_yaml,
                    "margin_mode": str,
                    "max_target_margin_pct": int_from_yaml,
                    "max_notional_usdc": decimal_from_yaml,
                    "absolute_notional_ceiling": decimal_from_yaml,
                    "max_open_orders": int_from_yaml,
                    "max_daily_loss_pct": decimal_from_yaml,
                    "max_consecutive_loss_count": int_from_yaml,
                },
            )
        )


@dataclass(frozen=True)
class LiveExecutionConfig:
    """``live.execution`` — sliced-TWAP parameters (§4, behaviour in §9/PR 5)."""

    default_style: ExecutionStyle = ExecutionStyle.SLICED_TWAP
    max_slippage_pct: Decimal = Decimal("0.005")
    plan_duration_minutes: int = 60
    slice_interval_seconds: int = 30

    def __post_init__(self) -> None:
        _coerce_enum(
            self,
            "default_style",
            ExecutionStyle,
            key="live.execution.default_style",
            expected="'sliced_twap'",
        )
        # A fraction despite the _pct name (0.005 = ±0.5%, §4/§9.2) — the spec
        # key is kept verbatim so config and spec never need translating.
        if not 0 < self.max_slippage_pct < 1:
            raise ValueError(
                f"live.execution.max_slippage_pct must be in (0, 1) — a fraction, "
                f"e.g. 0.005 for ±0.5% — got {self.max_slippage_pct}"
            )
        if self.plan_duration_minutes <= 0:
            raise ValueError(
                f"live.execution.plan_duration_minutes must be > 0, "
                f"got {self.plan_duration_minutes}"
            )
        if self.slice_interval_seconds <= 0:
            raise ValueError(
                f"live.execution.slice_interval_seconds must be > 0, "
                f"got {self.slice_interval_seconds}"
            )
        if self.slice_interval_seconds > self.plan_duration_minutes * 60:
            raise ValueError(
                f"live.execution.slice_interval_seconds "
                f"({self.slice_interval_seconds}) exceeds the whole plan duration "
                f"({self.plan_duration_minutes} minutes) — no slice would ever fire"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> LiveExecutionConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "default_style": str,
                    "max_slippage_pct": decimal_from_yaml,
                    "plan_duration_minutes": int_from_yaml,
                    "slice_interval_seconds": int_from_yaml,
                },
            )
        )


@dataclass(frozen=True)
class LiveWebsocketConfig:
    """``live.websocket`` — disconnect tolerance before safe mode (§11/§13)."""

    disconnect_safe_mode_after_seconds: int = 300

    def __post_init__(self) -> None:
        if self.disconnect_safe_mode_after_seconds <= 0:
            raise ValueError(
                f"live.websocket.disconnect_safe_mode_after_seconds must be > 0, "
                f"got {self.disconnect_safe_mode_after_seconds}"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> LiveWebsocketConfig:
        return cls(**config_overrides(cfg, {"disconnect_safe_mode_after_seconds": int_from_yaml}))


@dataclass(frozen=True)
class LiveProtectionConfig:
    """``live.protection`` — SL repair / TP failure policy (§17, behaviour in PR 5)."""

    sl_repair_max_attempts: int = 3
    sl_repair_retry_delay_seconds: int = 5
    tp_failure_mode: TpFailureMode = TpFailureMode.DEGRADED_PROTECTION

    def __post_init__(self) -> None:
        if self.sl_repair_max_attempts <= 0:
            raise ValueError(
                f"live.protection.sl_repair_max_attempts must be > 0, "
                f"got {self.sl_repair_max_attempts}"
            )
        if self.sl_repair_retry_delay_seconds <= 0:
            raise ValueError(
                f"live.protection.sl_repair_retry_delay_seconds must be > 0, "
                f"got {self.sl_repair_retry_delay_seconds}"
            )
        _coerce_enum(
            self,
            "tp_failure_mode",
            TpFailureMode,
            key="live.protection.tp_failure_mode",
            expected="'degraded_protection' (the only §17 policy)",
        )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> LiveProtectionConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "sl_repair_max_attempts": int_from_yaml,
                    "sl_repair_retry_delay_seconds": int_from_yaml,
                    "tp_failure_mode": str,
                },
            )
        )


@dataclass(frozen=True)
class KillSwitchConfig:
    """``live.kill_switch`` — dead man's switch parameters (§18, behaviour in PR 2)."""

    enabled: bool = True
    schedule_cancel_seconds: int = 120
    refresh_interval_seconds: int = 30
    on_refresh_failed: RefreshFailedPolicy = RefreshFailedPolicy.SAFE_MODE
    on_shutdown: ShutdownPolicy = ShutdownPolicy.CANCEL_BOT_OWNED_OPEN_ORDERS
    emergency_close_on_shutdown: bool = False

    def __post_init__(self) -> None:
        if self.schedule_cancel_seconds <= 0:
            raise ValueError(
                f"live.kill_switch.schedule_cancel_seconds must be > 0, "
                f"got {self.schedule_cancel_seconds}"
            )
        if self.refresh_interval_seconds <= 0:
            raise ValueError(
                f"live.kill_switch.refresh_interval_seconds must be > 0, "
                f"got {self.refresh_interval_seconds}"
            )
        # The refresh must land before the scheduled cancel fires, or every
        # refresh cycle races the exchange-side deadline it exists to push back.
        if self.refresh_interval_seconds >= self.schedule_cancel_seconds:
            raise ValueError(
                f"live.kill_switch.refresh_interval_seconds "
                f"({self.refresh_interval_seconds}) must be < "
                f"schedule_cancel_seconds ({self.schedule_cancel_seconds}), or the "
                "dead man's switch fires between refreshes"
            )
        _coerce_enum(
            self,
            "on_refresh_failed",
            RefreshFailedPolicy,
            key="live.kill_switch.on_refresh_failed",
            expected="'safe_mode' (the only §18 policy)",
        )
        _coerce_enum(
            self,
            "on_shutdown",
            ShutdownPolicy,
            key="live.kill_switch.on_shutdown",
            expected="'cancel_bot_owned_open_orders' (the only §18 policy)",
        )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> KillSwitchConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "enabled": bool_from_yaml,
                    "schedule_cancel_seconds": int_from_yaml,
                    "refresh_interval_seconds": int_from_yaml,
                    "on_refresh_failed": str,
                    "on_shutdown": str,
                    "emergency_close_on_shutdown": bool_from_yaml,
                },
            )
        )


@dataclass(frozen=True)
class LiveConfig:
    """Typed ``live:`` block — modes, gates, and the §4 sub-blocks.

    ``mode`` is required — there is no safe value to guess (a default the
    ``live`` subcommand always rejects would just be a worse error message).
    Every other default is the safest expressible state: testnet, real orders
    off. Every cross-field contradiction the spec defines is a construction
    error, so a :class:`LiveConfig` that exists is one whose gates are
    internally coherent.
    """

    mode: ExecutionMode
    network: str = "testnet"
    allow_real_orders: bool = False
    allow_manage_external_orders: bool = False
    order_owner_prefix: str = "hta"
    require_agent_wallet: bool = True
    safety: LiveSafetyConfig = field(default_factory=LiveSafetyConfig)
    execution: LiveExecutionConfig = field(default_factory=LiveExecutionConfig)
    websocket: LiveWebsocketConfig = field(default_factory=LiveWebsocketConfig)
    protection: LiveProtectionConfig = field(default_factory=LiveProtectionConfig)
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)

    def __post_init__(self) -> None:
        _coerce_enum(
            self,
            "mode",
            ExecutionMode,
            key="live.mode",
            expected=f"one of {', '.join(m.value for m in ExecutionMode)}",
        )
        # §22 / build order: mainnet_live is defined but not enabled in Phase 3
        # v1 — rejected here so it cannot be reached by any code path at all.
        if self.mode is ExecutionMode.MAINNET_LIVE:
            raise ValueError(
                "live.mode 'mainnet_live' is not enabled in Phase 3 v1 (§22) — "
                "use testnet_live or mainnet_tiny"
            )
        network = self.network.strip().lower() if isinstance(self.network, str) else self.network
        if network not in LEGAL_NETWORKS:
            raise ValueError(
                f"live.network must be one of {list(LEGAL_NETWORKS)}, got {self.network!r}"
            )
        object.__setattr__(self, "network", network)
        # §3.1: each live mode is pinned to its network. A testnet_live run
        # pointed at mainnet (or vice versa) is the exact
        # "wrong-network-by-accident" failure the split env vars exist to
        # prevent — refuse it at load rather than trust later checks.
        required_network = {
            ExecutionMode.TESTNET_LIVE: "testnet",
            ExecutionMode.MAINNET_TINY: "mainnet",
        }.get(self.mode)
        if required_network is not None and network != required_network:
            raise ValueError(
                f"live.mode '{self.mode.value}' requires live.network "
                f"'{required_network}', got {network!r}"
            )
        if self.mode is ExecutionMode.PAPER and self.allow_real_orders:
            raise ValueError(
                "live.allow_real_orders is true but live.mode is 'paper' — paper "
                "mode never places real orders (§4.1); fix whichever was unintended"
            )
        # §25 #8: managing non-bot-owned orders is out of scope for Phase 3 v1;
        # the key exists so enabling it later is a config change, not a schema one.
        if self.allow_manage_external_orders:
            raise ValueError(
                "live.allow_manage_external_orders is not supported in Phase 3 v1 "
                "(§25) — bot-owned orders only"
            )
        if not _OWNER_PREFIX_RE.fullmatch(self.order_owner_prefix or ""):
            raise ValueError(
                f"live.order_owner_prefix must be 1-16 alphanumeric characters "
                f"(it becomes a '_'-separated cloid_logical segment, §8.2), "
                f"got {self.order_owner_prefix!r}"
            )
        # §4.1 requires an active kill switch before any real order; with real
        # orders enabled and the switch off, the run could never place an order
        # — surface the contradiction at startup instead of as a silent
        # gate-rejection on every cycle.
        if self.allow_real_orders and not self.kill_switch.enabled:
            raise ValueError(
                "live.allow_real_orders is true but live.kill_switch.enabled is "
                "false — real orders require the kill switch (§4.1)"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> LiveConfig:
        overrides = config_overrides(
            cfg,
            {
                "mode": str,
                "network": str,
                "allow_real_orders": bool_from_yaml,
                "allow_manage_external_orders": bool_from_yaml,
                "order_owner_prefix": str,
                "require_agent_wallet": bool_from_yaml,
                "safety": LiveSafetyConfig.from_dict,
                "execution": LiveExecutionConfig.from_dict,
                "websocket": LiveWebsocketConfig.from_dict,
                "protection": LiveProtectionConfig.from_dict,
                "kill_switch": KillSwitchConfig.from_dict,
            },
        )
        # Named error, not a bare TypeError from the missing dataclass field:
        # an absent (or blank) mode is the most likely newcomer mistake and
        # the message must say "required", not guess a mode for them.
        if "mode" not in overrides:
            raise ValueError(
                "live.mode is required — one of paper, testnet_live, mainnet_tiny "
                "(mainnet_live is not enabled in Phase 3 v1)"
            )
        return cls(**overrides)


@dataclass(frozen=True)
class NotionalCaps:
    """The §5 rule-3 startup caps, computed once and logged at startup.

    Construction enforces the defining identity (``effective_notional_cap =
    min(pct_cap_notional, max_notional_usdc)`` implies ``effective <= pct`` and
    both non-negative), so a hand-built instance in a later PR (or a test
    fixture) can never report a coherent-looking but impossible cap pair.
    """

    pct_cap_notional: Decimal
    effective_notional_cap: Decimal

    def __post_init__(self) -> None:
        if self.pct_cap_notional < 0 or self.effective_notional_cap < 0:
            raise ValueError(
                f"notional caps must be >= 0, got pct_cap_notional="
                f"{self.pct_cap_notional}, effective_notional_cap="
                f"{self.effective_notional_cap}"
            )
        if self.effective_notional_cap > self.pct_cap_notional:
            raise ValueError(
                f"effective_notional_cap ({self.effective_notional_cap}) exceeds "
                f"pct_cap_notional ({self.pct_cap_notional}) — it is defined as "
                "min(pct_cap_notional, max_notional_usdc) (§5 rule 3)"
            )

    @property
    def below_exchange_minimum(self) -> bool:
        """§5 rule 4: a cap below the exchange minimum order value means the
        run is structurally unable to trade — startup must fail."""
        return self.effective_notional_cap < EXCHANGE_MIN_ORDER_NOTIONAL_USDC


def compute_notional_caps(account_equity: Decimal, safety: LiveSafetyConfig) -> NotionalCaps:
    """The §5 rule-3 formulas, verbatim:

    ``pct_cap_notional = account_equity × max_target_margin_pct / 100 × leverage``
    ``effective_notional_cap = min(pct_cap_notional, max_notional_usdc)``
    """
    pct_cap = account_equity * safety.max_target_margin_pct / 100 * safety.leverage
    return NotionalCaps(
        pct_cap_notional=pct_cap,
        effective_notional_cap=min(pct_cap, safety.max_notional_usdc),
    )


def validate_live_risk_consistency(live: LiveConfig, risk: RiskConfig) -> None:
    """Cross-check the ``live.safety`` fields that overlap the ``risk:`` block.

    PR 5 runs the AI gate off ``risk:`` and the hard live caps off
    ``live.safety:``. The two blocks are parsed independently, so without this
    check a config could size AI targets under one leverage/margin regime and
    cap live orders under another — with real money, silently. Rules:

    - ``leverage`` and ``margin_mode`` must be identical (one sizing regime);
    - ``live.safety.max_target_margin_pct`` may be *tighter* than the gate's
      cap (layered defense — the gate's §10.1 checks reject loudly) but never
      looser: extra live headroom the gate can never approve means the
      operator almost certainly edited the wrong block.

    Raises ``ValueError`` (a config error — callers exit 1, never clamp).
    """
    if live.safety.leverage != risk.leverage:
        raise ValueError(
            f"live.safety.leverage ({live.safety.leverage}) != risk.leverage "
            f"({risk.leverage}) — the AI gate and the live caps must size under "
            "one leverage regime; align the two blocks"
        )
    if live.safety.margin_mode is not risk.margin_mode:
        raise ValueError(
            f"live.safety.margin_mode ({live.safety.margin_mode.value}) != "
            f"risk.margin_mode ({risk.margin_mode.value}) — align the two blocks"
        )
    if live.safety.max_target_margin_pct > risk.max_target_margin_pct:
        raise ValueError(
            f"live.safety.max_target_margin_pct ({live.safety.max_target_margin_pct}) "
            f"exceeds risk.max_target_margin_pct ({risk.max_target_margin_pct}) — "
            "the live cap would advertise headroom the AI gate can never approve; "
            "a tighter live cap is fine, a looser one is a config mistake"
        )
