# TRADING AGENT — MASTER DEVELOPMENT PROMPT

## 0. ROLE

You are the lead software architect and senior Python engineer responsible for building a systematic, modular, paper-trading application from a completely empty repository.

The application is being built from scratch.

Do NOT reuse, import, copy, or depend on any previous Trading-GOAT repository or implementation.

The goal is to create a clean, production-oriented foundation that can eventually support automated trading, but the current system MUST remain in paper-trading/simulation mode.

The system must prioritize:

1. Correctness
2. Risk controls
3. Observability
4. Deterministic behavior
5. Testability
6. Failure handling
7. Auditability
8. Modular architecture
9. AI supervision
10. Ease of future expansion

Do not optimize for profitability before the system is technically reliable.

---

# 1. CORE PRODUCT OBJECTIVE

Build a systematic trading application that can:

- receive market data
- normalize market data
- calculate deterministic features
- determine market regime
- evaluate a deterministic trading strategy
- generate trade signals
- pass signals through risk management
- calculate position size
- create orders
- execute against a paper-trading broker
- maintain accurate positions
- calculate P&L
- monitor system health
- log every important action
- expose the internal state through a dashboard
- provide an AI supervisory layer
- safely flatten positions at the end of a configured trading session

The system must NOT use machine-learning models trained on historical market data.

Do not build:

- price prediction ML models
- reinforcement learning
- neural-network trading models
- custom predictive ML models

The intelligence layer should use deterministic algorithms plus an AI/agent layer for supervision, investigation, orchestration and controlled tooling.

---

# 2. CURRENT MARKET SCOPE

For V1, use:

Instrument:
BTC/USD

Market:
Crypto

Broker:
Alpaca

Environment:
Alpaca Paper Trading

The architecture MUST NOT hardcode BTC/USD throughout the codebase.

Design interfaces so that later we can support:

- ETH/USD
- additional crypto instruments
- equities
- forex
- other markets
- other brokers/data providers

The trading engine must operate independently of the specific broker.

---

# 3. SESSION MODEL

The application must NOT assume standard stock-market hours.

The session must be configurable.

Configuration should include:

- session_start
- session_end
- timezone
- entry_cutoff
- flatten_deadline
- maximum holding time

Crypto operates 24/7, but our application will define its own trading session.

At the end of the configured session:

1. stop accepting new entries
2. identify open positions
3. generate flatten requests
4. execute exits
5. verify position state
6. retry if necessary
7. raise a critical alert if still non-flat
8. mark the session completed only after verification

The invariant is:

POSITION = 0

at the end of the configured trading session.

Never assume that "order submitted" means "position closed".

Verify actual position state.

---

# 4. IMPORTANT SAFETY RULE

The system is PAPER TRADING ONLY.

Do NOT implement real-money trading.

Do NOT request or require live Alpaca credentials.

Do NOT add a live trading switch.

Use paper endpoints/credentials only.

Create an architecture that could theoretically support a future live adapter, but do not implement live execution.

The application should explicitly identify itself as:

PAPER_TRADING_ONLY

through configuration and dashboard status.

If a live endpoint or live credential is accidentally detected, fail safely and refuse to start the trading engine.

---

# 5. ARCHITECTURE

Use a modular architecture.

Recommended high-level structure:

trading-agent/
│
├── app/
│   ├── api/
│   ├── config/
│   ├── domain/
│   ├── events/
│   ├── market_data/
│   ├── features/
│   ├── regime/
│   ├── strategies/
│   ├── signals/
│   ├── risk/
│   ├── portfolio/
│   ├── orders/
│   ├── execution/
│   ├── brokers/
│   ├── sessions/
│   ├── monitoring/
│   ├── backtesting/
│   ├── ai_supervisor/
│   ├── storage/
│   └── main.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── configs/
│   ├── development.yaml
│   ├── paper.yaml
│   └── strategy.yaml
│
├── scripts/
│
├── dashboard/
│
├── data/
│
├── logs/
│
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md

Do not create unnecessary microservices.

Do not introduce Docker yet.

Do not introduce Kubernetes.

Do not introduce Kafka.

Do not introduce Redis unless a concrete requirement appears later.

Keep V1 simple and modular.

---

# 6. TECHNOLOGY STACK

Use:

Python 3.12

Backend:
FastAPI

Validation:
Pydantic / Pydantic Settings

Broker/data:
alpaca-py

HTTP:
httpx

WebSocket:
native async WebSocket implementation / Alpaca streaming interface

Database:
PostgreSQL architecture-ready

Database layer:
SQLAlchemy

Testing:
pytest

Logging:
Python logging with structured JSON-compatible records where useful

AI:
Ollama local API

AI orchestration:
LangGraph may be introduced only after the deterministic trading core works.

Frontend/dashboard:
Keep it lightweight.

Preferred V1 dashboard:

FastAPI + HTML/CSS/JavaScript

Do NOT introduce React unless there is a strong technical reason.

The dashboard should communicate with the backend through REST and WebSocket/SSE.

---

# 7. CONFIGURATION

Never hardcode secrets.

Use environment variables.

Create:

.env.example

Example configuration:

APP_ENV=development

TRADING_MODE=paper

ALPACA_API_KEY=
ALPACA_API_SECRET=

ALPACA_PAPER=true

ALPACA_DATA_FEED=

OLLAMA_BASE_URL=http://localhost:11434

DATABASE_URL=

TRADING_SYMBOL=BTC/USD

TIMEFRAME_CONTEXT=15m
TIMEFRAME_SIGNAL=5m
TIMEFRAME_EXECUTION=1m

SESSION_TIMEZONE=
SESSION_START=
SESSION_END=
FLATTEN_DEADLINE=

MAX_RISK_PER_TRADE=
MAX_DAILY_LOSS=
MAX_CONCURRENT_POSITIONS=1

Never commit .env.

Add .env to .gitignore.

Validate required configuration at startup.

If configuration is invalid:

- log the reason
- expose the failure in health status
- refuse to start the trading engine

---

# 8. LOGGING — EXTREMELY IMPORTANT

Observability is a first-class requirement.

I want to see the live internal flow from the terminal.

The console should clearly show events such as:

[INFO] SYSTEM STARTING
[INFO] CONFIGURATION LOADED
[INFO] PAPER MODE VERIFIED
[INFO] ALPACA CONNECTION INITIALIZED
[INFO] MARKET DATA STREAM CONNECTING
[INFO] MARKET DATA STREAM CONNECTED
[INFO] SUBSCRIBED TO BTC/USD
[INFO] CANDLE RECEIVED
[INFO] FEATURES UPDATED
[INFO] REGIME UPDATED
[INFO] STRATEGY EVALUATED
[INFO] SIGNAL GENERATED
[INFO] RISK CHECK PASSED
[INFO] POSITION SIZE CALCULATED
[INFO] ORDER CREATED
[INFO] ORDER SUBMITTED
[INFO] ORDER FILLED
[INFO] POSITION UPDATED
[INFO] P&L UPDATED

