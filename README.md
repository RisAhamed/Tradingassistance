# Trading Agent — systematic PAPER-TRADING system

> **PAPER_TRADING_ONLY.** There is deliberately no live-trading switch. The
> configuration model refuses anything but `mode: paper`, and the engine
> refuses to start against a live Alpaca endpoint. This is an
> architecture-validation project, **not** a profitability claim.

A modular, configuration-driven paper-trading agent (default `BTC/USD`, crypto)
built around one deterministic pipeline. Intelligence = deterministic algorithms
plus an optional event-driven AI supervisor (Ollama) that can only act through a
permission-checked tool gateway.

```text
MARKET DATA -> NORMALIZATION -> FEATURES -> REGIME -> STRATEGY -> SIGNAL ->
RISK -> POSITION SIZE -> OMS -> BROKER -> FILL -> POSITION -> P&L
```

- No ML price prediction. Every trading decision is deterministic and explainable.
- The AI can **never** place trades, bypass risk, or call the broker directly.
- The session model enforces `POSITION == 0` at session end
  (`cutoff -> flatten -> verify -> retry -> critical alert`).
- Every important state transition is structured-logged and persisted (SQLite)
  along the full chain: Signal -> RiskDecision -> Order -> Fill -> Position ->
  Trade -> P&L.

## Status

| Area | State |
| --- | --- |
| Phase A baseline hardening | **Complete** (regime/feature/market-data/risk/sizing/order/fill/session/reconcile/AI safety + config source-of-truth) |
| **Phase B: real Alpaca market data** | **Complete — execution disabled by default** |
| **Phase C: historical warm-up** | **Complete — strategy-ready at startup (~3 s) instead of ~5 h** |
| Execution gate | `execution.enabled: false` — fail-closed master kill-switch for orders |
| Alpaca market-data websocket (read-only) | **Verified live** against `wss://stream.data.alpaca.markets` |
| Historical warm-up (1m bars) | **Verified live** — 626 bars → 51×15m / 151×5m / 626×1m candles |
| Features + regime after restart | **Ready** (`features.ready=true`, regime `trending_bullish`) |
| Live handoff | **Verified** — gap 4.02 candles, within tolerance, no duplicates |
| Automated tests | **112 passing** (`pytest tests`) |
| `scripts/demo_mock.py` (offline, deterministic) | **Passing** — signals → orders → trade → verified flat |
| `python -m app.backtesting.run --length 600` (synthetic) | **Passing** — 35 signals, 26 entries |
| FastAPI + SSE + dashboard | **Working**, with a prominent `EXECUTION: DISABLED` banner |
| Order submission | **Blocked** while `execution.enabled: false` |
| Alpaca *trading* client | **Never constructed** while execution is disabled |
| Live execution | **Not implemented / not permitted** |
| AI flatten permission | **Disabled by configuration** (`ai.permissions.allow_flatten: false`) |

> The backtest/demo P&L is generated from synthetic data and only proves the
> pipeline executes end-to-end. It is **not** evidence of strategy edge.

## Quickstart (Windows PowerShell)

```powershell
# 1) Create the virtualenv (Python 3.11+)
py -3.11 -m venv .venv

# 2) Install dependencies (writes logs\pip_install.log)
.\scripts\install_deps.bat
#   ...or equivalently:
#   .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) Create your secrets file (NEVER commit .env)
Copy-Item .env.example .env
#   Fill in ALPACA_* only if you want the Alpaca paper/market-data adapters.

# 4) Run the offline, fully-deterministic demo (no network, no credentials)
.\.venv\Scripts\python.exe scripts\demo_mock.py
```

No API keys are required for the mock demo, the backtest, or the test suite.

## How to run and test

All commands are run from the repository root. On Windows use
`.\.venv\Scripts\python.exe`; on macOS/Linux use `.venv/bin/python`.

### Run the automated tests

```powershell
.\.venv\Scripts\python.exe -m pytest            # or: .\.venv\Scripts\python.exe -m pytest tests -q
```

