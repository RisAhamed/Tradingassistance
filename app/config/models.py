"""Pydantic models describing the CENTRAL configuration (configs/config.yaml).

Every value here is a *configuration knob*. Logic must never hardcode trading
parameters; it must read them from these models. Secrets are intentionally NOT
part of this model tree — they live in :mod:`app.config.settings` (.env).
"""
from __future__ import annotations

from datetime import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

TradingMode = Literal["paper"]
"""Only PAPER trading is allowed. Live mode is deliberately not representable."""


class _Section(BaseModel):
    """Base for config sections: forbid unknown keys to catch typos early."""

    model_config = {"extra": "forbid"}


class ApplicationConfig(_Section):
    name: str = "trading-agent"
    environment: Literal["development", "paper", "test"] = "development"
    log_level: str = "INFO"
    timezone: str = "UTC"


class TradingConfig(_Section):
    mode: TradingMode = "paper"
    enabled: bool = True
    symbol: str = Field(min_length=1)
    # Forward-compatible multi-symbol list (Phase L). Only ``symbol`` is used
    # today; the list exists so multi-symbol can land without a config break.
    symbols: list[str] | None = None
    market: str = "crypto"
    broker: str = "alpaca"
    enabled: bool = True
    symbol: str = Field(min_length=1)
    market: str = "crypto"
    # broker: str selected via the broker registry below; Literal removed so
    # adding a vendor does not require editing the schema.
    autostart: bool = True

    @field_validator("broker")
    @classmethod
    def _broker_known(cls, value: str) -> str:
        from app.brokers.registry import known_brokers

        if value not in known_brokers():
            raise ValueError(f"unknown broker '{value}' (known: {known_brokers()})")
        return value

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, value: str) -> str:
        if value != "paper":
            raise ValueError("PAPER_TRADING_ONLY: trading.mode must be 'paper'")
        return value

    @model_validator(mode="after")
    def _symbols_include_primary(self) -> "TradingConfig":
        if self.symbols is None:
            self.symbols = [self.symbol]
            return self
        if self.symbol not in self.symbols:
            raise ValueError("trading.symbol must be present in trading.symbols")
        return self


class SessionConfig(_Section):
    enabled: bool = True
    timezone: str = "UTC"
    start: str = "00:00"
    end: str = "23:59"
    entry_cutoff: str = "23:30"
    flatten_deadline: str = "23:55"
    max_holding_minutes: int = 240

    @field_validator("start", "end", "entry_cutoff", "flatten_deadline")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        _parse_hhmm(value)
        return value

    @property
    def start_time(self) -> time:
        return _parse_hhmm(self.start)

    @property
    def end_time(self) -> time:
        return _parse_hhmm(self.end)

    @property
    def entry_cutoff_time(self) -> time:
        return _parse_hhmm(self.entry_cutoff)

    @property
    def flatten_deadline_time(self) -> time:
        return _parse_hhmm(self.flatten_deadline)


class ReconnectConfig(_Section):
    enabled: bool = True
    max_attempts: int = 10
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0


class MockMarketConfig(_Section):
    """Deterministic mock provider knobs (paper/test only; never live).

    ``tick_seconds`` is how much *market time* each synthetic tick advances.
    It must be large enough that timeframe candles (and therefore regime
    warm-up) accumulate within the demo's tick budget.
    """

    tick_seconds: float = 120.0
    seed: int = 7


class CoveragePolicyConfig(_Section):
    """How much historical coverage is acceptable before trading may proceed."""

    minimum_percent: float = 90.0
    maximum_gap_candles: int = 5
    # fail -> warm-up fails (not ready) | warn -> proceed but flagged | continue -> proceed
    on_insufficient: Literal["fail", "warn", "continue"] = "fail"

    @field_validator("minimum_percent")
    @classmethod
    def _percent_range(cls, value: float) -> float:
        if not 0.0 < value <= 100.0:
            raise ValueError("coverage.minimum_percent must be in (0, 100]")
        return value


