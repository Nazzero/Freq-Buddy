"""
Strategy builder + backtest runner for Chart Sidekick.

Mode B: the user composes entry/exit conditions over the REAL indicator columns in
the frontend. We generate a freqtrade strategy that SUBCLASSES IndicatorDump (so it
inherits the exact populate_indicators producing all 159 columns), inject the
user's entry/exit logic, then run `freqtrade backtesting` on it. Results (stats +
trades) are parsed back and returned so the chart can plot the trades.

Conditions are a small safe DSL — NOT arbitrary code — so a malformed UI cannot
inject python. Each condition: {left, op, right} where left/right are a column
name or a number, op in a fixed allowlist.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
STRAT_DIR = PROJECT / "user_data" / "strategies"
RESULT_DIR = PROJECT / "user_data" / "backtest_results"
CONFIG = PROJECT / "user_data" / "config.json"
# Generated strategies live IN the strategies dir (not a subfolder) so their
# `from IndicatorDump import IndicatorDump` sibling import resolves. They are
# name-prefixed Gen_* and pruned automatically.
GEN_DIR = STRAT_DIR
FREQTRADE = str(PROJECT / ".venv" / "bin" / "freqtrade")

# allowed comparison operators -> how they render in pandas
OPS = {
    "<": "{l} < {r}",
    ">": "{l} > {r}",
    "<=": "{l} <= {r}",
    ">=": "{l} >= {r}",
    "==": "{l} == {r}",
    "!=": "{l} != {r}",
    "crosses_above": "(({l}) > ({r})) & (({l}).shift(1) <= ({r}).shift(1))",
    "crosses_below": "(({l}) < ({r})) & (({l}).shift(1) >= ({r}).shift(1))",
}

_jobs: dict[str, dict] = {}        # job_id -> {status, ...}
_jobs_lock = threading.Lock()


def list_strategies() -> list[str]:
    out = []
    for p in sorted(STRAT_DIR.glob("*.py")):
        if p.name in ("__init__.py", "IndicatorDump.py") or p.name.startswith("Gen_"):
            continue
        try:
            txt = p.read_text()
        except Exception:
            continue
        if "IStrategy" in txt and "class " in txt:
            out.append(p.stem)
    return out


def _operand(tok, columns: set[str]) -> str:
    """Render one side of a condition as a safe pandas expression.
    A bare number -> literal. A known column -> dataframe['col']. Anything else is
    rejected (returns None) so nothing arbitrary can slip in."""
    if tok is None or tok == "":
        return None
    s = str(tok).strip()
    # number?
    try:
        float(s)
        return s
    except ValueError:
        pass
    if s in columns:
        return f"dataframe['{s}']"
    return None


def build_condition(cond: dict, columns: set[str]) -> str | None:
    op = cond.get("op")
    if op not in OPS:
        return None
    l = _operand(cond.get("left"), columns)
    r = _operand(cond.get("right"), columns)
    if l is None or r is None:
        return None
    return "(" + OPS[op].format(l=l, r=r) + ")"


def conditions_expr(conds: list[dict], columns: set[str]) -> str:
    parts = [c for c in (build_condition(x, columns) for x in (conds or [])) if c]
    if not parts:
        return ""
    return " & ".join(parts)


def validate_spec(spec: dict, columns: set[str]) -> str | None:
    """Return an error string if the spec is unusable, else None. Catches empty
    or malformed conditions before we spin up a backtest. If an EXISTING strategy
    is named, the strategy file supplies its own logic so no conditions are needed."""
    if (spec.get("strategy") or "").strip():
        return None
    entry = spec.get("entry") or []
    if not entry and not (spec.get("can_short") and spec.get("short_entry")):
        return "no entry conditions"
    for grp_name in ("entry", "exit", "short_entry", "short_exit"):
        for c in (spec.get(grp_name) or []):
            if c.get("op") not in OPS:
                return f"{grp_name}: bad operator '{c.get('op')}'"
            if build_condition(c, columns) is None:
                return (f"{grp_name}: invalid condition "
                        f"{c.get('left')} {c.get('op')} {c.get('right')} "
                        "(unknown column or value)")
    if not conditions_expr(entry, columns) and not (
            spec.get("can_short") and conditions_expr(spec.get("short_entry") or [], columns)):
        return "entry conditions resolved to nothing"
    return None


def generate_strategy(spec: dict, columns: set[str]) -> tuple[str, str]:
    """Return (class_name, file_path) for a generated strategy that subclasses
    IndicatorDump and injects the user's entry/exit conditions + risk params."""
    cls = "Gen_" + uuid.uuid4().hex[:8]
    entry = conditions_expr(spec.get("entry") or [], columns)
    exit_ = conditions_expr(spec.get("exit") or [], columns)
    can_short = bool(spec.get("can_short"))
    short_entry = conditions_expr(spec.get("short_entry") or [], columns) if can_short else ""
    short_exit = conditions_expr(spec.get("short_exit") or [], columns) if can_short else ""

    roi = spec.get("minimal_roi") or {"0": 100}
    stoploss = float(spec.get("stoploss") if spec.get("stoploss") is not None else -0.10)
    tf = spec.get("timeframe", "15m")
    trailing = bool(spec.get("trailing_stop"))

    def _entry_block(expr, direction, tag):
        if not expr:
            return ""
        return (
            f"        df.loc[(\n            {expr}\n        ), ['enter_{direction}', 'enter_tag']] = (1, '{tag}')\n"
        )

    def _exit_block(expr, direction):
        if not expr:
            return ""
        return (
            f"        df.loc[(\n            {expr}\n        ), 'exit_{direction}'] = 1\n"
        )

    body_entry = _entry_block(entry, "long", "gen_long") or "        pass\n"
    body_entry += _entry_block(short_entry, "short", "gen_short")
    body_exit = _exit_block(exit_, "long") or "        pass\n"
    body_exit += _exit_block(short_exit, "short")

    code = f'''# AUTO-GENERATED by Chart Sidekick strategy builder. Do not edit by hand.
import pandas as pd
from pathlib import Path
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy

DUMP_DIR = Path(__file__).resolve().parents[1] / "indicator_dumps"


class {cls}(IStrategy):
    INTERFACE_VERSION = 3
    can_short = {can_short}
    timeframe = "{tf}"
    minimal_roi = {json.dumps(roi)}
    stoploss = {stoploss}
    trailing_stop = {trailing}
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 160

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Merge the PRECOMPUTED indicator dump (same values shown on the chart),
        # aligned by date. Avoids recomputing the custom indicators at backtest time.
        pair = metadata["pair"].replace("/", "_").replace(":", "_")
        f = DUMP_DIR / f"{{pair}}_indicators.csv"
        if not f.exists():
            return dataframe
        dump = pd.read_csv(f)
        dump["date"] = pd.to_datetime(dump["date"], utc=True)
        keep = [c for c in dump.columns
                if c == "date" or c not in dataframe.columns]
        out = dataframe.merge(dump[keep], on="date", how="left")
        out = out.ffill()
        return out

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
{body_entry}        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
{body_exit}        return df
'''
    path = GEN_DIR / f"{cls}.py"
    path.write_text(code)
    return cls, str(path)


