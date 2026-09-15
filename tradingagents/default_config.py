import os
import sys

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    "TRADINGAGENTS_MAX_TOKENS":           "max_tokens",
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")

# Completion-token cap the interactive CLI applies when neither the config nor
# TRADINGAGENTS_MAX_TOKENS sets one. A module constant, deliberately NOT the
# value of DEFAULT_CONFIG["max_tokens"]: the library path stays at None (each
# provider's own default) so importing the package changes nobody's requests,
# while the CLI — the one non-perp path with an operator at the keyboard —
# never goes out uncapped through a gateway (#177, #183). The perp bridge
# declares the same number as its own default; a contrib test pins them equal.
DEFAULT_MAX_TOKENS = 8192


# The validators for the integer LLM knobs (``max_tokens``,
# ``llm_max_retries``) live HERE, beside the ``_ENV_OVERRIDES`` rows they
# validate, not in ``graph/trading_graph.py`` where the graph applies them:
# they depend on nothing but ``sys.maxsize``, and the perp bridge gates the
# env values at daemon startup (#266) — reached through the graph module,
# that one import would drag the whole engine tree (langgraph, every agent
# and LLM client) into provider construction. ``trading_graph`` imports
# them from here, so the names upstream's tests import from it still
# resolve. NOT applied in ``_coerce`` below: the env overlay coerces
# against the default's type, and these knobs default to ``None``, so an
# env string rides through to whichever consumer validates it.
def _coerce_config_int(value, *, key, env, minimum, bound):
    """Validate an integer config knob, or raise ``ValueError`` naming ``key`` and ``env``.

    One policy for the family (``max_tokens``, ``llm_max_retries``; #264):
    an int or a numeric string is accepted; a bool, a non-integral numeric
    (``4096.7``, ``Decimal("2.5")``), a value below ``minimum`` or beyond
    ``sys.maxsize`` is refused. Numerics are range-checked BEFORE ``int()``
    because ``int(Decimal("1E999999999"))`` hangs rather than raising (#177);
    a string is bounded after parsing, since ``int()`` caps its digits.
    ``bound`` is the worded rule for the message and must describe
    ``minimum`` ("a positive integer (> 0)" for 1); nothing checks the pair.
    """
    base = f"config key '{key}' ({env}) must be {bound}"
    if isinstance(value, bool):
        raise ValueError(f"{base}, not a boolean: {value!r}")
    try:
        if isinstance(value, str):
            parsed = int(value)
        elif not (-sys.maxsize <= value <= sys.maxsize):
            parsed = None  # int() of a huge exponent, either sign, never returns
        else:
            parsed = int(value)
            if parsed != value:
                raise ValueError
    # ArithmeticError covers Decimal("NaN"), whose ordering comparison signals
    # InvalidOperation, and OverflowError: a bad value must never leak raw.
    except (TypeError, ValueError, ArithmeticError):
        raise ValueError(f"{base}, got {value!r}") from None
    if parsed is None or not (-sys.maxsize <= parsed <= sys.maxsize):
        raise ValueError(f"{base} within the platform integer range, got {value!r}")
    if parsed < minimum:
        raise ValueError(f"{base}, got {value!r}")
    return parsed


def _coerce_max_retries(value):
    """``llm_max_retries``: the SDK retry budget; 0 disables retries (#1091)."""
    return _coerce_config_int(
        value,
        key="llm_max_retries",
        env="TRADINGAGENTS_LLM_MAX_RETRIES",
        minimum=0,
        bound="a non-negative integer (>= 0)",
    )