- Config lives in `pyproject.toml` (`asyncio_mode = "auto"`, `testpaths = ["tests"]`).
- No network/credentials needed — tests use the mock provider/broker and an
  offline AI provider. Expect **112 passed**.

Coverage by file (the safety contract is executable here):

| Test file | Guards |
| --- | --- |
| `test_regime_feature_safety.py` | UNKNOWN regime never claims `bullish_regime`/`bearish_regime`; missing/insufficient/stale features never signal |
| `test_market_data_safety.py` | wrong symbol / invalid / duplicate / out-of-order ticks cannot trade; paired quote+trade accepted |
| `test_risk_exposure.py` | exposure gate defers to sizing; notional cap enforced post-sizing; backtest produces trades |
| `test_sizing_oms_safety.py` | malformed sizing inputs; no NaN/∞/≤0 quantity; duplicate submissions blocked; ambiguous → no blind retry |
| `test_fill_position_safety.py` | position from actual fills; partial fills; duplicate fills never double-count; weighted avg entry |
| `test_session_reconcile_safety.py` | entry cutoff; flatten must verify position==0; failed flatten → `HALTED`; mismatch/unknown blocks entries |
| `test_ai_safety.py` | AI cannot reach broker/order tools; unknown/unauthorized tools rejected; flatten disabled |
| `test_engine_pipeline.py` | mock market data → engine → fill → flat (async end-to-end) |
| `test_api.py` | health + secret-free config handling + dashboard served |
| `test_config_observability.py` | no secrets in `config.yaml`; dashboard panels; startup events |
| `test_phase_b_execution_gate.py` | **execution disabled blocks order creation + submission, never connects the broker, visible on dashboard/API; full pipeline still runs** |
| `test_phase_b_market_data.py` | Alpaca normalization, malformed-message rejection, duplicate/out-of-order ticks, candle boundaries/gaps, feature warm-up, reconnect/reconnect-failure, secret-free logs |
| `test_phase_c_warmup.py` | derived warm-up depth, historical normalization (dup/out-of-order/malformed/empty), warm-up→features→regime, live handoff, gap fail-closed, reconcile-not-retry policy, freshness measurement vs risk policy |

### Offline deterministic demo

```powershell
.\.venv\Scripts\python.exe scripts\demo_mock.py
```

Replays the scripted mock series against the mock broker, prints a JSON summary
(`signals`, `orders`, `trades`, `realized_pnl`, `regime`, `health`, candle
counts) and then performs a verified flatten. Exit code is 0 only if the
position ends flat and health is not `error`.

### Backtest (synthetic, offline)

```powershell
.\.venv\Scripts\python.exe -m app.backtesting.run --length 600 --pretty
```

Runs the *same* features/regime/strategy/risk/sizing code as live trading, but
against generated candles. Flags: `--length` (candles), `--seed`, `--symbol`,
`--pretty`, and `--csv <file>` to replay your own candles
(`timestamp,open,high,low,close,volume`; the timeframe comes from
`timeframes.signal`). **Do not tune the strategy on the synthetic P&L** — it is
an execution smoke test, not an optimization.

### Headless paper runner

This runner uses whatever `market_data.provider` / `trading.broker` are set to
in `configs/config.yaml` (shipped default: `alpaca`, which needs credentials in
`.env`). For an **offline** run, set both to `mock` in `configs/config.yaml`
first:

```powershell
.\.venv\Scripts\python.exe -m app.runner.paper --ticks 600     # mock replay + verified flatten
.\.venv\Scripts\python.exe -m app.runner.paper --duration 60   # run for N wall-clock seconds
```

With the mock provider it replays the scripted series and then flattens and
verifies flatness (exit code 1 if not flat). With a streaming provider it runs
until interrupted (`Ctrl+C`), then flattens if a position is open.

### API server + live dashboard

```powershell
.\.venv\Scripts\python.exe -m app
# then open: http://127.0.0.1:8000/dashboard
```

- Host/port come from `dashboard.host` / `dashboard.port` in `configs/config.yaml`.
- `trading.autostart: true` starts the engine with the web server. Set it to
  `false` (or use the test factory `create_app(autostart=False)`) to serve the
  API without starting the engine.
