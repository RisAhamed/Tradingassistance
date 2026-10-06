"""Session-VWAP-reversion strategy (experimental candidate H1, v0.1).

LONG ONLY: buys dips stretched below the TRUE session-anchored (UTC date
cumulative) VWAP in low-volatility regimes, requiring range containment and
intrabar stabilization. Differs from vwap_reversion v0.1 in the anchor's
semantics (session-cumulative vs trailing-20), the removal of the volume
participation gate (exhaustion means fading volume), and the narrowed
low_volatility-only regime gate.

This strategy makes NO profitability claims. Thresholds reuse the frozen
``strategy.mean_reversion`` section and must not be tuned post-validation.
"""
from __future__ import annotations

from app.config.models import StrategyConfig
from app.core.ids import new_signal_id
from app.domain.enums import Direction, ReasonCode, Regime
from app.domain.signals import StrategySignal
from app.strategies.base import Strategy, StrategyContext


class SessionVwapReversionStrategy(Strategy):
    """Deterministic session-VWAP dip buyer (long only)."""

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

        # H1 regime gate: low_volatility ONLY. Unknown, trending, and
        # high-volatility regimes are refused (frozen classifier).
        if context.regime.regime is not Regime.LOW_VOLATILITY:
            return None

        close = features.close
        session_vwap = features.session_vwap
        atr = features.atr
        rsi = features.rsi
        range_low = features.range_low
        high = features.high
        low = features.low
        if (
            close is None
            or session_vwap is None
            or atr is None
            or rsi is None
            or range_low is None
            or high is None
            or low is None
        ):
            return None
        if not (atr > 0):
            return None
        if not (high > low):
            # Degenerate bar: recovery ratio undefined — no signal.
            return None

        cfg = self.config.mean_reversion
        stretch = (session_vwap - close) / atr
        if stretch < cfg.stretch_atr_min:
            return None
        if rsi > cfg.rsi_max_long:
            return None
        if close < range_low:
            # Breakdown, not a dip — refuse.
            return None
        recovery = (close - low) / (high - low)
        if recovery < cfg.recovery_min:
            return None

        if not self._spread_ok(features.spread_percent):
            return None

        reasons = [
            ReasonCode.REVERSION_SETUP,
            ReasonCode.VWAP_STRETCH,
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