class HistoricalDataConfig(_Section):
    """Historical warm-up bars (Phase C) and coverage policy (Phase C1)."""

    enabled: bool = True
    provider: Literal["alpaca", "none"] = "none"
    required: bool = False
    bar_timeframe: str = "1m"
    lookback_bars: int | None = None
    maximum_history_bars: int = 10000
    startup_timeout_seconds: float = 30.0
    # A gap larger than this many base candles between the last historical bar and
    # the first live update marks the data as not trustworthy for trading.
    max_gap_candles: int = 5
    coverage: CoveragePolicyConfig = Field(default_factory=CoveragePolicyConfig)


class LiveBarsConfig(_Section):
    """Phase C1: Alpaca live bar stream — the canonical strategy candle source."""

    enabled: bool = True
    # When true, strategy candles are built ONLY from bars; trades no longer
    # create candles (they remain available for price/spread/microstructure).
    canonical: bool = True
    timeframe: str = "1m"
    required: bool = True
    startup_timeout_seconds: float = 20.0
    # Consecutive missing base candles tolerated before an outage is declared.
    max_gap_candles: int = 3
    # Halt entries and run an event-driven historical resync on a large gap.
    resync_enabled: bool = True
    resync_max_attempts: int = 2


class FreshnessStreamConfig(_Section):
    """Per-stream freshness ceiling (seconds)."""

    maximum_age_seconds: float = 90.0


class FreshnessBarConfig(_Section):
    """Cadence-aware freshness for the canonical bar stream."""

    expected_interval_seconds: float = 60.0
    grace_period_seconds: float = 30.0
    maximum_age_seconds: float = 90.0


class DataFreshnessConfig(_Section):
    """Separates *measuring* freshness from *risk policy* on stale data.

    Freshness is measured per stream (bars is the primary, strategy-relevant
    one) so the three are never merged into one misleading timestamp. The
    blocking decision belongs to ``risk.stale_market_data``.

    ``mode: cadence_aware`` makes the effective bar threshold
    ``max(bar.maximum_age_seconds, bar.expected_interval_seconds +
    bar.grace_period_seconds)`` — so a healthy 60 s bar stream with a
    60 s cadence is never falsely stale.
    """

    mode: Literal["cadence_aware", "static"] = "cadence_aware"
    bar: FreshnessBarConfig = Field(default_factory=FreshnessBarConfig)
    quote: FreshnessStreamConfig = Field(default_factory=FreshnessStreamConfig)
    trade: FreshnessStreamConfig = Field(default_factory=FreshnessStreamConfig)
    stale_action: Literal["block_entries", "warn_only"] = "block_entries"
    # Deprecated flat knobs kept for backward compatibility; the nested
    # fields above are authoritative when mode == "cadence_aware".
    threshold_seconds: float = 30.0
    quote_threshold_seconds: float = 30.0
    trade_threshold_seconds: float = 300.0

    @model_validator(mode="after")
    def _migrate_flat_thresholds(self) -> "DataFreshnessConfig":
        if self.mode == "cadence_aware":
            if self.bar.maximum_age_seconds == 90.0 and self.threshold_seconds != 30.0:
                object.__setattr__(self.bar, "maximum_age_seconds", self.threshold_seconds)
            if self.quote.maximum_age_seconds == 90.0 and self.quote_threshold_seconds != 30.0:
                object.__setattr__(self.quote, "maximum_age_seconds", self.quote_threshold_seconds)
            if self.trade.maximum_age_seconds == 90.0 and self.trade_threshold_seconds != 300.0:
                object.__setattr__(self.trade, "maximum_age_seconds", self.trade_threshold_seconds)
        return self


class MarketDataSymbolConfig(_Section):
    # Quote suffixes considered when expanding Alpaca-style BTCUSD into
    # canonical BTC/USD. Longest-first matching is used, so "USDT" wins.
    quote_suffixes: list[str] = Field(default_factory=lambda: ["USDT", "USDC", "USD", "BTC", "ETH"])
    base_pattern: str | None = None

    @field_validator("quote_suffixes")
    @classmethod
    def _suffixes_nonempty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("market_data.symbol.quote_suffixes must not be empty")
        return [str(v).upper() for v in value]


