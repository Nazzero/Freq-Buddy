"""
Chart Sidekick backend.

Serves:
  - the single-page app (chart + chat)
  - candlestick + indicator data from indicator_dumps CSVs
  - a chat bridge: browser POSTs a message, the Jcode brain loop polls
    /brain/pending, reads chart state, and pushes back tool-calls + a reply.

No external API key. The "brain" is whatever process polls the /brain/* routes
(here: the running Jcode session).
"""
from __future__ import annotations

import base64
import json
import math
import subprocess
import time
import uuid
from pathlib import Path
from threading import Lock

import pandas as pd
import numpy as np
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analyst

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DUMP_ROOT = PROJECT / "user_data" / "indicator_dumps"
SHOT_DIR = ROOT / "shots"        # chart screenshots the AI can look at
SHOT_DIR.mkdir(exist_ok=True)

PAIRS = [
    "BTC_USDT_USDT", "ETH_USDT_USDT", "SOL_USDT_USDT", "XRP_USDT_USDT",
    "BNB_USDT_USDT", "DOGE_USDT_USDT", "LINK_USDT_USDT",
]


def list_dump_strategies() -> list[str]:
    """Strategies that have indicator dumps on disk, newest-modified first.

    Each strategy is a subdir of indicator_dumps/ holding <PAIR>_indicators.csv.
    Legacy flat dumps directly under indicator_dumps/ surface as "default".
    """
    out = []
    if any(DUMP_ROOT.glob("*_indicators.csv")):
        out.append("default")
    subs = [d for d in DUMP_ROOT.iterdir()
            if d.is_dir() and any(d.glob("*_indicators.csv"))]
    subs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    out.extend(d.name for d in subs)
    return out


def resolve_strategy(strategy: str | None) -> str:
    """Pick a usable strategy: requested one if it has data, else the first
    available (newest dumps), else 'default'."""
    avail = list_dump_strategies()
    if strategy and strategy in avail:
        return strategy
    return avail[0] if avail else "default"


def dump_path(pair: str, strategy: str | None) -> Path:
    strat = resolve_strategy(strategy)
    if strat == "default":
        return DUMP_ROOT / f"{pair}_indicators.csv"
    return DUMP_ROOT / strat / f"{pair}_indicators.csv"


app = FastAPI(title="Chart Sidekick")

_cache: dict[str, tuple[float, pd.DataFrame]] = {}

TF_RULES = {
    "15m": None,
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1D",
}

DEFAULT_MODEL = "claude-sonnet-4-6"
_model_lock = Lock()
_selected_model = {"model": DEFAULT_MODEL}
_models_cache: dict[str, object] = {}


def list_models() -> list[str]:
    if "models" not in _models_cache:
        try:
            out = subprocess.run(
                ["jcode", "model", "list", "--no-update"],
                capture_output=True, text=True, timeout=30,
            )
            models = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        except Exception:
            models = []
        _models_cache["models"] = models or [DEFAULT_MODEL]
    return _models_cache["models"]  # type: ignore[return-value]


def load_pair(pair: str, strategy: str | None = None) -> pd.DataFrame:
    path = dump_path(pair, strategy)
    key = str(path)
    mtime = path.stat().st_mtime
    cached = _cache.get(key)
    if cached is None or cached[0] != mtime:
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"], utc=True)
        _cache[key] = (mtime, df)
    return _cache[key][1]


def profile_for(pair: str, strategy: str | None) -> dict:
    """Build/load the analyst profile for a pair under a given strategy,
    keying the cache + mtime check on that strategy's CSV."""
    strat = resolve_strategy(strategy)
    csv = dump_path(pair, strat)
    tag = "" if strat == "default" else strat
    return analyst.load_or_build_profile(
        pair, lambda p: load_pair(p, strat), csv_path=csv, tag=tag)