- Equivalent entry points: `uvicorn app.main:app --reload` or the console script
  `trading-agent` (installed from `pyproject.toml`).

The dashboard (`dashboard/index.html`) shows, live via SSE: system state,
market, features, regime, strategy, risk, position, P&L, orders, session, AI
supervisor status, and a live event stream. It also exposes human controls
(pause / resume / flatten / reconcile) that use the **same command layer as the
AI**.

### Poke the API

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/api/system/status
Invoke-RestMethod http://127.0.0.1:8000/api/regime
Invoke-RestMethod http://127.0.0.1:8000/api/config          # sanitized, no secrets
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/pause
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/flatten
```

Interactive docs: `http://127.0.0.1:8000/docs` (FastAPI/OpenAPI).

## Project structure

```text
tradebot/
|-- app/                          # the application package (all code)
|   |-- __main__.py               # `python -m app` -> API server + dashboard
|   |-- main.py                   # FastAPI factory (create_app) + console entry
|   |-- runtime.py                # build_runtime(): wires engine + AI; startup checklist
|   |-- api/
|   |   `-- routes.py             # REST + SSE endpoints + human controls
|   |-- config/
|   |   |-- models.py             # Pydantic schema of configs/config.yaml (source of truth)
|   |   |-- loader.py             # load_config(), get_env(), sanitized_config()
|   |   `-- settings.py           # EnvSettings: SECRETS from .env / environment
|   |-- core/                     # clock, ids, errors, structured logging
|   |-- domain/                   # pure domain models (market, features, regime,
|   |                             #   signals, orders, positions, risk, pnl, session, enums)
|   |-- market_data/
|   |   |-- base.py               # MarketDataProvider interface (+ dispatcher)
|   |   |-- mock.py               # deterministic synthetic provider
|   |   |-- alpaca_provider.py    # Alpaca (paper) market-data adapter
|   |   |-- aggregator.py         # trades -> OHLCV candles per timeframe
|   |   `-- store.py              # latest snapshot + candle store, data age
|   |-- features/
|   |   |-- indicators.py         # EMA, RSI, ATR, VWAP, rolling high/low, ...
|   |   `-- engine.py             # candles -> FeatureSnapshot
|   |-- regime/engine.py          # FeatureSnapshot -> RegimeSnapshot (UNKNOWN if unsure)
|   |-- strategies/
|   |   |-- base.py               # Strategy + StrategyContext
|   |   `-- breakout_momentum.py  # the only strategy (breakout + momentum)
|   |-- signals/generator.py      # attaches stop/target from RISK config
|   |-- risk/engine.py            # RiskEngine: the mandatory, fail-closed gate
|   |-- portfolio/
|   |   |-- position_sizing.py    # risk_amount / stop_distance, capped to max notional
|   |   |-- position_manager.py   # authoritative position state from FILLS
|   |   `-- pnl_engine.py         # realized/unrealized P&L, equity
|   |-- orders/oms.py             # order state machine, idempotency, fill dedupe
|   |-- execution/executor.py     # submit; ambiguous -> reconcile (never blind retry)
|   |-- brokers/
|   |   |-- base.py               # BrokerAdapter interface + reconcile()
|   |   |-- mock.py               # in-memory simulated broker
|   |   `-- alpaca.py             # Alpaca PAPER broker adapter
|   |-- sessions/manager.py       # session clock, entry cutoff, flat-at-end invariant
|   |-- events/                   # async event bus + EventType taxonomy
|   |-- monitoring/               # health registry + SystemState (dashboard payload)
|   |-- storage/                  # SQLAlchemy models + repository (SQLite/Postgres)
|   |-- ai_supervisor/
|   |   |-- provider.py           # Ollama provider (or UnavailableProvider)
|   |   |-- gateway.py            # permission-checked tool gateway
|   |   |-- tools.py              # read-only + controlled tools bound to the engine
|   |   `-- supervisor.py         # event-driven investigations
|   |-- backtesting/
|   |   |-- data.py               # synthetic candle generation
|   |   |-- engine.py             # runs the SAME pipeline on historical candles
|   |   |-- metrics.py            # P&L / drawdown / Sharpe / profit factor
|   |   `-- run.py                # CLI: python -m app.backtesting.run
|   `-- runner/
|       |-- engine.py             # TradingEngine: orchestrates the pipeline + safety gates
|       |-- factories.py          # builds provider/broker/storage from config
|       `-- paper.py              # headless paper runner
|-- configs/config.yaml           # SINGLE SOURCE OF TRUTH for non-secret behaviour
|-- dashboard/index.html          # live dashboard (SSE)
|-- scripts/
|   |-- demo_mock.py              # offline end-to-end demo
|   |-- acceptance_alpaca.py      # PHASE B real-data acceptance (execution disabled)
|   `-- install_deps.bat          # installs deps into .venv
|-- tests/                        # pytest suite (support.py = shared fixtures)
|-- data/                         # SQLite database (gitignored)
|-- logs/                         # structured logs (gitignored)
|-- pyproject.toml                # deps, console script, pytest config
|-- requirements.txt              # pinned runtime deps for install_deps.bat
`-- .env.example                  # secrets template (copy to .env)
```

## How the pieces connect

`TradingEngine` (`app/runner/engine.py`) is the orchestrator that holds every
component and enforces the safety gates. Data flows in one direction, and side
effects (persistence, monitoring, AI, dashboard) hang off an async event bus so a
failure in one never stops the pipeline.

```text
        provider (mock|alpaca)
              |  Quote/Trade
              v
      on_market_update  --(validate: symbol/price/crossed/out-of-order/duplicate)
              |
              v
        MarketStore (snapshot + data age)   CandleAggregator (per timeframe)
              |                                      |  on candle close
              |                                      v
              |                        FeatureEngine -> FeatureSnapshot (ready?)
              |                                      |
              |                        RegimeEngine -> RegimeSnapshot (UNKNOWN?)
              |                                      |
              |                        StrategyContext -> Strategy.evaluate()
              |                                      |
              |                        SignalGenerator (stop/target from RISK cfg)
              |                                      |
              v                                      v
        RiskEngine (mandatory gate) <----- StrategySignal
              |  approved
              v
        PositionSizer (risk budget / stop, capped to max notional)
              |
              v
        OMS (validate qty, idempotency, dedupe) -> OrderExecutor -> Broker
              |  fill(s)
              v
        PositionManager (AUTHORITATIVE)  ->  PnlEngine  ->  Trade records
              |
              v
        SessionManager (cutoff / closeout / flat-at-end)   ->  repository (SQLite)