class MarketDataConfig(_Section):
    # ``provider`` is a registry-resolved vendor name (not a Literal) so new
    # market-data providers can be added without altering the schema.
    provider: str = "alpaca"
    feed: str = "crypto"
    feed: str = "crypto"
    feed: str = "crypto"
    feed: str = "crypto"
    websocket_enabled: bool = True
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    freshness: DataFreshnessConfig = Field(default_factory=DataFreshnessConfig)
    history: HistoricalDataConfig = Field(default_factory=HistoricalDataConfig)
    bars: LiveBarsConfig = Field(default_factory=LiveBarsConfig)
    mock: MockMarketConfig = Field(default_factory=MockMarketConfig)
    # Clock integrity: updates stamped further than this into the future are
    # rejected before they can touch store/aggregator/feature state.
    max_future_skew_seconds: float = 5.0

    @field_validator("provider")
    @classmethod
    def _provider_known(cls, value: str) -> str:
        from app.market_data.registry import known_providers

        if value not in known_providers():
            raise ValueError(f"unknown market-data provider '{value}' (known: {known_providers()})")
        return value

    symbol: MarketDataSymbolConfig = Field(default_factory=MarketDataSymbolConfig)


class TimeframeCandidateConfig(_Section):
    """Candidate timeframes for one slot plus the data minimum required."""

    timeframes: list[str] = Field(default_factory=list)
    min_candles: int = 10

    @model_validator(mode="before")
    @classmethod
    def _accept_list(cls, v):
        if isinstance(v, list):
            return {"timeframes": v}
        return v


class TimeframeSelectionConfig(_Section):
    """Dynamic timeframe selection (deterministic, rule-based, configurable)."""

    mode: Literal["dynamic", "static"] = "dynamic"
    context_candidates: TimeframeCandidateConfig = Field(
        default_factory=lambda: TimeframeCandidateConfig(timeframes=["15m", "30m", "1h"])
    )
    signal_candidates: TimeframeCandidateConfig = Field(
        default_factory=lambda: TimeframeCandidateConfig(timeframes=["1m", "3m", "5m", "15m"])
    )
    execution_candidates: TimeframeCandidateConfig = Field(
        default_factory=lambda: TimeframeCandidateConfig(timeframes=["1m", "3m"])
    )
    selection_method: Literal["rule_based"] = "rule_based"
    volatility_high_threshold: float = 0.02
    volatility_low_threshold: float = 0.005
    spread_max_percent: float = 0.10
    liquidity_min_volume_ratio: float = 0.5


class TradePlanConfig(_Section):
    """TradePlan construction policy (data/decision architecture only)."""

    enabled: bool = True
    stop_atr_multiplier: float = 2.0
    target_atr_multiplier: float = 3.0
    default_expected_holding_minutes: float = 30.0
    maximum_holding_minutes: float = 240.0
    # Phase D.1: decision-trace ring-buffer size.
    decision_trace_max_stages: int = 64


class TimeframesConfig(_Section):
    context: str = "15m"
    signal: str = "5m"
    execution: str = "1m"
    available: list[str] = Field(default_factory=lambda: ["1m", "3m", "5m", "15m", "30m", "1h"])
    selection: TimeframeSelectionConfig = Field(default_factory=TimeframeSelectionConfig)


class EmaConfig(_Section):
    enabled: bool = True
    periods: list[int] = Field(default_factory=lambda: [20, 50])


class RsiConfig(_Section):
    enabled: bool = True
    period: int = 14


class AtrConfig(_Section):
    enabled: bool = True
    period: int = 14


class ToggleConfig(_Section):
    enabled: bool = True


class RangeConfig(_Section):
    lookback_periods: int = 20


class FeaturesConfig(_Section):
    ema: EmaConfig = Field(default_factory=EmaConfig)
    rsi: RsiConfig = Field(default_factory=RsiConfig)
    atr: AtrConfig = Field(default_factory=AtrConfig)
    vwap: ToggleConfig = Field(default_factory=ToggleConfig)
    # True session-anchored VWAP (H1): cumulative typical-price VWAP over the
    # current UTC session date. Pure window function; no state. Default on.
    session_vwap: ToggleConfig = Field(default_factory=ToggleConfig)
    volatility: ToggleConfig = Field(default_factory=ToggleConfig)
    volume: ToggleConfig = Field(default_factory=ToggleConfig)
    range: RangeConfig = Field(default_factory=RangeConfig)


