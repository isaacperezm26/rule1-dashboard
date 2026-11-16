# Rule #1 Dashboard audit — handoff / resume plan

_Working notes so this audit can be resumed in a later session. Not part of the app; delete when the audit is merged._

## Where things stand

- Branch: `claude/dashboard-audit-corrections-ad3oyc`
- All fixes live in `rule1_dashboard.py`; offline tests in `tests/test_analysis.py` (`python -m unittest discover -s tests -v`).
- **Nothing is pushed yet.** `git push` is refused with HTTP 403: Claude's GitHub access to `isaacperezm26/rule1-dashboard` is not connected. Fix: reconnect GitHub at https://claude.ai/connect-github (and install the Claude GitHub App on the repo), then `git push -u origin claude/dashboard-audit-corrections-ad3oyc`.
- A personal cloud environment named `rule1-dashboard` now allows the Yahoo Finance hosts and the Chart.js CDN, so live data works in sessions that use it.

## Done (committed locally unless marked)

Valuation
- Sticker Price: analyst growth now comes from `growth_estimates` instead of `earningsGrowth` (which is quarterly YoY); historical growth prefers book value/share, falls back to EPS; future P/E = min(2 × growth, median historical P/E); no Sticker Price for negative EPS or no positive growth (was: silent 10% default / negative prices).
- Ten Cap: owner earnings = net income + D&A + Δreceivables + Δpayables + tax − capex (was: OCF − 30% of capex).
- Payback Time: FCF grows at the capped valuation growth rate (was: uncapped FCF CAGR); FCF row shown (was hard-coded N/A); 8-year payback price added.

Data
- All statements aligned on one period timeline; empty stub years dropped.
- EPS / BVPS use each year's share count; share count = market cap ÷ price (fixes GOOGL multi-class, ADRs, BRK-B).
- Foreign-currency statements converted to the listing currency; pence-quoted (GBp) stocks handled.
- CAGR uses real time span; undefined for non-positive endpoints.
- Dividend yield no longer 100× too high (AAPL showed 36%).
- ROIC = NOPAT ÷ invested capital, per year; debt-free companies no longer penalised.
- Margins / ROE from the same fiscal year as the table.
- Unadjusted closes for moving averages; 52-week range from day highs/lows.
- Throttled Yahoo responses (empty quote, no exception) are retried, then returned as a labelled partial result that is not cached.

Scores / verdict
- Removed hard-coded 7/10 Meaning and Management scores.
- Moat score = Big 5 vs the 10% target; missing metrics excluded, not failed.
- Unknown tickers and ETFs return an error instead of an AVOID verdict.

UI / server
- 50/200-day MA lines actually drawn; stale values cleared between tickers; out-of-order responses ignored; currency-aware formatting; ROIC row; notes box; disclaimer.
- Threaded server, ticker validation, proper HTTP status codes, binds 127.0.0.1 locally, wildcard CORS header removed.

## Found on live data and fixed

1. Analyst growth: Yahoo's long-term (`LTG`) stock estimate is blank for every ticker checked, so the code now uses the annualised two-year forward estimate (`0y` and `+1y` rows). Growth is strictly the lower of history and analysts; no Sticker Price when that is not positive.
2. Pence-quoted stocks (VOD.L, SHEL.L): EPS and dividends arrive in pounds while the price is in pence; both are restated.
3. GOOGL Ten Cap fell back to "OCF − capex" because its depreciation sits on a different line; the line is now chosen per year.
4. Banks / insurers (JPM): a note explains that the cash-flow models don't fit.
5. Throttled Yahoo responses (empty quote, no exception) are retried, then returned as a labelled, uncached partial result.
6. `docs/screenshot.png` regenerated from a real AAPL run.

Live sweep checked: AAPL, MSFT, NVDA, GOOGL, AMZN, TSLA, TSM, BRK-B, JPM, RIVN, VOD.L, SHEL.L, SPY (rejected as an ETF), and a nonexistent ticker (rejected).

## Still to do

1. **Push the branch** once GitHub access is fixed (see above). All work is committed locally only.
2. Delete this file after the branch is pushed / merged.
3. Optional follow-ups, not done: Yahoo only provides about four fiscal years, so growth rates are 3-year, not the 10-year record Rule #1 prefers; interest expense is often missing from Yahoo (AAPL shows N/A for interest coverage); `trailingEps` units for pence-quoted loss-makers can't be cross-checked against a P/E.

## How to verify live

```bash
python3 rule1_dashboard.py      # then open http://localhost:8080
python -m unittest discover -s tests -v
```