```

Cross-cutting connections:

- **Event bus** (`app/events`): every transition emits a typed `EventType`
  (`SYSTEM_READY`, `SIGNAL_GENERATED`, `RISK_APPROVED/REJECTED`, `ORDER_*`,
  `POSITION_UPDATED`, `TRADE_CLOSED`, `FLATTEN_*`, `RECONCILIATION_*`, `AI_*`,
  ...). The dashboard SSE stream, the AI supervisor, and storage subscribe here.
- **SystemState** (`app/monitoring/state.py`): the sanitized snapshot serialized
  by `engine.payload()`; this is exactly what the REST API and dashboard render.
- **Storage** (`app/storage`): auxiliary and best-effort — persistence failures
  never block trading. Enable/disable with `storage.enabled`.
- **AI supervisor** (`app/ai_supervisor`): subscribes to *meaningful* events
  (order rejected, risk rejected, reconciliation failure, system error, flatten
  failure) and can only call the permission-checked tool gateway. It cannot trade.
- **Human controls** (`app/api/routes.py`): pause/resume/flatten/reconcile go
  through the *same* engine command layer the AI uses.
- **Backtesting** reuses the identical `FeatureEngine`/`RegimeEngine`/`Strategy`/
  `RiskEngine`/`PositionSizer` classes — only the data source and execution differ,
  so the offline run exercises the same code paths as paper trading.

### Safety invariants (Phase A + Phase B)

- **Execution gate (Phase B, fail-closed)** — `execution.enabled` defaults to
  `false`. When disabled: market data, features, regime, strategy, signal
  generation, risk evaluation and sizing all still run, but **no order may be
  created or submitted** — the engine stops after sizing and emits
  `EXECUTION_BLOCKED`. The broker is never even connected, so no broker order
  endpoint can be reached, and reconciliation is a documented no-op.

- **Regime safety** — if the regime is `UNKNOWN` (insufficient candles / missing
  indicators), the strategy WAITs and never claims `bullish_regime`,
  `bearish_regime`, or `trend_confirmed`.
- **Feature safety** — missing/stale/insufficient features surface as `None` and
  block evaluation via `FeatureSnapshot.ready`; VWAP-missing is never "above".
- **Market-data safety** — wrong symbol, invalid/crossed prices, duplicate or
  out-of-order ticks are dropped; the engine never invents a replacement price.
  Quotes and trades are tracked as separate streams (a paired quote+trade at the
  same timestamp is valid).
- **Risk authority** — every entry funnels through `RiskEngine`; the strategy,
  signal generator, and AI cannot bypass it. The maximum position value is
  enforced *after sizing* (`quantity * price <= cap`), where quantity is known.
- **Position-sizing safety** — malformed inputs (zero/missing stop, non-positive
  or NaN equity, non-finite cap/price) are rejected; no NaN/∞/≤0 quantity reaches
  the OMS.
- **Order idempotency** — duplicate submissions (same order id or client order id)
  are blocked; an ambiguous submission flags reconciliation instead of blind retry.
- **Fill/position safety** — position state derives from actual fills; partial
  fills accumulate; duplicate fill events never double-count; average entry is a
  weighted mean.
- **Session closeout** — the entry cutoff stops new entries; closeout flattens and
  then **verifies** `position == 0`. A merely *submitted* exit order does not
  count; a failed flatten emits a critical `FLATTEN_FAILED` and sets the session
  to `HALTED`.
- **Reconciliation** — startup/recovery compares broker vs internal state; unknown
  or mismatched broker state blocks new entries (it never assumes flat).
- **AI safety** — the AI has no broker/order tools, cannot call unknown or
  unauthorized tools, and `request_flatten` stays disabled unless explicitly
  permitted (it is not, in this phase).

## Phase B — real Alpaca market data with execution disabled

Phase B streams **real** Alpaca paper-account market data through the whole
pipeline while keeping order submission switched off.

```powershell
# 1) credentials (paper) in .env — ALPACA_API_KEY / ALPACA_API_SECRET
# 2) keep execution OFF (this is the shipped default in configs/config.yaml)
#    execution:
#      enabled: false
# 3) run the acceptance script (read-only, broker forced to mock)
.\.venv\Scripts\python.exe scripts\acceptance_alpaca.py --seconds 420
```

The script is read-only **by construction**: it asserts
`execution.enabled == False`, forces the broker adapter to the in-memory mock
(so no Alpaca *trading* client is ever built), and only opens the market-data
websocket. It prints a pass/fail checklist.

### Manual acceptance procedure (first real-data run)

1. `configs/config.yaml` → `market_data.provider: alpaca`, `execution.enabled: false`.
2. `.env` → paper credentials; confirm `ALPACA_PAPER=true`.
3. `.\.venv\Scripts\python.exe scripts\acceptance_alpaca.py --seconds 600`
4. Verify each acceptance item:

   | # | Criterion | Where to look |
   | --- | --- | --- |
   | 1 | Alpaca connects | `AlpacaConnected` log/event |
   | 2 | BTC/USD data arrives | `AlpacaSubscriptionStarted symbols=BTC/USD` |
   | 3 | Data normalized | dashboard *Market Data* panel: bid/ask/last/spread populated |
   | 4 | Candles form | `CANDLE_COMPLETED` logs; 1m/5m/15m candle counts |
   | 5 | Features warm up | `FEATURES_UPDATED` with `ready`/`missing` |
   | 6 | Regime leaves UNKNOWN | `REGIME_CHANGED` once ≥ `regime.min_candles` context candles close |
   | 7 | Strategy evaluates | `STRATEGY_EVALUATED` / `SIGNAL_GENERATED` |
   | 8 | Risk evaluates | `RISK_EVALUATED` with `approved` + reason |
   | 9 | **No order submitted** | `Orders` panel empty; `EXECUTION_BLOCKED` logged; `GET /api/execution` → disabled |
   | 10 | Dashboard reflects live state | `http://127.0.0.1:8000/dashboard` |

