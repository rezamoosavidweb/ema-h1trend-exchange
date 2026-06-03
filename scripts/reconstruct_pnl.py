#!/usr/bin/env python3
"""
Reconstruct the *true* realised PnL / win-rate for the live bot.

Why this exists
---------------
The bot's per-symbol `position_closed` events were written from a flaky
secondary REST poll (`get_closed_pnl`) that sometimes returned empty, leaving
`exit_price=NaN, pnl=0`. Roughly a third of closes — including real wins — were
silently logged as break-even, so any win-rate computed from the local logs is
understated.

This script ignores those local PnL values and rebuilds the truth from two
authoritative sources:

  1. Bybit's closed-PnL REST endpoint  (read-only; covers historical trades)
  2. The new _ws_events-*.jsonl journal (real-time realisedPnl, going forward)

It then prints a win-rate report and writes a tidy CSV
(logs/bybit_bot/_reconstructed_pnl.csv).

Usage
-----
    python scripts/reconstruct_pnl.py                 # last 7 days from Bybit
    python scripts/reconstruct_pnl.py --days 4
    python scripts/reconstruct_pnl.py --start 2026-05-30 --end 2026-06-04
    python scripts/reconstruct_pnl.py --source ws     # only the WS journal
    python scripts/reconstruct_pnl.py --source rest    # only Bybit REST (default both)

Read-only: this script never places, amends, or cancels any order.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

LOG_DIR = _REPO_ROOT / "logs" / "bybit_bot"
OUT_CSV = LOG_DIR / "_reconstructed_pnl.csv"


# ── Source 1: Bybit closed-PnL REST (authoritative for history) ────────────────

async def fetch_rest_closes(start_ms: int, end_ms: int) -> list[dict]:
    """
    Page through Bybit's closed-PnL endpoint for the window. Bybit caps each
    request window at 7 days, so callers must keep (end-start) <= 7 days.
    Returns a list of normalised trade dicts.
    """
    from exchange.bybit_client import BybitClient

    api_key    = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet    = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
    demo       = os.getenv("BYBIT_DEMO", "false").lower() == "true"
    if not api_key:
        raise SystemExit("BYBIT_API_KEY missing — set it in .env to use the REST source.")

    client = BybitClient(api_key=api_key, api_secret=api_secret,
                         testnet=testnet, demo=demo)
    mode = "DEMO" if demo else ("TESTNET" if testnet else "LIVE")
    print(f"[rest] querying Bybit closed-PnL  mode={mode}  "
          f"{_ms(start_ms)} -> {_ms(end_ms)}")

    out: list[dict] = []
    cursor = ""
    for _ in range(50):  # hard page cap, ~2500 trades
        kwargs = dict(category="linear", limit=100,
                      startTime=str(start_ms), endTime=str(end_ms))
        if cursor:
            kwargs["cursor"] = cursor
        resp = await client._call("get_closed_pnl", **kwargs)
        result = resp.get("result", {}) or {}
        for r in result.get("list", []) or []:
            out.append(_norm_rest(r))
        cursor = result.get("nextPageCursor") or ""
        if not cursor:
            break
    print(f"[rest] fetched {len(out)} closed-PnL records")
    return out


def _norm_rest(r: dict) -> dict:
    pnl = _f(r.get("closedPnl"))
    return {
        "source":   "rest",
        "symbol":   r.get("symbol", ""),
        "side":     r.get("side", ""),            # side of the *closing* order
        "qty":      _f(r.get("closedSize")),
        "entry":    _f(r.get("avgEntryPrice")),
        "exit":     _f(r.get("avgExitPrice")),
        "pnl":      pnl,
        "closed_at": _ms(r.get("updatedTime")),
        "closed_ms": int(r.get("updatedTime", 0) or 0),
    }


# ── Source 2: WS event journal (authoritative going forward) ───────────────────

def load_ws_closes() -> list[dict]:
    """
    Extract realised-PnL closes from _ws_events-*.jsonl. A position record with
    size==0 carries the close's realisedPnl.
    """
    out: list[dict] = []
    for fp in sorted(glob.glob(str(LOG_DIR / "_ws_events-*.jsonl"))):
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "position" not in (msg.get("topic") or ""):
                continue
            for pos in msg.get("data", []) or []:
                if _f(pos.get("size")) != 0:
                    continue
                rpnl = pos.get("realisedPnl") or pos.get("cumRealisedPnl")
                if rpnl in (None, ""):
                    continue
                out.append({
                    "source":    "ws",
                    "symbol":    pos.get("symbol", ""),
                    "side":      pos.get("side", ""),
                    "qty":       _f(pos.get("size")),
                    "entry":     _f(pos.get("avgPrice")),
                    "exit":      float("nan"),
                    "pnl":       _f(rpnl),
                    "closed_at": msg.get("received_ts", ""),
                    "closed_ms": 0,
                })
    if out:
        print(f"[ws]   extracted {len(out)} closes from _ws_events journal")
    else:
        print("[ws]   no _ws_events journal yet (populates after bot restart)")
    return out


# ── Local position_closed log (for the discrepancy report only) ────────────────

def load_local_closes() -> list[dict]:
    out: list[dict] = []
    for fp in sorted(glob.glob(str(LOG_DIR / "*USDT-*.jsonl"))):
        for line in open(fp, encoding="utf-8"):
            if '"position_closed"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("event") != "position_closed":
                continue
            ex = d.get("exit_price")
            out.append({
                "symbol":    d.get("symbol", ""),
                "pnl":       _f(d.get("pnl_usdt")),
                "missing":   ex is None or (isinstance(ex, float) and ex != ex),
                "closed_at": d.get("closed_at", ""),
            })
    return out


# ── Reporting ──────────────────────────────────────────────────────────────────

def report(trades: list[dict], local: list[dict]) -> None:
    trades = sorted(trades, key=lambda t: t.get("closed_at", ""))
    n = len(trades)
    if not n:
        print("\nNo authoritative trades found in the selected window/source.")
        return

    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), sum(losses)

    print("\n" + "=" * 64)
    print("TRUE PERFORMANCE  (authoritative: Bybit closed-PnL / WS realisedPnl)")
    print("=" * 64)
    print(f"Trades        : {n}")
    print(f"Win rate      : {100*len(wins)/n:.1f}%   ({len(wins)} W / {len(losses)} L)")
    print(f"Net PnL       : {sum(pnls):+.2f} USDT")
    print(f"Gross profit  : {gp:+.2f}   |  Gross loss: {gl:+.2f}")
    if gl != 0:
        print(f"Profit factor : {gp/abs(gl):.2f}")
    if wins:
        print(f"Avg win       : {gp/len(wins):+.2f}   |  Max win : {max(wins):+.2f}")
    if losses:
        print(f"Avg loss      : {gl/len(losses):+.2f}   |  Max loss: {min(losses):+.2f}")

    print("\n--- by day ---")
    byday = defaultdict(lambda: [0, 0, 0.0])
    for t in trades:
        day = (t.get("closed_at") or "")[:10] or "unknown"
        byday[day][0] += 1
        byday[day][1] += 1 if t["pnl"] > 0 else 0
        byday[day][2] += t["pnl"]
    for day in sorted(byday):
        c, w, p = byday[day]
        print(f"{day}: trades={c:2d}  wins={w:2d}  wr={100*w/c:5.1f}%  pnl={p:+8.2f}")

    print("\n--- by symbol ---")
    bysym = defaultdict(lambda: [0, 0, 0.0])
    for t in trades:
        s = t["symbol"]
        bysym[s][0] += 1
        bysym[s][1] += 1 if t["pnl"] > 0 else 0
        bysym[s][2] += t["pnl"]
    for s in sorted(bysym, key=lambda x: bysym[x][2]):
        c, w, p = bysym[s]
        print(f"{s:14s}: trades={c:2d}  wins={w:2d}  wr={100*w/c:5.1f}%  pnl={p:+8.2f}")

    # Discrepancy vs the local log the previous analysis trusted
    if local:
        local_net = sum(t["pnl"] for t in local)
        missing   = sum(1 for t in local if t["missing"])
        print("\n--- vs local position_closed log (the understated source) ---")
        print(f"local records      : {len(local)}  "
              f"(of which {missing} had exit_price=NaN / pnl=0)")
        print(f"local net PnL      : {local_net:+.2f} USDT")
        print(f"true  net PnL      : {sum(pnls):+.2f} USDT")
        print(f"PnL hidden by bug  : {sum(pnls)-local_net:+.2f} USDT")

    # CSV
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source", "closed_at", "symbol", "side",
                                          "qty", "entry", "exit", "pnl"])
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k, "") for k in w.fieldnames})
    print(f"\nWrote {n} trades → {OUT_CSV}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _ms(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)\
                       .strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


async def amain(args) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

    trades: list[dict] = []

    if args.source in ("rest", "both"):
        if args.start and args.end:
            start = _parse_day(args.start)
            end   = _parse_day(args.end)
        else:
            end   = datetime.now(timezone.utc)
            start = end - timedelta(days=args.days)
        # Bybit caps each request at 7 days; chunk if needed.
        chunk = timedelta(days=7)
        cur = start
        while cur < end:
            seg_end = min(cur + chunk, end)
            trades += await fetch_rest_closes(int(cur.timestamp() * 1000),
                                              int(seg_end.timestamp() * 1000))
            cur = seg_end

    if args.source in ("ws", "both"):
        trades += load_ws_closes()

    # De-dup if both sources overlap (same symbol + closed_ms + pnl)
    seen, uniq = set(), []
    for t in trades:
        key = (t["symbol"], t.get("closed_ms") or t.get("closed_at"), round(t["pnl"], 4))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)

    report(uniq, load_local_closes())


def main() -> None:
    # Windows consoles often default to a legacy codepage; force UTF-8 so the
    # report (and any unicode in symbols) prints cleanly.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="Lookback window (default 7).")
    p.add_argument("--start", help="Start date YYYY-MM-DD (UTC).")
    p.add_argument("--end",   help="End date YYYY-MM-DD (UTC).")
    p.add_argument("--source", choices=["rest", "ws", "both"], default="both",
                   help="Where to read authoritative PnL from (default both).")
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
