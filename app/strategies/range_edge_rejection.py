"""Range-edge-rejection strategy (experimental candidate H2, v0.1).

LONG ONLY: buys failed downside breakouts — price prints at or below the
established 20-bar range low, then closes back inside the range with
intrabar recovery, in low-volatility regimes. The edge, if any, is
positional (trapped breakdown sellers must cover), not predictive.

Thresholds reuse the frozen ``strategy.mean_reversion`` section
(recovery_min, rsi_max_long); no H2-specific parameters exist. This
strategy makes NO profitability claims.
"""
from __future__ import annotations

from app.config.models import StrategyConfig
from app.core.ids import new_signal_id
from app.domain.enums import Direction, ReasonCode, Regime
from app.domain.signals import StrategySignal
from app.strategies.base import Strategy, StrategyContext


class RangeEdgeRejectionStrategy(Strategy):
    """Deterministic failed-breakdown buyer (long only)."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.name = config.name

    def evaluate(self, context: StrategyContext) -> StrategySignal | None:
        if context.position_open:
            # One position at a time is enforced by risk; do not stack signals.
            return None

        features = context.features
        if not features.ready:
            return None

        # H2 regime gate: low_volatility ONLY (approved design §8).
        if context.regime.regime is not Regime.LOW_VOLATILITY:
            return None

        close = features.close
        range_low = features.range_low
        high = features.high
        low = features.low
        rsi = features.rsi
        atr = features.atr
        if (
            close is None
            or range_low is None
            or high is None
            or low is None
            or rsi is None
            or atr is None
        ):
            return None
        if not (atr > 0):
            return None
        if not (high > low):
            # Degenerate bar: recovery ratio undefined — no signal.
            return None

        # Boundary test: the bar printed at or through the established low.
        # Zero additional tolerance: bar extremes are exact exchange prints.
        if not (low <= range_low):
            return None
        # Close back inside: breakdown failed on the SAME bar.
        if not (close >= range_low):
            return None

        cfg = self.config.mean_reversion
        recovery = (close - low) / (high - low)
        if recovery < cfg.recovery_min:
            return None
        if rsi > cfg.rsi_max_long:
            return None

        if not self._spread_ok(features.spread_percent):
            return None

        reasons = [
            ReasonCode.EDGE_REJECTION,
            ReasonCode.RANGE_EDGE_TOUCH,
            ReasonCode.RANGE_CONTAINED,
        ]
        if features.spread_percent is not None:
            reasons.append(ReasonCode.SPREAD_ACCEPTABLE)
        return self._signal(context, reasons, close)

    # -- helpers ------------------------------------------------------------

    def _spread_ok(self, spread_percent: float | None) -> bool:
        # Same convention as the other strategies: an unmeasurable spread
        # (e.g. backtest, which has no quotes) is "not measurable", never
        # fabricated into a passing value.
        if spread_percent is None:
            return True
        return spread_percent <= self.config.spread.maximum_percent

    def _signal(
        self,
        context: StrategyContext,
        reasons: list[ReasonCode],
        entry_reference: float,
    ) -> StrategySignal:
        return StrategySignal(
            signal_id=new_signal_id(),
            timestamp=context.now,
            symbol=context.symbol,
            strategy=self.name,
            direction=Direction.LONG,
            reason_code=reasons[0],
            reason=" AND ".join(code.value for code in reasons),
            entry_reference=entry_reference,
            timeframe=context.timeframe,
            regime=context.regime.regime,
            features=context.features.numeric_values(),
            correlation_id=context.correlation_id,
            session_id=context.session_id,
        )