Errors:

[ERROR] MARKET DATA DISCONNECTED
[ERROR] ORDER REJECTED
[ERROR] INVALID MARKET DATA
[ERROR] RISK CHECK FAILED
[ERROR] RECONCILIATION MISMATCH
[ERROR] FLATTEN FAILED

Critical:

[CRITICAL] POSITION STATE UNKNOWN
[CRITICAL] DAILY LOSS LIMIT BREACHED
[CRITICAL] SESSION FLATTEN FAILED
[CRITICAL] BROKER CONNECTION LOST

Logs must include useful context.

For example:

timestamp
level
component
event
symbol
session_id
order_id
position_id
message
error
latency
metadata

Do not log secrets.

Never log:

- API keys
- API secrets
- passwords
- tokens

---

# 9. LOGGING DESIGN

Create a central logging utility.

Components should use:

logger.info(...)
logger.warning(...)
logger.error(...)
logger.exception(...)
logger.critical(...)

Use structured event names.

Examples:

MARKET_DATA_RECEIVED
FEATURES_UPDATED
REGIME_CHANGED
SIGNAL_CREATED
SIGNAL_REJECTED
RISK_CHECK_STARTED
RISK_CHECK_PASSED
RISK_CHECK_FAILED
ORDER_CREATED
ORDER_SUBMITTED
ORDER_FILLED
ORDER_REJECTED
POSITION_OPENED
POSITION_UPDATED
POSITION_CLOSED
SESSION_STARTED
SESSION_CLOSEOUT_STARTED
FLATTEN_REQUESTED
FLATTEN_COMPLETED
RECONCILIATION_STARTED
RECONCILIATION_PASSED
RECONCILIATION_FAILED
AI_INVESTIGATION_STARTED
AI_TOOL_CALLED
AI_ACTION_REJECTED

---

# 10. LIVE TERMINAL FLOW

The terminal must not become unreadable.

Use human-readable logs.

Optionally support:

--verbose

and:

--quiet

modes.

Default development mode should provide enough information to understand the complete flow.

Example:

------------------------------------------------------------
TRADING AGENT
MODE: PAPER
SYMBOL: BTC/USD
SESSION: ACTIVE
------------------------------------------------------------

[12:01:02] MARKET_DATA
BTC/USD bid=... ask=... spread=...

[12:01:03] FEATURES
EMA20=...
EMA50=...
RSI=...
ATR=...
VWAP=...

[12:01:04] REGIME
TRENDING_BULLISH

[12:01:05] STRATEGY
NO_SIGNAL

[12:01:10] STRATEGY
BREAKOUT_DETECTED

[12:01:10] RISK
PASSED

[12:01:10] ORDER
BUY qty=...

[12:01:11] EXECUTION
FILLED

[12:01:11] POSITION
LONG qty=...

[12:01:11] P&L
UNREALIZED=...

This should make debugging possible without opening the dashboard.

---

# 11. MARKET DATA ENGINE

Build a MarketDataProvider interface.

Example concept:

MarketDataProvider
    connect()
    disconnect()
    subscribe()
    unsubscribe()
    stream()
    health()

Then implement:

AlpacaMarketDataProvider

Responsibilities:

- connect
- authenticate
- subscribe
- receive messages
- validate messages
- normalize messages
- publish internal market-data events
- detect stale data
- reconnect after disconnect
- expose connection health

Do not let Alpaca-specific response structures leak throughout the application.

Convert them into internal domain objects.

---

# 12. NORMALIZED MARKET DATA

Create domain models such as:

Quote
Trade
Candle
MarketSnapshot

Example Candle:

timestamp
symbol
open
high
low
close
volume
timeframe

Example Quote:

timestamp
symbol
bid
ask
bid_size
ask_size

Validate all incoming data.

Reject malformed data.

Log rejected data.

Never feed invalid data into the strategy.

---

# 13. RECONNECTION AND FALLBACKS

The market data system must be resilient.

If WebSocket disconnects:

1. log error
2. mark market data unhealthy
3. stop creating new signals
4. attempt reconnection
5. use exponential backoff
6. restore subscriptions
7. verify data freshness
8. resume only after validation

Never continue trading blindly during a stale-data condition.

Fallback hierarchy:

LIVE STREAM
→ reconnect
→ retry
→ mark unhealthy
→ block new entries

Do NOT invent market prices.

Do NOT use stale data to generate new trades.

---

# 14. FEATURE ENGINE

Build a deterministic feature engine.

Initial features:

- EMA20
- EMA50
- RSI
- ATR
- VWAP
- recent range high
- recent range low
- short-term return
- volatility
- volume ratio where available
- spread
- data age

Feature calculations must be deterministic and testable.

The feature engine should receive normalized market data and produce:

FeatureSnapshot

Do not mix feature calculation with strategy logic.

---

# 15. REGIME ENGINE

Create a deterministic regime classifier.

Initial conceptual regimes:

TRENDING_BULLISH
TRENDING_BEARISH
RANGE_BOUND
HIGH_VOLATILITY
LOW_VOLATILITY
UNKNOWN

If conditions are ambiguous:

UNKNOWN

The strategy must be able to refuse trading when regime is UNKNOWN.

Never force a regime classification.

Log regime transitions.

---

# 16. STRATEGY

Initial strategy:

BREAKOUT + MOMENTUM

This is an experimental strategy.

Do not claim that it is profitable.

The purpose of V1 is to validate the complete trading architecture.

Initial concept:

LONG:

price breaks recent range high
AND
momentum confirmation
AND
trend/regime compatible
AND
price above relevant reference
AND
spread acceptable
AND
risk permits trade

SHORT:

price breaks recent range low
AND
bearish momentum confirmation
AND
regime compatible
AND
spread acceptable
AND
risk permits trade

Keep thresholds configurable.

Do not bury constants inside strategy code.

Create:

Strategy interface

Example:

evaluate(context) -> StrategySignal | None

The strategy must NOT directly place orders.

---

# 17. SIGNAL MODEL

Create a structured signal:

signal_id
timestamp
symbol
strategy
direction
confidence/reason_code
entry_reference
stop_reference
take_profit_reference
timeframe
reason
features_snapshot
regime

Do not allow free-form AI output to directly become a trading signal.

Signals originate from deterministic strategy code.

---

# 18. RISK ENGINE

Risk is a mandatory gate.

Flow:

STRATEGY SIGNAL
        ↓
RISK ENGINE
        ↓
APPROVE / REJECT

Risk checks should include:

- trading session active
- symbol allowed
- market data fresh
- spread acceptable
- position limit
- daily loss limit
- risk-per-trade limit
- maximum exposure
- maximum holding time
- duplicate trade protection
- cooldown if required
- system health
- reconciliation status