def _coerce_max_tokens(value):
    """``max_tokens``: the completion cap; 0 is a provider 400 on every call (#177)."""
    return _coerce_config_int(
        value,
        key="max_tokens",
        env="TRADINGAGENTS_MAX_TOKENS",
        minimum=1,
        bound="a positive integer (> 0)",
    )


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
    loudly at startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    # When False, the gated agents (Portfolio Manager, Research Manager,
    # Trader) always generate free text instead of structured output — see
    # bind_structured()'s docstring for why. The Sentiment Analyst is not gated.
    "structured_output": True,
    "deep_think_llm": "gpt-5.6",
    "quick_think_llm": "gpt-5.6-luna",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Sampling temperature, forwarded to every provider when set. None leaves
    # each provider at its own default. Lower values reduce run-to-run
    # variation on models that honor it; reasoning models largely ignore it
    # and no setting makes LLM output bit-identical across runs (see README).
    "temperature": None,
    # SDK retry budget forwarded to every provider chat client. None leaves each
    # provider/SDK at its own default (usually 2). Raise it to ride out bursty
    # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
    "llm_max_retries": None,
    # Completion-token cap, forwarded to every provider when set. None leaves
    # each provider at its own default — risky through gateway providers,
    # where some upstreams treat "no cap" as "full context" and reject every
    # call (#177); the graph warns once when a gateway provider runs uncapped.
    # The CLI fills DEFAULT_MAX_TOKENS in when this is still None (#183).
    "max_tokens": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category).
    # The configured value is the exact vendor chain — requests are NOT silently
    # routed to vendors you didn't choose. For ordered fallback, list several,
    # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
        "macro_data": "fred",                # Options: fred (needs FRED_API_KEY)
        "prediction_markets": "polymarket",  # Options: polymarket (keyless)
        # sosovalue needs SOSOVALUE_API_KEY (free); with it unset the chain
        # falls through to farside (currently Cloudflare-blocked; its stale
        # cache may serve up to 14 days) and then the no-data sentinel — which
        # is also the emergency-disable path.
        "crypto_etf_flows": "sosovalue,farside",  # Options: sosovalue (SOSOVALUE_API_KEY), farside (keyless)
        "crypto_sentiment": "alternative_me",  # Options: alternative_me (keyless, Fear & Greed)
        # Crypto options implied volatility (DVOL index + 25-delta skew).
        # Shipped OFF and cut over to "deribit" on 2026-08-12 — the deliberate,
        # dated server-side action the ship-off note asked for. From that date the
        # paper-BTC deployment's analyst input surface gains a tool, a prompt
        # paragraph and a report section, so a later review can attribute any
        # behaviour change to it. The perp engine's config overlay carries a fixed
        # key list and does not pipe ``data_vendors`` through (see
        # dataflows/interface.py), so this default IS that deployment's live value:
        # switching the category back off is a code change, not a YAML edit.
        "options_data": "deribit",           # Options: deribit (keyless, BTC/ETH only), none
        # US macro economic calendar (scheduled events + releases vs forecast)
        # and corporate BTC treasury holdings/activity, both served by the
        # SoSoValue key already deployed for crypto_etf_flows. Shipped OFF and
        # cut over to "sosovalue" on 2026-09-02 — the deliberate, dated flip
        # the ship-off note asked for, kept separate from options_data's
        # 2026-08-12 cutover so the two input-surface changes stay attributable
        # apart. Per the note above this default IS the running deployment's
        # live value, so the first deploy carrying this commit is the dated
        # input-surface segmentation point, and switching either category back
        # off is a code change, not a YAML edit.
        "economic_calendar": "sosovalue",    # Options: sosovalue (SOSOVALUE_API_KEY), none
        "btc_treasuries": "sosovalue",       # Options: sosovalue (SOSOVALUE_API_KEY), none
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",       # NSE India (Nifty 50)
        ".BO":  "^BSESN",      # BSE India (Sensex)
        ".T":   "^N225",       # Tokyo (Nikkei 225)
        ".HK":  "^HSI",        # Hong Kong (Hang Seng)
        ".L":   "^FTSE",       # London (FTSE 100)
        ".TO":  "^GSPTSE",     # Toronto (TSX Composite)
        ".AX":  "^AXJO",       # Australia (ASX 200)
        ".SS":  "000001.SS",   # Shanghai (SSE Composite)
        ".SZ":  "399001.SZ",   # Shenzhen (SZSE Component)
        "":     "SPY",         # default for US-listed tickers (no suffix)
    },
})
