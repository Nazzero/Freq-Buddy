"""
Analyst layer for Chart Sidekick.

The brain (LLM) can't ingest the raw 42k x 159 CSV — too many tokens. Instead it
reasons over three compact, derived views and only ever asks the server to compute
exact numbers on the full raw data:

  1. PROFILE   — a statistical fingerprint of every column (range, percentiles,
                 std, corr-to-close, oscillator/price/binary kind). Derived from
                 ALL rows, ~15-30 tokens/col. "What the numbers look like."
  2. GLOSSARY  — each custom indicator mapped to the source function that produces
                 it + a plain-language purpose. "What the indicator MEANS." This is
                 the keystone for MATRIX/ROXY/etc which no model was trained on.
  3. SLICE     — on demand, a small window of raw rows (RAG, not full ingest).
                 "What it looks like in motion."

This module builds (1) and (2) from the strategy .py files + the dumped CSVs and
caches them. The exact answer to any quant question still comes from the existing
deterministic compute path in server.py — the analyst layer only guides WHAT to
compute and gives the LLM enough grounding to propose accurate queries.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
STRAT_DIR = PROJECT / "user_data" / "strategies"
CACHE_DIR = ROOT / "_analyst_cache"
CACHE_DIR.mkdir(exist_ok=True)

OHLCV = ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# 1. STATISTICAL PROFILE
# --------------------------------------------------------------------------- #
def _kind(col: str, s: pd.Series, close_med: float) -> str:
    """Classify a column so the LLM understands its shape without seeing values."""
    if not pd.api.types.is_numeric_dtype(s):
        return "categorical"
    valid = s.dropna()
    if valid.empty:
        return "empty"
    nun = valid.nunique()
    lo, hi = float(valid.min()), float(valid.max())
    if nun <= 2 and lo >= 0 and hi <= 1:
        return "binary"
    if -1.05 <= lo and hi <= 1.05:
        return "unit"            # 0..1 (e.g. laguerre rsi, probabilities)
    if -105 <= lo and hi <= 105 and (hi - lo) > 5:
        return "oscillator"      # bounded ~-100..100 or 0..100
    if close_med > 0 and 0.2 * close_med <= np.median(valid) <= 5 * close_med:
        return "price"           # tracks the price scale (support/resist/bands)
    return "other"


def compute_profile(df: pd.DataFrame) -> dict:
    """Compact per-column stats over the FULL frame. ~15-30 tokens/col when JSON'd."""
    close = df["close"] if "close" in df.columns else None
    close_med = float(close.median()) if close is not None else 0.0
    cols = {}
    for col in df.columns:
        if col == "date":
            continue
        s = df[col]
        kind = _kind(col, s, close_med)
        entry: dict = {"kind": kind}
        if pd.api.types.is_numeric_dtype(s):
            if pd.api.types.is_bool_dtype(s):
                s = s.astype("int8")
            valid = s.dropna()
            if valid.empty:
                entry.update({"null_pct": 100.0})
                cols[col] = entry
                continue
            q = valid.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
            entry.update({
                "min": round(float(valid.min()), 6),
                "max": round(float(valid.max()), 6),
                "mean": round(float(valid.mean()), 6),
                "std": round(float(valid.std()), 6),
                "p5": round(float(q.loc[0.05]), 6),
                "p50": round(float(q.loc[0.5]), 6),
                "p95": round(float(q.loc[0.95]), 6),
                "last": round(float(valid.iloc[-1]), 6),
                "n_unique": int(valid.nunique()),
                "null_pct": round(float(s.isna().mean() * 100), 2),
            })
            # correlation to close — tells the LLM if it leads/tracks price
            if close is not None and col not in ("close",):
                try:
                    c = float(s.corr(close))
                    if np.isfinite(c):
                        entry["corr_close"] = round(c, 3)
                except Exception:
                    pass
        else:
            entry["sample"] = [str(v) for v in s.dropna().unique()[:5]]
        cols[col] = entry

    # regime/notable summary so outliers survive the compression
    notable = _notable_events(df) if close is not None else []
    return {
        "rows": int(len(df)),
        "date_range": [str(df["date"].iloc[0])[:19], str(df["date"].iloc[-1])[:19]]
        if "date" in df.columns and len(df) else None,
        "close_median": round(close_med, 6),
        "n_columns": len([c for c in df.columns if c != "date"]),
        "columns": cols,
        "notable_events": notable,
    }