def apply_timeframe(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = TF_RULES.get((tf or "15m").lower())
    if not rule:
        return df.reset_index(drop=True)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    for col in df.columns:
        if col not in agg and col != "date":
            agg[col] = "last"
    out = (
        df.set_index("date")
        .resample(rule, origin="epoch", label="left", closed="left")
        .agg(agg)
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return out


@app.get("/api/pairs")
def api_pairs():
    return {"pairs": PAIRS}


@app.get("/api/dump_strategies")
def api_dump_strategies():
    """Strategies that have indicator dumps the chart can load, plus the one
    that should be selected by default (newest dumps)."""
    strats = list_dump_strategies()
    return {"strategies": strats, "current": strats[0] if strats else None}


@app.get("/api/models")
def api_models():
    with _model_lock:
        current = _selected_model["model"]
    return {"models": list_models(), "current": current}


class ModelSel(BaseModel):
    model: str


@app.post("/api/model")
def api_set_model(sel: ModelSel):
    with _model_lock:
        _selected_model["model"] = sel.model
    return {"ok": True, "current": sel.model}


@app.get("/api/columns")
def api_columns(pair: str = "BTC_USDT_USDT", strategy: str | None = None):
    df = load_pair(pair, strategy)
    ohlcv = ["open", "high", "low", "close", "volume"]
    indicators = [c for c in df.columns if c not in (["date"] + ohlcv)]
    return {"ohlcv": ohlcv, "indicators": indicators}


@app.get("/api/ranges")
def api_ranges(pair: str = "BTC_USDT_USDT", strategy: str | None = None):
    """Per-indicator value range, used to auto-assign subplots by scale.

    bucket: '0-1' | '0-100' | 'price' | 'other'
    """
    df = load_pair(pair, strategy)
    ohlcv = ["open", "high", "low", "close", "volume"]
    close_med = float(df["close"].median())
    out = {}
    for col in df.columns:
        if col in (["date"] + ohlcv):
            continue
        s = df[col]
        if not pd.api.types.is_numeric_dtype(s):
            out[col] = {"bucket": "other", "min": None, "max": None}
            continue
        lo = float(s.min()) if s.notna().any() else 0.0
        hi = float(s.max()) if s.notna().any() else 0.0
        if -0.05 <= lo and hi <= 1.5:
            bucket = "0-1"
        elif -5 <= lo and hi <= 105:
            bucket = "0-100"
        elif close_med > 0 and 0.3 * close_med <= hi <= 3 * close_med:
            bucket = "price"
        else:
            bucket = "other"
        out[col] = {"bucket": bucket, "min": lo, "max": hi}
    return JSONResponse(out)


# ---------- analyst layer: profile / glossary / source / slice ----------
# The brain reasons over these compact, derived views instead of ingesting the
# full raw CSV. Exact numbers still come from /api/analyze (deterministic).

@app.get("/api/profile")
def api_profile(pair: str = "BTC_USDT_USDT", strategy: str | None = None):
    """Statistical fingerprint of every column over the FULL frame (cached).
    ~9k tokens describes 6.7M cells with statistical fidelity."""
    prof = profile_for(pair, strategy)
    return JSONResponse(prof)


@app.get("/api/profile_text")
def api_profile_text(pair: str = "BTC_USDT_USDT", strategy: str | None = None):
    """Compact one-line-per-column profile (+ producing function) for cheap
    always-on prompt grounding (~2.4k tokens)."""
    prof = profile_for(pair, strategy)
    idx = analyst.load_or_build_index()
    return {"ok": True, "pair": pair, "text": analyst.compact_profile_text(prof, idx),
            "rows": prof.get("rows"), "date_range": prof.get("date_range")}


@app.get("/api/indicator_source")
def api_indicator_source(name: str):
    """Map an indicator column -> the python function that produces it + its
    source code. Lets the brain read the REAL formula for a custom indicator
    (MATRIX/ROXY/etc.) that no model was trained on."""
    idx = analyst.load_or_build_index()
    return JSONResponse(analyst.resolve_indicator_source(name, idx))


@app.get("/api/strategy_source")
def api_strategy_source(file: str, max_lines: int = 600):
    """Read a strategy .py file (path-safe, restricted to strategies dir)."""
    safe = Path(file).name
    p = PROJECT / "user_data" / "strategies" / safe
    if not p.exists():
        return {"ok": False, "error": f"not found: {safe}"}
    lines = p.read_text().splitlines()
    truncated = len(lines) > max_lines
    return {"ok": True, "file": safe, "n_lines": len(lines),
            "truncated": truncated, "source": "\n".join(lines[:max_lines])}


@app.get("/api/slice")
def api_slice(pair: str = "BTC_USDT_USDT", start: str | None = None,
              end: str | None = None, cols: str | None = None, max_rows: int = 60,
              strategy: str | None = None):
    """Small raw window for the requested columns (RAG, not full ingest) so the
    brain can see actual behavior over a span without huge token cost."""
    df = load_pair(pair, strategy)
    col_list = [c.strip() for c in cols.split(",")] if cols else None
    return JSONResponse(analyst.slice_window(df, start, end, col_list, max_rows))


@app.get("/api/data")
def api_data(pair: str = "BTC_USDT_USDT", start: str | None = None,
             end: str | None = None, max_points: int = 4000, tf: str = "15m",
             strategy: str | None = None):
    """Return series as columnar JSON. Resampled to tf, then downsampled if needed.

    OHLC aggregation uses open=first, high=max, low=min, close=last. Indicators
    use last-in-bucket so the chart stays aligned with the displayed candles.
    """
    df = load_pair(pair, strategy)
    if start:
        df = df[df["date"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["date"] <= pd.Timestamp(end, tz="UTC")]
    df = apply_timeframe(df.reset_index(drop=True), tf)

    n = len(df)
    if max_points and n > max_points:
        stride = math.ceil(n / max_points)
        grp = (df.index // stride)
        agg = {"date": "first", "open": "first", "high": "max",
               "low": "min", "close": "last", "volume": "sum"}
        for col in df.columns:
            if col not in agg and col != "date":
                agg[col] = "last"
        df = df.groupby(grp).agg(agg).reset_index(drop=True)

    out = {"date": df["date"].dt.strftime("%Y-%m-%dT%H:%M:%S").tolist()}
    for col in df.columns:
        if col == "date":
            continue
        vals = df[col].to_numpy()
        out[col] = [None if (v is None or (isinstance(v, float) and not math.isfinite(v))) else float(v) if isinstance(v, (int, float)) else v
                    for v in vals.tolist()]
    out["_n_total"] = n
    out["_downsampled"] = n > (max_points or n)
    out["_tf"] = (tf or "15m").lower()
    return JSONResponse(out)


# ---------- data analysis ----------
# The brain can't read raw numbers itself; it asks here for computed answers.

class AnalyzeReq(BaseModel):
    pair: str = "BTC_USDT_USDT"
    strategy: str | None = None   # which strategy's indicator dump to compute on
    op: str                       # "eval" | "cross_recross" | "value_at" | "stat"
    indicator: str | None = None  # column to compare against
    price_field: str = "close"    # which price series: close|low|high|open
    within_bars: int = 6          # for cross_recross: recross window
    window: str | None = None     # "1M" | "30D" | "1W" etc. (rolling period for counting)
    start: str | None = None
    end: str | None = None
    # generic edge eval:
    expr: str | None = None       # boolean pandas expression over columns, e.g. "close < MLN_Green_low & MATRIX > MATRIX.shift(1)"
    confirm_expr: str | None = None  # optional 2nd condition that must become true within within_bars after entry
    forward_bars: int = 24       # bars ahead to measure outcome (return, max favorable/adverse)
    holds: list[int] | None = None  # optional multi-hold sweep: forward stats at each of these bar-holds (entries identical across holds)
    cooldown_bars: int | None = None  # one-position-at-a-time: skip new entries until i+cooldown (None=forward_bars, 0=off)
    edges_only: bool = True       # count only rising edges (entry transitions), not every True bar
    max_events: int = 300         # cap returned event timestamps/markers
    # compare op: run the same edge template across several indicators and rank them
    indicators: list[str] | None = None  # e.g. ["MLN_Green_low","MLN_Green_low_2","MID_LINE_NEW"]
    expr_template: str | None = None      # use {ind} placeholder, e.g. "(close.shift(1) >= {ind}) & (close < {ind})"
    confirm_template: str | None = None   # e.g. "close > {ind}"
    # recipe op: AI-written multi-line pandas (LOW_BOTTOM class of stateful signals)
    recipe: str | None = None             # pandas code; MUST assign a boolean var named `signal`
    signal_name: str = "signal"           # name of the entry-signal variable in the recipe
    baseline_name: str | None = None      # optional 2nd boolean in the recipe; scored on the SAME edge machinery so you get an A/B (e.g. filtered `signal` vs unfiltered `baseline`) in one query
    save_as: str | None = None            # if set, persist the signal (+defined indicators) under this name
    # corr_scan op: rank every other indicator by correlation with a target indicator
    corr_top: int = 12                    # how many top correlates to return
    corr_method: str = "pearson"          # "pearson" | "spearman"
    corr_on: str = "level"                # "level" (raw values) | "return" (pct_change, find co-movement)
    corr_lag: int = 0                     # shift the OTHER indicators by N bars (lead/lag search); 0 = contemporaneous
    corr_min_abs: float = 0.0             # drop pairs with |corr| below this
    corr_exclude: list[str] | None = None # drop candidate cols whose name CONTAINS any of these (kill trivial self-derivatives like bands)


def _resolve_col(df: pd.DataFrame, name: str | None) -> str | None:
    if not name:
        return None
    if name in df.columns:
        return name
    low = name.lower()
    for c in df.columns:
        if c.lower() == low:
            return c
    for c in df.columns:
        if low in c.lower():
            return c
    return None


def _eval_expr(df: pd.DataFrame, expr: str) -> "pd.Series":
    """Evaluate a boolean pandas expression against df columns.

    Columns are exposed by their real names. Also exposes lowercase aliases so the
    LLM can write `close < mln_green_low`. Allows shift()/rolling()/diff() etc via
    pandas eval engine='python' on a namespaced env. Returns a boolean Series.
    """
    env = {}
    for c in df.columns:
        if c == "date":
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        env[c] = s
        env[c.lower()] = s
    env["np"] = np
    env["pd"] = pd
    res = pd.eval(expr, engine="python", local_dict=env, global_dict={})
    if isinstance(res, pd.Series):
        return res.fillna(False).astype(bool)
    # scalar/ndarray
    arr = np.asarray(res)
    return pd.Series(arr, index=df.index).fillna(False).astype(bool)


# ---------- recipe sandbox (Option B: AI writes raw multi-line pandas) ----------
# The AI can write a short pandas RECIPE (multiple statements, the LOW_BOTTOM
# class of stateful patterns). It is shown to the user (echo) BEFORE running, and
# executed in a locked-down sandbox: AST-checked (no import/exec/eval/open/dunder/
# attribute-escapes), only df columns + a curated helper set exposed, time-capped.

import ast as _ast

# Attribute access policy for recipes. We expose the FULL pandas/numpy/scipy
# statistical vocabulary (mean, std, var, median, quantile, corr, cov, skew,
# kurt, rolling, ewm, rank, zscore via scipy, etc.) and only block the handful
# of attributes that enable sandbox escape. Anything that isn't a dunder or an
# explicit escape hatch is allowed — so every stat/math function is available.
_BLOCKED_ATTRS = {
    # escape hatches / introspection that could break out of the sandbox
    "__class__", "__bases__", "__subclasses__", "__globals__", "__dict__",
    "__getattribute__", "__reduce__", "__reduce_ex__", "__builtins__",
    "__import__", "__code__", "__closure__", "__func__", "__self__",
    "__module__", "__loader__", "__spec__", "__init__", "__new__",
    # filesystem / process on objects that might carry them
    "system", "popen", "remove", "unlink", "rmtree", "open", "read", "write",
    "to_csv", "to_pickle", "to_parquet", "to_feather", "to_sql", "to_hdf",
    "eval", "exec", "query",
}
_FORBIDDEN_NAMES = {"__import__", "eval", "exec", "compile", "open", "globals",
                    "locals", "vars", "getattr", "setattr", "delattr", "input",
                    "exit", "quit", "breakpoint", "memoryview", "__builtins__"}


def _check_recipe_ast(code: str) -> str | None:
    """Static safety check. Returns an error string if the code is unsafe, else None.
    Allows the full math/stats vocabulary; blocks only escape hatches."""
    try:
        tree = _ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"syntax error: {e}"
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            return "import is not allowed in a recipe (np, pd, stats are pre-imported)"
        if isinstance(node, (_ast.Lambda,)):
            continue  # lambdas are fine (used in groupby.transform)
        if isinstance(node, _ast.Attribute):
            if node.attr in _BLOCKED_ATTRS or (node.attr.startswith("__") and node.attr.endswith("__")):
                return f"attribute '{node.attr}' is not allowed"
        if isinstance(node, _ast.Name) and node.id in _FORBIDDEN_NAMES:
            return f"name '{node.id}' is not allowed"
        if isinstance(node, _ast.Call):
            fn = node.func
            if isinstance(fn, _ast.Name) and fn.id in _FORBIDDEN_NAMES:
                return f"call to '{fn.id}' is not allowed"
    return None


# safe builtins the recipe may call (no import/open/eval/etc.)
_SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "round": round, "len": len,
    "range": range, "True": True, "False": False, "None": None,
    "int": int, "float": float, "bool": bool, "sum": sum, "sorted": sorted,
    "enumerate": enumerate, "zip": zip, "list": list, "tuple": tuple,
    "dict": dict, "set": set, "any": any, "all": all,
}

# scipy.stats for full statistical vocabulary (zscore, skew, kurtosis,
# pearsonr, spearmanr, percentileofscore, linregress, ...). math for scalars.
import math as _math
try:
    from scipy import stats as _scipy_stats
except Exception:  # scipy optional; recipes can still use np/pd
    _scipy_stats = None


def run_recipe(df: pd.DataFrame, code: str, signal_name: str = "signal",
               baseline_name: str | None = None) -> dict:
    """Execute an AI-written pandas recipe in a sandbox. The recipe assigns a
    variable named `signal` (the entry condition, boolean) and may also assign
    extra numeric columns (custom indicators). Returns the signal Series + any
    new numeric columns it defined.

    Columns are exposed by real name AND lowercase alias. pd/np available.
    """
    err = _check_recipe_ast(code)
    if err:
        return {"ok": False, "error": err}

    env: dict[str, object] = {}
    for c in df.columns:
        if c == "date":
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        env[c] = s
        env[c.lower()] = s
    env["pd"] = pd
    env["np"] = np
    env["math"] = _math
    if _scipy_stats is not None:
        env["stats"] = _scipy_stats        # scipy.stats: zscore, skew, kurtosis, pearsonr, ...
    env["index"] = df.index
    env["__builtins__"] = _SAFE_BUILTINS

    before = set(env.keys())
    try:
        exec(compile(code, "<recipe>", "exec"), env, env)  # sandboxed: AST-checked, no real builtins
    except Exception as e:
        return {"ok": False, "error": f"recipe runtime error: {type(e).__name__}: {e}"}

    if signal_name not in env:
        return {"ok": False, "error": f"recipe must assign a variable named '{signal_name}' (the entry condition)"}

    sig = env[signal_name]
    try:
        sig = pd.Series(sig, index=df.index) if not isinstance(sig, pd.Series) else sig
        sig = sig.fillna(False).astype(bool)
    except Exception as e:
        return {"ok": False, "error": f"signal is not a boolean series: {e}"}

    # optional A/B baseline signal (e.g. the unfiltered version of `signal`)
    base = None
    base_warn = None
    if baseline_name and baseline_name in env:
        try:
            b = env[baseline_name]
            b = pd.Series(b, index=df.index) if not isinstance(b, pd.Series) else b
            base = b.fillna(False).astype(bool)
        except Exception as e:
            return {"ok": False, "error": f"baseline '{baseline_name}' is not a boolean series: {e}"}
    elif baseline_name:
        # B: degrade gracefully instead of hard-failing. The recipe asked for an
        # A/B but never defined the baseline var, so run the single edge and warn.
        base_warn = (f"baseline '{baseline_name}' was requested but the recipe never "
                     f"assigned it; ran single-edge only (no A/B comparison).")

    # collect any NEW numeric series the recipe defined (potential custom indicators)
    defined = {}
    for k, v in env.items():
        if k in before or k.startswith("_") or k in (signal_name, baseline_name):
            continue
        if isinstance(v, pd.Series) and pd.api.types.is_numeric_dtype(v) and not pd.api.types.is_bool_dtype(v):
            defined[k] = v
    return {"ok": True, "signal": sig, "baseline": base, "baseline_warn": base_warn, "defined": defined}


def _compute_edge(df: pd.DataFrame, expr: str, confirm_expr: str | None,
                  within_bars: int, forward_bars: int, edges_only: bool,
                  max_events: int, cooldown_bars: int | None = None,
                  precond: "pd.Series | None" = None,
                  holds: "list[int] | None" = None) -> dict:
    """Core edge computation. Returns dict with summary/per_month/sample/markers,
    or {"ok": False, "error": ...}.

    cooldown_bars: if set (>=0), enforce one-position-at-a-time. After taking an
    entry at bar i, skip every later rising-edge entry until i + cooldown_bars
    (the position's exit). Defaults to forward_bars when None so "hold N bars"
    automatically means "no re-entry until exit". Pass 0 to disable.

    precond: an already-computed boolean entry Series (e.g. from a recipe). When
    provided, expr is ignored for the condition and used only as a label.
    """
    if precond is not None:
        cond = precond.fillna(False).astype(bool)
    else:
        try:
            cond = _eval_expr(df, expr)
        except Exception as e:
            return {"ok": False, "error": f"expr error: {e}", "expr": expr}

    confirm = None
    if confirm_expr:
        try:
            confirm = _eval_expr(df, confirm_expr)
        except Exception as e:
            return {"ok": False, "error": f"confirm_expr error: {e}", "confirm_expr": confirm_expr}

    dates = df["date"]
    close = pd.to_numeric(df["close"], errors="coerce").to_numpy()
    n = len(df)
    cond_np = cond.to_numpy()

    fb = max(1, int(forward_bars))
    wb = max(0, int(within_bars))
    # cooldown defaults to the hold length (forward_bars) so re-entry is blocked
    # until the prior trade exits, matching "hold then sell, don't re-enter".
    cd = fb if cooldown_bars is None else max(0, int(cooldown_bars))

    if edges_only:
        raw_entries = [i for i in range(n) if cond_np[i] and (i == 0 or not cond_np[i - 1])]
    else:
        raw_entries = [i for i in range(n) if cond_np[i]]

    if cd > 0:
        entries = []
        next_ok = -1
        for i in raw_entries:
            if i >= next_ok:
                entries.append(i)
                next_ok = i + cd  # block re-entry until this trade's exit
    else:
        entries = raw_entries

    confirm_np = confirm.to_numpy() if confirm is not None else None

    events = []
    confirmed = 0
    mfe_list, mae_list = [], []
    for i in entries:
        ev = {"i": i, "date": dates.iloc[i].strftime("%Y-%m-%d %H:%M"),
              "close": float(close[i]) if np.isfinite(close[i]) else None}
        if confirm_np is not None:
            hi = min(n, i + wb + 1)
            hit = next((j for j in range(i, hi) if confirm_np[j]), None)
            ev["confirmed"] = hit is not None
            if hit is not None:
                ev["confirm_bars"] = hit - i
                ev["confirm_date"] = dates.iloc[hit].strftime("%Y-%m-%d %H:%M")
                ev["confirm_price"] = float(close[hit]) if np.isfinite(close[hit]) else None
                confirmed += 1
        # hypothetical exit = entry bar + forward_bars (the hold), capped to data end
        xi = min(n - 1, i + fb)
        ev["exit_i"] = xi
        ev["exit_date"] = dates.iloc[xi].strftime("%Y-%m-%d %H:%M")
        ev["exit_price"] = float(close[xi]) if np.isfinite(close[xi]) else None
        end = min(n, i + fb + 1)
        if end > i + 1 and np.isfinite(close[i]) and close[i] != 0:
            seg = close[i + 1:end]
            seg = seg[np.isfinite(seg)]
            if len(seg):
                ret = (seg[-1] / close[i] - 1.0) * 100
                mfe = (np.max(seg) / close[i] - 1.0) * 100
                mae = (np.min(seg) / close[i] - 1.0) * 100
                ev["fwd_return_pct"] = round(float(ret), 3)
                ev["mfe_pct"] = round(float(mfe), 3)
                ev["mae_pct"] = round(float(mae), 3)
                mfe_list.append(mfe)
                mae_list.append(mae)
        events.append(ev)

    per_month = {}
    for e in events:
        per_month[e["date"][:7]] = per_month.get(e["date"][:7], 0) + 1

    used = events if confirm_np is None else [e for e in events if e.get("confirmed")]
    used_ret = [e["fwd_return_pct"] for e in used if "fwd_return_pct" in e]

    summary = {
        "entries": len(entries),
        "raw_entries": len(raw_entries),
        "cooldown_bars": cd,
        "confirmed_within_window": (confirmed if confirm_np is not None else None),
        "confirm_rate_pct": (round(100 * confirmed / len(entries), 1) if (confirm_np is not None and entries) else None),
        "forward_bars": fb,
        "win_rate_pct": (round(100 * sum(1 for r in used_ret if r > 0) / len(used_ret), 1) if used_ret else None),
        "avg_fwd_return_pct": (round(float(np.mean(used_ret)), 3) if used_ret else None),
        "median_fwd_return_pct": (round(float(np.median(used_ret)), 3) if used_ret else None),
        "avg_mfe_pct": (round(float(np.mean(mfe_list)), 3) if mfe_list else None),
        "avg_mae_pct": (round(float(np.mean(mae_list)), 3) if mae_list else None),
    }
    markers = [{"date": e["date"], "price": e["close"], "confirmed": e.get("confirmed"),
                "exit_date": e.get("exit_date"), "exit_price": e.get("exit_price"),
                "confirm_date": e.get("confirm_date"), "confirm_price": e.get("confirm_price"),
                "confirm_bars": e.get("confirm_bars"), "fwd_return_pct": e.get("fwd_return_pct")}
               for e in used[:max_events]]

    out = {
        "ok": True, "expr": expr, "confirm_expr": confirm_expr,
        "summary": summary, "per_month": per_month,
        "data_range": [dates.iloc[0].strftime("%Y-%m-%d"), dates.iloc[-1].strftime("%Y-%m-%d")],
        "bars": n, "sample": events[:25], "markers": markers, "n_markers": len(markers),
    }

    # Multi-hold sweep: same entry set, measure forward stats at each hold length.
    # Entry selection (and cooldown) is unchanged; only the exit window varies, so
    # the holds are directly comparable on identical trades.
    if holds:
        used_idx = [e["i"] for e in used]
        sweep = []
        for h in holds:
            hb = max(1, int(h))
            rets = []
            hmarkers = []
            for e in used:
                i = e["i"]
                xi = min(n - 1, i + hb)
                ret = None
                end = min(n, i + hb + 1)
                if end > i + 1 and np.isfinite(close[i]) and close[i] != 0:
                    seg = close[i + 1:end]
                    seg = seg[np.isfinite(seg)]
                    if len(seg):
                        ret = (seg[-1] / close[i] - 1.0) * 100
                        rets.append(ret)
                hmarkers.append({
                    "date": e["date"], "price": e["close"], "confirmed": e.get("confirmed"),
                    "confirm_date": e.get("confirm_date"), "confirm_price": e.get("confirm_price"),
                    "confirm_bars": e.get("confirm_bars"),
                    "exit_date": dates.iloc[xi].strftime("%Y-%m-%d %H:%M"),
                    "exit_price": float(close[xi]) if np.isfinite(close[xi]) else None,
                    "fwd_return_pct": round(float(ret), 3) if ret is not None else None,
                })
            sweep.append({
                "forward_bars": hb,
                "n": len(rets),
                "win_rate_pct": (round(100 * sum(1 for r in rets if r > 0) / len(rets), 1) if rets else None),
                "avg_fwd_return_pct": (round(float(np.mean(rets)), 3) if rets else None),
                "median_fwd_return_pct": (round(float(np.median(rets)), 3) if rets else None),
                "markers": hmarkers[:max_events],
            })
        out["hold_sweep"] = sweep

    return out


def _eval_edge(df: pd.DataFrame, req: AnalyzeReq) -> dict:
    if not req.expr:
        return {"ok": False, "error": "eval op needs 'expr'"}
    res = _compute_edge(df, req.expr, req.confirm_expr, req.within_bars,
                        req.forward_bars, req.edges_only, req.max_events,
                        req.cooldown_bars, holds=req.holds)
    if res.get("ok"):
        res["op"] = "eval"
        res["pair"] = req.pair
        res["edges_only"] = req.edges_only
    return res


def _recipe_edge(df: pd.DataFrame, req: AnalyzeReq) -> dict:
    """Run an AI-written pandas recipe, then feed its `signal` into the same
    trusted edge machinery (cooldown, forward stats, markers)."""
    if not req.recipe:
        return {"ok": False, "error": "recipe op needs 'recipe' (pandas code assigning a boolean `signal`)"}
    rr = run_recipe(df, req.recipe, req.signal_name, req.baseline_name)
    if not rr.get("ok"):
        return {"ok": False, "error": rr.get("error"), "recipe": req.recipe}
    sig = rr["signal"]
    # the recipe already produced the per-bar entry condition; edges_only still
    # collapses runs of True into the first bar (one entry per signal episode).
    res = _compute_edge(df, expr=f"recipe:{req.signal_name}", confirm_expr=req.confirm_expr,
                        within_bars=req.within_bars, forward_bars=req.forward_bars,
                        edges_only=req.edges_only, max_events=req.max_events,
                        cooldown_bars=req.cooldown_bars, precond=sig, holds=req.holds)
    if res.get("ok"):
        res["op"] = "recipe"
        res["pair"] = req.pair
        res["recipe"] = req.recipe
        res["signal_name"] = req.signal_name
        res["defined_indicators"] = sorted(rr["defined"].keys())
        res["signal_true_bars"] = int(sig.sum())
        if rr.get("baseline_warn"):
            res["baseline_warn"] = rr["baseline_warn"]
        # A/B: if the recipe defined a baseline boolean, score it on the SAME
        # machinery so the answer can compare win rates (e.g. filtered vs not).
        base = rr.get("baseline")
        if base is not None:
            bres = _compute_edge(df, expr=f"recipe:{req.baseline_name}", confirm_expr=req.confirm_expr,
                                 within_bars=req.within_bars, forward_bars=req.forward_bars,
                                 edges_only=req.edges_only, max_events=req.max_events,
                                 cooldown_bars=req.cooldown_bars, precond=base, holds=req.holds)
            if bres.get("ok"):
                bs = bres.get("summary") or {}
                keep = ("entries", "raw_entries", "confirmed_within_window", "confirm_rate_pct",
                        "win_rate_pct", "avg_fwd_return_pct", "median_fwd_return_pct",
                        "avg_mfe_pct", "avg_mae_pct")
                res["baseline"] = {"name": req.baseline_name,
                                   **{k: bs.get(k) for k in keep if k in bs}}
                res["baseline"]["signal_true_bars"] = int(base.sum())
            else:
                res["baseline"] = {"name": req.baseline_name, "error": bres.get("error")}
    return res


def _compare(df: pd.DataFrame, req: AnalyzeReq) -> dict:
    inds = req.indicators or []
    if not inds:
        return {"ok": False, "error": "compare op needs 'indicators' list"}
    expr_t = req.expr_template or "(close.shift(1) >= {ind}) & (close < {ind})"
    conf_t = req.confirm_template if req.confirm_template is not None else "close > {ind}"
    rows = []
    best_markers = None
    best_key = None
    marker_groups = []  # per-indicator marker sets so the chart can show ALL compared signals
    for raw in inds:
        col = _resolve_col(df, raw)
        if col is None:
            rows.append({"indicator": raw, "ok": False, "error": "not found"})
            continue
        expr = expr_t.replace("{ind}", col)
        conf = conf_t.replace("{ind}", col) if conf_t else None
        r = _compute_edge(df, expr, conf, req.within_bars, req.forward_bars,
                          req.edges_only, req.max_events, req.cooldown_bars,
                          holds=req.holds)
        if not r.get("ok"):
            rows.append({"indicator": col, "ok": False, "error": r.get("error")})
            continue
        s = r["summary"]
        row = {"indicator": col, "ok": True, **s}
        if r.get("hold_sweep"):
            row["hold_sweep"] = r["hold_sweep"]
        rows.append(row)
        marker_groups.append({"indicator": col, "markers": r["markers"]})
        # pick best by win_rate then avg_fwd_return for default markers
        score = (s.get("win_rate_pct") or 0, s.get("avg_fwd_return_pct") or 0)
        if best_key is None or score > best_key:
            best_key = score
            best_markers = r["markers"]
            best_ind = col
    ranked = sorted([x for x in rows if x.get("ok")],
                    key=lambda x: ((x.get("win_rate_pct") or -1), (x.get("avg_fwd_return_pct") or -1)),
                    reverse=True)
    return {
        "ok": True, "op": "compare", "pair": req.pair,
        "expr_template": expr_t, "confirm_template": conf_t,
        "forward_bars": req.forward_bars, "within_bars": req.within_bars,
        "cooldown_bars": (req.cooldown_bars if req.cooldown_bars is not None else req.forward_bars),
        "results": rows, "ranked": ranked,
        "best": (best_ind if best_markers else None),
        "markers": best_markers or [],
        "marker_groups": marker_groups,  # ALL indicators' markers, for per-indicator chart display
        "n_markers": sum(len(g["markers"]) for g in marker_groups),
    }


def _corr_scan(df: pd.DataFrame, req: AnalyzeReq) -> dict:
    """Rank every other numeric indicator by its correlation with a target indicator.
    Answers 'find an indicator strongly correlated with X'. Supports level vs return
    space, pearson/spearman, and a lag (shift the others by N bars) for lead/lag edges."""
    target = _resolve_col(df, req.indicator)
    if target is None:
        return {"ok": False, "error": f"indicator not found: {req.indicator}"}

    # numeric candidate columns (skip date, the target itself, all-NaN/constant cols)
    skip = {"date", target}
    # auto-drop trivial self-derivatives: bands/variants sharing the target's stem
    # (e.g. MID_LINE_NEW -> MID_LINE_NEW_upper_band_1). These correlate ~1.0 and carry
    # no independent signal. The brain can also pass corr_exclude substrings.
    stem = target.replace("_upper_band", "").replace("_lower_band", "")
    stem = "_".join([p for p in stem.split("_") if not p.isdigit()]).rstrip("_")
    excl = [e.lower() for e in (req.corr_exclude or [])]
    cols = []
    for c in df.columns:
        if c in skip:
            continue
        cl = c.lower()
        if stem and len(stem) >= 4 and stem.lower() in cl:
            continue  # same family as the target -> trivially correlated
        if any(e in cl for e in excl):
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().sum() < 20 or s.nunique(dropna=True) < 2:
            continue
        cols.append(c)

    tgt = pd.to_numeric(df[target], errors="coerce")
    on = req.corr_on if req.corr_on in ("level", "return") else "level"
    if on == "return":
        tgt = tgt.pct_change().replace([np.inf, -np.inf], np.nan)

    method = req.corr_method if req.corr_method in ("pearson", "spearman") else "pearson"
    lag = int(req.corr_lag or 0)

    rows = []
    for c in cols:
        oth = pd.to_numeric(df[c], errors="coerce")
        if on == "return":
            oth = oth.pct_change().replace([np.inf, -np.inf], np.nan)
        if lag:
            oth = oth.shift(lag)  # positive lag => other LEADS target by `lag` bars
        pair = pd.concat([tgt, oth], axis=1).dropna()
        if len(pair) < 20:
            continue
        try:
            r = pair.iloc[:, 0].corr(pair.iloc[:, 1], method=method)
        except Exception:
            continue
        if r is None or (isinstance(r, float) and (r != r)):  # NaN
            continue
        if abs(r) < (req.corr_min_abs or 0.0):
            continue
        rows.append({"indicator": c, "corr": round(float(r), 4),
                     "abs": round(abs(float(r)), 4), "n": int(len(pair))})

    rows.sort(key=lambda x: x["abs"], reverse=True)
    top = rows[:max(1, int(req.corr_top or 12))]
    dates = df["date"]
    return {
        "ok": True, "op": "corr_scan", "pair": req.pair,
        "target": target, "on": on, "method": method, "lag": lag,
        "candidates_scanned": len(cols),
        "excluded_stem": stem,
        "data_range": [dates.iloc[0].strftime("%Y-%m-%d"),
                       dates.iloc[-1].strftime("%Y-%m-%d")],
        "bars": int(len(df)),
        "top": top,
    }



@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    df = load_pair(req.pair, req.strategy).copy()
    if req.start:
        df = df[df["date"] >= pd.Timestamp(req.start, tz="UTC")]
    if req.end:
        df = df[df["date"] <= pd.Timestamp(req.end, tz="UTC")]
    df = df.reset_index(drop=True)
    if df.empty:
        return {"ok": False, "error": "no rows in range"}

    if req.op == "eval":
        return _eval_edge(df, req)

    if req.op == "recipe":
        return _recipe_edge(df, req)

    if req.op == "compare":
        return _compare(df, req)

    if req.op == "corr_scan":
        return _corr_scan(df, req)

    if req.op == "cross_recross":
        col = _resolve_col(df, req.indicator)
        if col is None:
            return {"ok": False, "error": f"indicator not found: {req.indicator}"}
        pf = req.price_field if req.price_field in df.columns else "close"
        price = df[pf].to_numpy()
        ind = df[col].to_numpy()
        dates = df["date"]
        n = len(df)
        below = price < ind                      # price under indicator
        # a "fall under" = transition from not-below -> below
        events = []                              # (idx_fall, idx_recross or None)
        i = 1
        while i < n:
            if below[i] and not below[i - 1]:
                fall = i
                # look for first bar within window where price rises back above
                rec = None
                hi = min(n, fall + req.within_bars + 1)
                for j in range(fall + 1, hi):
                    if price[j] > ind[j]:
                        rec = j
                        break
                events.append((fall, rec))
                # advance past this episode (to the recross or window end) to avoid double-count
                i = (rec if rec is not None else hi) + 0
                if i <= fall:
                    i = fall + 1
            else:
                i += 1

        total_falls = len(events)
        recrossed = [e for e in events if e[1] is not None]
        # group into the requested rolling window (default 1 month)
        win = req.window or "1M"
        # per-calendar-month tally of qualifying recrosses
        per_period = {}
        for fall, rec in recrossed:
            d = dates.iloc[fall]
            key = d.strftime("%Y-%m")
            per_period[key] = per_period.get(key, 0) + 1
        sample = []
        for fall, rec in recrossed[:25]:
            sample.append({
                "fell_under": dates.iloc[fall].strftime("%Y-%m-%d %H:%M"),
                "rose_above": dates.iloc[rec].strftime("%Y-%m-%d %H:%M"),
                "bars_to_recross": rec - fall,
            })
        return {
            "ok": True,
            "op": "cross_recross",
            "pair": req.pair,
            "indicator": col,
            "price_field": pf,
            "within_bars": req.within_bars,
            "total_falls_under": total_falls,
            "recrossed_within_window": len(recrossed),
            "per_month": per_period,
            "data_range": [dates.iloc[0].strftime("%Y-%m-%d"),
                           dates.iloc[-1].strftime("%Y-%m-%d")],
            "bars": n,
            "sample": sample,
        }

    if req.op == "value_at":
        col = _resolve_col(df, req.indicator)
        ts = pd.Timestamp(req.start or req.end, tz="UTC")
        idx = (df["date"] - ts).abs().idxmin()
        row = df.loc[idx]
        return {"ok": True, "op": "value_at",
                "date": row["date"].strftime("%Y-%m-%d %H:%M"),
                "close": float(row["close"]),
                "indicator": col,
                "value": (float(row[col]) if col else None)}

    if req.op == "stat":
        col = _resolve_col(df, req.indicator) or "close"
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        return {"ok": True, "op": "stat", "column": col,
                "min": float(s.min()), "max": float(s.max()),
                "mean": float(s.mean()), "count": int(s.count())}

    # graceful fallback: unknown op but enough info to run a known one
    if req.recipe:
        return _recipe_edge(df, req)
    if req.indicators or req.expr_template:
        return _compare(df, req)
    if req.expr:
        return _eval_edge(df, req)
    return {"ok": False, "error": f"unknown op: {req.op}",
            "hint": "valid ops: eval, recipe, compare, cross_recross, value_at, stat"}


# ---------- chat bridge ----------
# browser -> queue -> brain polls -> brain pushes response -> browser polls

class ChatMsg(BaseModel):
    text: str
    chart_state: dict | None = None  # current visible range, hidden traces, pair
    model: str | None = None         # explicit model from the picker (avoids racing /api/model)


_lock = Lock()
_pending: list[dict] = []       # messages waiting for brain
_responses: dict[str, dict] = {}  # id -> {reply, actions}
_inflight: dict[str, dict] = {}   # id -> original message (text/chart_state) for history
_cancelled: set[str] = set()      # ids the user asked to STOP; brain checks + bails
_brain_last_seen: float = 0.0     # epoch secs of the brain's most recent poll (heartbeat)
_activity: list[dict] = []        # ring buffer of brain activity events for the logs panel
ACTIVITY_MAX = 400                # keep last N events in memory
# If an in-flight turn has had no brain activity for this long and still has no
# response, treat the brain as crashed and requeue the message (see
# api_chat_result). Must exceed the longest gap between a brain's own activity
# events within a single turn (decide1 -> fetch -> decide2), which is well under
# a minute, so 90s never trips on a healthy slow turn.
STALE_REQUEUE_SECS = 90
HISTORY_FILE = ROOT / "chat_history.jsonl"

# Rolling in-memory conversation so the brain has memory across turns. Seeded from
# the on-disk history on first use so a server restart doesn't lose context.
_convo: list[dict] = []          # [{role:"user"/"assistant", text, query?}]
CONVO_MAX_TURNS = 16             # keep last N exchanges (user+assistant counted separately)
_convo_seeded = {"done": False}


def _seed_convo_from_disk() -> None:
    if _convo_seeded["done"]:
        return
    _convo_seeded["done"] = True
    for it in _read_history()[-(CONVO_MAX_TURNS // 2):]:
        if it.get("text"):
            _convo.append({"role": "user", "text": it["text"]})
        if it.get("reply"):
            _convo.append({"role": "assistant", "text": it["reply"]})
    del _convo[:-CONVO_MAX_TURNS]


def _convo_add(role: str, text: str) -> None:
    if not text:
        return
    _convo.append({"role": role, "text": text})
    del _convo[:-CONVO_MAX_TURNS]


def _append_history(entry: dict) -> None:
    try:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class ShotMsg(BaseModel):
    png_b64: str


@app.post("/api/screenshot")
def api_screenshot(s: ShotMsg):
    """Save a chart screenshot the AI can Read. Returns its absolute path."""
    sid = uuid.uuid4().hex[:12]
    raw = s.png_b64.split(",", 1)[-1]  # tolerate data: URL prefix
    try:
        data = base64.b64decode(raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad base64"}, status_code=400)
    p = SHOT_DIR / f"{sid}.png"
    p.write_bytes(data)
    # keep the dir from growing without bound: keep newest ~40 shots
    shots = sorted(SHOT_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
    for old in shots[:-40]:
        try:
            old.unlink()
        except OSError:
            pass
    return {"ok": True, "path": str(p)}


@app.post("/api/chat")
def api_chat(msg: ChatMsg):
    mid = uuid.uuid4().hex[:12]
    with _model_lock:
        # picker sends the model atomically with the message; fall back to the
        # server-global selection if absent. also sync the global so the UI stays
        # consistent on the next turn.
        if msg.model:
            _selected_model["model"] = msg.model
        model = _selected_model["model"]
    with _lock:
        _seed_convo_from_disk()
        history = list(_convo)  # prior turns BEFORE this message
        qitem = {
            "id": mid,
            "text": msg.text,
            "chart_state": msg.chart_state or {},
            "model": model,
            "ts": time.time(),
            "history": history,
        }
        _pending.append(qitem)
        # Keep the full queue payload so a stale in-flight turn (brain crashed
        # after it dequeued the message but before replying) can be requeued and
        # answered instead of hanging the UI spinner forever.
        _inflight[mid] = {"text": msg.text, "chart_state": msg.chart_state or {},
                          "model": model, "ts": time.time(), "queued_ts": time.time(),
                          "qitem": qitem}
        _convo_add("user", msg.text)
    return {"id": mid}


@app.get("/api/chat/result/{mid}")
def api_chat_result(mid: str):
    with _lock:
        r = _responses.pop(mid, None)
        if r is not None:
            return {"ready": True, **r}
        # Self-heal a stuck turn: the message is in-flight (brain claimed it) but
        # has no response, is not waiting in the queue, and the brain has shown no
        # activity for it recently -> the brain almost certainly crashed after it
        # dequeued the message. Requeue it so a (restarted) brain answers it
        # instead of the UI spinning forever. Guarded so a long legit turn that
        # is still emitting activity is never double-processed.
        inf = _inflight.get(mid)
        if inf and mid not in _cancelled and not any(m.get("id") == mid for m in _pending):
            last = max((e["ts"] for e in _activity if e.get("mid") == mid),
                       default=inf.get("queued_ts", inf.get("ts", 0.0)))
            if time.time() - last > STALE_REQUEUE_SECS and inf.get("qitem"):
                inf["queued_ts"] = time.time()
                _pending.append(dict(inf["qitem"]))
                _activity.append({"ts": time.time(), "mid": mid, "phase": "requeue",
                                  "detail": "stale in-flight turn requeued (brain likely crashed)"})
                del _activity[:-ACTIVITY_MAX]
    return {"ready": False}


@app.post("/api/chat/cancel/{mid}")
def api_chat_cancel(mid: str):
    """User pressed STOP. Mark the turn cancelled so the brain bails at its next
    checkpoint, drop it from the queue, and hand the browser an immediate result."""
    with _lock:
        _cancelled.add(mid)
        _pending[:] = [m for m in _pending if m.get("id") != mid]
        _inflight.pop(mid, None)
        _responses[mid] = {"reply": "(stopped by user)", "actions": [],
                           "model_used": None, "cancelled": True}
        _activity.append({"ts": time.time(), "mid": mid, "phase": "cancel",
                          "detail": "user pressed stop"})
        del _activity[:-ACTIVITY_MAX]
    return {"ok": True}


@app.get("/api/chat/cancelled/{mid}")
def api_chat_cancelled(mid: str):
    """Brain polls this between phases to know if it should abort early."""
    with _lock:
        return {"cancelled": mid in _cancelled}


# --- brain activity log (what the brain is doing, for the logs panel) ---
class ActivityEvent(BaseModel):
    mid: str | None = None
    phase: str                      # e.g. "model_call", "op", "subprocess", "done", "error"
    detail: str = ""
    meta: dict | None = None

@app.post("/brain/log")
def brain_log(ev: ActivityEvent):
    global _brain_last_seen
    with _lock:
        _brain_last_seen = time.time()   # heartbeat: brain active mid-turn
        _activity.append({"ts": time.time(), "mid": ev.mid, "phase": ev.phase,
                          "detail": ev.detail, "meta": ev.meta or {}})
        del _activity[:-ACTIVITY_MAX]
    return {"ok": True}

@app.get("/api/activity")
def api_activity(since: float = 0.0, mid: str | None = None, limit: int = 200):
    """Return brain activity events newer than `since` (epoch secs). Optionally
    filter to one turn (mid). Used by the live logs panel."""
    with _lock:
        evs = [e for e in _activity if e["ts"] > since and (mid is None or e.get("mid") == mid)]
    return {"events": evs[-limit:], "now": time.time()}


# --- brain side ---
@app.get("/brain/pending")
def brain_pending():
    global _brain_last_seen
    with _lock:
        _brain_last_seen = time.time()   # heartbeat: brain is alive and polling
        items = list(_pending)
        _pending.clear()
    return {"messages": items}


@app.get("/api/brain/status")
def api_brain_status():
    """UI connectivity light. `connected` is true only while the brain has polled
    recently (it polls /brain/pending every ~2s), meaning it is alive and ready
    to talk to the AI. `busy` is true while a turn is in flight."""
    with _lock:
        age = time.time() - _brain_last_seen if _brain_last_seen else None
        busy = bool(_inflight)
        # Idle brain polls every ~0.6s, so a >6s gap means it is gone. While a turn
        # is in flight the brain blocks on model calls and only checks in via
        # /brain/log every ~13s, so allow a longer gap before declaring it offline.
        limit = 20.0 if busy else 6.0
        connected = age is not None and age < limit
    return {"connected": connected, "busy": busy, "age": age}


class BrainReply(BaseModel):
    id: str
    reply: str
    actions: list[dict] = []  # [{op:"zoom",args:{...}}, ...]
    model_used: str | None = None


@app.post("/brain/reply")
def brain_reply(r: BrainReply):
    with _lock:
        if r.id in _cancelled:
            # user already stopped this turn; keep the "stopped" result, drop flag
            _cancelled.discard(r.id)
            _inflight.pop(r.id, None)
            return {"ok": True, "ignored": "cancelled"}
        _responses[r.id] = {"reply": r.reply, "actions": r.actions,
                            "model_used": r.model_used}
        orig = _inflight.pop(r.id, {})
        _convo_add("assistant", r.reply)
    _append_history({
        "id": r.id,
        "ts": orig.get("ts", time.time()),
        "text": orig.get("text", ""),
        "reply": r.reply,
        "actions": r.actions,
        "model": r.model_used or orig.get("model"),
        "chart_state": orig.get("chart_state", {}),
    })
    return {"ok": True}


@app.get("/api/history")
def api_history(limit: int = 100):
    items = _read_history()
    return {"sessions": items[-limit:][::-1]}


@app.get("/api/history/{mid}")
def api_history_one(mid: str):
    for it in _read_history():
        if it.get("id") == mid:
            return it
    return JSONResponse({"error": "not found"}, status_code=404)


def _rewrite_history(items: list[dict]) -> None:
    tmp = HISTORY_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(it) + "\n" for it in items))
    tmp.replace(HISTORY_FILE)


@app.delete("/api/history/{mid}")
def api_history_delete(mid: str):
    items = _read_history()
    kept = [it for it in items if it.get("id") != mid]
    if len(kept) == len(items):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    _rewrite_history(kept)
    return {"ok": True, "deleted": mid, "remaining": len(kept)}


@app.delete("/api/history")
def api_history_clear():
    n = len(_read_history())
    _rewrite_history([])
    return {"ok": True, "cleared": n}


@app.post("/api/chat/new")
def api_chat_new():
    """Start a fresh chat session: drop the in-memory cross-turn memory and
    archive the on-disk transcript so the next turn starts clean (but nothing
    is lost — old turns roll into a timestamped file)."""
    turns = len(_convo)
    _convo.clear()
    archived = 0
    items = _read_history()
    if items:
        archived = len(items)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        arch = ROOT / f"chat_history.{stamp}.jsonl"
        try:
            arch.write_text("\n".join(json.dumps(it) for it in items) + "\n")
        except OSError:
            archived = 0
        _rewrite_history([])
    return {"ok": True, "cleared_turns": turns, "archived": archived}


# ---------- backtest / strategy builder ----------
# The user composes entry/exit conditions over real indicator columns; we generate
# a freqtrade strategy that merges the precomputed dump, run it, and return stats +
# trades. Long-running, so it's a background job polled by the UI.
import backtest as _bt
import dumpjobs as _dj


@app.get("/api/strategies")
def api_strategies():
    return {"strategies": _bt.list_strategies()}


# ---------- indicator-dump management (strategy selector +/gear) ----------
@app.get("/api/dumpable_pairs")
def api_dumpable_pairs():
    """Pairs with downloaded 15m base data (csv form) that can be dumped."""
    return {"pairs": _dj.dumpable_pairs()}


class DumpReq(BaseModel):
    strategy: str
    pairs: list[str] = []
    timerange: str | None = None


@app.post("/api/dump_jobs")
def api_start_dump(req: DumpReq):
    pairs = _dj._valid_pairs(req.pairs)
    bad = _dj.validate_dump(req.strategy, pairs, req.timerange)
    if bad:
        return JSONResponse({"ok": False, "error": bad}, status_code=400)
    job_id = _dj.start_dump_job(req.strategy, pairs, req.timerange or None)
    return {"ok": True, "job_id": job_id}


@app.get("/api/dump_jobs/{job_id}")
def api_dump_status(job_id: str):
    return _dj.dump_status(job_id)


@app.get("/api/dump_registry/{strategy}")
def api_dump_registry(strategy: str):
    entry = _dj.registry_get(strategy)
    return {"ok": True, "entry": entry}


@app.delete("/api/dump_strategies/{strategy}")
def api_delete_dump_strategy(strategy: str):
    err = _dj.delete_strategy(strategy)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    _cache.clear()  # drop any cached frames for the removed strategy
    return {"ok": True}


class BacktestSpec(BaseModel):
    strategy: str | None = None        # name of an existing strategy to run (Mode A)
    pair: str = "BTC_USDT_USDT"
    pairs: list[str] | None = None
    entry: list[dict] = []
    exit: list[dict] = []
    short_entry: list[dict] | None = None
    short_exit: list[dict] | None = None
    can_short: bool = False
    minimal_roi: dict | None = None
    stoploss: float | None = None
    trailing_stop: bool = False
    timeframe: str = "15m"
    start: str | None = None
    end: str | None = None
    max_open_trades: int | None = None
    stake_amount: float | str | None = None
    enable_protections: bool = False


@app.post("/api/backtest")
def api_backtest(spec: BacktestSpec):
    s = spec.model_dump()
    if not s.get("pairs"):
        s["pairs"] = [s.get("pair", "BTC_USDT_USDT")]
    df = load_pair(s["pairs"][0])
    columns = {c for c in df.columns if c != "date"}
    bad = _bt.validate_spec(s, columns)
    if bad:
        return JSONResponse({"ok": False, "error": bad}, status_code=400)
    job_id = _bt.start_job(s, columns)
    return {"ok": True, "job_id": job_id}


@app.get("/api/backtest/{job_id}")
def api_backtest_status(job_id: str):
    return _bt.job_status(job_id)


# ---------- static SPA ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "index.html").read_text()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8777, log_level="warning")