If ANY mandatory risk check fails:

REJECT.

The strategy cannot override risk.

The AI cannot override risk.

---

# 19. POSITION SIZING

Calculate size from risk.

Conceptually:

risk_amount / stop_distance

Then apply:

- minimum quantity
- quantity step
- maximum quantity
- maximum exposure
- account constraints

Position sizing must be deterministic.

Log:

account equity
risk budget
stop distance
raw quantity
normalized quantity
final quantity

---

# 20. ORDER MANAGEMENT SYSTEM

Create an OMS.

Responsibilities:

- create order intent
- validate order
- assign order ID
- submit order
- track order state
- handle updates
- handle fills
- handle rejection
- handle cancellation
- prevent duplicate submission
- reconcile order state

Order state machine:

NEW
→ VALIDATED
→ SUBMITTED
→ ACKNOWLEDGED
→ PARTIALLY_FILLED
→ FILLED

Failure branches:

REJECTED
CANCEL_REQUESTED
CANCELLED
EXPIRED

Never assume successful submission means successful execution.

---

# 21. BROKER ABSTRACTION

Create:

BrokerAdapter

Methods such as:

connect()
get_account()
get_positions()
get_orders()
submit_order()
cancel_order()
get_order()
close_position()
health()
reconcile()

Then implement:

AlpacaPaperBroker

The rest of the application should depend on BrokerAdapter, not directly on Alpaca.

---

# 22. POSITION MANAGER

The Position Manager must maintain:

- current position
- average entry
- quantity
- direction
- realized P&L
- unrealized P&L
- stop
- target
- holding duration

Position states:

FLAT
LONG
SHORT

Position changes must generate events.

---

# 23. P&L ENGINE

Track:

realized P&L
unrealized P&L
fees
estimated/slippage where applicable
gross P&L
net P&L
daily P&L

Do not calculate P&L from assumptions if actual fill data is available.

Prefer actual execution/fill information.

---

# 24. SESSION MANAGER

Create a SessionManager.

States:

OFFLINE
STARTING
READY
TRADING
PAUSED
CLOSEOUT
HALTED
RECONCILIATION
COMPLETED

Responsibilities:

- session start
- session end
- entry cutoff
- flatten
- reconciliation
- session summary

At closeout:

1. block new entries
2. inspect positions
3. request flatten
4. wait for fills
5. re-check position
6. retry if appropriate
7. escalate failure
8. verify FLAT
9. mark session complete

---

# 25. FAIL-SAFE BEHAVIOR

Define explicit behavior for failures.

### Market data failure

Block new entries.

### Broker failure

Block new entries.

### Unknown position

Block new entries.

### Reconciliation mismatch

Block new entries.

### Risk engine failure

Fail CLOSED.

Do not trade.

### Strategy exception

Skip signal and log error.

### Database failure

Do not silently continue if persistence is required for safe operation.

### AI failure

Trading engine continues according to deterministic rules.

AI must never be a single point of failure for core trading safety.

---

# 26. BACKTESTING

Create a backtesting engine using the same:

- feature engine
- regime engine
- strategy
- risk engine
- position sizing

The only differences should be:

Historical data source
+
simulated execution

Do not create a completely separate strategy implementation for backtesting.

Backtest metrics:

- net P&L
- return
- trade count
- win rate
- average win
- average loss
- expectancy
- profit factor
- maximum drawdown
- Sharpe-like risk-adjusted metric
- average holding time
- fees
- slippage
- rejected trades
- forced exits

Do not optimize the strategy automatically yet.

---

# 27. PAPER TRADING ENGINE

Paper mode should use:

REAL MARKET DATA
+
PAPER BROKER

not:

fake random market data.

However, also build a deterministic mock data source for unit tests.

This gives us:

Production-like paper testing
+
fast deterministic automated testing

---

# 28. AI SUPERVISOR

Only introduce the AI layer after the deterministic core is functional.

Use Ollama local API/cloud modle  initially., 

Create:

AIProvider

and:

OllamaProvider

The AI must NOT directly execute arbitrary code.

The AI interacts through controlled tools.

Initial read-only tools:

get_system_status()
get_market_state()
get_features()
get_regime()
get_strategy_status()
get_positions()
get_orders()
get_account()
get_daily_pnl()
get_recent_trades()
get_recent_events()
get_risk_status()
get_execution_metrics()

Later controlled tools:

pause_strategy()
resume_strategy()
request_reconciliation()
request_flatten()

Every AI tool call must be logged.

---

# 29. AI PERMISSION MODEL

Classify tools:

READ_ONLY
LOW_RISK_CONTROL
HIGH_RISK_CONTROL

Example:

READ_ONLY:
get_positions()

LOW_RISK:
pause_strategy()

HIGH_RISK:
flatten_position()

The AI must not bypass:

- risk engine
- OMS
- session manager
- permission checks

AI action:

AI
→ Tool Gateway
→ Permission Check
→ Validation
→ Tool
→ Result

Never:

AI
→ Broker API directly

---

# 30. AI FAILURE BEHAVIOR

If Ollama is unavailable:

The trading system MUST continue operating safely without AI.

If AI output is malformed:

Reject it.

If AI requests an unknown tool:

Reject it.

If AI requests an unauthorized action:

Reject it.

If AI is unavailable:

Log:

AI_UNAVAILABLE

The deterministic system continues.

---

# 31. DASHBOARD

Build a local dashboard.

Example:

http://127.0.0.1:8000/dashboard

The dashboard should show:

## SYSTEM

- system status
- trading mode
- current session
- uptime
- broker connection
- market-data connection
- AI connection
- database connection

## MARKET

- symbol
- latest price
- bid
- ask
- spread
- volume
- data timestamp
- data age

## FEATURES

- EMA20
- EMA50
- RSI
- ATR
- VWAP
- range high
- range low
- volatility

## REGIME

Current regime.

Also show recent regime transitions.

## STRATEGY

- strategy name
- last evaluation
- current signal
- signal reason
- signal timestamp
- recent signals

## RISK

- account equity
- risk per trade
- daily loss
- remaining risk budget
- exposure
- risk checks
- rejected signals

## ORDERS

Table:

order_id
timestamp
symbol
side
quantity
type
price
status

## POSITION

- direction
- quantity
- entry
- current price
- unrealized P&L
- realized P&L
- holding time

## TRADES

Recent completed trades.

## SESSION

- start
- end
- time remaining
- closeout state
- flat/not-flat status

## EVENTS

Live event stream.

## LOGS

Recent application logs/errors.

## AI

- Ollama status
- last AI event
- tool calls
- investigations
- rejected AI actions

---

# 32. REAL-TIME DASHBOARD

Do not require manual page refresh.

Use WebSocket or Server-Sent Events.

