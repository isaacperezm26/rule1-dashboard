# Rule #1 Investing Dashboard

A single-file Python web app that runs a full **Phil Town "Rule #1"** stock analysis on any ticker using live data from Yahoo Finance. Type a symbol, and it pulls the financials, scores the business, runs three valuation models, checks the technical timing, and gives you a final verdict — all in your browser.

![Rule #1 Dashboard screenshot](docs/screenshot.png)

> **Disclaimer:** This is an educational tool, not financial advice. Do your own research before making any investment decision.

## What it shows

- **The Big 5 growth rates** — Revenue, EPS, Book Value (Equity), Operating Cash Flow, and Free Cash Flow over the available history.
- **Moat & financial health scores** — ROIC, ROE, debt-to-equity, current ratio, profit/operating margins, and interest coverage, distilled into 1–10 scores.
- **Three valuation models:**
  - **10-Cap** — owner-earnings yield (intrinsic value = owner earnings × 10).
  - **Sticker Price** — a 10-year DCF with a 50% Margin of Safety price.
  - **Payback Time** — how many years of free cash flow it takes to recoup the market cap.
- **Technical timing** — 50/200-day moving averages, golden/death cross, 52-week range, and volume trend.
- **A final verdict** — Strong Buy / Buy / Watchlist / Avoid, with a confidence level.

## Run it locally

Requires **Python 3.8+**.

```bash
git clone https://github.com/isaacperezm26/rule1-dashboard.git
cd rule1-dashboard
pip install -r requirements.txt
python rule1_dashboard.py
```

The server starts on **http://localhost:8080** and opens your browser automatically. Enter any ticker (e.g. `AAPL`, `GOOGL`, `AMZN`) in the search bar.

Press `Ctrl+C` to stop.

## How it works

The whole app is one file, `rule1_dashboard.py`, built on Python's standard-library HTTP server — no web framework. It:

1. Serves a single-page dashboard at `/`.
2. Fetches financials on demand from Yahoo Finance (via [`yfinance`](https://github.com/ranaroussi/yfinance)) at `/api/analyze?ticker=…`.
3. Computes the Rule #1 metrics and returns them as JSON, which the page renders.

On first run it will auto-install any missing dependencies.

## Deploying

A `Procfile` is included (`web: python rule1_dashboard.py`) for Heroku/Railway-style platforms, and the app reads the `PORT` environment variable when present.

> **Note:** Yahoo Finance frequently rate-limits or blocks requests coming from cloud/data-center IPs, so a hosted instance may fail to fetch data even when it works perfectly on your machine. Running locally is the most reliable option.

## License

[MIT](LICENSE) © Isaac Perez