class RegimeTrending(_Section):
    ema_distance_atr_min: float = 0.25


class RegimeVolatility(_Section):
    high_volatility_percent: float = 0.02
    low_volatility_percent: float = 0.005


class RegimeConfig(_Section):
    enabled: bool = True
    trending: RegimeTrending = Field(default_factory=RegimeTrending)
    volatility: RegimeVolatility = Field(default_factory=RegimeVolatility)
    min_candles: int = 20


class BreakoutConfig(_Section):
    lookback_periods: int = 20
    buffer_percent: float = 0.05


class MomentumConfig(_Section):
    enabled: bool = True
    rsi_min_long: float = 55
    rsi_max_long: float = 75
    rsi_min_short: float = 25
    rsi_max_short: float = 45


class TrendConfig(_Section):
    enabled: bool = True
    fast_ema: int = 20
    slow_ema: int = 50


class StrategyVolatilityConfig(_Section):
    minimum_atr: float = 0
    maximum_atr: float = 0


class SpreadConfig(_Section):
    maximum_percent: float = 0.10


class CooldownConfig(_Section):
    enabled: bool = True
    minutes: int = 5


class MeanReversionConfig(_Section):
    """VWAP-reversion entry thresholds (frozen candidate values, v0.1)."""

    stretch_atr_min: float = 1.0
    rsi_max_long: float = 35.0
    recovery_min: float = 0.30
    volume_ratio_min: float = 0.7


class StrategySelectionConfig(_Section):
    mode: Literal["manual", "regime_aware"] = "manual"
    manual_name: str = "breakout_momentum"
    fallback_name: str = "breakout_momentum"
    map: dict[str, str] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _allowed_mode(cls, v: str) -> str:
        if v not in ("manual", "regime_aware"):
            raise ValueError(f"strategy.selection.mode must be manual|regime_aware, got '{v}'")
        return v

    @model_validator(mode="after")
    def _validate_registered(self) -> "StrategySelectionConfig":
        from app.strategies.registry import known_strategies

        known = known_strategies()
        for field_name, name in (
            ("manual_name", self.manual_name),
            ("fallback_name", self.fallback_name),
        ):
            if name not in known:
                raise ValueError(
                    f"strategy.selection.{field_name} references unknown strategy '{name}' (known: {known})"
                )
        for regime, name in self.map.items():
            if name not in known:
                raise ValueError(
                    f"strategy.selection.map['{regime}'] references unknown strategy '{name}' (known: {known})"
                )
        return self


class StrategyConfig(_Section):
    enabled: bool = True
    name: str = "breakout_momentum"
    version: str = "1.0"
    # Directions the execution seam may open. BTC/USD spot validation is
    # LONG ONLY (Alpaca spot has no short counterpart); generic short
    # simulation stays available for future short-capable markets.
    allowed_directions: list[Literal["long", "short"]] = Field(default_factory=lambda: ["long", "short"])
    breakout: BreakoutConfig = Field(default_factory=BreakoutConfig)
    momentum: MomentumConfig = Field(default_factory=MomentumConfig)
    trend: TrendConfig = Field(default_factory=TrendConfig)
    volatility: StrategyVolatilityConfig = Field(default_factory=StrategyVolatilityConfig)
    spread: SpreadConfig = Field(default_factory=SpreadConfig)
    cooldown: CooldownConfig = Field(default_factory=CooldownConfig)
    mean_reversion: MeanReversionConfig = Field(default_factory=MeanReversionConfig)
    selection: StrategySelectionConfig = Field(default_factory=StrategySelectionConfig)


class StopLossConfig(_Section):
    enabled: bool = True
    atr_multiplier: float = 1.5


class TakeProfitConfig(_Section):
    enabled: bool = True
    risk_reward_ratio: float = 2.0


class StaleDataConfig(_Section):
    # DEPRECATED: the effective freshness threshold is owned by
    # ``market_data.freshness`` (app/decision/freshness.py FreshnessPolicy).
    # Kept only for backwards compatibility; do not treat as authoritative.
    maximum_age_seconds: float = 5.0