def _timerange(spec: dict) -> str | None:
    s = (spec.get("start") or "").replace("-", "")
    e = (spec.get("end") or "").replace("-", "")
    if not s and not e:
        return None
    return f"{s}-{e}"


def _pair_arg(p: str) -> str:
    """UI pairs look like BTC_USDT_USDT; freqtrade wants BTC/USDT:USDT."""
    if "/" in p:
        return p
    parts = p.split("_")
    if len(parts) == 3:
        return f"{parts[0]}/{parts[1]}:{parts[2]}"
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}"
    return p


def run_backtest(job_id: str, spec: dict, columns: set[str]) -> None:
    """Worker: generate strategy, run freqtrade backtesting, parse result."""
    def upd(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)

    cls = None
    path = None
    try:
        # Two modes: (a) run an EXISTING strategy from the list by name, or
        # (b) generate one from entry/exit conditions. The UI uses (a).
        existing = (spec.get("strategy") or "").strip()
        if existing:
            cls = existing
        else:
            cls, path = generate_strategy(spec, columns)
        upd(status="running", strategy=cls, phase="backtesting")
        cmd = [FREQTRADE, "backtesting",
               "--config", str(CONFIG),
               "--strategy", cls,
               "--timeframe", spec.get("timeframe", "15m"),
               "--export", "trades",
               "--cache", "none"]
        # ROI / stoploss / trailing overrides: freqtrade has no CLI flags for these,
        # so when the user adjusts them in the UI we write a tiny override config and
        # pass it as a SECOND --config (freqtrade deep-merges configs left->right).
        ov = {}
        if spec.get("minimal_roi"):
            ov["minimal_roi"] = {str(k): float(v) for k, v in spec["minimal_roi"].items()}
        if spec.get("stoploss") is not None:
            ov["stoploss"] = float(spec["stoploss"])
        if spec.get("trailing_stop"):
            ov["trailing_stop"] = True
        ov_path = None
        if ov:
            ov_path = GEN_DIR / f"ovcfg_{job_id}.json"
            ov_path.write_text(json.dumps(ov))
            cmd += ["--config", str(ov_path)]
        tr = _timerange(spec)
        if tr:
            cmd += ["--timerange", tr]
        pairs = [_pair_arg(p) for p in (spec.get("pairs") or [])]
        if pairs:
            cmd += ["--pairs", *pairs]
        if spec.get("max_open_trades"):
            cmd += ["--max-open-trades", str(int(spec["max_open_trades"]))]
        if spec.get("stake_amount"):
            cmd += ["--stake-amount", str(spec["stake_amount"])]
        if spec.get("enable_protections"):
            cmd += ["--enable-protections"]

        upd(cmd=" ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(PROJECT), timeout=1800)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            upd(status="error", error="backtest failed", log=tail)
            return
        result = parse_latest_result(cls)
        result["stdout_tail"] = (proc.stdout or "")[-1500:]
        upd(status="done", result=result, finished=time.time())
    except subprocess.TimeoutExpired:
        upd(status="error", error="backtest timed out (>30m)")
    except Exception as e:
        upd(status="error", error=str(e))
    finally:
        # keep generated file for debugging but prune old ones
        _prune_generated()
        # remove this run's override config (do not leave junk in strategies dir)
        try:
            for f in GEN_DIR.glob(f"ovcfg_{job_id}.json"):
                f.unlink()
        except OSError:
            pass


def _prune_generated(keep: int = 20):
    files = sorted(GEN_DIR.glob("Gen_*.py"), key=lambda p: p.stat().st_mtime)
    for p in files[:-keep]:
        try:
            p.unlink()
        except OSError:
            pass


def parse_latest_result(strategy: str) -> dict:
    """Read the newest backtest zip and extract stats + trades for the strategy."""
    zips = sorted(RESULT_DIR.glob("backtest-result-*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        return {"ok": False, "error": "no result file produced"}
    zp = zips[0]
    with zipfile.ZipFile(zp) as z:
        main = [n for n in z.namelist()
                if n.endswith(".json") and "_config" not in n and "market_change" not in n]
        if not main:
            return {"ok": False, "error": "result json not found in zip"}
        data = json.loads(z.read(main[0]))
    strat_block = (data.get("strategy") or {})
    key = strategy if strategy in strat_block else (next(iter(strat_block), None))
    s = strat_block.get(key, {}) if key else {}
    trades = []
    for t in s.get("trades", [])[:2000]:
        trades.append({
            "pair": t.get("pair"),
            "open_date": (t.get("open_date") or "")[:19],
            "close_date": (t.get("close_date") or "")[:19],
            "open_rate": t.get("open_rate"),
            "close_rate": t.get("close_rate"),
            "profit_pct": round((t.get("profit_ratio") or 0) * 100, 3),
            "profit_abs": t.get("profit_abs"),
            "is_short": t.get("is_short", False),
            "exit_reason": t.get("exit_reason"),
        })
    summary = {
        "total_trades": s.get("total_trades"),
        "wins": s.get("wins"), "losses": s.get("losses"), "draws": s.get("draws"),
        "win_rate_pct": round((s.get("winrate") or 0) * 100, 2) if s.get("winrate") is not None
                        else (round(100 * (s.get("wins") or 0) / s["total_trades"], 2)
                              if s.get("total_trades") else None),
        "profit_total_pct": round((s.get("profit_total") or 0) * 100, 3),
        "profit_total_abs": s.get("profit_total_abs"),
        "avg_profit_pct": round((s.get("profit_mean") or 0) * 100, 3),
        "max_drawdown_pct": round((s.get("max_drawdown_account") or s.get("max_drawdown") or 0) * 100, 2),
        "sharpe": s.get("sharpe"),
        "sortino": s.get("sortino"),
        "cagr": s.get("cagr"),
        "expectancy": s.get("expectancy"),
        "best_pair": (s.get("best_pair") or {}).get("key"),
        "worst_pair": (s.get("worst_pair") or {}).get("key"),
        "backtest_start": s.get("backtest_start"),
        "backtest_end": s.get("backtest_end"),
    }
    return {"ok": True, "strategy": key, "summary": summary,
            "trades": trades, "result_file": zp.name}


def start_job(spec: dict, columns: set[str]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "started": time.time(), "spec": spec}
    t = threading.Thread(target=run_backtest, args=(job_id, spec, columns), daemon=True)
    t.start()
    return job_id


def job_status(job_id: str) -> dict:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else {"status": "unknown"}
