# Rule #1 Dashboard audit — handoff / resume plan

_Working notes so this audit can be resumed in a later session. Not part of the app; delete when the audit is merged._

## Where things stand

- Branch: `claude/dashboard-audit-corrections-ad3oyc`, **pushed to GitHub** and a clean fast-forward from `main`. Not yet merged; no pull request was opened.
- All fixes live in `rule1_dashboard.py`; offline tests in `tests/test_analysis.py` (48 tests, `python -m unittest discover -s tests -v`). `README.md` describes the corrected methods, `docs/screenshot.png` is a live AAPL run, and `.gitignore` was added.
- Verified with Python 3.13, yfinance 1.8.0, pandas 3.0.5.
- A personal cloud environment named `rule1-dashboard` allows the Yahoo Finance hosts and the Chart.js CDN, so live data works in sessions that use it. Its setup script (`pip install -r requirements.txt`) has not yet been exercised by a fresh session.

## Done

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

## Added after the audit

- Verdict now uses all three valuation methods (previously Sticker Price only), with a pass/fail chip per method in the banner. Offline tests: 48.

## Found on live data and fixed

1. Analyst growth: Yahoo's long-term (`LTG`) stock estimate is blank for every ticker checked, so the code now uses the annualised two-year forward estimate (`0y` and `+1y` rows). Growth is strictly the lower of history and analysts; no Sticker Price when that is not positive.
2. Pence-quoted stocks (VOD.L, SHEL.L): EPS and dividends arrive in pounds while the price is in pence; both are restated.
3. GOOGL Ten Cap fell back to "OCF − capex" because its depreciation sits on a different line; the line is now chosen per year.
4. Banks / insurers (JPM): a note explains that the cash-flow models don't fit.
5. Throttled Yahoo responses (empty quote, no exception) are retried, then returned as a labelled, uncached partial result.
6. `docs/screenshot.png` regenerated from a real AAPL run.

Live sweep checked: AAPL, MSFT, NVDA, GOOGL, AMZN, TSLA, TSM, BRK-B, JPM, RIVN, VOD.L, SHEL.L, SPY (rejected as an ETF), and a nonexistent ticker (rejected).

## Judgment calls made (change these if you disagree)

These are readings of Rule #1, not bug fixes. Each is a small, isolated piece of `analyze()`; the constants are at the top of the file (`MARR`, `MOS`, `MAX_GROWTH`, `MAX_FUTURE_PE`, `BIG5_TARGET`, `PAYBACK_TARGET`).

- **Historical growth** uses book value per share first, and EPS only when book value isn't growing (buyback-heavy companies).
- **Growth is strictly the lower of history and analysts**, capped at 15%. If the lower one is zero or negative there is no Sticker Price at all.
- **Analyst growth** is the annualised two-year forward estimate, because Yahoo's long-term figure is blank.
- **Ten Cap treats all capex as maintenance capex** (Yahoo doesn't split it), which is the conservative choice and lowers the value for heavy investors such as GOOGL and AMZN.
- **The verdict combines all three methods** (`decide_verdict()`): the Margin of Safety price is the buy test and 7+ quality scores gate BUY / STRONG BUY. Ten Cap and Payback Time raise or lower confidence, a disagreement holds back a STRONG BUY, and they lift an above-Sticker stock to WATCHLIST when both pass, or when one passes and the price is within 20% of Sticker (`NEAR_MISS`). They never produce a buy alone; a method with no result counts neither way; no Sticker Price always means AVOID / WAIT.
- **Scores** give 2 / 1 / 0 points per metric using the same good / warn / bad bands the page colours by; a 7+ counts as passing.
- **Meaning and Management are unscored** rather than estimated.

## Looked at and deliberately left alone

- Technical timing uses 50/200-day moving averages. Rule #1 itself uses MACD, stochastics and a 10-day average; switching would be a redesign, not a correction.
- "Golden cross" is reported as a state (50-day above 200-day), not as the crossing event; the label now says so.
- The single-file, no-framework design and the auto-install of dependencies on first run.

## Known limitations

- **Short history.** Yahoo supplies about four fiscal years, so growth rates are 3-year and the historical P/E is a median of about four points. Rule #1 prefers a 10-year record.
- **Currency conversion uses today's exchange rate for every year.** Growth rates are unaffected, but older years' converted amounts are not what they were worth at the time.
- **Share-count restatement is a heuristic.** A jump of more than 1.5× between years, or a statement count far from market cap ÷ price, is treated as a split or ADR ratio. A company that genuinely issued that many shares would be misread.
- **Interest expense is often missing** from Yahoo (AAPL shows N/A for interest coverage); it is left out of the score rather than guessed.
- **Earnings-based valuation doesn't suit every company**: banks and insurers (noted on the page), and holding companies like BRK-B whose EPS swings with investment gains.
- **Pence-quoted loss-makers:** trailing EPS units can't be cross-checked against a P/E, so pounds are assumed.
- **Partial results:** if Yahoo is still throttling after three tries, the page shows a labelled partial analysis without analyst estimates or trailing EPS.

## Still to do

1. **Merge to `main`** (fast-forward):
   `git fetch origin && git checkout main && git merge origin/claude/dashboard-audit-corrections-ad3oyc && git push origin main`
2. Delete this file once merged (`git rm AUDIT_HANDOFF.md`).
3. Optional, not started: a longer history source for 10-year growth; Rule #1's own technical indicators.

## How to verify live

```bash
python -m unittest discover -s tests -v
python3 rule1_dashboard.py      # then open http://localhost:8080
```

The tests replace Yahoo with in-memory fixtures (`FakeTicker` in `tests/test_analysis.py`), so they run offline. For a live check, try AAPL, GOOGL (multi-class shares), TSM (ADR, TWD statements), VOD.L (pence), JPM (bank), RIVN (losses), SPY and a made-up ticker (both should be rejected). The page loads Chart.js from cdn.jsdelivr.net, so the chart needs internet access.