The dashboard should update when events occur.

Example:

Market update
→ dashboard update

Signal
→ dashboard update

Order
→ dashboard update

Fill
→ dashboard update

Position
→ dashboard update

Risk event
→ dashboard update

Error
→ dashboard update

---

# 33. DASHBOARD DESIGN

Keep the first UI functional rather than visually elaborate.

Use cards:

SYSTEM
MARKET
STRATEGY
RISK
POSITION
ORDERS
P&L
SESSION
AI
EVENTS

Use clear status indicators:

HEALTHY
WARNING
ERROR
HALTED
PAPER

Do not spend time building a beautiful frontend before the backend works.

---

# 34. HEALTH ENDPOINTS

Create:

GET /health

GET /health/ready

GET /health/live

GET /api/system/status

GET /api/market/latest

GET /api/features

GET /api/regime

GET /api/strategy/status

GET /api/risk/status

GET /api/orders

GET /api/positions

GET /api/pnl

GET /api/events

GET /api/logs

GET /api/session

GET /api/ai/status

Use structured JSON responses.

---

# 35. EVENT BUS

Create an internal event system.

Example events:

MarketDataReceived
CandleClosed
FeaturesUpdated
RegimeChanged
SignalGenerated
SignalRejected
RiskApproved
RiskRejected
OrderCreated
OrderSubmitted
OrderFilled
OrderRejected
PositionUpdated
TradeClosed
PnlUpdated
SessionStarted
CloseoutStarted
FlattenCompleted
ReconciliationFailed
AIInvestigationStarted
AIActionRequested
AIActionRejected

Every important state transition should generate an event.

---

# 36. DATABASE / PERSISTENCE

Initially design the storage layer around PostgreSQL.

Tables/entities:

sessions
market_events
signals
risk_decisions
orders
fills
positions
trades
pnl_records
system_events
ai_actions

Do not over-normalize unnecessarily.

Every trade should be traceable:

Signal
→ RiskDecision
→ Order
→ Fill
→ Position
→ Trade
→ P&L

This is mandatory for debugging.

---

# 37. TRACEABILITY

Every important operation should have identifiers.

Use:

session_id
correlation_id
signal_id
order_id
position_id
trade_id
event_id

This allows us to answer:

"Why did this trade happen?"

The system should be able to trace:

MARKET EVENT
→ FEATURES
→ REGIME
→ SIGNAL
→ RISK
→ ORDER
→ FILL
→ POSITION
→ P&L

---

# 38. TESTING REQUIREMENTS

Do not just write code.

For every component:

1. implement
2. write tests
3. run tests
4. fix failures
5. manually verify behavior
6. log verification
7. only then move forward

Tests must include:

Unit tests
Integration tests
Failure tests
State transition tests
API tests
End-to-end tests

Critical safety behavior requires explicit tests.

---

# 39. MANDATORY TEST CASES

Test:

- malformed market data
- missing market data
- stale market data
- WebSocket disconnect
- reconnect
- duplicate signal
- duplicate order
- order rejection
- partial fill
- full fill
- cancellation
- position mismatch
- risk rejection
- daily loss limit
- maximum position limit
- session close
- flatten success
- flatten failure
- AI unavailable
- AI malformed response
- unauthorized AI tool
- database failure
- broker failure

---

# 40. DEVELOPMENT PROCESS

This is extremely important.

DO NOT implement the whole application in one response.

Use incremental development.

For every phase:

PHASE START
→ inspect current repository
→ explain objective
→ implement minimum required code
→ run tests
→ run application
→ verify behavior
→ inspect logs
→ fix problems
→ update README
→ show me what changed
→ STOP

Do not automatically continue to the next phase.

Wait for confirmation before beginning the next major phase.

---

# 41. PHASE ORDER

Use this order:

PHASE 1
Project foundation

PHASE 2
Configuration and environment validation

PHASE 3
Logging and observability

PHASE 4
FastAPI application and health system

PHASE 5
Domain models and schemas

PHASE 6
Market-data abstraction

PHASE 7
Alpaca market-data connection

PHASE 8
Live crypto data stream

PHASE 9
Candle aggregation

PHASE 10
Feature engine

PHASE 11
Regime engine

PHASE 12
Strategy engine

PHASE 13
Risk engine

PHASE 14
Position sizing

PHASE 15
Order management system

PHASE 16
Alpaca paper broker

PHASE 17
Position/P&L engine

PHASE 18
Session manager and flattening

PHASE 19
Persistence

PHASE 20
Backtesting engine

PHASE 21
Paper trading runner

PHASE 22
Monitoring dashboard

PHASE 23
AI provider abstraction

PHASE 24
Ollama integration

PHASE 25
AI supervisor

PHASE 26
AI tool gateway

PHASE 27
Failure/recovery testing

PHASE 28
Full end-to-end validation

Do not skip phases.

---

# 42. PHASE GATES

Before moving to the next phase, verify:

- code runs
- tests pass
- no obvious errors
- logs are readable
- dashboard/API works where applicable
- configuration is correct
- failures are handled
- README is updated

If a phase fails:

STOP.

Fix it before proceeding.

Never build additional layers on top of a broken foundation.

---

# 43. GIT WORKFLOW

After every stable phase:

git status
git diff
git add
git commit

Use meaningful commits.

Examples:

feat: initialize trading application
feat: add configuration system
feat: add structured logging
feat: add market data abstraction
feat: add alpaca crypto stream
feat: add feature engine
feat: add risk engine

Do not make giant commits containing unrelated changes.

---

# 44. ERROR HANDLING

Do not use broad silent exception handling.

Bad:

try:
    ...
except:
    pass

Never do this.

Use:

try:
    ...
except SpecificException as exc:
    logger.exception(...)
    handle_failure(...)

Every recovery path must be explicit.

---

# 45. RETRY POLICY

Only retry operations that are safe to retry.

Use exponential backoff for:

- market-data reconnect
- transient network requests
- temporary broker/API errors

Do not blindly retry order submission.

Order submission requires idempotency/duplicate protection.

Never accidentally create two trades because a network request timed out.

---

# 46. DATA FRESHNESS

Every market-data object should have a timestamp.

Calculate:

data_age

If data_age exceeds configured limits:

MARKET_DATA_STALE

Then:

- block new signals
- block new entries
- log warning
- dashboard warning
- attempt recovery

Do not trade using stale information.

---

# 47. IDEMPOTENCY

Critical operations must be idempotent.

Especially:

order submission
flatten requests
reconciliation
session closeout

The system must protect against:

duplicate events
duplicate callbacks
network retries
process restarts

---

# 48. RESTART RECOVERY

The system should eventually be able to restart safely.

At startup:

1. load configuration
2. verify paper mode
3. connect database
4. connect market data
5. connect broker
6. query broker positions
7. query broker orders
8. compare with internal state
9. reconcile
10. only then allow trading