5. Warm-up reality check: the regime needs `regime.min_candles` (default 20)
   **completed** context candles. With `context: 15m` that is up to ~5 hours of
   streaming before the first classification — during this period the regime is
   legitimately `UNKNOWN` and the strategy waits. The acceptance script reports
   `warmup.candles_remaining` so you know how far along you are.

## Phase C — historical warm-up (strategy-ready at startup)

Phase C removes the Phase B warm-up problem: the app no longer needs ~5 hours of
live trading before it can evaluate the strategy.

```text
HISTORICAL 1m BARS -> NORMALIZE -> CANDLE HISTORY (1m/5m/15m)
                   -> FEATURES -> REGIME -> STRATEGY READY -> LIVE STREAM
```

```yaml
# configs/config.yaml
market_data:
  history:
    enabled: true
    provider: alpaca          # none | alpaca
    required: false           # true => a failed warm-up is not ready
    bar_timeframe: "1m"       # canonical startup source
    lookback_bars: null       # null => DERIVED from the pipeline config
    maximum_history_bars: 10000
    startup_timeout_seconds: 30
    max_gap_candles: 5        # history->live gap tolerance (fail-closed)
```

**Required depth is calculated, not hardcoded.** `compute_warmup_requirement()`
inspects the live configuration (EMA periods, RSI/ATR periods, range lookback,
breakout lookback, `regime.min_candles`) and converts it into base-timeframe
bars. With the shipped config that is:

