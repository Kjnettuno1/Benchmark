# Exact total returns via GitHub Action

This replaces the flawed `price + typed-yield` estimate with **exact** total
returns computed from **adjusted close** (dividends reinvested). The Action runs
in the cloud on a schedule, computes each sleeve's trailing-1-year and
since-base total return, and writes them into a `LiveReturns` tab of your Google
Sheet. Your existing Benchmark and Weight Drift tabs stay exactly as you have
them — you just point two columns at the new tab.

## Files

```
compute_total_returns.py            # the script (edit CONFIG at the top)
requirements.txt                    # yfinance, pandas, gspread
.github/workflows/total_returns.yml # schedule + manual trigger
```

Commit these to a repo (the GitHub web editor is fine — no local Python needed).

## One-time setup

**1. Service account (lets the Action write to your sheet).**
- In Google Cloud Console: create a project, enable the **Google Sheets API**,
  create a **Service Account**, and add a **JSON key**. Download the JSON.
- Copy the service account's email (looks like
  `something@your-project.iam.gserviceaccount.com`).

**2. Share the sheet.**
- Open your benchmark Google Sheet → Share → paste the service-account email →
  give it **Editor** → Send. (This is what authorises the write.)

**3. GitHub secrets** (repo → Settings → Secrets and variables → Actions → New):
- `GCP_SA_KEY` — paste the **entire** contents of the JSON key file.
- `SHEET_ID` — the long id in your sheet URL between `/d/` and `/edit`:
  `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`.

**4. First run.**
- Actions tab → *Market-Portfolio Total Returns* → **Run workflow**.
- It creates the `LiveReturns` tab and fills it. Check the run log for the
  computed numbers; an audit `returns.csv` is attached as an artifact.

## Wire the sheet to it (once)

The Action writes `LiveReturns` in the **same sleeve order** as your Benchmark
rows:

| LiveReturns | A (Sleeve) | B (Trailing_1Y_TR) | C (SinceBase_TR) |
|---|---|---|---|
| row 2 | Global Equities | … | … |
| row 3 | Government Bonds | … | … |
| … | … | … | … |
| row 9 | Private Equity | … | … |

**Benchmark tab** — set the *Total Return (used)* cell for each sleeve to the
trailing-1Y value (rows 10–17 map to LiveReturns rows 2–9):

```
H10  =IFERROR(LiveReturns!B2, "")
H11  =IFERROR(LiveReturns!B3, "")
...                              (down to)
H17  =IFERROR(LiveReturns!B9, "")
```

**Weight Drift tab** — set the *Cum. Total Return since base* cell for each
sleeve (rows 8–15 map to LiveReturns rows 2–9):

```
F8   =IFERROR(LiveReturns!C2, "")
F9   =IFERROR(LiveReturns!C3, "")
...                              (down to)
F15  =IFERROR(LiveReturns!C9, "")
```

That's it. Once wired, the `TTM Yield` column on the Benchmark tab and the
`(1+yield)^years` term on the Drift tab are no longer used — the numbers are now
exact. You can hide those helper columns.

## Keeping it correct

- **Refreshing weights:** when a newer DLS update publishes, paste the new
  weights into the Benchmark tab and change `BASE_DATE` in the script to the new
  as-of date. The since-base returns recompute against the new anchor.
- **Blends:** the government sleeve is already a US + ex-US blend
  (`GOVT` + `IGOV`, 40/60). Make IG credit, TIPS, or high yield global the same
  way — add an ex-US leg with a weight in the `SLEEVES` config.
- **Staleness check:** the `AsOf` timestamp is written to `LiveReturns` col D.
  Surface it on your Benchmark tab if you want a visible "last updated".

## Notes

- **Data source:** `yfinance` with `auto_adjust=True` gives dividend+split
  adjusted close (i.e. total-return prices) for free. If a ticker occasionally
  returns no data, just re-run the workflow. To use your existing **Twelve Data**
  key instead, replace `fetch_adj_close()` with a Twelve Data `time_series` call
  — but confirm your plan returns **dividend-adjusted** prices, or you're back to
  price-only.
- **Proxy vs index:** these are ETF total returns (fees + tracking error vs the
  raw index — single-digit bp to a few tenths). The raw proprietary indices need
  a licensed feed.
- Not investment advice.
