"""Backtesting entry point:  ``python -m app.backtesting.run``."""
from __future__ import annotations

import argparse
import json

from app.backtesting.data import generate_history, load_csv
from app.backtesting.engine import BacktestEngine
from app.config.loader import get_env, load_config
from app.core.logging import configure_logging
from app.config.loader import PROJECT_ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a backtest")
    parser.add_argument("--csv", type=str, default=None, help="CSV with timestamp,open,high,low,close,volume")
    parser.add_argument("--length", type=int, default=600, help="synthetic candles to generate")
    parser.add_argument("--seed", type=int, default=7, help="synthetic data seed")
    parser.add_argument("--symbol", type=str, default=None, help="override symbol")
    parser.add_argument("--pretty", action="store_true", help="pretty-print the result")
    args = parser.parse_args()

    env = get_env()
    config = load_config(env=env)
    configure_logging(config, env, project_root=PROJECT_ROOT)
    if args.symbol:
        config.trading.symbol = args.symbol

    timeframe = config.timeframes.signal
    if args.csv:
        candles = load_csv(args.csv, symbol=config.trading.symbol, timeframe=timeframe)
        source = args.csv
    else:
        candles = generate_history(config.trading.symbol, timeframe=timeframe, length=args.length, seed=args.seed)
        source = f"synthetic(seed={args.seed}, length={args.length})"

    result = BacktestEngine(config).run(candles)
    payload = {"source": source, "symbol": config.trading.symbol, "timeframe": timeframe, **result.to_dict()}
    print(json.dumps(payload, indent=2 if args.pretty else None, default=str))


if __name__ == "__main__":
    main()