class RiskConfig(_Section):
    enabled: bool = True
    risk_per_trade_percent: float = 0.5
    maximum_daily_loss_percent: float = 2.0
    maximum_open_positions: int = 1
    maximum_position_value_percent: float = 20.0
    maximum_orders_per_session: int = 20
    stop_loss: StopLossConfig = Field(default_factory=StopLossConfig)
    take_profit: TakeProfitConfig = Field(default_factory=TakeProfitConfig)
    maximum_holding_minutes: int = 240
    stale_market_data: StaleDataConfig = Field(default_factory=StaleDataConfig)
    maximum_spread_percent: float = 0.10


class PositionSizingConfidenceConfig(_Section):
    """Conviction sizing (Phase G): scales the risk budget from a ceiling.

    ``risk_per_trade_percent`` remains the ceiling — conviction only ever
    *reduces* exposure. ``effective = risk_percent * lerp(min, max, conf)``.
    """
    enabled: bool = True
    min_multiplier: float = 0.25
    max_multiplier: float = 1.00

    @model_validator(mode="after")
    def _bounds(self) -> "PositionSizingConfidenceConfig":
        if not (0.0 < self.min_multiplier <= self.max_multiplier <= 1.0):
            raise ValueError(
                "position_sizing.confidence requires 0 < min_multiplier <= max_multiplier <= 1.0"
            )
        return self


class PositionSizingConfig(_Section):
    method: str = "risk_based"
    # Deprecated compatibility field for direct programmatic construction.
    # AppConfig uses risk.risk_per_trade_percent as the YAML authority.
    risk_per_trade_percent: float | None = None
    minimum_quantity: float = 0.000001
    maximum_quantity: float = 100.0
    quantity_precision: int = 6
    confidence: PositionSizingConfidenceConfig = Field(default_factory=PositionSizingConfidenceConfig)


class SlippageConfig(_Section):
    protection_enabled: bool = True
    maximum_percent: float = 0.20


class AmbiguousRetryConfig(_Section):
    """Policy for an *ambiguous* order submission (timeout / unknown outcome).

    Phase A deliberately removed blind order retries, because resubmitting can
    duplicate exposure. This configuration states the real behaviour explicitly
    instead of implying an automatic retry.
    """

    action: Literal["reconcile"] = "reconcile"
    max_reconcile_attempts: int = 3


class ExecutionConfig(_Section):
    """Execution gate — PHASE B: this is the master kill-switch for orders.

    ``enabled=False`` is the DEFAULT (fail-closed): market data, features,
    regime, strategy, signal generation and risk evaluation all continue, but
    no order may be created or submitted to any broker. A missing/ambiguous
    configuration therefore keeps execution disabled.
    """

    enabled: bool = False
    order_type: Literal["market", "limit"] = "market"
    allow_short: bool = True
    slippage: SlippageConfig = Field(default_factory=SlippageConfig)
    retry: AmbiguousRetryConfig = Field(default_factory=AmbiguousRetryConfig)
    duplicate_order_protection: bool = True
    # D.5: how often the engine polls the broker for fill/order-state updates.
    order_sync_interval_seconds: float = 1.0
    # D.5.5-R2: bound exit retries so a rejected exit cannot storm the broker.
    max_exit_attempts: int = 3
    exit_backoff_seconds: float = 2.0


class SessionCloseoutConfig(_Section):
    enabled: bool = True
    flatten_all_positions: bool = True
    stop_new_entries_before_end_minutes: int = 30
    retry_flatten: bool = True
    maximum_flatten_attempts: int = 3
    require_position_zero: bool = True


class BacktestingExecutionConfig(_Section):
    """Simulated execution model — every assumption lives here, never in code."""

    spread_percent: float = 0.02
    slippage_percent: float = 0.05
    fee_percent: float = 0.25
    fee_model: Literal["in_kind", "quote"] = "in_kind"
    partial_fill_enabled: bool = False
    partial_fill_max_fraction: float = 0.5
    latency_bars: int = 0
    entry_price: Literal["close", "next_open"] = "close"
    exit_price: Literal["stop_target", "close"] = "stop_target"