If reconciliation fails:

HALT NEW ENTRIES.

---

# 49. HUMAN CONTROL

The dashboard should eventually expose:

PAUSE
RESUME
FLATTEN

But these actions must go through the same controlled command layer as AI actions.

Human control has priority.

If the system is paused:

No new entries.

If flatten is requested:

Flatten and verify.

---

# 50. DEVELOPMENT COMMANDS

Create simple commands such as:

python -m app

or:

uvicorn app.main:app --reload

and eventually:

python -m app.runner.paper

and:

python -m app.backtesting.run

Make the developer workflow documented in README.

---

# 51. STARTUP VALIDATION

When the application starts, run a startup checklist:

[1] Configuration
[2] Environment
[3] Paper mode
[4] Alpaca credentials
[5] Market-data connectivity
[6] Broker connectivity
[7] Database connectivity
[8] Ollama connectivity
[9] Session configuration
[10] Risk configuration

Display:

SYSTEM READY

only if all mandatory components are healthy.

If AI is unavailable, that should NOT prevent the deterministic trading engine from running.

If broker or market data is unavailable, trading should NOT start.

---

# 52. SECURITY

Never expose:

- API secrets
- database passwords
- tokens

through:

- logs
- dashboard
- API responses
- exceptions
- Git
- README

Mask sensitive information.

---

# 53. PERFORMANCE

Do not prematurely optimize.

Prioritize correctness.

However:

- avoid blocking the event loop
- use async I/O where appropriate
- do not perform expensive operations on every tick unnecessarily
- separate tick-level data from candle-close strategy evaluation
- avoid calling the LLM on every market event

The LLM should be event-driven and selective.

---

# 54. AI SHOULD NOT RUN ON EVERY TICK

Never do:

market tick
→ LLM
→ decision

Instead:

market events
→ deterministic trading engine

Important exception:
→ AI supervisor

Examples:

ORDER_REJECTED
HIGH_SLIPPAGE
RECONCILIATION_FAILURE
UNEXPECTED_POSITION
SYSTEM_ERROR
REGIME_CHANGE
RISK_WARNING

AI should investigate meaningful events, not every market tick.

---

# 55. STRATEGY EXECUTION FREQUENCY

Use:

15m:
market context

5m:
strategy evaluation

1m:
execution timing

Do not evaluate the LLM at these frequencies.

The deterministic engine handles them.

---

# 56. PAPER-TRADING DASHBOARD PRINCIPLE

The dashboard should make it possible for a developer to answer:

"What is the system doing right now?"

within a few seconds.

It should answer:

- Are we connected?
- What is the market doing?
- What is the current regime?
- What does the strategy see?
- Did it generate a signal?
- Why?
- Did risk approve it?
- What order was created?
- Was it filled?
- What position exists?
- What is P&L?
- What is the session state?
- What errors occurred?
- What did the AI do?

---

# 57. IMPORTANT: EXPLAINABILITY

Every signal must have a reason.

Example:

SIGNAL:

LONG

REASON:

breakout_above_range
AND
bullish_regime
AND
momentum_confirmed
AND
spread_acceptable

Every risk rejection should also have a reason:

REJECTED:

daily_loss_limit
or
spread_too_high
or
stale_data
or
position_limit
or
session_closed

Never produce unexplained BUY/SELL signals.

---

# 58. FIRST IMPLEMENTATION

START NOW WITH PHASE 1 ONLY.

Do not implement market data yet.

Do not implement the strategy yet.

Do not implement Alpaca yet.

Do not implement Ollama yet.

First create:

- project structure
- pyproject.toml
- virtual-environment instructions
- .gitignore
- .env.example
- configuration package
- logging package
- basic FastAPI application
- health endpoint
- application entry point
- test framework
- README

Then run the application.

Verify:

GET /health

returns a structured healthy response.

Verify the terminal displays startup logs.

Write tests.

Run the tests.

Show the result.

STOP.

Wait for approval before PHASE 2.

---

# 59. REQUIRED OUTPUT AFTER EACH PHASE

After completing a phase, report:

## 1. What was implemented

List exact files.

## 2. Architecture change

Explain how the new component fits into the system.

## 3. Tests

Show:

tests passed
tests failed

## 4. Manual verification

Show commands used.

## 5. Runtime logs

Show representative output.

## 6. Current system state

Explain what works now.

## 7. Known limitations

Explicitly list anything unfinished.

## 8. Next phase

Tell me exactly what the next phase will implement.

Then STOP.

---

# 60. IMPORTANT DEVELOPMENT RULE

Do not hide failures.

If something does not work:

say so clearly.

Do not claim:

"working"

unless you actually executed the relevant tests or verification.

If an API behaves differently from the documentation:

inspect the current official documentation and adapt.

If a dependency has changed:

use the currently supported API rather than inventing deprecated code.

---

# 61. OFFICIAL DOCUMENTATION

When implementing external integrations, prefer official documentation.

For Alpaca:

https://docs.alpaca.markets/

For Ollama:

https://docs.ollama.com/

Before writing integration code, verify the current API/SDK behavior.

Do not invent endpoints, parameters, or SDK methods.

---

# 62. FINAL ARCHITECTURAL PRINCIPLE

The final system should follow:

MARKET DATA
    ↓
NORMALIZATION
    ↓
FEATURES
    ↓
REGIME
    ↓
STRATEGY
    ↓
SIGNAL
    ↓
RISK
    ↓
POSITION SIZE
    ↓
OMS
    ↓
BROKER
    ↓
FILL
    ↓
POSITION
    ↓
P&L

And independently:

EVENT
    ↓
AI SUPERVISOR
    ↓
CONTROLLED TOOLS
    ↓
INVESTIGATION / OPERATIONS

The AI must never replace the deterministic safety layer.

The risk engine must always be able to reject a trade.

The OMS must control order lifecycle.

The session manager must enforce the flat-at-session-end requirement.

The system must be observable through:

1. Terminal logs
2. REST API
3. Live dashboard
4. Persistent event history

---

# 63. BEGIN

You are now working inside a completely empty repository.

Start with PHASE 1 only.

Before writing code:

1. inspect the repository
2. confirm it is a clean project
3. briefly describe the Phase 1 implementation plan
4. implement Phase 1
5. run tests
6. start the FastAPI server
7. verify /health
8. verify startup logging
9. report exact files changed
10. report test results
11. report any problems

DO NOT continue to Phase 2 automatically.

STOP after Phase 1 and wait for my instruction.

The goal is not to produce the maximum amount of code.

The goal is to produce a correct foundation and iterate safely.

# CONFIGURATION-FIRST / SINGLE SOURCE OF TRUTH REQUIREMENT

This is a strict architectural requirement.

The application MUST be configuration-driven.

