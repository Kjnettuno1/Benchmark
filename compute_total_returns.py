"""
compute_total_returns.py
------------------------
Computes EXACT total returns (dividends reinvested) for each market-portfolio
sleeve and writes them into a dedicated "LiveReturns" tab of your Google Sheet.

Why this is exact where GOOGLEFINANCE was not:
    GOOGLEFINANCE only returns PRICE (split-adjusted, dividends excluded), so
    total return had to be approximated with a manually-typed yield. Here we use
    ADJUSTED CLOSE, which already folds reinvested dividends and splits into the
    price series. Total return over any window is then simply:

        adj_close[end] / adj_close[start] - 1

    ...with no yield assumption at all.

Private-equity sleeve (replication approach):
    Rather than a listed-PE basket (PSP), which is mostly public-equity beta, the
    PE sleeve is REPLICATED with leveraged small-cap value -- the evidence-backed
    proxy for PE's economic drivers (small size + cheap value + modest leverage;
    see Stafford (HBS) and Chingono & Rasmussen / Verdad). Leverage is applied as
    "homemade leverage", net of a cash financing cost:

        sleeve_return = L * small_value_return - (L - 1) * cash_return

Produces two numbers per sleeve:
    * Trailing 1-year total return  -> feeds the Benchmark tab
    * Total return since BASE_DATE  -> feeds the Weight Drift tab

Runs headless in GitHub Actions. Edit the CONFIG block below in the web editor.
"""

from __future__ import annotations
import os
import sys
import json
import datetime as dt

import pandas as pd
import yfinance as yf
import gspread


# =====================================================================
# CONFIG  --  edit these; everything below is machinery
# =====================================================================

# The DLS "as-of" date your base weights correspond to.
# Rebased to end-2024 (Swinkels/Robeco Expected Returns 2026-2030, Sept 2025).
# When you refresh weights from a newer DLS vintage, change this to match.
BASE_DATE = dt.date(2024, 12, 31)

# Trailing window length in days for the 1-year figure.
TRAILING_DAYS = 365

# The worksheet (tab) this script writes into. It is created if missing.
WORKSHEET_NAME = "LiveReturns"

# Financing leg for the borrowed portion of any leveraged sleeve.
# SGOV = ~0-3m T-bills; its ADJUSTED close captures the T-bill yield as return.
# (BIL is an equivalent alternative with longer history.)
CASH_TICKER = "SGOV"

# Sleeves IN THE SAME ORDER as rows on your Benchmark tab (top to bottom).
# Each entry is (name, legs, leverage):
#   legs     = list of (ticker, weight) -- one leg, or several for a blend.
#              Leg weights are normalised, so they need not sum to exactly 1.
#   leverage = 1.0 for a normal sleeve; >1.0 applies homemade leverage net of
#              the CASH_TICKER financing cost.
#
# The PE sleeve is the replication proxy: small-cap value, modestly levered.
#   - Equity leg AVUV (Avantis US Small Cap Value) has deep size+value loadings.
#     Alternatives: DFSV, IJS, VBR. Blend them if you prefer not to single-source.
#   - Leverage 1.30 is a MODEST, editable assumption. Set to 1.0 for the
#     unlevered deep-small-value version; raise toward ~1.5 for a more
#     aggressive match to gross PE. This is the main knob to think about.
SLEEVES: list[tuple[str, list[tuple[str, float]], float]] = [
    ("Global Equities (incl. EM)",                    [("VT",   1.00)],                 1.00),
    ("Government Bonds (global)",                      [("GOVT", 0.40), ("IGOV", 0.60)], 1.00),  # US all-mat + ex-US
    ("Investment-Grade Credit (global)",              [("LQD",  1.00)],                 1.00),  # add IBND for global
    ("Real Estate (listed)",                          [("REET", 1.00)],                 1.00),
    ("Emerging-Market Debt",                          [("EMB",  1.00)],                 1.00),
    ("Inflation-Linked Bonds",                        [("TIP",  1.00)],                 1.00),  # add WIP for global
    ("High-Yield Bonds",                              [("HYG",  1.00)],                 1.00),  # add IHY/GHYG for global
    ("Private Equity (replication: lev. small-value)", [("AVUV", 1.00)],                1.30),
    ("Private Credit (BDC proxy)",                     [("BIZD", 1.00)],                1.00),  # BDCs already internally levered -> no homemade leverage
]

# Secrets supplied by GitHub Actions (see README_SETUP.md):
#   SHEET_ID   -> the long id from your sheet's URL
#   GCP_SA_KEY -> the full service-account JSON, pasted as a secret
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "").strip()


# =====================================================================
# Machinery
# =====================================================================

def unique_tickers() -> list[str]:
    seen, out = set(), []
    for _name, legs, _lev in SLEEVES:
        for tkr, _w in legs:
            if tkr not in seen:
                seen.add(tkr)
                out.append(tkr)
    if CASH_TICKER not in seen:
        out.append(CASH_TICKER)
    return out