def _notable_events(df: pd.DataFrame, k: int = 8) -> list:
    """Biggest forward price moves (date + magnitude) so the LLM knows where the
    action is even though per-bar values were compressed away."""
    if "close" not in df.columns or len(df) < 50:
        return []
    ret = df["close"].pct_change().fillna(0.0)
    win = max(4, len(df) // 1000)               # ~rolling window of moves
    roll = ret.rolling(win).sum()
    dates = pd.to_datetime(df["date"], errors="coerce")
    idx = roll.abs().sort_values(ascending=False).index[: k * 3]
    out, seen = [], []
    for i in idx:
        d = dates.iloc[i]
        if pd.isna(d):
            continue
        if any(abs((d - s).total_seconds()) < 86400 * 3 for s in seen):
            continue
        seen.append(d)
        out.append({"date": str(d)[:10], "move_pct": round(float(roll.iloc[i]) * 100, 2)})
        if len(out) >= k:
            break
    return out


# --------------------------------------------------------------------------- #
# 2. INDICATOR GLOSSARY  (col -> source function + plain purpose)
# --------------------------------------------------------------------------- #
# We map every dumped column back to the python function that produced it, so the
# LLM can read the REAL formula for a custom indicator (MATRIX etc.) on demand.

def _iter_py_files():
    for p in sorted(STRAT_DIR.glob("*.py")):
        if p.name == "__init__.py":
            continue
        yield p


def _returned_dict_keys(fn: ast.FunctionDef) -> list[str]:
    """Keys of any dict literal this function returns (column producers like
    calculate_matrix_indicators return {'MATRIX': ..., 'ROXY': ...})."""
    keys: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for kx in node.value.keys:
                if isinstance(kx, ast.Constant) and isinstance(kx.value, str):
                    keys.append(kx.value)
    return keys


def _assigned_df_cols(fn: ast.FunctionDef) -> list[str]:
    """Columns assigned via df['X'] = ... or dataframe['X'] = ... inside fn."""
    cols: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and isinstance(tgt.slice.value, str)):
                    cols.append(tgt.slice.value)
    return cols


def build_index() -> dict:
    """Parse every strategy .py: map column-name -> {file, function, lineno}.
    Best-effort & static (ast) so it's safe and import-free."""
    index: dict[str, dict] = {}
    funcs: dict[str, dict] = {}        # fully-qualified fn -> location
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            loc = {"file": path.name, "function": node.name,
                   "lineno": node.lineno, "end_lineno": getattr(node, "end_lineno", None)}
            funcs[f"{path.name}:{node.name}"] = loc
            produced = set(_returned_dict_keys(node)) | set(_assigned_df_cols(node))
            for col in produced:
                # first definer wins, but prefer a dict-returning producer fn
                if col not in index:
                    index[col] = loc
    return {"columns": index, "functions": funcs}


def get_source(file: str, lineno: int, end_lineno: int | None) -> str:
    path = STRAT_DIR / file
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return ""
    end = end_lineno or min(lineno + 120, len(lines))
    return "\n".join(lines[lineno - 1: end])


def resolve_indicator_source(name: str, index: dict) -> dict:
    """Map an indicator column -> its producing function source code."""
    cols = index.get("columns", {})
    loc = cols.get(name)
    if not loc:
        low = name.lower()
        for c, l in cols.items():
            if c.lower() == low:
                loc = l
                break
        if not loc:
            # strip common smoothing prefixes/suffixes (fast_, _1h, _raw)
            base = re.sub(r"^(fast_)|(_1h|_raw|_inv)+$", "", name)
            for c, l in cols.items():
                if c.lower() == base.lower():
                    loc = l
                    break
    if not loc:
        return {"ok": False, "error": f"no source mapping for '{name}'",
                "hint": "column may be a direct assignment in a strategy; try read_strategy"}
    src = get_source(loc["file"], loc["lineno"], loc.get("end_lineno"))
    return {"ok": True, "indicator": name, **loc, "source": src}


