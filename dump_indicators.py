#!/usr/bin/env python3
"""Generate per-pair indicator CSVs the Chart Sidekick plots.

Reads the project config + a strategy, populates indicators (with informative
timeframe merges via a real DataProvider), and writes one CSV per pair to
user_data/indicator_dumps/<PAIR>_indicators.csv.

Run inside the freqtrade docker container:
  docker compose run --rm --entrypoint python3 freqtrade \
    user_data/../chartsidekick/dump_indicators.py --strategy Vaultwave
"""
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

from freqtrade.configuration import Configuration
from freqtrade.enums import RunMode
from freqtrade.optimize.backtesting import Backtesting

# Pairs the sidekick lists (server.py PAIRS). CSV name uses underscores.
SIDEKICK_PAIRS = {
    "BTC/USDT:USDT": "BTC_USDT_USDT",
    "ETH/USDT:USDT": "ETH_USDT_USDT",
    "SOL/USDT:USDT": "SOL_USDT_USDT",
    "XRP/USDT:USDT": "XRP_USDT_USDT",
    "BNB/USDT:USDT": "BNB_USDT_USDT",
    "DOGE/USDT:USDT": "DOGE_USDT_USDT",
    "LINK/USDT:USDT": "LINK_USDT_USDT",
}

OUT_ROOT = Path("/freqtrade/user_data/indicator_dumps")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/freqtrade/user_data/config.json")
    ap.add_argument("--strategy", default="Vaultwave")
    ap.add_argument("--timerange", default=None,
                    help="optional freqtrade timerange, e.g. 20240101-20250501")
    args = ap.parse_args()

    overrides = {
        "strategy": args.strategy,
        "runmode": RunMode.BACKTEST,
    }
    if args.timerange:
        overrides["timerange"] = args.timerange

    config = Configuration.from_files([args.config])
    config.update(overrides)
    # Restrict to the pairs the sidekick plots that actually have data.
    config["pairs"] = list(SIDEKICK_PAIRS.keys())
    config["exchange"]["pair_whitelist"] = list(SIDEKICK_PAIRS.keys())

    bt = Backtesting(config)
    data, _timerange = bt.load_bt_data()
    # Activate the resolved strategy; this wires the DataProvider in so
    # informative timeframe lookups (1h/2h) inside populate_indicators work.
    strategy = bt.strategylist[0]
    bt._set_strategy(strategy)

    # Per-strategy subdir so the frontend can pick which strategy's
    # indicators to load: indicator_dumps/<Strategy>/<PAIR>_indicators.csv
    out_dir = OUT_ROOT / args.strategy
    out_dir.mkdir(parents=True, exist_ok=True)

    for pair, csv_pair in SIDEKICK_PAIRS.items():
        df = data.get(pair)
        if df is None or df.empty:
            print(f"[skip] {pair}: no base-timeframe data loaded")
            continue
        out = strategy.advise_indicators(df.copy(), {"pair": pair})
        out_path = out_dir / f"{csv_pair}_indicators.csv"
        out.to_csv(out_path, index=False)
        print(f"[ok] {csv_pair}: {len(out)} rows, {len(out.columns)} cols -> {out_path}")

    print("done")


if __name__ == "__main__":
    main()
