# Trading Agent — systematic PAPER-TRADING system (Alpaca paper + Ollama supervision)

> **PAPER_TRADING_ONLY.** No live-trading switch exists.
> The engine refuses to start against live endpoints.

## What this is

A modular, configuration-driven paper-trading app for `BTC/USD` (crypto,
Alpaca paper broker) running a deterministic pipeline:

```text
MARKET DATA -> NORMALIZATION -> FEATURES -> REGIME -> STRATEGY -> SIGNAL ->
RISK -> POSITION SIZE -> OMS -> BROKER -> FILL -> POSITION -> P&L
```

* No ML price-prediction models. Intelligence = deterministic algorithms plus
  an event-driven AI supervisor (Ollama) with permission-checked tools.
* Session model enforces `POSITION = 0` at session end (cutoff -> flatten ->
  verify -> retry -> critical alert).
* Every important action is structured-logged and persisted (SQLite) for the
  Signal -> RiskDecision -> Order -> Fill -> Position -> Trade -> P&L chain.

## Quickstart (Windows PowerShell)

```powershell
.\scripts\install_deps.bat
Copy-Item .env.example .env   # fill in secrets; NEVER commit .env
.\.venv\Scripts\python.exe scripts\demo_mock.py
.\.venv\Scripts\python.exe -m app.backtesting.run --length 600 --pretty

## Verified runs (deterministic, offline)

* backtest (synthetic seed=7, 600x 5m): 26 entries, net +1895.11 (+1.90%),
  21W/5L, profit_factor 6.73 — proves the pipeline executes, not profitability.
* demo_mock (600 mock ticks): 5 signals, 2 orders, 1 trade, flatten verified.
* pytest: risk-exposure regression, engine pipeline, API surface — all pass.

## Layout

`app/api` REST+SSE, `app/ai_supervisor` Ollama+gateway, `app/backtesting`
shared-code engine, `app/brokers` Alpaca paper + mock, `app/config` YAML+env,
`app/runner` TradingEngine, `app/sessions` flat-at-end manager,
`dashboard/index.html` live dashboard, `scripts/demo_mock.py` offline demo.

## Safety

Paper-only enforcement at load; startup checklist gates trading; risk is
fail-closed and authoritative; ambiguous submissions trigger reconciliation
(never blind retry); restart reconciles broker vs internal state; human
controls share the AI's permission-checked command layer.

.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m app
# open http://127.0.0.1:8000/dashboard
```

Headless paper runner: `.\.venv\Scripts\python.exe -m app.runner.paper --ticks 600`

## Configuration

* Behaviour: `configs/config.yaml` (single source of truth).
* Secrets only: `.env` (`ALPACA_*`, `OLLAMA_*`, `DATABASE_URL`).
* Key knobs: `trading.mode=paper`, session cutoff/flatten times, risk limits
  (`risk_per_trade`, `max_daily_loss`, `maximum_position_value_percent`),
  sizing (`risk_amount / stop_distance`, capped to max notional), AI
  permissions, timeframes (`15m` context / `5m` signal / `1m` execution).