class BacktestingConfig(_Section):
    enabled: bool = True
    initial_capital: float = 100000
    commission: float = 0
    slippage_percent: float = 0.05
    execution: BacktestingExecutionConfig = Field(default_factory=BacktestingExecutionConfig)
    # G.1: fixed ISO-8601 anchor for reproducible synthetic history. null
    # keeps the legacy utcnow-anchored (non-reproducible) behaviour.
    reference_start: str | None = None


class OptimizationWindowConfig(_Section):
    """Walk-forward geometry (Phase H). Fold i trains on
    ``data[i*step : i*step+train]`` and validates on the next
    ``validation`` bars; a fold is kept only when both slices are complete.
    """
    train_bars: int = 2000
    validation_bars: int = 500
    step_bars: int = 500


class OptimizationScoringConfig(_Section):
    """Walk-forward scoring weights (Phase H). Must sum to 1.0."""
    net_pnl: float = 0.35
    profit_factor: float = 0.25
    max_drawdown: float = 0.20
    expectancy: float = 0.10
    stability: float = 0.10

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "OptimizationScoringConfig":
        total = (self.net_pnl + self.profit_factor + self.max_drawdown
                 + self.expectancy + self.stability)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"optimization.scoring weights must sum to 1.0 (±1e-9), got {total}"
            )
        return self


class OptimizationConfig(_Section):
    """Offline walk-forward tuner (Phase H). CLI-only, default off, never
    touches live trading or configs/config.yaml."""
    enabled: bool = False
    spaces_dir: str = "app/optimization/spaces"
    output_dir: str = "logs/proposals"
    window: OptimizationWindowConfig = Field(default_factory=OptimizationWindowConfig)
    scoring: OptimizationScoringConfig = Field(default_factory=OptimizationScoringConfig)

class AiModelConfig(_Section):
    name: str = ""
    temperature: float = 0.1
    max_tokens: int = 2000


class AiEndpointConfig(_Section):
    base_url: str = "http://localhost:11434"


class AiBehaviorConfig(_Section):
    run_on_every_tick: bool = False
    run_on_events_only: bool = True
    investigate_order_rejection: bool = True
    investigate_risk_failure: bool = True
    investigate_reconciliation_failure: bool = True
    investigate_system_error: bool = True
    investigate_unexpected_position: bool = True

    @field_validator("run_on_every_tick")
    @classmethod
    def _never_every_tick(cls, value: bool) -> bool:
        if value:
            raise ValueError("AI must not run on every tick (ai.behavior.run_on_every_tick)")
        return value


class AiPermissionsConfig(_Section):
    read_only: bool = True
    allow_pause: bool = True
    allow_resume: bool = True
    allow_reconciliation: bool = True
    allow_flatten: bool = False


class AiLimitsConfig(_Section):
    maximum_tool_calls_per_event: int = 5
    timeout_seconds: int = 30


class AiConfig(_Section):
    enabled: bool = True
    provider: Literal["ollama"] = "ollama"
    model: AiModelConfig = Field(default_factory=AiModelConfig)
    endpoint: AiEndpointConfig = Field(default_factory=AiEndpointConfig)
    behavior: AiBehaviorConfig = Field(default_factory=AiBehaviorConfig)
    permissions: AiPermissionsConfig = Field(default_factory=AiPermissionsConfig)
    limits: AiLimitsConfig = Field(default_factory=AiLimitsConfig)

class ConsoleLoggingConfig(_Section):
    enabled: bool = True
    format: Literal["structured", "pretty"] = "structured"


class FileLoggingConfig(_Section):
    enabled: bool = True
    path: str = "logs/trading-agent.log"


class RotationConfig(_Section):
    enabled: bool = True
    maximum_size_mb: int = 50
    backup_count: int = 5


class EventLoggingConfig(_Section):
    market_data: bool = True
    strategy: bool = True
    signals: bool = True
    risk: bool = True
    orders: bool = True
    execution: bool = True
    positions: bool = True
    pnl: bool = True
    session: bool = True
    ai: bool = True