I do NOT want to open individual Python/code files to change trading parameters, strategy thresholds, risk values, model selection, provider selection, market configuration, timeframes, logging behavior, AI behavior, or other operational settings.

All configurable behavior must have a corresponding configuration entry.

The primary source of truth for non-secret application configuration must be:

configs/config.yaml

The actual secrets must remain in environment variables / .env and MUST NOT be stored directly inside config.yaml.

The architecture must separate:

1. CONFIGURATION
2. SECRETS
3. CODE/LOGIC

Code implements behavior.

config.yaml controls behavior.

.env provides secrets.

---

# 1. CONFIGURATION HIERARCHY

Use this precedence:

1. Explicit runtime override, if intentionally supported
2. Environment variable
3. configs/config.yaml
4. Safe application default

However, do NOT create hidden defaults for important trading parameters.

If a critical configuration value is missing, fail validation rather than silently inventing a value.

The final effective configuration should be visible through a sanitized configuration endpoint/log, without exposing secrets.

---

# 2. CENTRAL CONFIGURATION FILE

Create:

configs/config.yaml

This should become the primary operational configuration file.

Organize it into clear sections.

Example structure:

```yaml
application:
  name: trading-agent
  environment: development
  log_level: INFO
  timezone: UTC

trading:
  mode: paper
  enabled: true
  symbol: BTC/USD
  market: crypto
  broker: alpaca

session:
  enabled: true
  timezone: UTC
  start: "00:00"
  end: "23:59"
  entry_cutoff: "23:30"
  flatten_deadline: "23:55"
  max_holding_minutes: 240

market_data:
  provider: alpaca
  feed: crypto
  websocket_enabled: true
  reconnect:
    enabled: true
    max_attempts: 10
    initial_delay_seconds: 1
    max_delay_seconds: 60

timeframes:
  context: 15m
  signal: 5m
  execution: 1m

features:
  ema:
    enabled: true
    periods:
      - 20
      - 50

  rsi:
    enabled: true
    period: 14

  atr:
    enabled: true
    period: 14

  vwap:
    enabled: true

  volatility:
    enabled: true

  volume:
    enabled: true

strategy:
  enabled: true
  name: breakout_momentum
  version: "1.0"

  breakout:
    lookback_periods: 20
    buffer_percent: 0.05

  momentum:
    enabled: true
    rsi_min_long: 55
    rsi_max_long: 75
    rsi_min_short: 25
    rsi_max_short: 45

  trend:
    enabled: true
    fast_ema: 20
    slow_ema: 50

  volatility:
    minimum_atr: 0
    maximum_atr: 0

  spread:
    maximum_percent: 0.10

  cooldown:
    enabled: true
    minutes: 5

risk:
  enabled: true

  risk_per_trade_percent: 0.5
  maximum_daily_loss_percent: 2.0

  maximum_open_positions: 1

  maximum_position_value_percent: 20

  maximum_orders_per_session: 20

  stop_loss:
    enabled: true
    atr_multiplier: 1.5

  take_profit:
    enabled: true
    risk_reward_ratio: 2.0

  maximum_holding_minutes: 240

  stale_market_data:
    maximum_age_seconds: 5

  maximum_spread_percent: 0.10

position_sizing:
  method: risk_based
  risk_per_trade_percent: 0.5
  minimum_quantity: 0
  maximum_quantity: 0
  quantity_precision: 6

execution:
  order_type: market
  allow_short: true

  slippage:
    protection_enabled: true
    maximum_percent: 0.20

  retry:
    enabled: true
    maximum_attempts: 3

  duplicate_order_protection: true

session_closeout:
  enabled: true
  flatten_all_positions: true
  stop_new_entries_before_end_minutes: 30
  retry_flatten: true
  maximum_flatten_attempts: 3
  require_position_zero: true

backtesting:
  enabled: true
  initial_capital: 100000
  commission: 0
  slippage_percent: 0.05

ai:
  enabled: true
  provider: ollama

  model:
    name: ""
    temperature: 0.1
    max_tokens: 2000

  endpoint:
    base_url: http://localhost:11434

  behavior:
    run_on_every_tick: false
    run_on_events_only: true
    investigate_order_rejection: true
    investigate_risk_failure: true
    investigate_reconciliation_failure: true
    investigate_system_error: true
    investigate_unexpected_position: true

  permissions:
    read_only: true
    allow_pause: true
    allow_resume: true
    allow_reconciliation: true
    allow_flatten: false

  limits:
    maximum_tool_calls_per_event: 5
    timeout_seconds: 30

logging:
  level: INFO

  console:
    enabled: true
    format: structured

  file:
    enabled: true
    path: logs/trading-agent.log

  rotation:
    enabled: true
    maximum_size_mb: 50
    backup_count: 5

  events:
    market_data: true
    strategy: true
    signals: true
    risk: true
    orders: true
    execution: true
    positions: true
    pnl: true
    session: true
    ai: true

dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8000

  refresh:
    mode: websocket
    fallback: polling
    polling_interval_seconds: 2

monitoring:
  enabled: true

  health_checks:
    market_data: true
    broker: true
    database: true
    ai: true

alerts:
  enabled: true

  on:
    broker_disconnect: true
    market_data_disconnect: true
    stale_market_data: true
    risk_limit_breach: true
    order_rejection: true
    reconciliation_failure: true
    flatten_failure: true
    system_error: true
```

The exact initial values may be adjusted after validating the implementation and strategy requirements.

Do NOT blindly assume the example numbers above are optimal trading parameters.

The important requirement is that the parameters are configurable rather than hardcoded.

---

# 3. ABSOLUTELY NO HARD-CODED TRADING PARAMETERS

Do NOT write code such as:

```python
if rsi > 55:
```

or:

```python
risk = 0.005
```

or:

```python
lookback = 20
```

or:

```python
atr_multiplier = 1.5
```

or:

```python
max_positions = 1
```

or:

```python
symbol = "BTC/USD"
```

or:

```python
model = "some-model"
```

inside business logic.

Instead:

```python
settings.strategy.momentum.rsi_min_long
```

```python
settings.risk.risk_per_trade_percent
```

```python
settings.strategy.breakout.lookback_periods
```

```python
settings.risk.stop_loss.atr_multiplier
```

```python
settings.risk.maximum_open_positions
```

```python
settings.trading.symbol
```

```python
settings.ai.model.name
```

The same principle applies to every configurable parameter.

---

# 4. STRATEGY MUST BE CONFIGURABLE

The strategy implementation must not hardcode the strategy parameters.

The configuration must control:

- strategy name
- strategy enabled/disabled
- strategy version
- breakout lookback
- breakout buffer
- momentum thresholds
- RSI thresholds
- EMA periods
- volatility filters
- spread filters
- volume filters
- cooldown
- entry conditions
- exit conditions
- stop-loss methodology
- take-profit methodology
- risk/reward ratio

