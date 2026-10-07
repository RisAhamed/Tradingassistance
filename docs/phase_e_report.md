# Phase E Report — Observation-Only Sentiment + Microstructure Layer

**Date:** 2026-10-07 · **Status:** COMPLETE — awaiting approval (STOP after this report; Phase F NOT started)

## 1. Provider implemented + official docs URL

- **Implemented:** `FearGreedProvider` (`app/sentiment/providers/fear_greed.py`) — **Alternative.me Fear & Greed Index** (free, no key required).
- **Official docs URL (cited in module docstring):** https://api.alternative.me/doc/ (endpoint `https://api.alternative.me/fng/`).
- **Why this one:** free with no API key (no new secret, no new dependency — `httpx` already present). Registry left clean for `cryptopanic` / `funding`.
- Normalization: index 0-100 maps to `score = (value - 50) / 50`; raw value carried as `fear_greed`; microstructure fields `None` (never fabricated).

## 2. Files created / modified (exact paths)

**Created:** `app/sentiment/__init__.py`, `app/sentiment/errors.py` (`SentimentError`, `UnsupportedSymbolError`), `app/sentiment/models.py` (`SentimentSnapshot`), `app/sentiment/provider.py` (ABC + `ProviderHealth`), `app/sentiment/registry.py` (decorator + dict + `build_sentiment_provider`), `app/sentiment/mock.py` (`MockSentimentProvider`, registered `mock`), `app/sentiment/providers/__init__.py`, `app/sentiment/providers/fear_greed.py` (registered `fear_greed`), `app/sentiment/providers/none.py` (registered `none`), `tests/test_phase_e_sentiment.py`, `tests/test_phase_e_engine.py`, `tests/test_phase_e_surface.py`.

**Modified:** `app/config/models.py` (`SentimentConfig` + `mock_seed`, `TradingConfig.symbols` + validator, `AppConfig.sentiment`), `app/config/settings.py` (`sentiment_api_token` + masking), `app/events/types.py` (3 sentiment events), `app/domain/features.py` (6 optional fields; fresh-only `numeric_values()`), `app/features/engine.py` (`sentiment=` kwarg + staleness), `app/runner/factories.py` (`create_sentiment_provider`), `app/runner/engine.py` (provider build, poll loop, staleness check, `sentiment_payload()`, `payload()["sentiment"]`, persistence, `stop()` cancel), `app/storage/models.py` (`SentimentRecordRow`), `app/storage/repository.py` (`save_sentiment`), `app/api/routes.py` (`GET /api/sentiment`), `app/ai_supervisor/tools.py` (read-only `get_sentiment`), `dashboard/index.html` (Sentiment card `id="sentiment"`), `configs/config.yaml` (`sentiment:` default OFF), `.env.example` (`SENTIMENT_API_TOKEN=`).

**Untouched:** `RiskEngine`, `RiskContext`, `StrategySignal.confidence`, strategies, `execution.enabled=false`, paper-only checks, `AiPermissionsConfig`. No new dependency.

## 3. Injection seam (why)

New optional `sentiment=` kwarg on `FeatureEngine.compute()`, fed from `TradingEngine._features_for()` via `_fresh_sentiment_for_features()`. The existing `snapshot` arg is the market-data `MarketSnapshot` — overloading it would conflate domains. Dedicated kwarg, default `None`: all existing call sites behavior-identical. Staleness enforced inside `FeatureEngine` against `sentiment.maximum_age_seconds`.

## 4. Staleness rule (chosen + enforcement)

**`mark_stale` (default):** snapshot older than `maximum_age_seconds` (900 s) sets `sentiment_stale=True`, keeps `sentiment_age_seconds`, leaves values `None`, **excluded from `numeric_values()`** — visible metadata, never numeric. `drop` mode withholds the snapshot entirely. `SENTIMENT_STALE` emitted edge-triggered on the tick path. `ready`/`missing` untouched — sentiment never blocks evaluation. Provider failure: WARNING + `SENTIMENT_UNAVAILABLE`, loop continues, zero orders.

## 5. pytest summary (green)

- New Phase E tests: **14/14 pass** (sentiment 7, engine 4, surface 3).
- Full suite: every file passes in shards (single `pytest -q` exceeds the 30 s tool window; run per-group, all green, no existing test modified). All 519 pre-existing tests still pass + 14 new.

## 6. Manual verification output

Engine with `sentiment.provider=mock` (single poll):

```text
execution.enabled = False
events: ['SentimentUpdated']
sentiment.payload: {score: -0.195, fear_greed: 44.12, funding_rate: -0.0003329,
  open_interest_change_24h: 0.0385, news_volume: 9, source: mock,
  symbol: BTC/USD, stale: false, provider: mock, enabled: true,
  health: {connected: true, detail: "mock sentiment"}}
orders = 0
```

Confirmed: `execution.enabled == False` and `orders == 0` throughout.

## 7. Known limitations (deferred)

1. `cryptopanic`/`funding` not implemented (registry + factory ready; unknown names fail clearly).
2. `sentiment.per_symbol_overrides` is a schema placeholder (Phase L).
3. Engine uses `trading.symbol` only; `trading.symbols` list exists for Phase L.
4. `fear_greed` is market-wide, annotated with the requested symbol (docstring); microstructure fields `None` from this provider.
5. No gating/confidence/selection role (Phases F/G).
6. Persistence best-effort (counted in `Repository.failures`, never fatal).

## 8. Confirm: no trading behaviour changed

**CONFIRMED.** Metadata-only: `ready`/`missing` unchanged, no `RiskEngine`/`RiskContext` change, no confidence change, no strategy change, `execution.enabled` still `false`, paper-only checks untouched, failures yield zero orders. `sentiment.enabled` defaults `false`: default startup behavior-identical.