class LoggingConfig(_Section):
    level: str = "INFO"
    console: ConsoleLoggingConfig = Field(default_factory=ConsoleLoggingConfig)
    file: FileLoggingConfig = Field(default_factory=FileLoggingConfig)
    rotation: RotationConfig = Field(default_factory=RotationConfig)
    events: EventLoggingConfig = Field(default_factory=EventLoggingConfig)


class DashboardRefreshConfig(_Section):
    mode: Literal["sse", "websocket", "polling"] = "sse"
    fallback: Literal["polling"] = "polling"
    polling_interval_seconds: float = 2.0


class DashboardConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    refresh: DashboardRefreshConfig = Field(default_factory=DashboardRefreshConfig)


class HealthChecksConfig(_Section):
    market_data: bool = True
    broker: bool = True
    database: bool = True
    ai: bool = True


class MonitoringConfig(_Section):
    enabled: bool = True
    health_checks: HealthChecksConfig = Field(default_factory=HealthChecksConfig)


class AlertTriggersConfig(_Section):
    broker_disconnect: bool = True
    market_data_disconnect: bool = True
    stale_market_data: bool = True
    risk_limit_breach: bool = True
    order_rejection: bool = True
    reconciliation_failure: bool = True
    flatten_failure: bool = True
    system_error: bool = True


class AlertsConfig(_Section):
    enabled: bool = True
    on: AlertTriggersConfig = Field(default_factory=AlertTriggersConfig)


class StorageConfig(_Section):
    enabled: bool = True
    url: str = ""
    sqlite_path: str = "data/trading_agent.sqlite3"
    echo: bool = False

class SoakConfig(_Section):
    """Long-duration live-bar soak verification (never auto-enabled on startup)."""

    enabled: bool = False
    duration_minutes: float = 60.0
    expected_bar_interval_seconds: float = 60.0
    maximum_missing_bars: int = 3
    required_live_timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m"])
    fail_on_unexpected_state: bool = True
    # D.1.3 soak policy (driven by scripts/soak_alpaca.py when provided).
    duration_seconds: float | None = None
    minimum_live_bars: int = 1
    minimum_live_candles: int = 1
    maximum_stale_duration_seconds: float = 120.0
    maximum_recovery_time_seconds: float = 120.0
    maximum_reconnects: int = 5
    failure_policy: str = "fail_closed"


class OutageConfig(_Section):
    """Deterministic fault injection used by the outage/recovery harness."""

    enabled: bool = False
    scenario: Literal[
        "none", "missing_bars", "delayed_bars", "duplicate_bars",
        "out_of_order_bars", "disconnect", "stale_data",
        "history_fetch_failure", "incomplete_recovery", "recovery_timeout",
        "drop_trades", "drop_quotes", "future_timestamp",
    ] = "none"
    missing_bars: int = 10
    delay_bars: int = 3
    duplicate_bars: int = 2


class RecoveryConfig(_Section):
    """Recovery behaviour after market-data failure (D.1.3)."""

    enabled: bool = True
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 60.0
    backoff_multiplier: float = 2.0
    recovery_timeout_seconds: float = 30.0
    require_historical_resync: bool = True
    require_bar_resync: bool = True
    require_feature_rebuild: bool = True
    require_regime_rebuild: bool = True
    require_readiness_revalidation: bool = True


class FaultInjectionConfig(_Section):
    """Deterministic local fault injection (D.1.3). Never auto-enabled."""

    enabled: bool = False
    scenarios: list[str] = Field(default_factory=list)
    duration_seconds: float = 60.0
    disconnect_after_seconds: float = 10.0
    drop_bars: bool = False
    drop_trades: bool = False
    drop_quotes: bool = False
    duplicate_messages: bool = False
    out_of_order_messages: bool = False
    timestamp_anomaly: bool = False
    gap_candles: int = 3


class TestingConfig(_Section):
    soak: SoakConfig = Field(default_factory=SoakConfig)
    outage: OutageConfig = Field(default_factory=OutageConfig)