| Driver | Value |
| --- | --- |
| `ema` | 50 (from `features.ema.periods`) |
| `rsi` / `atr` | 15 |
| `range_lookback` / `breakout_lookback` | 21 |
| `regime_min_candles` | 20 |
| **base candles** | **50** (deepest) |
| signal candles (5m) | 250 |
| context candles (15m) | 750 |
| **1-minute bars requested** | **751** |

Historical 1-minute bars are folded through the **existing** `CandleAggregator`
(one print per bar), so `1m`/`5m`/`15m` come from the same aggregation code as
live data — there is no second pipeline.

**Live handoff & gaps.** After warm-up the engine records
`last_historical_at` and seeds the trade watermark, so a replayed live update
cannot duplicate a historical bar. The first accepted live update completes the
handoff (`LIVE_HANDOFF_COMPLETED`) and reports the gap; a gap larger than
`max_gap_candles` sets `data_integrity` unhealthy and blocks entries.

**Freshness vs risk policy.** `market_data.freshness` *measures* freshness
(`threshold_seconds`, used by health/dashboard); `risk.stale_market_data`
decides whether staleness *blocks entries*, and `freshness.stale_action` chooses
`block_entries` (default, fail-closed) or `warn_only`.

**Ambiguous submissions.** `execution.retry.action: reconcile` — the schema only
admits `reconcile`; a blind retry is unrepresentable.

### Verified real-data restart (read-only, execution disabled)

```
WarmupRequested   bars=751 timeframe=1m
WarmupReceived    received=626 first=05:32 last=18:02Z
WarmupCandlesBuilt 15m=51  5m=151  1m=626
WarmupFeaturesReady ready=True
WarmupRegimeReady  regime=trending_bullish
LiveHandoffCompleted last_hist=18:02:00Z first_live=18:06:01Z gap=4.02 candles (within tolerance)
orders = 0   execution = DISABLED
```

Run it yourself:

```powershell
.\.venv\Scripts\python.exe scripts\acceptance_alpaca.py --seconds 240
```

## Phase C1 — canonical live 1-minute bars, coverage & recovery

