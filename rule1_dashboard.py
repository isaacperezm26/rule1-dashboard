#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║   RULE #1 INTERACTIVE DASHBOARD — Yahoo Finance      ║
║   Run:  python3 rule1_dashboard.py                   ║
║   Open: http://localhost:8080                        ║
╚══════════════════════════════════════════════════════╝
"""

import sys, os, json, math, time, threading, webbrowser
import subprocess, importlib, warnings
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Older yfinance talks to Yahoo with plain `requests`, which Yahoo quickly
# throttles with "Too Many Requests" (HTTP 429). 0.2.60+ uses curl_cffi to
# impersonate a real Chrome browser, which is what stops the rate limiting.
MIN_YF = (0, 2, 60)

def ensure(pkg, name=None):
    try:
        importlib.import_module(name or pkg)
    except ImportError:
        print(f"  Installing {pkg}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

ensure("pandas")
ensure("numpy")

def ensure_yfinance():
    """Import yfinance, upgrading it first if it's too old to dodge rate limits."""
    try:
        import yfinance as yf
        ver = tuple(int(p) for p in yf.__version__.split(".")[:3])
        if ver >= MIN_YF:
            return yf
    except Exception:
        pass
    print("  Updating yfinance for reliable Yahoo Finance access…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yfinance>=1.2.0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # If an old yfinance was already imported, re-exec so the new one loads clean.
    if "yfinance" in sys.modules:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    import yfinance as yf
    return yf

warnings.filterwarnings("ignore")
yf = ensure_yfinance()
import pandas as pd
import numpy as np

PORT = int(os.environ.get("PORT", 8080))

# ─────────────────────────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────────────────────────

def to_f(val, default=None):
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return default

def safe(d, key, default=None):
    val = d.get(key, default) if isinstance(d, dict) else default
    return default if val is None else to_f(val, val) if isinstance(val, (int, float)) else val

def clean_for_json(obj):
    """Recursively replace NaN/Inf with None so the result is valid JSON.

    Python's default json.dumps emits the literal tokens ``NaN`` / ``Infinity``
    for these floats, which JavaScript's JSON.parse rejects. yfinance often
    returns NaN for missing price/volume points, so we sanitize the whole
    response payload before serializing.
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_for_json(v) for v in obj]
    return obj

def cagr(values):
    v = [x for x in values if x is not None and x > 0]
    if len(v) < 2:
        return None
    years = len(v) - 1
    try:
        r = (v[-1] / v[0]) ** (1 / years) - 1
        return None if (math.isnan(r) or math.isinf(r)) else r
    except Exception:
        return None

def get_row(df, *keys):
    for key in keys:
        if key in df.index:
            vals = df.loc[key].sort_index()  # oldest first
            return [to_f(v) for v in vals.values]
    return []

# ─────────────────────────────────────────────────────────────────
#  CORE ANALYSIS
# ─────────────────────────────────────────────────────────────────

def analyze(sym):
    tk = yf.Ticker(sym)
    info = tk.info or {}

    # ── Basic info ──────────────────────────────────────────────
    name     = info.get("longName") or info.get("shortName") or sym
    price    = to_f(info.get("currentPrice") or info.get("regularMarketPrice"))
    prev_cls = to_f(info.get("previousClose"))
    mktcap   = to_f(info.get("marketCap"))
    shares   = to_f(info.get("sharesOutstanding"))
    sector   = info.get("sector", "N/A")
    industry = info.get("industry", "N/A")
    desc_raw = info.get("longBusinessSummary", "")
    desc     = (desc_raw[:500] + "...") if len(desc_raw) > 500 else desc_raw

    price_chg     = (price - prev_cls) if (price and prev_cls) else None
    price_chg_pct = (price_chg / prev_cls) if (price_chg and prev_cls) else None

    # ── Financial statements ────────────────────────────────────
    try:
        inc = tk.income_stmt
        bal = tk.balance_sheet
        cf  = tk.cash_flow
    except Exception:
        inc = bal = cf = pd.DataFrame()

    revenues   = get_row(inc, "Total Revenue")
    net_inc    = get_row(inc, "Net Income")
    op_inc     = get_row(inc, "Operating Income", "EBIT")
    int_exp    = get_row(inc, "Interest Expense")
    equity     = get_row(bal, "Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity")
    total_debt = get_row(bal, "Total Debt", "Long Term Debt")
    curr_ast   = get_row(bal, "Current Assets", "Total Current Assets")
    curr_lib   = get_row(bal, "Current Liabilities", "Total Current Liabilities")
    ocf        = get_row(cf,  "Operating Cash Flow", "Total Cash From Operating Activities")
    capex_raw  = get_row(cf,  "Capital Expenditure", "Capital Expenditures")

    # FCF = OCF - |CapEx|
    fcf = []
    for i in range(min(len(ocf), len(capex_raw))):
        o, c = ocf[i], capex_raw[i]
        fcf.append((o - abs(c)) if (o is not None and c is not None) else None)

    # Per-share series
    eps_vals = [(ni / shares) if (ni and shares and shares > 0) else None for ni in net_inc]
    bvps     = [(eq / shares) if (eq and shares and shares > 0) else None for eq in equity]

    # Year labels (oldest first)
    year_labels = []
    if not inc.empty:
        year_labels = [str(d.year) for d in sorted(inc.columns)]

    # Most-recent values
    rev_now  = revenues[-1]   if revenues   else None
    ni_now   = net_inc[-1]    if net_inc    else None
    ocf_now  = ocf[-1]        if ocf        else None
    fcf_now  = fcf[-1]        if fcf        else None
    eq_now   = equity[-1]     if equity     else None
    dbt_now  = total_debt[-1] if total_debt else None
    ca_now   = curr_ast[-1]   if curr_ast   else None
    cl_now   = curr_lib[-1]   if curr_lib   else None
    oi_now   = op_inc[-1]     if op_inc     else None
    ie_now   = int_exp[-1]    if int_exp    else None
    cx_now   = capex_raw[-1]  if capex_raw  else None

    # ── Financial ratios ────────────────────────────────────────
    roic = None
    if ni_now and eq_now and dbt_now:
        ic = eq_now + dbt_now
        roic = ni_now / ic if ic > 0 else None

    roe              = to_f(info.get("returnOnEquity"))
    de_ratio         = (dbt_now / eq_now) if (dbt_now and eq_now and eq_now > 0) else None
    current_ratio    = (ca_now / cl_now)  if (ca_now and cl_now and cl_now > 0) else None
    profit_margin    = to_f(info.get("profitMargins"))
    operating_margin = to_f(info.get("operatingMargins"))
    fcf_margin       = (fcf_now / rev_now) if (fcf_now and rev_now and rev_now > 0) else None
    int_coverage     = (oi_now / abs(ie_now)) if (oi_now and ie_now and ie_now != 0) else None

    # ── Growth rates ────────────────────────────────────────────
    gr = {
        "revenue":    cagr(revenues),
        "net_income": cagr(net_inc),
        "eps":        cagr([v for v in eps_vals if v and v > 0]),
        "bvps":       cagr([v for v in bvps if v and v > 0]),
        "ocf":        cagr(ocf),
        "fcf":        cagr(fcf),
    }

    # ── Valuation ───────────────────────────────────────────────
    maint_capex     = abs(cx_now) * 0.30 if cx_now else 0
    owner_earnings  = (ocf_now - maint_capex) if ocf_now else None
    ten_cap_val     = owner_earnings * 10 if owner_earnings else None
    ten_cap_ps      = (ten_cap_val / shares) if (ten_cap_val and shares) else None

    curr_eps        = to_f(info.get("trailingEps"))
    pe              = to_f(info.get("trailingPE"))
    analyst_growth  = to_f(info.get("earningsGrowth"))
    hist_eps_growth = cagr([v for v in eps_vals if v and v > 0])

    candidates = [g for g in [hist_eps_growth, analyst_growth] if g is not None and g > 0]
    growth_used = min(min(candidates), 0.15) if candidates else 0.10

    future_eps    = (curr_eps * (1 + growth_used) ** 10) if curr_eps else None
    future_pe     = min(2 * growth_used * 100, 50)
    future_price  = (future_eps * future_pe) if future_eps else None
    sticker_price = (future_price / (1.15 ** 10)) if future_price else None
    mos_price     = (sticker_price * 0.5) if sticker_price else None

    # Payback time
    payback_yrs = None
    if mktcap and fcf_now and fcf_now > 0:
        fcf_g   = max(gr["fcf"] or 0, 0)
        running = 0
        cur_fcf = fcf_now
        for yr in range(1, 31):
            running += cur_fcf
            if running >= mktcap:
                payback_yrs = yr
                break
            cur_fcf *= (1 + fcf_g)

    # ── Technical ───────────────────────────────────────────────
    try:
        hist     = tk.history(period="1y")
        closes   = hist["Close"].round(2).tolist()
        volumes  = hist["Volume"].tolist()
        dt_strs  = [str(d.date()) for d in hist.index]
        ma50     = to_f(hist["Close"].rolling(50).mean().iloc[-1]) if len(hist) >= 50 else None
        ma200    = to_f(hist["Close"].rolling(200).mean().iloc[-1]) if len(hist) >= 200 else None
        high52   = to_f(hist["Close"].max())
        low52    = to_f(hist["Close"].min())
        recent_v = float(hist["Volume"].tail(20).mean())
        avg_v    = float(hist["Volume"].mean())
        vol_flag = recent_v > avg_v * 1.10

        signals = []
        if price and ma50:   signals.append("above_50ma"   if price > ma50  else "below_50ma")
        if price and ma200:  signals.append("above_200ma"  if price > ma200 else "below_200ma")
        if ma50 and ma200:   signals.append("golden_cross" if ma50 > ma200  else "death_cross")
        if vol_flag:         signals.append("rising_volume")
    except Exception:
        closes = volumes = dt_strs = signals = []
        ma50 = ma200 = high52 = low52 = None

    # ── Scores ──────────────────────────────────────────────────
    def score_metric(val, thresholds):
        """thresholds: list of (value, score) tuples, ascending"""
        if val is None: return 5
        for threshold, sc in thresholds:
            if val >= threshold: return sc
        return thresholds[0][1]

    moat_sc = round(min(10, max(1,
        (3 if roic and roic >= 0.10 else 1) +
        (2 if gr["revenue"] and gr["revenue"] >= 0.05 else 1) +
        (2 if gr["fcf"] and gr["fcf"] >= 0.05 else 1) +
        (2 if gr["eps"] and gr["eps"] >= 0.05 else 1) +
        (1 if roe and roe >= 0.15 else 0)
    )))

    fin_sc = round(min(10, max(1,
        (2 if roic and roic >= 0.10 else 0) +
        (2 if de_ratio is not None and de_ratio < 1.0 else 0) +
        (2 if current_ratio and current_ratio >= 1.5 else 1 if current_ratio and current_ratio >= 1.0 else 0) +
        (2 if profit_margin and profit_margin >= 0.10 else 1 if profit_margin and profit_margin >= 0.05 else 0) +
        (2 if int_coverage and int_coverage >= 5 else 1 if int_coverage and int_coverage >= 2 else 0)
    )))

    meaning_sc = 7  # placeholder — qualitative

    overall = round((moat_sc + fin_sc + meaning_sc) / 3)

    # ── Verdict ─────────────────────────────────────────────────
    is_undervalued = price and mos_price and price <= mos_price
    is_strong_fin  = fin_sc >= 7
    is_good_moat   = moat_sc >= 7

    if is_undervalued and is_strong_fin and is_good_moat:
        verdict, confidence = "STRONG BUY", "High"
    elif is_undervalued and (is_strong_fin or is_good_moat):
        verdict, confidence = "BUY", "Medium"
    elif price and sticker_price and price <= sticker_price:
        verdict, confidence = "WATCHLIST", "Medium"
    else:
        verdict, confidence = "AVOID / WAIT", "Medium"

    # ── Return ──────────────────────────────────────────────────
    return {
        "info": {
            "name": name, "ticker": sym, "price": price,
            "price_change": price_chg, "price_change_pct": price_chg_pct,
            "market_cap": mktcap, "sector": sector, "industry": industry,
            "description": desc, "pe_ratio": pe, "eps": curr_eps,
            "dividend_yield": to_f(info.get("dividendYield")),
            "beta": to_f(info.get("beta")),
            "52w_high": to_f(info.get("fiftyTwoWeekHigh")) or high52,
            "52w_low":  to_f(info.get("fiftyTwoWeekLow"))  or low52,
        },
        "big5": {
            "year_labels": year_labels,
            "revenues": revenues, "net_incomes": net_inc,
            "eps_values": eps_vals, "bvps": bvps,
            "ocf": ocf, "fcf": fcf,
            "growth_rates": gr,
        },
        "financials": {
            "roic": roic, "roe": roe, "de_ratio": de_ratio,
            "current_ratio": current_ratio, "profit_margin": profit_margin,
            "fcf_margin": fcf_margin, "operating_margin": operating_margin,
            "interest_coverage": int_coverage,
        },
        "valuation": {
            "owner_earnings": owner_earnings,
            "ten_cap_value": ten_cap_val, "ten_cap_per_share": ten_cap_ps,
            "market_cap": mktcap,
            "sticker_price": sticker_price, "mos_price": mos_price,
            "payback_years": payback_yrs,
            "growth_rate_used": growth_used,
            "future_eps": future_eps, "future_pe": future_pe,
            "future_price": future_price, "current_eps": curr_eps,
        },
        "technical": {
            "ma50": ma50, "ma200": ma200, "signals": signals,
            "prices": closes, "volumes": volumes, "dates": dt_strs,
            "52w_high": high52, "52w_low": low52,
        },
        "scores": {
            "meaning": meaning_sc, "moat": moat_sc,
            "management": 7, "financial": fin_sc, "overall": overall,
        },
        "verdict": {"verdict": verdict, "confidence": confidence},
    }

# ─────────────────────────────────────────────────────────────────
#  HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rule #1 Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:      #0b0f1a;
    --card:    #111827;
    --border:  #1f2937;
    --text:    #e5e7eb;
    --muted:   #6b7280;
    --green:   #10b981;
    --red:     #ef4444;
    --yellow:  #f59e0b;
    --blue:    #3b82f6;
    --purple:  #8b5cf6;
    --gold:    #fbbf24;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }

  /* ── Header ── */
  header {
    background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
    display: flex; align-items: center; gap: 24px; flex-wrap: wrap;
  }
  .logo { font-size: 22px; font-weight: 800; color: var(--gold); letter-spacing: -0.5px; white-space: nowrap; }
  .logo span { color: var(--text); font-weight: 400; }
  .search-wrap { display: flex; gap: 10px; flex: 1; max-width: 480px; }
  .search-wrap input {
    flex: 1; background: #1f2937; border: 1px solid #374151; border-radius: 8px;
    padding: 10px 16px; color: var(--text); font-size: 15px; outline: none;
    text-transform: uppercase;
  }
  .search-wrap input:focus { border-color: var(--blue); }
  .search-wrap input::placeholder { text-transform: none; color: var(--muted); }
  .btn {
    background: var(--blue); color: white; border: none; border-radius: 8px;
    padding: 10px 22px; font-size: 14px; font-weight: 600; cursor: pointer;
    transition: background 0.2s;
  }
  .btn:hover { background: #2563eb; }
  .btn:disabled { background: #374151; cursor: not-allowed; }
  .examples { color: var(--muted); font-size: 13px; }
  .examples button {
    background: none; border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 3px 10px; cursor: pointer; font-size: 12px; margin: 2px;
  }
  .examples button:hover { border-color: var(--blue); color: var(--blue); }

  /* ── Main ── */
  main { max-width: 1280px; margin: 0 auto; padding: 28px 24px; }

  /* ── State screens ── */
  #welcome, #loading, #error {
    text-align: center; padding: 80px 20px; color: var(--muted);
  }
  #welcome h2 { font-size: 28px; color: var(--text); margin-bottom: 12px; }
  #welcome p  { font-size: 15px; max-width: 500px; margin: 0 auto; }
  .spinner {
    width: 48px; height: 48px; border: 4px solid var(--border);
    border-top-color: var(--blue); border-radius: 50%;
    animation: spin 0.8s linear infinite; margin: 0 auto 20px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #error { color: var(--red); }

  /* ── Company hero ── */
  #hero {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 24px 28px; margin-bottom: 20px;
    display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start;
  }
  .hero-left { flex: 1; min-width: 200px; }
  .company-name { font-size: 26px; font-weight: 800; margin-bottom: 4px; }
  .company-meta { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
  .price-row { display: flex; align-items: baseline; gap: 12px; }
  .price-big { font-size: 36px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .price-change { font-size: 15px; font-weight: 600; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .hero-right { display: flex; gap: 32px; flex-wrap: wrap; }
  .stat-mini { text-align: right; }
  .stat-mini .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-mini .val { font-size: 16px; font-weight: 600; margin-top: 2px; }
  .desc-row { flex-basis: 100%; color: var(--muted); font-size: 13px; line-height: 1.6; border-top: 1px solid var(--border); padding-top: 14px; }

  /* ── Verdict banner ── */
  #verdict-banner {
    border-radius: 12px; padding: 18px 24px; margin-bottom: 20px;
    display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  }
  .verdict-label { font-size: 28px; font-weight: 900; letter-spacing: -0.5px; }
  .verdict-sub { font-size: 13px; opacity: 0.8; }
  .verdict-prices { display: flex; gap: 28px; margin-left: auto; flex-wrap: wrap; }
  .vp-item .vp-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; opacity: 0.7; }
  .vp-item .vp-val   { font-size: 18px; font-weight: 700; margin-top: 2px; }

  /* ── Grid ── */
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 20px; }
  .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
  @media (max-width: 900px) { .grid2, .grid3, .grid4 { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 600px) { .grid2, .grid3, .grid4 { grid-template-columns: 1fr; } }

  /* ── Cards ── */
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 24px;
  }
  .card-title {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--muted); margin-bottom: 16px; font-weight: 600;
  }
  .metric-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .metric-row:last-child { border-bottom: none; }
  .metric-name { font-size: 13px; color: var(--muted); }
  .metric-val  { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .metric-val.good   { color: var(--green); }
  .metric-val.bad    { color: var(--red); }
  .metric-val.warn   { color: var(--yellow); }
  .metric-val.neutral { color: var(--text); }

  /* ── Score ring ── */
  .score-card { text-align: center; }
  .score-ring { position: relative; width: 80px; height: 80px; margin: 0 auto 10px; }
  .score-ring svg { transform: rotate(-90deg); }
  .score-num {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    font-size: 22px; font-weight: 800;
  }
  .score-name { font-size: 12px; color: var(--muted); font-weight: 500; }

  /* ── Big 5 table ── */
  .big5-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .big5-table th {
    background: #1f2937; color: var(--muted); text-align: right;
    padding: 8px 10px; font-weight: 500; font-size: 11px;
  }
  .big5-table th:first-child { text-align: left; }
  .big5-table td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border); }
  .big5-table td:first-child { text-align: left; color: var(--muted); font-weight: 500; }
  .big5-table tr:last-child td { border-bottom: none; }
  .cagr-badge {
    display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600;
  }
  .cagr-green { background: rgba(16,185,129,0.15); color: var(--green); }
  .cagr-red   { background: rgba(239,68,68,0.15);  color: var(--red); }
  .cagr-grey  { background: rgba(107,114,128,0.15); color: var(--muted); }

  /* ── Chart ── */
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; margin-bottom: 20px; }
  .chart-wrap { position: relative; height: 260px; }

  /* ── Technical signals ── */
  .signal-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .signal {
    padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;
  }
  .sig-green  { background: rgba(16,185,129,0.15); color: var(--green); border: 1px solid rgba(16,185,129,0.3); }
  .sig-red    { background: rgba(239,68,68,0.15);  color: var(--red);   border: 1px solid rgba(239,68,68,0.3); }
  .sig-yellow { background: rgba(245,158,11,0.15); color: var(--yellow); border: 1px solid rgba(245,158,11,0.3); }

  /* ── Section title ── */
  .section-title { font-size: 18px; font-weight: 700; margin: 28px 0 14px; color: var(--gold); }
  .section-title span { color: var(--muted); font-size: 13px; font-weight: 400; margin-left: 8px; }

  /* ── Progress bar ── */
  .prog-bar { height: 6px; background: #1f2937; border-radius: 3px; margin-top: 6px; }
  .prog-fill { height: 100%; border-radius: 3px; }

  /* ── MA lines in chart ── */
  .ma-legend { display: flex; gap: 16px; margin-bottom: 10px; font-size: 12px; }
  .ma-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 5px; }

  #result { display: none; }
</style>
</head>
<body>

<header>
  <div class="logo">Rule <span>#</span>1 <span style="color:var(--gold)">▸</span> Dashboard</div>
  <div class="search-wrap">
    <input id="tickerInput" type="text" placeholder="Enter ticker (e.g. AAPL)" maxlength="10" />
    <button class="btn" id="analyzeBtn" onclick="runAnalysis()">Analyze</button>
  </div>
  <div class="examples">
    Quick:
    <button onclick="quickTicker('AAPL')">AAPL</button>
    <button onclick="quickTicker('MSFT')">MSFT</button>
    <button onclick="quickTicker('NVDA')">NVDA</button>
    <button onclick="quickTicker('GOOGL')">GOOGL</button>
    <button onclick="quickTicker('AMZN')">AMZN</button>
    <button onclick="quickTicker('TSLA')">TSLA</button>
  </div>
</header>

<main>
  <div id="welcome">
    <h2>Rule #1 Investing Dashboard</h2>
    <p>Enter any stock ticker above to get a full Phil Town Rule #1 analysis — Big 5 growth rates, intrinsic value, financial health, technical timing, and a final verdict.</p>
  </div>

  <div id="loading" style="display:none">
    <div class="spinner"></div>
    <div id="loadingTicker" style="font-size:20px; color:var(--text); font-weight:700; margin-bottom:8px;"></div>
    <div style="color:var(--muted)">Fetching live data from Yahoo Finance…</div>
  </div>

  <div id="error" style="display:none">
    <div style="font-size:48px; margin-bottom:16px;">⚠️</div>
    <div id="errorMsg" style="font-size:16px;"></div>
  </div>

  <div id="result">

    <!-- Hero -->
    <div id="hero">
      <div class="hero-left">
        <div class="company-name" id="companyName"></div>
        <div class="company-meta" id="companyMeta"></div>
        <div class="price-row">
          <div class="price-big" id="priceDisplay"></div>
          <div class="price-change" id="priceChange"></div>
        </div>
      </div>
      <div class="hero-right">
        <div class="stat-mini"><div class="label">Market Cap</div><div class="val" id="heroMktcap"></div></div>
        <div class="stat-mini"><div class="label">P/E Ratio</div><div class="val" id="heroPE"></div></div>
        <div class="stat-mini"><div class="label">52w High</div><div class="val" id="hero52H"></div></div>
        <div class="stat-mini"><div class="label">52w Low</div><div class="val" id="hero52L"></div></div>
        <div class="stat-mini"><div class="label">Beta</div><div class="val" id="heroBeta"></div></div>
        <div class="stat-mini"><div class="label">Div. Yield</div><div class="val" id="heroDivYield"></div></div>
      </div>
      <div class="desc-row" id="companyDesc"></div>
    </div>

    <!-- Verdict -->
    <div id="verdict-banner">
      <div>
        <div class="verdict-label" id="verdictLabel"></div>
        <div class="verdict-sub" id="verdictSub"></div>
      </div>
      <div class="verdict-prices">
        <div class="vp-item"><div class="vp-label">Current Price</div><div class="vp-val" id="vpCurrent"></div></div>
        <div class="vp-item"><div class="vp-label">Sticker Price</div><div class="vp-val" id="vpSticker"></div></div>
        <div class="vp-item"><div class="vp-label">MOS Buy Price</div><div class="vp-val" id="vpMOS"></div></div>
        <div class="vp-item"><div class="vp-label">Upside / Downside</div><div class="vp-val" id="vpUpside"></div></div>
      </div>
    </div>

    <!-- Scores -->
    <div class="section-title">Scores <span>Rule #1 Framework</span></div>
    <div class="grid4" id="scoresGrid"></div>

    <!-- Price Chart -->
    <div class="section-title">Price History <span>1 Year — Daily</span></div>
    <div class="chart-card">
      <div class="ma-legend">
        <span><span class="ma-dot" style="background:#3b82f6"></span>Price</span>
        <span><span class="ma-dot" style="background:#f59e0b"></span>50-day MA</span>
        <span><span class="ma-dot" style="background:#8b5cf6"></span>200-day MA</span>
        <span id="maValues" style="margin-left:auto; color:var(--muted); font-size:11px;"></span>
      </div>
      <div class="chart-wrap"><canvas id="priceChart"></canvas></div>
    </div>

    <!-- Big 5 -->
    <div class="section-title">Big 5 Numbers <span>Historical growth trends</span></div>
    <div class="card" style="margin-bottom:20px; overflow-x:auto;">
      <table class="big5-table" id="big5Table"></table>
    </div>

    <!-- Financials -->
    <div class="section-title">Financial Health</div>
    <div class="grid2" id="finGrid"></div>

    <!-- Valuation -->
    <div class="section-title">Intrinsic Value Analysis</div>
    <div class="grid3" id="valGrid"></div>

    <!-- Technical -->
    <div class="section-title">Technical Analysis</div>
    <div class="card" style="margin-bottom:32px;">
      <div class="card-title">Signals & Timing</div>
      <div class="grid2">
        <div>
          <div class="metric-row"><span class="metric-name">50-Day Moving Average</span><span class="metric-val" id="tMA50"></span></div>
          <div class="metric-row"><span class="metric-name">200-Day Moving Average</span><span class="metric-val" id="tMA200"></span></div>
          <div class="metric-row"><span class="metric-name">Price vs 50MA</span><span class="metric-val" id="tVs50"></span></div>
          <div class="metric-row"><span class="metric-name">Price vs 200MA</span><span class="metric-val" id="tVs200"></span></div>
        </div>
        <div>
          <div class="card-title">Active Signals</div>
          <div class="signal-list" id="signalList"></div>
        </div>
      </div>
    </div>

  </div><!-- #result -->
</main>

<script>
// ── Helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function fmt(n, decimals=2) {
  if (n == null) return 'N/A';
  return n.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}
function fmtP(n) { return n != null ? '$' + fmt(n) : 'N/A'; }
function fmtPct(n) { return n != null ? (n*100).toFixed(1) + '%' : 'N/A'; }
function fmtB(n) {
  if (n == null) return 'N/A';
  if (Math.abs(n) >= 1e12) return '$' + (n/1e12).toFixed(2) + 'T';
  if (Math.abs(n) >= 1e9)  return '$' + (n/1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6)  return '$' + (n/1e6).toFixed(2) + 'M';
  return '$' + fmt(n);
}
function fmtNum(n) {
  if (n == null) return 'N/A';
  if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(2) + 'M';
  return fmt(n, 0);
}
function cls(val, goodAbove, warnAbove) {
  if (val == null) return 'neutral';
  if (val >= goodAbove) return 'good';
  if (val >= warnAbove) return 'warn';
  return 'bad';
}
function clsInv(val, goodBelow, warnBelow) {
  if (val == null) return 'neutral';
  if (val <= goodBelow) return 'good';
  if (val <= warnBelow) return 'warn';
  return 'bad';
}

// ── Chart state ───────────────────────────────────────────────────────────────
let priceChartInst = null;

// ── Main analysis ─────────────────────────────────────────────────────────────
async function runAnalysis(ticker) {
  ticker = (ticker || $('tickerInput').value).trim().toUpperCase();
  if (!ticker) return;

  $('tickerInput').value = ticker;
  $('welcome').style.display   = 'none';
  $('error').style.display     = 'none';
  $('result').style.display    = 'none';
  $('loading').style.display   = 'block';
  $('loadingTicker').textContent = ticker;
  $('analyzeBtn').disabled = true;

  try {
    const res  = await fetch(`/api/analyze?ticker=${encodeURIComponent(ticker)}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    $('loading').style.display = 'none';
    $('result').style.display  = 'block';

    renderAll(data);
  } catch(e) {
    $('loading').style.display = 'none';
    $('error').style.display   = 'block';
    $('errorMsg').textContent  = `Could not load data for "${ticker}": ${e.message}`;
  } finally {
    $('analyzeBtn').disabled = false;
  }
}

function quickTicker(t) {
  $('tickerInput').value = t;
  runAnalysis(t);
}

$('tickerInput').addEventListener('keydown', e => { if(e.key === 'Enter') runAnalysis(); });

// ── Render ────────────────────────────────────────────────────────────────────
function renderAll(d) {
  renderHero(d.info);
  renderVerdict(d.verdict, d.info, d.valuation);
  renderScores(d.scores);
  renderChart(d.technical, d.info.price);
  renderBig5(d.big5);
  renderFinancials(d.financials);
  renderValuation(d.valuation, d.info);
  renderTechnical(d.technical, d.info.price);
}

function renderHero(info) {
  $('companyName').textContent = `${info.name} (${info.ticker})`;
  $('companyMeta').textContent = `${info.sector}  •  ${info.industry}`;
  $('priceDisplay').textContent = info.price != null ? `$${fmt(info.price)}` : 'N/A';

  if (info.price_change != null) {
    const sign = info.price_change >= 0 ? '+' : '';
    const pct  = info.price_change_pct != null ? ` (${sign}${fmtPct(info.price_change_pct)})` : '';
    const el   = $('priceChange');
    el.textContent = `${sign}$${fmt(info.price_change)}${pct}`;
    el.className   = 'price-change ' + (info.price_change >= 0 ? 'pos' : 'neg');
  }

  $('heroMktcap').textContent  = fmtB(info.market_cap);
  $('heroPE').textContent      = info.pe_ratio != null ? fmt(info.pe_ratio, 1) + 'x' : 'N/A';
  $('hero52H').textContent     = fmtP(info['52w_high']);
  $('hero52L').textContent     = fmtP(info['52w_low']);
  $('heroBeta').textContent    = info.beta != null ? fmt(info.beta, 2) : 'N/A';
  $('heroDivYield').textContent = info.dividend_yield != null ? fmtPct(info.dividend_yield) : '—';
  $('companyDesc').textContent = info.description;
}

function renderVerdict(verdict, info, val) {
  const v = verdict.verdict;
  const colors = {
    'STRONG BUY': ['#052e16', '#10b981'],
    'BUY':        ['#022c22', '#34d399'],
    'WATCHLIST':  ['#1c1917', '#f59e0b'],
    'AVOID / WAIT': ['#1f0a0a', '#ef4444'],
  };
  const [bg, fg] = colors[v] || ['#1f2937', '#e5e7eb'];
  const banner = $('verdict-banner');
  banner.style.background = bg;
  banner.style.border     = `1px solid ${fg}33`;

  $('verdictLabel').textContent = v;
  $('verdictLabel').style.color = fg;
  $('verdictSub').textContent   = `Confidence: ${verdict.confidence}`;

  $('vpCurrent').textContent = fmtP(info.price);
  $('vpSticker').textContent = fmtP(val.sticker_price);
  $('vpMOS').textContent     = fmtP(val.mos_price);

  if (info.price && val.sticker_price) {
    const upside = (val.sticker_price - info.price) / info.price;
    const el = $('vpUpside');
    el.textContent = (upside >= 0 ? '+' : '') + fmtPct(upside);
    el.style.color = upside >= 0 ? '#10b981' : '#ef4444';
  } else {
    $('vpUpside').textContent = 'N/A';
  }
}

function renderScores(scores) {
  const items = [
    { key: 'meaning',    label: 'Meaning'    },
    { key: 'moat',       label: 'Moat'       },
    { key: 'management', label: 'Management' },
    { key: 'financial',  label: 'Financial Health' },
  ];
  const grid = $('scoresGrid');
  grid.innerHTML = '';
  items.forEach(({ key, label }) => {
    const val = scores[key] || 5;
    const color = val >= 7 ? '#10b981' : val >= 5 ? '#f59e0b' : '#ef4444';
    const pct   = val / 10;
    const r = 30, circ = 2 * Math.PI * r;
    grid.innerHTML += `
      <div class="card score-card">
        <div class="score-ring">
          <svg viewBox="0 0 80 80" width="80" height="80">
            <circle cx="40" cy="40" r="${r}" fill="none" stroke="#1f2937" stroke-width="7"/>
            <circle cx="40" cy="40" r="${r}" fill="none" stroke="${color}" stroke-width="7"
              stroke-dasharray="${circ}" stroke-dashoffset="${circ * (1 - pct)}"
              stroke-linecap="round"/>
          </svg>
          <div class="score-num" style="color:${color}">${val}</div>
        </div>
        <div class="score-name">${label}</div>
      </div>`;
  });
}

function renderChart(tech, currentPrice) {
  const ctx = document.getElementById('priceChart').getContext('2d');
  if (priceChartInst) { priceChartInst.destroy(); priceChartInst = null; }

  if (!tech.prices || tech.prices.length === 0) {
    $('maValues').textContent = 'Price data unavailable';
    return;
  }

  // MA arrays aligned to dates
  const len  = tech.prices.length;
  const ma50  = new Array(len).fill(null);
  const ma200 = new Array(len).fill(null);
  if (tech.ma50)  ma50[len-1]  = tech.ma50;
  if (tech.ma200) ma200[len-1] = tech.ma200;

  // Update legend
  const parts = [];
  if (tech.ma50)  parts.push(`50MA: $${tech.ma50.toFixed(2)}`);
  if (tech.ma200) parts.push(`200MA: $${tech.ma200.toFixed(2)}`);
  $('maValues').textContent = parts.join('  •  ');

  // Subsample to 120 points for performance
  const stride = Math.max(1, Math.floor(len / 120));
  const idxs = [];
  for (let i = 0; i < len; i += stride) idxs.push(i);
  if (idxs[idxs.length-1] !== len-1) idxs.push(len-1);

  const labels = idxs.map(i => tech.dates[i]);
  const prices = idxs.map(i => tech.prices[i]);

  const gradient = ctx.createLinearGradient(0, 0, 0, 260);
  gradient.addColorStop(0, 'rgba(59,130,246,0.3)');
  gradient.addColorStop(1, 'rgba(59,130,246,0)');

  priceChartInst = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Price',
          data: prices,
          borderColor: '#3b82f6',
          backgroundColor: gradient,
          borderWidth: 2,
          pointRadius: 0,
          fill: true,
          tension: 0.3,
        },
        tech.ma50 ? {
          label: '50 MA',
          data: idxs.map(i => i === len-1 ? tech.ma50 : null),
          borderColor: '#f59e0b',
          borderWidth: 1.5,
          borderDash: [4,3],
          pointRadius: 0,
          fill: false,
        } : null,
        tech.ma200 ? {
          label: '200 MA',
          data: idxs.map(i => i === len-1 ? tech.ma200 : null),
          borderColor: '#8b5cf6',
          borderWidth: 1.5,
          borderDash: [4,3],
          pointRadius: 0,
          fill: false,
        } : null,
      ].filter(Boolean),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1f2937',
          borderColor: '#374151',
          borderWidth: 1,
          callbacks: {
            label: ctx => ` $${ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) : 'N/A'}`
          }
        }
      },
      scales: {
        x: { grid: { color: '#1f2937' }, ticks: { color: '#6b7280', maxTicksLimit: 8, font: { size: 11 } } },
        y: { grid: { color: '#1f2937' }, ticks: { color: '#6b7280', font: { size: 11 }, callback: v => '$'+v.toFixed(0) } },
      }
    }
  });
}

function renderBig5(b5) {
  const labels = b5.year_labels;
  const gr = b5.growth_rates;

  function row(label, values, fmt_fn, cagrVal) {
    const badge = cagrVal != null
      ? `<span class="cagr-badge ${cagrVal >= 0.05 ? 'cagr-green' : cagrVal >= 0 ? 'cagr-grey' : 'cagr-red'}">${cagrVal >= 0 ? '+' : ''}${(cagrVal*100).toFixed(1)}% CAGR</span>`
      : `<span class="cagr-badge cagr-grey">N/A</span>`;
    const cells = values.map(v => `<td style="color:${v == null ? '#4b5563' : '#e5e7eb'}">${fmt_fn(v)}</td>`).join('');
    return `<tr><td>${label} <small style="color:var(--muted)">${badge}</small></td>${cells}</tr>`;
  }

  const yearHeaders = labels.map(y => `<th>${y}</th>`).join('');
  const fmtB2 = v => v == null ? '—' : (Math.abs(v) >= 1e9 ? (v/1e9).toFixed(1)+'B' : Math.abs(v) >= 1e6 ? (v/1e6).toFixed(1)+'M' : v.toFixed(0));
  const fmtD  = v => v == null ? '—' : '$' + v.toFixed(2);

  $('big5Table').innerHTML = `
    <thead><tr><th>Metric</th>${yearHeaders}</tr></thead>
    <tbody>
      ${row('Revenue',        b5.revenues,   fmtB2, gr.revenue)}
      ${row('Net Income',     b5.net_incomes,fmtB2, gr.net_income)}
      ${row('EPS',            b5.eps_values, fmtD,  gr.eps)}
      ${row('Book Value/Sh',  b5.bvps,       fmtD,  gr.bvps)}
      ${row('Op. Cash Flow',  b5.ocf,        fmtB2, gr.ocf)}
      ${row('Free Cash Flow', b5.fcf,        fmtB2, gr.fcf)}
    </tbody>`;
}

function renderFinancials(fin) {
  const rows1 = [
    { label: 'ROIC',             val: fin.roic,             fmt: fmtPct, goodA: 0.10, warnA: 0.05 },
    { label: 'ROE',              val: fin.roe,              fmt: fmtPct, goodA: 0.15, warnA: 0.10 },
    { label: 'Operating Margin', val: fin.operating_margin, fmt: fmtPct, goodA: 0.15, warnA: 0.05 },
    { label: 'Profit Margin',    val: fin.profit_margin,    fmt: fmtPct, goodA: 0.10, warnA: 0.05 },
    { label: 'FCF Margin',       val: fin.fcf_margin,       fmt: fmtPct, goodA: 0.10, warnA: 0.05 },
  ];
  const rows2 = [
    { label: 'Debt / Equity',    val: fin.de_ratio,       fmt: v => fmt(v,2)+'x', inv: true, goodA: 0.5, warnA: 1.5 },
    { label: 'Current Ratio',    val: fin.current_ratio,  fmt: v => fmt(v,2)+'x', goodA: 1.5, warnA: 1.0 },
    { label: 'Interest Coverage',val: fin.interest_coverage, fmt: v => fmt(v,1)+'x', goodA: 5, warnA: 2 },
  ];

  function makeCard(rows, title) {
    const inner = rows.map(r => {
      const c = r.inv
        ? clsInv(r.val, r.goodA, r.warnA)
        : cls(r.val, r.goodA, r.warnA);
      return `<div class="metric-row">
        <span class="metric-name">${r.label}</span>
        <span class="metric-val ${c}">${r.val != null ? r.fmt(r.val) : 'N/A'}</span>
      </div>`;
    }).join('');
    return `<div class="card"><div class="card-title">${title}</div>${inner}</div>`;
  }
  $('finGrid').innerHTML = makeCard(rows1, 'Profitability') + makeCard(rows2, 'Balance Sheet & Safety');
}

function renderValuation(val, info) {
  const price = info.price;

  function valCard(title, lines, highlight) {
    const inner = lines.map(([k, v]) => `
      <div class="metric-row">
        <span class="metric-name">${k}</span>
        <span class="metric-val ${v && v.cls ? v.cls : 'neutral'}">${v && v.text ? v.text : (v || 'N/A')}</span>
      </div>`).join('');
    return `<div class="card"><div class="card-title">${title}</div>${inner}</div>`;
  }

  const tenCapClass = (val.ten_cap_per_share && price)
    ? (price <= val.ten_cap_per_share ? 'good' : 'bad') : 'neutral';
  const stickerClass = (val.sticker_price && price)
    ? (price <= val.sticker_price ? 'good' : price <= val.sticker_price * 1.5 ? 'warn' : 'bad') : 'neutral';
  const paybackClass = val.payback_years
    ? (val.payback_years <= 8 ? 'good' : val.payback_years <= 15 ? 'warn' : 'bad') : 'neutral';

  $('valGrid').innerHTML = `
    ${valCard('10-Cap (Owner Earnings)', [
      ['Owner Earnings',    fmtB(val.owner_earnings)],
      ['× 10',             '= Intrinsic Value'],
      ['10-Cap (Total)',    fmtB(val.ten_cap_value)],
      ['10-Cap / Share',   { text: fmtP(val.ten_cap_per_share), cls: tenCapClass }],
      ['Market Cap',       fmtB(val.market_cap)],
      ['Assessment',       { text: (val.ten_cap_per_share && price) ? (price <= val.ten_cap_per_share ? '✅ Undervalued' : '❌ Overvalued') : 'N/A', cls: tenCapClass }],
    ])}
    ${valCard('Sticker Price (DCF)', [
      ['Current EPS',      fmtP(val.current_eps)],
      ['Growth Rate Used', fmtPct(val.growth_rate_used)],
      ['Future EPS (10y)', fmtP(val.future_eps)],
      ['Future PE',        val.future_pe != null ? val.future_pe.toFixed(1)+'x' : 'N/A'],
      ['Sticker Price',    { text: fmtP(val.sticker_price), cls: stickerClass }],
      ['MOS Price (50%)',  { text: fmtP(val.mos_price), cls: (val.mos_price && price && price <= val.mos_price ? 'good' : 'neutral') }],
    ])}
    ${valCard('Payback Time (FCF)', [
      ['Free Cash Flow',   fmtB(val.market_cap ? null : null)],
      ['Market Cap',       fmtB(val.market_cap)],
      ['Years to Payback', { text: val.payback_years != null ? val.payback_years + ' years' : 'N/A', cls: paybackClass }],
      ['Target',           '≤ 8 years'],
      ['Assessment',       val.payback_years != null
        ? { text: val.payback_years <= 8 ? '✅ Passes' : val.payback_years <= 15 ? '⚠️ Marginal' : '❌ Too Long', cls: paybackClass }
        : 'N/A'],
    ])}`;
}

function renderTechnical(tech, price) {
  const ma50Text  = tech.ma50  ? `$${tech.ma50.toFixed(2)}`  : 'N/A';
  const ma200Text = tech.ma200 ? `$${tech.ma200.toFixed(2)}` : 'N/A';
  $('tMA50').textContent  = ma50Text;
  $('tMA200').textContent = ma200Text;

  if (price && tech.ma50) {
    const pct = ((price - tech.ma50) / tech.ma50 * 100).toFixed(1);
    $('tVs50').textContent  = (pct >= 0 ? '+' : '') + pct + '%';
    $('tVs50').className    = 'metric-val ' + (price > tech.ma50 ? 'good' : 'bad');
  } else { $('tVs50').textContent = 'N/A'; $('tVs50').className = 'metric-val neutral'; }

  if (price && tech.ma200) {
    const pct = ((price - tech.ma200) / tech.ma200 * 100).toFixed(1);
    $('tVs200').textContent = (pct >= 0 ? '+' : '') + pct + '%';
    $('tVs200').className   = 'metric-val ' + (price > tech.ma200 ? 'good' : 'bad');
  } else { $('tVs200').textContent = 'N/A'; $('tVs200').className = 'metric-val neutral'; }

  const signalMeta = {
    'above_50ma':    ['Above 50-Day MA',   'sig-green'],
    'below_50ma':    ['Below 50-Day MA',   'sig-red'],
    'above_200ma':   ['Above 200-Day MA',  'sig-green'],
    'below_200ma':   ['Below 200-Day MA',  'sig-red'],
    'golden_cross':  ['Golden Cross 🌟',   'sig-green'],
    'death_cross':   ['Death Cross ☠️',   'sig-red'],
    'rising_volume': ['Rising Volume 📈',  'sig-yellow'],
  };
  const sl = $('signalList');
  sl.innerHTML = '';
  (tech.signals || []).forEach(s => {
    const [label, cls] = signalMeta[s] || [s, 'sig-yellow'];
    const span = document.createElement('span');
    span.className = 'signal ' + cls;
    span.textContent = label;
    sl.appendChild(span);
  });
  if (sl.children.length === 0) sl.innerHTML = '<span style="color:var(--muted); font-size:13px;">No signals available</span>';
}
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────
#  HTTP SERVER
# ─────────────────────────────────────────────────────────────────

# Cache recent analyses so re-checking a ticker (or clicking the quick buttons)
# doesn't hit Yahoo again, and retry briefly if a request is rate-limited.
_CACHE = {}
_CACHE_TTL = 600  # seconds

def _is_rate_limit(err):
    m = str(err).lower()
    return "too many" in m or "rate limit" in m or "rate-limit" in m or "429" in m

def analyze_cached(sym):
    hit = _CACHE.get(sym)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    for attempt in range(3):
        try:
            result = analyze(sym)
            _CACHE[sym] = (time.time(), result)
            return result
        except Exception as e:
            if _is_rate_limit(e) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))  # back off, then retry
                continue
            raise


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._send(200, "text/html", HTML.encode())

        elif parsed.path == "/api/analyze":
            params = parse_qs(parsed.query)
            ticker = (params.get("ticker", [""])[0]).upper().strip()
            if not ticker:
                self._send(400, "application/json", json.dumps({"error": "Missing ticker"}).encode())
                return
            try:
                print(f"  Analyzing {ticker}…", flush=True)
                data = analyze_cached(ticker)
                payload = json.dumps(clean_for_json(data), allow_nan=False)
                self._send(200, "application/json", payload.encode())
                print(f"  ✓ {ticker} done", flush=True)
            except Exception as e:
                print(f"  ✗ Error: {e}", flush=True)
                msg = ("Yahoo Finance is rate-limiting requests right now. "
                       "Wait ~30 seconds and try again.") if _is_rate_limit(e) else str(e)
                self._send(500, "application/json", json.dumps({"error": msg}).encode())

        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress access logs

# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def main():
    print("\n" + "─"*56)
    print("  Rule #1 Interactive Dashboard — Yahoo Finance")
    print("─"*56)
    print(f"\n  ✓ Server starting on http://localhost:{PORT}")
    print("  ✓ Opening browser automatically…")
    print("\n  Enter any ticker in the search bar.")
    print("  Press Ctrl+C to stop.\n")
    print("─"*56 + "\n")

    host = "0.0.0.0"
    server = HTTPServer((host, PORT), Handler)

    if os.environ.get("PORT"):
        print(f"  ✓ Running in cloud mode on port {PORT}")
    else:
        def open_browser():
            import time; time.sleep(0.8)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  Server stopped. Goodbye!")

if __name__ == "__main__":
    main()