# --------------------------------------------------------------------------- #
# 3. SLICE  (small raw window — RAG, not full ingest)
# --------------------------------------------------------------------------- #
def slice_window(df: pd.DataFrame, start: str | None, end: str | None,
                 cols: list[str] | None, max_rows: int = 60) -> dict:
    """Return a SMALL window of raw rows for the columns the LLM asked for, so it
    can see actual behavior without ingesting the whole frame."""
    sub = df
    if start:
        sub = sub[sub["date"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        sub = sub[sub["date"] <= pd.Timestamp(end, tz="UTC")]
    sub = sub.reset_index(drop=True)
    if len(sub) > max_rows:                       # downsample evenly, keep ends
        idx = np.linspace(0, len(sub) - 1, max_rows).round().astype(int)
        sub = sub.iloc[np.unique(idx)].reset_index(drop=True)
    keep = ["date"] + [c for c in (cols or ["open", "high", "low", "close", "volume"])
                       if c in sub.columns]
    sub = sub[keep]
    rows = []
    for _, r in sub.iterrows():
        row = {"date": str(r["date"])[:19]}
        for c in keep[1:]:
            v = r[c]
            row[c] = (None if (isinstance(v, float) and not np.isfinite(v))
                      else round(float(v), 6) if isinstance(v, (int, float, np.floating)) else str(v))
        rows.append(row)
    return {"ok": True, "n": len(rows), "columns": keep, "rows": rows}


# --------------------------------------------------------------------------- #
# caching
# --------------------------------------------------------------------------- #
def profile_path(pair: str) -> Path:
    return CACHE_DIR / f"{pair}_profile.json"


INDEX_PATH = CACHE_DIR / "indicator_index.json"


def load_or_build_profile(pair: str, df_loader) -> dict:
    p = profile_path(pair)
    csv = PROJECT / "user_data" / "indicator_dumps" / f"{pair}_indicators.csv"
    if p.exists() and csv.exists() and p.stat().st_mtime >= csv.stat().st_mtime:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    prof = compute_profile(df_loader(pair))
    p.write_text(json.dumps(prof))
    return prof


def load_or_build_index() -> dict:
    newest = max((f.stat().st_mtime for f in _iter_py_files()), default=0)
    if INDEX_PATH.exists() and INDEX_PATH.stat().st_mtime >= newest:
        try:
            return json.loads(INDEX_PATH.read_text())
        except Exception:
            pass
    idx = build_index()
    INDEX_PATH.write_text(json.dumps(idx))
    return idx


# --------------------------------------------------------------------------- #
# compact one-line-per-column profile for always-on prompt grounding (~2.4k tok)
# --------------------------------------------------------------------------- #
def compact_profile_text(prof: dict, index: dict | None = None) -> str:
    """One terse line per column: name, kind, range, p50, corr-to-close, and the
    producing function. Cheap enough to inject every turn so the LLM is grounded
    in the real data shape + where each indicator's formula lives."""
    cols_idx = (index or {}).get("columns", {})
    lines = []
    for c, e in prof.get("columns", {}).items():
        if "min" in e:
            rng = f"[{e['min']:.4g},{e['max']:.4g}]"
            corr = e.get("corr_close", "-")
            head = f"{c}: {e['kind']} {rng} p50={e.get('p50')} corr={corr}"
        else:
            head = f"{c}: {e['kind']}"
        loc = cols_idx.get(c)
        if loc:
            head += f"  <{loc['file']}:{loc['function']}>"
        lines.append(head)
    ne = prof.get("notable_events") or []
    ev = ("\nNOTABLE MOVES: " +
          ", ".join(f"{x['date']}({x['move_pct']:+}%)" for x in ne)) if ne else ""
    return "\n".join(lines) + ev