Strategy candles now come from Alpaca's **live 1-minute bar stream**, not from
individual trade arrivals.

```text
ALPACA 1m BAR -> NORMALIZE -> VALIDATE -> CANDLE STORE -> 5m/15m AGGREGATION
              -> FEATURES -> REGIME -> STRATEGY
```

```yaml
market_data:
  bars:
    enabled: true
    canonical: true        # strategy candles come ONLY from bars
    timeframe: "1m"
    required: true
    max_gap_candles: 3     # tolerated missing bars before an outage
    resync_enabled: true   # event-driven historical resync on a large gap
  freshness:
    threshold_seconds: 30           # PRIMARY: live 1m bar freshness
    quote_threshold_seconds: 30     # quotes (bid/ask/spread)
    trade_threshold_seconds: 300    # trades are sparse on crypto
    stale_action: block_entries
  history:
    coverage:
      minimum_percent: 80.0         # measured real-feed value (83.6%)
      maximum_gap_candles: 5
      on_insufficient: fail         # fail | warn | continue
```

**Real-data mode** is an explicit switch (no Python edits, no secrets):

```powershell
# .env
MARKET_DATA_PROVIDER=alpaca
MARKET_DATA_HISTORY_PROVIDER=alpaca
```

Leaving them unset keeps the safe offline defaults (`history.provider: none`).

- **Freshness is measured per stream** — `bar_freshness`, `quote_freshness`,
  `trade_freshness` are reported separately and never merged into one timestamp.
  Entry blocking still uses the risk threshold (`risk.stale_market_data`) and
  now keys off **bar** freshness when bars are enabled.
- **Coverage is explicit** — `requested / received / coverage% / first / last /
  missing_intervals / largest_gap / gap_count` with a `PASS|WARNING|FAIL` verdict.
  A short series is never silently treated as a complete one.
- **Gap handling** — normal 1-minute progression is not an outage. Beyond
  `max_gap_candles`: `BAR_GAP_DETECTED` -> `DATA_GAP` -> entries blocked ->
  `resync()` -> historical fetch -> candle rebuild -> feature rebuild -> regime
  rebuild -> verify -> readiness restored. A socket reconnect alone never
  restores readiness.
- **Mock parity** — the mock provider emits the same canonical bars, so the demo
  rehearses the production path.

## Configuration — one source of truth

Two layers, deliberately separated:

1. **Behaviour → `configs/config.yaml`** (single source of truth, no secrets).
2. **Secrets → `.env` / environment** (`EnvSettings`, read from `.env`).

Key non-secret knobs in `configs/config.yaml`:

| Section | Highlights |
| --- | --- |
| `trading` | `symbol`, `market`, `broker` (`alpaca\|mock`), `autostart` |
| `timeframes` | `context: 15m`, `signal: 5m`, `execution: 1m` |
| `features` | EMA periods, RSI/ATR periods, VWAP, range lookback |
| `regime` | `ema_distance_atr_min`, volatility thresholds, `min_candles` |
| `strategy` | breakout buffer, RSI bands, EMA pairs, spread limit, cooldown |
| `risk` | `risk_per_trade_percent`, `maximum_daily_loss_percent`, `maximum_open_positions`, `maximum_position_value_percent`, `maximum_orders_per_session`, stop ATR multiplier, RR ratio |
| `position_sizing` | method, risk %, min/max quantity, precision |
| `execution` | **`enabled` (PHASE B master gate, default `false`)**, order type, shorting, retry, duplicate-order protection |
| `session` / `session_closeout` | session times, `entry_cutoff`, `flatten_deadline`, flatten retries |
| `market_data` | provider, feed, reconnect, `max_age_seconds`, and the `mock` block (`tick_seconds`, `seed`) used by the offline demo |
| `ai` | provider, model, endpoint, investigation triggers, **permissions** |
| `logging`, `dashboard`, `monitoring`, `alerts`, `storage` | ops/observability knobs |