def fetch_adj_close(tickers: list[str]) -> dict[str, pd.Series]:
    """Return {ticker: adjusted-close Series} from a bit before BASE_DATE to today.

    auto_adjust=True makes 'Close' the dividend+split-adjusted (total-return)
    series, which is exactly what we need.
    """
    start = (BASE_DATE - dt.timedelta(days=10)).isoformat()
    series: dict[str, pd.Series] = {}
    for tkr in tickers:
        try:
            df = yf.Ticker(tkr).history(start=start, auto_adjust=True)
            if df is None or df.empty or "Close" not in df.columns:
                print(f"  WARN {tkr}: no data returned", file=sys.stderr)
                continue
            s = df["Close"].dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None)
            series[tkr] = s
            print(f"  OK   {tkr}: {len(s)} rows, "
                  f"{s.index.min().date()} -> {s.index.max().date()}")
        except Exception as exc:  # noqa: BLE001 - log and continue per ticker
            print(f"  WARN {tkr}: {exc}", file=sys.stderr)
    return series


def price_on_or_before(s: pd.Series, target: dt.date):
    """Last available adjusted close on/before `target` (nearest prior trading day)."""
    sub = s.loc[: pd.Timestamp(target)]
    return float(sub.iloc[-1]) if len(sub) else None


def leg_returns(s: pd.Series) -> tuple[float | None, float | None]:
    """(trailing_1y_total_return, since_base_total_return) for one ticker."""
    if s is None or s.empty:
        return None, None
    end_date = s.index.max().date()
    end_px = float(s.iloc[-1])
    start_1y = price_on_or_before(s, end_date - dt.timedelta(days=TRAILING_DAYS))
    start_base = price_on_or_before(s, BASE_DATE)
    r_1y = (end_px / start_1y - 1.0) if start_1y else None
    r_base = (end_px / start_base - 1.0) if start_base else None
    return r_1y, r_base


def blend(legs: list[tuple[str, float]], series: dict[str, pd.Series]):
    """Weighted-average total return across a sleeve's legs, for both windows.

    A leg with missing data is dropped and remaining weights re-normalise;
    if all legs fail, returns (None, None, note).
    """
    tot_w = acc_1y = acc_base = 0.0
    ok_1y = ok_base = False
    dropped = []
    for tkr, w in legs:
        r1, rb = leg_returns(series.get(tkr))
        if r1 is None and rb is None:
            dropped.append(tkr)
            continue
        tot_w += w
        if r1 is not None:
            acc_1y += w * r1
            ok_1y = True
        if rb is not None:
            acc_base += w * rb
            ok_base = True
    if tot_w == 0:
        return None, None, "all legs failed: " + ",".join(dropped)
    note = ("dropped " + ",".join(dropped)) if dropped else ""
    return (acc_1y / tot_w if ok_1y else None,
            acc_base / tot_w if ok_base else None,
            note)


def apply_leverage(r, cash_r, L):
    """Homemade leverage net of financing: L*r - (L-1)*cash_r."""
    if r is None:
        return None
    if L == 1.0:
        return r
    if cash_r is None:
        return None  # cannot finance the borrowed portion without a cash return
    return L * r - (L - 1.0) * cash_r


def build_rows(series: dict[str, pd.Series]) -> list[list]:
    """Header + one row per sleeve, in Benchmark-tab order."""
    as_of = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    cash_1y, cash_base = leg_returns(series.get(CASH_TICKER))
    rows: list[list] = [["Sleeve", "Trailing_1Y_TR", "SinceBase_TR", "AsOf", "Note"]]
    for name, legs, lev in SLEEVES:
        r1, rb, note = blend(legs, series)
        if lev != 1.0:
            r1 = apply_leverage(r1, cash_1y, lev)
            rb = apply_leverage(rb, cash_base, lev)
            note = (note + " | " if note else "") + f"lev {lev:g}x, fin {CASH_TICKER}"
        rows.append([
            name,
            "" if r1 is None else round(r1, 6),
            "" if rb is None else round(rb, 6),
            as_of,
            note,
        ])
    return rows


def write_to_sheet(rows: list[list]) -> None:
    if not SHEET_ID or not GCP_SA_KEY:
        raise SystemExit("Missing SHEET_ID or GCP_SA_KEY environment variable.")
    gc = gspread.service_account_from_dict(json.loads(GCP_SA_KEY))
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=20, cols=6)
    ws.clear()
    ws.update(range_name="A1", values=rows, value_input_option="RAW")
    print(f"Wrote {len(rows) - 1} sleeves to '{WORKSHEET_NAME}'.")


def write_csv(rows: list[list], path: str = "returns.csv") -> None:
    """Local audit copy, uploaded by the workflow as an artifact."""
    pd.DataFrame(rows[1:], columns=rows[0]).to_csv(path, index=False)
    print(f"Wrote audit copy: {path}")


def main() -> None:
    print(f"Base date: {BASE_DATE} | trailing: {TRAILING_DAYS}d | cash: {CASH_TICKER}")
    tickers = unique_tickers()
    print(f"Fetching adjusted close for: {', '.join(tickers)}")
    series = fetch_adj_close(tickers)
    rows = build_rows(series)

    print("\nComputed total returns:")
    for row in rows[1:]:
        print(f"  {row[0]:46s} 1Y={row[1]!s:>9}  base={row[2]!s:>9}  {row[4]}")

    write_csv(rows)
    write_to_sheet(rows)
    print("\nDone.")


if __name__ == "__main__":
    main()