The architecture should allow future strategies such as:

```yaml
strategy:
  name: breakout_momentum
```

or:

```yaml
strategy:
  name: mean_reversion
```

or:

```yaml
strategy:
  name: vwap_reversion
```

or:

```yaml
strategy:
  name: momentum
```

without rewriting the entire trading engine.

The strategy registry/factory should select the configured strategy.

---

# 5. STRATEGY CONDITIONS SHOULD BE CONFIGURABLE

Where practical, conditions should be represented through configuration rather than hardcoded constants.

For example:

```yaml
strategy:
  conditions:
    require_trend_confirmation: true
    require_momentum_confirmation: true
    require_volume_confirmation: false
    require_vwap_confirmation: false
    require_spread_check: true
    require_volatility_check: true
```

This allows us to enable/disable individual filters without editing strategy code.

Do not turn the system into an unnecessarily generic rule-language engine.

Keep the actual strategy implementation readable and strongly typed.

The goal is configurable behavior, not configuration complexity.

---

# 6. MARKET CONFIGURATION

The market must be configurable.

Do not hardcode:

BTC/USD

throughout the application.

Use:

```yaml
trading:
  market: crypto
  symbol: BTC/USD
  broker: alpaca
```

Later we should be able to configure:

```yaml
trading:
  market: crypto
  symbol: ETH/USD
```

without changing Python code.

The same architecture should eventually support:

crypto
equities
forex
ETFs

provided the selected broker/data provider supports the instrument.

---

# 7. BROKER PROVIDER CONFIGURATION

The broker must be configurable.

Example:

```yaml
trading:
  broker: alpaca
```

Market-data provider separately:

```yaml
market_data:
  provider: alpaca
```

Do not assume broker and market-data provider must always be the same.

Create provider factories/interfaces.

Example concept:

```text
BrokerProvider
    ├── AlpacaPaperBroker
    └── FutureBroker

MarketDataProvider
    ├── AlpacaMarketData
    └── FutureMarketDataProvider
```

The configuration determines which implementation is instantiated.

If an unsupported provider is selected:

- configuration validation must fail
- log a clear error
- do not start trading

---

# 8. API KEY CONFIGURATION

Secrets must NEVER be stored directly in config.yaml.

Use environment variables:

```env
ALPACA_API_KEY=
ALPACA_API_SECRET=
```

and reference them from the configuration system.

For example:

```yaml
credentials:
  api_key_env: ALPACA_API_KEY
  api_secret_env: ALPACA_API_SECRET
```

The application should resolve those environment variables at runtime.

The dashboard and logs must never display the actual values.

Display only:

```text
ALPACA_API_KEY: CONFIGURED
ALPACA_API_SECRET: CONFIGURED
```

Never:

```text
ALPACA_API_SECRET=actual-secret
```

---

# 9. AI PROVIDER CONFIGURATION

The AI provider must be completely configurable.

Example:

```yaml
ai:
  enabled: true

  provider: ollama

  model:
    name: <configured-model>
    temperature: 0.1
    max_tokens: 2000

  endpoint:
    base_url: http://localhost:11434
```

Do not hardcode the Ollama model name inside Python.

If we later switch models:

```yaml
ai:
  model:
    name: another-model
```

the application should use the new model without code changes.

---

# 10. AI PROVIDER ABSTRACTION

Create:

```text
AIProvider
```

Implement:

```text
OllamaProvider
```

Later we may add:

```text
OtherProvider
```

The application should depend on:

```text
AIProvider
```

rather than:

```text
OllamaProvider
```

directly.

The provider is selected through config.

---

# 11. AI PROMPT CONFIGURATION

Do not hardcode large AI system prompts inside Python source files.

Create a configurable prompt section.

Preferred structure:

configs/prompts/

Examples:

```text
system_supervisor.txt
market_investigation.txt
risk_investigation.txt
execution_investigation.txt
reconciliation_investigation.txt
```

And configure them through:

```yaml
ai:
  prompts:
    supervisor: configs/prompts/system_supervisor.txt
    market_investigation: configs/prompts/market_investigation.txt
    risk_investigation: configs/prompts/risk_investigation.txt
    execution_investigation: configs/prompts/execution_investigation.txt
```

This allows AI behavior and instructions to evolve without modifying application logic.

The prompt files themselves must NOT contain secrets.

---

# 12. AI BEHAVIOR CONFIGURATION

AI behavior should be configurable.

For example:

```yaml
ai:
  behavior:
    enabled: true
    event_driven: true
    investigate_order_rejection: true
    investigate_market_data_failure: true
    investigate_risk_failure: true
    investigate_reconciliation_failure: true
    investigate_flatten_failure: true
    investigate_unexpected_position: true
```

The AI must NOT run continuously on every market tick.

AI invocation should be controlled through configuration and event triggers.

---

# 13. AI PERMISSION CONFIGURATION

AI permissions must be configurable but default to safe behavior.

Example:

```yaml
ai:
  permissions:
    read_only: true
    allow_pause: true
    allow_resume: true
    allow_reconciliation: true
    allow_flatten: false
```

A dangerous capability must never become available merely because the LLM requests it.

The permission system must exist outside the LLM.

The AI can request an action.

The application decides whether that action is permitted.

---

# 14. RISK CONFIGURATION

ALL risk parameters must be configurable.

This includes:

- risk per trade
- maximum daily loss
- maximum exposure
- maximum position size
- maximum open positions
- maximum orders
- maximum holding time
- stop-loss
- take-profit
- risk/reward
- spread limits
- slippage limits
- stale-data limits
- cooldown
- session restrictions
- flatten requirements

No risk number should be hidden in code.

---

# 15. EXECUTION CONFIGURATION

Order behavior must be configurable.

Example:

```yaml
execution:
  order_type: market
  allow_short: true

  retry:
    enabled: true
    maximum_attempts: 3

  slippage:
    protection_enabled: true
    maximum_percent: 0.20

  duplicate_order_protection: true
```

If we later support:

market
limit
stop
stop-limit

the selected execution behavior should come from configuration.

---

# 16. SESSION CONFIGURATION

Session behavior must be configurable.

Do not hardcode market hours.

Configuration controls:

- timezone
- session start
- session end
- entry cutoff
- flatten deadline
- maximum holding time
- whether overnight positions are permitted

For the current system:

```yaml
session_closeout:
  flatten_all_positions: true
  require_position_zero: true
```

This is a hard safety invariant.

---

# 17. LOGGING CONFIGURATION

Logging behavior must also be configurable.

Configuration should control:

- log level
- console enabled
- file enabled
- file path
- rotation
- event categories
- verbose mode
- structured vs readable output

Do not hardcode:

```python
logging.INFO
```

throughout the application.

Use:

```yaml
logging:
  level: INFO
```

as the source of truth.