Secrets (never in `config.yaml`): `ALPACA_API_KEY`, `ALPACA_API_SECRET`,
`OLLAMA_API_KEY`, `DATABASE_URL`, `QUIVER_API_KEY`. `GET /api/config` returns a
**sanitized** view (secrets masked); logs redact secrets.

> To run fully offline (no Alpaca keys), set in `.env`:
> `ALPACA_*` blank and, for the demo/backtest, override provider/broker to `mock`
> (the demo script already does this programmatically).

## API reference (abridged)

| Method & path | Purpose |
| --- | --- |
| `GET /health`, `/health/ready`, `/health/live` | liveness/readiness + component checks |
| `GET /api/system/status` | mode, symbol, broker/provider, components, uptime |
| `GET /api/market/latest` | latest normalized snapshot |
| `GET /api/features`, `/api/regime`, `/api/strategy/status` | feature/regime/strategy state |
| `GET /api/risk/status`, `/api/orders`, `/api/positions`, `/api/pnl` | risk, orders, position, P&L |
| `GET /api/session`, `/api/events`, `/api/logs`, `/api/ai/status` | session, events, log tail, AI status |
| `GET /api/config` | sanitized configuration |
| `GET /api/execution` | **execution gate status** (`ENABLED`/`DISABLED`, reason) |
| `GET /api/warmup` | warm-up status: required/requested/historical bars, feature & regime readiness, last historical + first live timestamps, handoff, gap, freshness policy |
| `GET /api/market/status` | live feed state: provider, connected, symbol, bid/ask/last, spread, data age/status, current 1m/5m/15m candles |
| `GET /api/stream` | SSE live event stream (used by the dashboard) |
| `POST /api/control/{pause,resume,flatten,reconcile}` | human controls (same layer as AI) |
| `GET /dashboard` | the live dashboard page |

## Persistence & logs

- SQLite (SQLAlchemy) at `data/trading_agent.sqlite3` by default (or set
  `DATABASE_URL` / `storage.url`). Set `storage.enabled: false` to run without it.
- Structured JSON logs at `logs/trading-agent.log` (path in `logging.file.path`).
  `GET /api/logs` returns the tail.

## Troubleshooting

- **No candles / regime UNKNOWN** — the regime needs ~50 candles per timeframe.
  The demo/backtest warm up automatically (mock `tick_seconds`). On real data this
  is a natural warm-up period; entries simply wait.
- **`/health` shows an error** — read `error` in the JSON and `logs/trading-agent.log`.
  Most common causes: missing `ALPACA_*` credentials or a live-endpoint URL (rejected).
- **`paper_trading_only` / startup refused** — set `trading.mode: paper`; the
  config model rejects anything else.
- **Tests fail after editing config** — run `pytest` again; the suite asserts
  `config.yaml` contains no secret-like tokens.

## Limitations / not in scope (this phase)

- **Paper trading only** — no live execution path exists by design.
- **Candles are built from trades, not quotes.** On Alpaca's crypto feed
  `BTC/USD` quotes arrive continuously but **trades are very sparse** (measured:
  ~1 trade per 18 s). Phase C fixes the *warm-up* problem (history supplies the
  candles/features/regime immediately), but the **live** stream still advances
  slowly, so:
  - a fresh `MARKET_DATA_STALE` may still appear between sparse trades
    (fail-closed: entries stay blocked meanwhile);
  - the live handoff gap is typically a few candles (observed 4.02, tolerance 5).
  A natural next step is to subscribe to **1-minute bars** on the live stream too,
  which would make live candles deterministic.
- `market_data.freshness.threshold_seconds` (30 s) is the *measurement*; the
  blocking threshold remains `risk.stale_market_data.maximum_age_seconds` (5 s),
  which is intentionally tight for a sparse feed and needs review before
  execution is ever enabled.
- The AI supervisor is optional and cannot flatten or trade; it is a read-only
  investigation layer unless permissions are explicitly widened (they are not).
- The backtest/demo P&L is synthetic and is **not** evidence of profitability.

---

*Keep this README as the mirror of the source of truth: when you change
`configs/config.yaml`, the engine's behaviour changes accordingly — update the
tables above to match.*