class PaperSafetyConfig(_Section):
    """Independent paper-experiment safety cap (GAP-1).

    A rejection-only gate enforced downstream of strategy, risk, and sizing
    at the single order-submission funnel. It can only ever RESTRICT order
    flow: there is no code path by which it increases, scales, or authorizes
    any quantity, nor does it gate live trading (paper mode is required and
    re-verified on every check). ``enabled=False`` (default) leaves order
    flow exactly as before; the configured values remain visible and hashed.
    """

    enabled: bool = False
    max_qty_per_order: float = 0.001
    max_open_qty: float = 0.001
    max_orders_per_session: int = 3
    max_notional_exposure: float = 150.0


class SentimentConfig(_Section):
    """Observation-only sentiment layer (Phase E).

    Enriches FeatureSnapshot for display/persistence. NEVER gates entries,
    adjusts confidence, or selects strategies in this phase (explicitly
    deferred to Phases F/G). Default OFF.
    """

    enabled: bool = False
    provider: Literal["fear_greed", "cryptopanic", "funding", "mock", "none"] = "none"
    refresh_interval_seconds: float = 300.0
    maximum_age_seconds: float = 900.0
    stale_action: Literal["mark_stale", "drop"] = "mark_stale"
    http_timeout_seconds: float = 10.0
    mock_seed: int = 7
    # Forward-compatible multi-symbol placeholder (Phase L). No code reads it
    # yet; it exists so the config file never needs a breaking migration.
    per_symbol_overrides: dict[str, str] = Field(default_factory=dict)



class AppConfig(_Section):
    """Root configuration object (everything from configs/config.yaml)."""

    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    trading: TradingConfig
    session: SessionConfig = Field(default_factory=SessionConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    timeframes: TimeframesConfig = Field(default_factory=TimeframesConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    position_sizing: PositionSizingConfig = Field(default_factory=PositionSizingConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    paper_safety: PaperSafetyConfig = Field(default_factory=PaperSafetyConfig)
    session_closeout: SessionCloseoutConfig = Field(default_factory=SessionCloseoutConfig)
    backtesting: BacktestingConfig = Field(default_factory=BacktestingConfig)
    optimization: OptimizationConfig = Field(default_factory=OptimizationConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
    trade_plan: TradePlanConfig = Field(default_factory=TradePlanConfig)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    fault_injection: FaultInjectionConfig = Field(default_factory=FaultInjectionConfig)

    @model_validator(mode="after")
    def _session_ordering(self) -> "AppConfig":
        """Validate that the session timeline is internally consistent."""
        if not self.session.enabled:
            return self
        start = self.session.start_time
        cutoff = self.session.entry_cutoff_time
        deadline = self.session.flatten_deadline_time
        end = self.session.end_time
        if not (start <= cutoff <= end):
            raise ValueError("session.entry_cutoff must lie between session.start and session.end")
        if not (cutoff <= deadline <= end):
            raise ValueError("session.flatten_deadline must lie between entry_cutoff and end")
        legacy_risk = self.position_sizing.risk_per_trade_percent
        if legacy_risk is not None and legacy_risk != self.risk.risk_per_trade_percent:
            raise ValueError(
                "position_sizing.risk_per_trade_percent is deprecated and must match "
                "risk.risk_per_trade_percent when supplied"
            )
        # D.1.4: session.max_holding_minutes (engine exit trigger) and
        # risk.maximum_holding_minutes (risk-gate blocker) must not silently
        # disagree — a mismatch means the engine and the risk gate enforce
        # different holding limits. TradePlan carries the same policy to the
        # plan lifecycle.
        holding = {
            self.session.max_holding_minutes,
            self.risk.maximum_holding_minutes,
        }
        if self.trade_plan.maximum_holding_minutes is not None:
            holding.add(self.trade_plan.maximum_holding_minutes)
        if len(holding) > 1:
            raise ValueError(
                "session.max_holding_minutes, risk.maximum_holding_minutes and "
                "trade_plan.maximum_holding_minutes must be equal"
            )
        return self


def _parse_hhmm(value: str) -> time:
    """Parse a ``HH:MM`` string into a :class:`datetime.time`."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time '{value}', expected HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid time '{value}'") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time '{value}'")
    return time(hour=hour, minute=minute)