---

# 18. DASHBOARD CONFIGURATION

Dashboard settings must be configurable:

```yaml
dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8000

  refresh:
    mode: websocket
    polling_interval_seconds: 2
```

Do not hardcode the port in application logic.

---

# 19. FEATURE CONFIGURATION

Every indicator must be individually configurable.

Example:

```yaml
features:

  ema:
    enabled: true
    periods: [20, 50]

  rsi:
    enabled: true
    period: 14

  atr:
    enabled: true
    period: 14

  vwap:
    enabled: true
```

If an indicator is disabled:

- do not calculate it unnecessarily
- strategy validation should know that it is unavailable
- if the strategy requires it, fail clearly rather than silently producing incorrect signals

---

# 20. CONFIGURATION VALIDATION

Create a dedicated configuration validation layer.

At startup:

```text
Load config.yaml
      ↓
Load environment variables
      ↓
Resolve secrets
      ↓
Validate schema
      ↓
Validate cross-field relationships
      ↓
Validate provider availability
      ↓
Validate safety constraints
      ↓
Start application
```

Examples of invalid configuration:

- risk per trade <= 0
- maximum daily loss <= 0
- session end before session start
- unsupported broker
- unsupported strategy
- missing API key
- AI model missing when AI is enabled
- negative position size
- flatten requirement disabled when the system requires flat sessions

The application must refuse to start when critical configuration is invalid.

---

# 21. CROSS-CONFIGURATION VALIDATION

Validate relationships between settings.

Examples:

If:

```yaml
ai:
  enabled: true
```

then:

```yaml
ai:
  provider: ...
  model:
    name: ...
```

must be valid.

If:

```yaml
session_closeout:
  require_position_zero: true
```

then a valid flatten mechanism must exist.

If:

```yaml
strategy:
  momentum:
    enabled: true
```

then required momentum features must be enabled.

If:

```yaml
execution:
  allow_short: true
```

but the broker/instrument does not support shorting:

fail safely.

---

# 22. CONFIGURATION SNAPSHOT

At startup, log a sanitized configuration summary.

Example:

```text
============================================================
TRADING AGENT CONFIGURATION
============================================================
Environment       : development
Trading Mode      : PAPER
Broker            : ALPACA
Market Data       : ALPACA
Market            : CRYPTO
Symbol            : BTC/USD

Context TF        : 15m
Signal TF         : 5m
Execution TF      : 1m

Strategy          : breakout_momentum
Risk / Trade      : 0.50%
Daily Loss Limit  : 2.00%
Max Positions     : 1

AI Enabled        : YES
AI Provider       : OLLAMA
AI Model          : <configured-model>

Dashboard         : ENABLED
Dashboard Port    : 8000

API Credentials   : CONFIGURED
============================================================
```

Never print secret values.

---

# 23. CONFIGURATION API

Expose a sanitized configuration endpoint:

GET /api/config

It should return only safe configuration.

Example:

```json
{
  "environment": "development",
  "mode": "paper",
  "broker": "alpaca",
  "symbol": "BTC/USD",
  "strategy": "breakout_momentum",
  "risk": {
    "risk_per_trade_percent": 0.5,
    "maximum_daily_loss_percent": 2.0
  },
  "ai": {
    "enabled": true,
    "provider": "ollama",
    "model": "configured-model"
  }
}
```

Never return:

- API keys
- API secrets
- passwords
- tokens
- raw environment variables

---

# 24. CONFIGURATION HOT RELOAD

Do NOT implement unrestricted hot reload initially.

Trading parameters changing while a trade is active can create dangerous inconsistencies.

For V1:

Configuration is loaded at startup.

If config.yaml changes:

require an explicit application restart.

Later we can introduce controlled runtime configuration changes if necessary.

---

# 25. CONFIGURATION VERSIONING

Add:

```yaml
config:
  version: "1.0"
```

Log the configuration version at startup.

This allows future changes to be tracked.

---

# 26. STRATEGY EXPERIMENTS

We should eventually be able to create separate configurations for experiments.

For example:

configs/
    config.yaml
    paper.yaml
    conservative.yaml
    aggressive.yaml
    backtest.yaml

The application should support selecting a configuration profile without modifying Python code.

Do not duplicate the entire configuration unnecessarily.

Use a base configuration with controlled overrides where practical.

---

# 27. NO MAGIC NUMBERS

This is a strict rule.

A "magic number" is any unexplained numeric/string value in business logic.

Avoid:

```python
sleep(5)
```

if it represents a configurable retry delay.

Instead:

```python
sleep(settings.market_data.reconnect.initial_delay_seconds)
```

Avoid:

```python
if spread > 0.001:
```

Use:

```python
settings.risk.maximum_spread_percent
```

Avoid:

```python
if rsi > 55:
```

Use configuration.

If a value truly represents a technical constant that should never change, document it clearly.

But all trading-related values must be configurable.

---

# 28. CONFIGURATION VS CODE RESPONSIBILITY

Do NOT put algorithms into YAML.

Bad:

```yaml
strategy:
  python_expression: "price > ema20 and rsi > 55"
```

Do not build an unsafe arbitrary code execution mechanism.

Instead:

YAML defines parameters and feature switches.

Python defines the algorithm.

Example:

```yaml
strategy:
  breakout:
    lookback_periods: 20
```

Python:

```python
range_high = calculate_range_high(
    candles,
    settings.strategy.breakout.lookback_periods
)
```

This preserves maintainability and safety.

---

# 29. CONFIGURATION CHANGE WORKFLOW

When I want to experiment:

1. modify config.yaml
2. restart application
3. application validates configuration
4. application logs the effective configuration
5. run paper/backtest
6. compare results

I should NOT need to modify Python files for ordinary strategy/risk/model/provider experimentation.

---

# 30. FINAL REQUIREMENT

Before considering the configuration architecture complete, audit the entire repository.

Search for:

- hardcoded symbols
- hardcoded broker names
- hardcoded API URLs
- hardcoded model names
- hardcoded strategy thresholds
- hardcoded risk values
- hardcoded timeframes
- hardcoded session times
- hardcoded retry values
- hardcoded logging settings
- hardcoded dashboard ports
- hardcoded AI prompts
- hardcoded provider selection

Move every legitimate operational/trading configuration item into the configuration system.

Then document:

"Where do I change X?"

The README should contain a configuration reference showing exactly where to change:

- market
- symbol
- broker
- data provider
- strategy
- strategy thresholds
- indicators
- timeframes
- risk
- position sizing
- execution
- session
- flattening
- AI provider
- AI model
- AI prompts
- AI permissions
- logging
- dashboard
- monitoring
- alerts

The final goal is:

CODE = IMPLEMENTATION

CONFIG.YAML = BEHAVIOR / PARAMETERS

.ENV = SECRETS

Do not violate this separation.