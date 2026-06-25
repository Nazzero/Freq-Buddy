#!/usr/bin/env python3
"""Generate per-pair indicator CSVs the Chart Sidekick plots.

Reads the project config + a strategy, populates indicators (with informative
timeframe merges via a real DataProvider), and writes one CSV per pair to
<out-root>/<Strategy>/<PAIR>_indicators.csv.

Runs on the host venv (the chartsidekick server drives it via subprocess):
  .venv/bin/python chartsidekick/dump_indicators.py --strategy Vaultwave

Progress: prints one "[i/N] PAIR ..." line per pair plus a final "done" so the
server can stream a progress bar.
"""
import argparse
import logging
import sys
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

# Default config + output root resolve to this project unless overridden.
PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT / "user_data" / "config.json"
DEFAULT_OUT_ROOT = PROJECT / "user_data" / "indicator_dumps"


def _csv_to_ccxt(csv_pair: str) -> str:
    """BTC_USDT_USDT -> BTC/USDT:USDT (reverse of SIDEKICK_PAIRS)."""
    for ccxt, csv in SIDEKICK_PAIRS.items():
        if csv == csv_pair:
            return ccxt
    parts = csv_pair.split("_")
    if len(parts) == 3:
        return f"{parts[0]}/{parts[1]}:{parts[2]}"
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}"
    return csv_pair


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--strategy", default="Vaultwave")
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT),
                    help="root dir for indicator_dumps (default: project user_data/indicator_dumps)")
    ap.add_argument("--pairs", default=None,
                    help="comma-separated subset of pairs to dump (ccxt BTC/USDT:USDT or csv BTC_USDT_USDT). "
                         "Default: all sidekick pairs.")
    ap.add_argument("--timerange", default=None,
                    help="optional freqtrade timerange, e.g. 20240101-20250501")
    args = ap.parse_args()

    # Resolve the pair set (subset support). Accept both ccxt and csv forms.
    if args.pairs:
        want = {p.strip() for p in args.pairs.split(",") if p.strip()}
        ccxt_want = set()
        for p in want:
            ccxt_want.add(_csv_to_ccxt(p) if "/" not in p else p)
        pair_map = {c: v for c, v in SIDEKICK_PAIRS.items() if c in ccxt_want}
        # allow pairs not in the canonical map (any downloaded pair)
        for c in ccxt_want:
            if c not in pair_map:
                csv_pair = c.replace("/", "_").replace(":", "_")
                pair_map[c] = csv_pair
    else:
        pair_map = dict(SIDEKICK_PAIRS)

    if not pair_map:
        print("[err] no valid pairs to dump", flush=True)
        sys.exit(2)

    overrides = {
        "strategy": args.strategy,
        "runmode": RunMode.BACKTEST,
    }
    if args.timerange:
        overrides["timerange"] = args.timerange

    config = Configuration.from_files([args.config])
    config.update(overrides)
    config["pairs"] = list(pair_map.keys())
    config["exchange"]["pair_whitelist"] = list(pair_map.keys())

    bt = Backtesting(config)
    data, _timerange = bt.load_bt_data()
    strategy = bt.strategylist[0]
    bt._set_strategy(strategy)

    out_dir = Path(args.out_root) / args.strategy
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(pair_map)
    print(f"start strategy={args.strategy} pairs={total}", flush=True)
    done = 0
    for i, (pair, csv_pair) in enumerate(pair_map.items(), 1):
        df = data.get(pair)
        if df is None or df.empty:
            print(f"[{i}/{total}] skip {csv_pair}: no base-timeframe data", flush=True)
            continue
        out = strategy.advise_indicators(df.copy(), {"pair": pair})
        out_path = out_dir / f"{csv_pair}_indicators.csv"
        out.to_csv(out_path, index=False)
        done += 1
        print(f"[{i}/{total}] ok {csv_pair}: {len(out)} rows, {len(out.columns)} cols", flush=True)

    print(f"done dumped={done}/{total}", flush=True)


if __name__ == "__main__":
    main()
