#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║   RULE #1 INTERACTIVE DASHBOARD — Yahoo Finance      ║
║   Run:  python3 rule1_dashboard.py                   ║
║   Open: http://localhost:8080                        ║
╚══════════════════════════════════════════════════════╝
"""

import sys, os, re, json, math, time, threading, webbrowser
import subprocess, importlib, statistics, warnings
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
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

PORT = int(os.environ.get("PORT", 8080))

# ── Rule #1 constants ────────────────────────────────────────────
MARR            = 0.15   # minimum acceptable rate of return
MOS             = 0.50   # margin of safety
MAX_GROWTH      = 0.15   # cap on the growth rate fed into the Sticker Price
MAX_FUTURE_PE   = 50
BIG5_TARGET     = 0.10   # every Big 5 number should be ≥ 10% a year
PAYBACK_TARGET  = 8      # years

# ─────────────────────────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────────────────────────

class AnalysisError(Exception):
    """A problem the user can act on (bad ticker, unsupported security…)."""
    def __init__(self, msg, status=404):
        super().__init__(msg)
        self.status = status

def to_f(val, default=None):
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return default

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

def cagr(values, periods=None):
    """Compound annual growth between the first and last reported points.

    Returns ``(rate, years)``. The rate is None when growth is undefined —
    fewer than two data points, or a start/end value that is zero or negative.
    Gaps in the middle don't shorten the time span: the number of years comes
    from the actual period dates (or positions), not from how many values
    happen to be usable.
    """
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return None, None
    (i0, v0), (i1, v1) = pts[0], pts[-1]
    years = float(i1 - i0)
    if periods is not None:
        try:
            years = (periods[i1] - periods[i0]).days / 365.25
        except Exception:
            pass
    if years < 0.75:
        return None, None
    span = int(round(years))
    if v0 <= 0 or v1 <= 0:
        return None, span
    try:
        r = (v1 / v0) ** (1 / years) - 1
    except Exception:
        return None, span
    if isinstance(r, complex) or math.isnan(r) or math.isinf(r):
        return None, span
    return r, span

def turned_negative(values):
    """True when a series started positive and ended at/below zero."""
    pts = [v for v in values if v is not None]
    return len(pts) >= 2 and pts[0] > 0 and pts[-1] <= 0

def fetch_frame(tk, attr):
    """Fetch one financial statement; an unavailable statement is just empty."""
    try:
        df = getattr(tk, attr)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        if _is_rate_limit(e):
            raise
        return pd.DataFrame()

def statement_periods(*frames):
    """All fiscal period-end dates found in any statement, oldest first."""
    cols = set()
    for df in frames:
        if df is not None and not df.empty:
            cols.update(c for c in df.columns if isinstance(c, pd.Timestamp))
    return sorted(cols)

def get_row(df, periods, *keys):
    """One statement line aligned to ``periods`` (None where not reported).

    Every series is aligned to the same list of period-end dates so a value
    can never land under the wrong fiscal year, even when the income
    statement, balance sheet and cash-flow statement cover different years.
    """
    blank = [None] * len(periods)
    if df is None or df.empty:
        return blank
    for key in keys:
        if key in df.index:
            s = df.loc[key]
            if isinstance(s, pd.DataFrame):      # duplicated row label
                s = s.iloc[0]
            vals = [to_f(s.get(p)) for p in periods]
            if any(v is not None for v in vals):
                return vals
    return blank

def equivalent_shares(series, shares_now):
    """Restate a statement share-count series in units of today's quoted share.

    Statement share counts are not always comparable with the share that
    trades: ADRs represent several ordinary shares, multi-class companies quote
    one class, and an occasional split isn't restated. We (1) carry split-like
    jumps back through older years and (2) rescale the series when the latest
    count is far from the count implied by market cap ÷ price.

    Returns ``(series, exact)``; ``exact`` is True when nothing was restated.
    """
    s = [v if (v is not None and v > 0) else None for v in series]
    exact = True
    last = None
    for i in range(len(s) - 1, -1, -1):
        if s[i] is None:
            continue
        if last is not None:
            ratio = last / s[i]
            if ratio > 1.5 or ratio < 1 / 1.5:
                for j in range(i + 1):
                    if s[j] is not None:
                        s[j] *= ratio
                exact = False
        last = s[i]
    latest = next((v for v in reversed(s) if v is not None), None)
    if latest and shares_now:
        k = shares_now / latest
        if not (0.8 <= k <= 1.25):
            # ADR ratios and splits are whole numbers (5 shares per ADR, 10-for-1…);
            # snap to one when close so buyback noise doesn't leak into the ratio.
            whole = round(k) if k > 1 else round(1 / k)
            if whole >= 1 and abs((k if k > 1 else 1 / k) - whole) / whole < 0.12:
                k = float(whole) if k > 1 else 1.0 / whole
            s = [v * k if v is not None else None for v in s]
            exact = False
    return s, exact

# Exchanges that quote in a minor unit (pence, cents, agorot):
#   quote currency → (major ISO code, quote units per major unit)
MINOR_UNITS = {
    "GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0), "ZAC": ("ZAR", 100.0),
    "ILA": ("ILS", 100.0),
}

def split_currency(code):
    if not code:
        return None, 1.0
    if code in MINOR_UNITS:
        return MINOR_UNITS[code]
    return str(code).upper(), 1.0

_FX_CACHE = {}
_FX_TTL = 3600

def fx_rate(src, dst):
    """Units of ``dst`` per one unit of ``src`` (major ISO codes), or None."""
    if src == dst:
        return 1.0
    hit = _FX_CACHE.get((src, dst))
    if hit and time.time() - hit[0] < _FX_TTL:
        return hit[1]
    for pair, invert in ((f"{src}{dst}=X", False), (f"{dst}{src}=X", True)):
        try:
            h = yf.Ticker(pair).history(period="5d")
            r = to_f(h["Close"].dropna().iloc[-1])
        except Exception as e:
            if _is_rate_limit(e):
                raise
            continue
        if r and r > 0:
            rate = (1 / r) if invert else r
            _FX_CACHE[(src, dst)] = (time.time(), rate)
            return rate
    return None

def analyst_growth(tk):
    """Analysts' forward EPS growth estimate as ``(rate, label)``.

    Prefers the long-term (≈5-year) estimate; falls back to next fiscal year.
    """
    try:
        ge = tk.growth_estimates
    except Exception as e:
        if _is_rate_limit(e):
            raise
        return None, None
    if not isinstance(ge, pd.DataFrame) or ge.empty:
        return None, None
    col = next((c for c in ("stockTrend", "stock") if c in ge.columns), None)
    if col is None:
        return None, None
    for period, label in (("LTG", "long-term"), ("+5y", "next 5 years"),
                          ("+1y", "next fiscal year")):
        if period in ge.index:
            g = to_f(ge.loc[period, col])
            if g is not None:
                return g, label
    return None, None

def tier(val, good, warn):
    """2 / 1 / 0 points for a higher-is-better metric; None when unknown."""
    if val is None:
        return None
    return 2 if val >= good else 1 if val >= warn else 0

def tier_inv(val, good, warn):
    """2 / 1 / 0 points for a lower-is-better metric; None when unknown."""
    if val is None:
        return None
    return 2 if val <= good else 1 if val <= warn else 0

def to_score(parts):
    """1–10 score from 0/1/2-point parts, ignoring metrics with no data.

    Missing data is neither a pass nor a fail: the score is the share of
    points earned out of the points that could actually be assessed. With
    fewer than two assessable metrics there is no score at all.
    """
    got = [p for p in parts if p is not None]
    if len(got) < 2:
        return None
    return max(1, round(10 * sum(got) / (2 * len(got))))

# ─────────────────────────────────────────────────────────────────
#  CORE ANALYSIS
# ─────────────────────────────────────────────────────────────────

def analyze(sym):
    tk = yf.Ticker(sym)
    try:
        info = tk.info or {}
    except Exception as e:
        if _is_rate_limit(e):
            raise
        info = {}
    notes = []

    # ── Basic info ──────────────────────────────────────────────
    quote_type = str(info.get("quoteType") or "").upper()
    name     = info.get("longName") or info.get("shortName") or sym
    price    = to_f(info.get("currentPrice")) or to_f(info.get("regularMarketPrice"))
    prev_cls = to_f(info.get("regularMarketPreviousClose")) or to_f(info.get("previousClose"))
    mktcap   = to_f(info.get("marketCap"))
    sector   = info.get("sector") or "N/A"
    industry = info.get("industry") or "N/A"
    desc_raw = info.get("longBusinessSummary") or ""
    desc     = (desc_raw[:500] + "...") if len(desc_raw) > 500 else desc_raw

    if quote_type and quote_type != "EQUITY":
        raise AnalysisError(
            f"{sym} is a {quote_type.lower().replace('_', ' ')}, not a company stock. "
            "Rule #1 analysis needs a business with financial statements.", 422)

    price_chg     = (price - prev_cls) if (price is not None and prev_cls) else None
    price_chg_pct = (price_chg / prev_cls) if (price_chg is not None and prev_cls) else None

    # ── Financial statements ────────────────────────────────────
    inc = fetch_frame(tk, "income_stmt")
    bal = fetch_frame(tk, "balance_sheet")
    cf  = fetch_frame(tk, "cash_flow")

    if price is None and inc.empty and bal.empty and cf.empty:
        raise AnalysisError(f"No data found for \"{sym}\". Check the ticker symbol "
                            "(use Yahoo Finance symbols, e.g. BRK-B, SHOP.TO).")

    # One shared timeline for all three statements. Drop stub periods where
    # Yahoo lists the year but reports none of the headline numbers.
    periods = statement_periods(inc, bal, cf)
    core = [get_row(inc, periods, "Total Revenue"),
            get_row(inc, periods, "Net Income"),
            get_row(bal, periods, "Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"),
            get_row(cf,  periods, "Operating Cash Flow", "Total Cash From Operating Activities")]
    periods = [p for i, p in enumerate(periods) if any(r[i] is not None for r in core)]
    n = len(periods)

    def irow(*keys): return get_row(inc, periods, *keys)
    def brow(*keys): return get_row(bal, periods, *keys)
    def crow(*keys): return get_row(cf,  periods, *keys)

    # ── Currencies ──────────────────────────────────────────────
    # Statements are in the company's reporting currency, the price in the
    # listing currency. They differ for ADRs and foreign listings, so every
    # statement amount is converted before it is compared with price/market cap.
    cur_code            = info.get("currency")
    cur_major, unit     = split_currency(cur_code)              # unit: quote units per major unit
    fin_major, fin_unit = split_currency(info.get("financialCurrency") or cur_code)
    conv, comparable = 1.0, True
    if cur_major and fin_major and (cur_major != fin_major or fin_unit != 1.0):
        rate = fx_rate(fin_major, cur_major)
        if rate:
            conv = rate / fin_unit
            if cur_major != fin_major:
                notes.append(f"Financial statements are reported in {fin_major} and were converted "
                             f"to {cur_major} at today's rate (1 {fin_major} = {rate:.4g} {cur_major}).")
        else:
            comparable = False
            notes.append(f"Financial statements are reported in {fin_major} but the stock trades in "
                         f"{cur_major}, and no exchange rate was available. Statement figures are shown "
                         f"in {fin_major}; price-based valuations are disabled.")
    stmt_cur = cur_major if comparable else fin_major
    ps_unit  = unit if comparable else 1.0      # major currency → per-share quote units

    def money(vals):
        return [v * conv if v is not None else None for v in vals]

    # ── Share count ─────────────────────────────────────────────
    # Market cap ÷ price is the share count that is consistent with the quoted
    # price. `sharesOutstanding` alone is wrong for multi-class companies
    # (GOOGL reports only its Class A shares there) and for ADRs.
    info_shares = to_f(info.get("impliedSharesOutstanding")) or to_f(info.get("sharesOutstanding"))
    shares = None
    if mktcap and price:
        shares = mktcap / (price / unit)
        # Guard: some minor-unit listings report market cap in the minor unit.
        if unit != 1.0 and info_shares and shares / info_shares > unit / 3:
            mktcap /= unit
            shares /= unit
    if not shares or shares <= 0:
        shares = info_shares
    if mktcap is None and shares and price:
        mktcap = shares * price / unit

    # ── Statement lines (all aligned to `periods`, oldest first) ─
    revenues   = money(irow("Total Revenue", "Operating Revenue"))
    net_inc    = money(irow("Net Income", "Net Income Common Stockholders"))
    op_inc     = money(irow("Operating Income", "EBIT"))
    pretax     = money(irow("Pretax Income"))
    tax_prov   = money(irow("Tax Provision"))
    tax_rate   = irow("Tax Rate For Calcs")
    int_exp    = money(irow("Interest Expense", "Interest Expense Non Operating"))
    eps_rep    = irow("Diluted EPS", "Basic EPS")
    dil_shares = irow("Diluted Average Shares", "Basic Average Shares")

    equity     = money(brow("Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"))
    total_debt = money(brow("Total Debt", "Long Term Debt"))
    inv_cap    = money(brow("Invested Capital"))
    curr_ast   = money(brow("Current Assets", "Total Current Assets"))
    curr_lib   = money(brow("Current Liabilities", "Total Current Liabilities"))
    ord_shares = brow("Ordinary Shares Number", "Share Issued")

    ocf        = money(crow("Operating Cash Flow", "Total Cash From Operating Activities"))
    capex_raw  = money(crow("Capital Expenditure", "Capital Expenditures"))
    fcf_rep    = money(crow("Free Cash Flow"))
    dna        = money(crow("Depreciation And Amortization", "Depreciation Amortization Depletion",
                            "Depreciation"))
    if not any(v is not None for v in dna):
        dna    = money(irow("Reconciled Depreciation"))
    chg_ar     = money(crow("Changes In Account Receivables", "Change In Receivables"))
    chg_ap     = money(crow("Change In Account Payable", "Change In Payable",
                            "Change In Payables And Accrued Expense"))

    # FCF = OCF − |CapEx|  (Yahoo's own line when it reports one)
    fcf = []
    for i in range(n):
        if fcf_rep[i] is not None:
            fcf.append(fcf_rep[i])
        elif ocf[i] is not None and capex_raw[i] is not None:
            fcf.append(ocf[i] - abs(capex_raw[i]))
        else:
            fcf.append(None)

    # ── Per-share series ────────────────────────────────────────
    # Each year uses THAT year's share count (not today's), restated into
    # units of the quoted share so the values are comparable with the price.
    dil_eq, dil_exact = equivalent_shares(dil_shares, shares)
    ord_eq, _         = equivalent_shares(ord_shares, shares)
    have_dil = any(v is not None for v in dil_eq)
    have_ord = any(v is not None for v in ord_eq)

    eps_vals, bvps = [], []
    for i in range(n):
        if eps_rep[i] is not None and dil_exact:
            eps_vals.append(eps_rep[i] * conv * ps_unit)
        elif net_inc[i] is not None and dil_eq[i]:
            eps_vals.append(net_inc[i] * ps_unit / dil_eq[i])
        elif net_inc[i] is not None and not have_dil and shares:
            eps_vals.append(net_inc[i] * ps_unit / shares)
        else:
            eps_vals.append(None)

        if equity[i] is not None and ord_eq[i]:
            bvps.append(equity[i] * ps_unit / ord_eq[i])
        elif equity[i] is not None and not have_ord and shares:
            bvps.append(equity[i] * ps_unit / shares)
        else:
            bvps.append(None)
    if n and ((not have_dil and all(v is None for v in eps_rep)) or not have_ord):
        notes.append("Historical share counts were not available for every year, so some per-share "
                     "history uses today's share count and may understate past dilution or buybacks.")

    # ── ROIC per year = NOPAT ÷ invested capital ────────────────
    roic_vals = []
    for i in range(n):
        ic = inv_cap[i]
        if ic is None and equity[i] is not None:
            ic = equity[i] + (total_debt[i] or 0)
        if ic is None or ic <= 0:
            roic_vals.append(None)
            continue
        if op_inc[i] is not None:
            t = tax_rate[i]
            if t is None and tax_prov[i] is not None and pretax[i] and pretax[i] > 0:
                t = tax_prov[i] / pretax[i]
            t = min(max(t if t is not None else 0.21, 0.0), 0.5)
            nopat = op_inc[i] * (1 - t) if op_inc[i] > 0 else op_inc[i]
        elif net_inc[i] is not None:
            nopat = net_inc[i]
        else:
            roic_vals.append(None)
            continue
        roic_vals.append(nopat / ic)

    year_labels = [str(p.year) for p in periods]
    fiscal_year = year_labels[-1] if year_labels else None

    # Most-recent fiscal year values (strictly the latest period — never an
    # older year silently standing in for a missing number)
    def last(vals): return vals[-1] if vals else None
    rev_now, ni_now   = last(revenues), last(net_inc)
    ocf_now, fcf_now  = last(ocf), last(fcf)
    eq_now, dbt_now   = last(equity), last(total_debt)
    ca_now, cl_now    = last(curr_ast), last(curr_lib)
    oi_now, ie_now    = last(op_inc), last(int_exp)
    cx_now            = last(capex_raw)

    # ── Financial ratios (latest fiscal year) ───────────────────
    roic      = last(roic_vals)
    roic_hist = [v for v in roic_vals if v is not None]
    roic_avg  = (sum(roic_hist) / len(roic_hist)) if roic_hist else None

    neg_equity = eq_now is not None and eq_now <= 0
    roe = (ni_now / eq_now) if (ni_now is not None and eq_now and eq_now > 0) else None
    if roe is None and not neg_equity:
        roe = to_f(info.get("returnOnEquity"))

    de_ratio = (dbt_now / eq_now) if (dbt_now is not None and eq_now and eq_now > 0) else None
    current_ratio = (ca_now / cl_now) if (ca_now is not None and cl_now and cl_now > 0) else None

    profit_margin = (ni_now / rev_now) if (ni_now is not None and rev_now and rev_now > 0) else None
    if profit_margin is None:
        profit_margin = to_f(info.get("profitMargins"))
    operating_margin = (oi_now / rev_now) if (oi_now is not None and rev_now and rev_now > 0) else None
    if operating_margin is None:
        operating_margin = to_f(info.get("operatingMargins"))
    fcf_margin = (fcf_now / rev_now) if (fcf_now is not None and rev_now and rev_now > 0) else None

    debt_free    = dbt_now is not None and dbt_now == 0
    int_coverage = (oi_now / abs(ie_now)) if (oi_now is not None and ie_now) else None
    no_interest  = int_coverage is None and debt_free and oi_now is not None and oi_now > 0

    # ── Growth rates ────────────────────────────────────────────
    series = {"revenue": revenues, "net_income": net_inc, "eps": eps_vals,
              "bvps": bvps, "ocf": ocf, "fcf": fcf}
    gr, gr_years = {}, {}
    for key, vals in series.items():
        gr[key], gr_years[key] = cagr(vals, periods)

    # ── Technical / price history ───────────────────────────────
    # Five years of UNADJUSTED closes (split-adjusted only): moving averages
    # and 52-week levels must use prices that actually traded, and the older
    # history gives the 200-day average a full year of points to plot.
    closes, volumes, dt_strs, ma50_series, ma200_series, signals = [], [], [], [], [], []
    ma50 = ma200 = high52 = low52 = None
    close_all = pd.Series(dtype=float)
    try:
        hist = tk.history(period="5y", auto_adjust=False)
    except Exception as e:
        if _is_rate_limit(e):
            raise
        hist = pd.DataFrame()
    if isinstance(hist, pd.DataFrame) and not hist.empty and "Close" in hist:
        if getattr(hist.index, "tz", None) is not None:
            hist = hist.copy()
            hist.index = hist.index.tz_localize(None)
        close_all = hist["Close"].dropna()
    if len(close_all):
        ma50_all  = close_all.rolling(50).mean()
        ma200_all = close_all.rolling(200).mean()
        cutoff    = close_all.index[-1] - pd.DateOffset(years=1)
        yr        = close_all[close_all.index >= cutoff]
        hist_yr   = hist.loc[yr.index]

        closes       = [round(float(v), 2) for v in yr.values]
        dt_strs      = [str(d.date()) for d in yr.index]
        ma50_series  = [to_f(v) for v in ma50_all.loc[yr.index].round(2).values]
        ma200_series = [to_f(v) for v in ma200_all.loc[yr.index].round(2).values]
        ma50, ma200  = to_f(ma50_all.iloc[-1]), to_f(ma200_all.iloc[-1])
        high52 = to_f(hist_yr["High"].max()) if "High" in hist_yr else to_f(yr.max())
        low52  = to_f(hist_yr["Low"].min())  if "Low"  in hist_yr else to_f(yr.min())

        vol_flag = False
        if "Volume" in hist_yr:
            vol      = hist_yr["Volume"]
            volumes  = [to_f(v) for v in vol.values]
            recent_v = to_f(vol.tail(20).mean())
            avg_v    = to_f(vol.mean())
            vol_flag = bool(recent_v and avg_v and recent_v > avg_v * 1.10)

        if price and ma50:   signals.append("above_50ma"   if price > ma50  else "below_50ma")
        if price and ma200:  signals.append("above_200ma"  if price > ma200 else "below_200ma")
        if ma50 and ma200:   signals.append("golden_cross" if ma50 > ma200  else "death_cross")
        if vol_flag:         signals.append("rising_volume")

    # ── Valuation ───────────────────────────────────────────────
    # 1) Ten Cap — Phil Town's owner earnings:
    #    net income + D&A + Δreceivables + Δpayables + income tax − maintenance capex.
    #    Yahoo doesn't split out maintenance capex, so all capex is treated as
    #    maintenance (the conservative choice).
    capex_abs = abs(cx_now) if cx_now is not None else None
    oe_parts, oe_method, owner_earnings = None, None, None
    if ni_now is not None and last(dna) is not None and capex_abs is not None:
        oe_parts = {
            "net_income": ni_now, "depreciation": last(dna),
            "chg_receivables": last(chg_ar) or 0.0, "chg_payables": last(chg_ap) or 0.0,
            "income_tax": last(tax_prov) or 0.0, "maintenance_capex": -capex_abs,
        }
        owner_earnings = sum(oe_parts.values())
        oe_method = "town"
    elif ocf_now is not None:
        owner_earnings = ocf_now - (capex_abs or 0.0)
        oe_method = "ocf_less_capex"

    ten_cap_val = ten_cap_ps = None
    if owner_earnings is not None and owner_earnings > 0 and comparable:
        ten_cap_val = owner_earnings * 10
        ten_cap_ps  = (ten_cap_val * ps_unit / shares) if shares else None

    # 2) Sticker Price
    curr_eps, eps_source = to_f(info.get("trailingEps")), "TTM"
    if curr_eps is None and comparable and last(eps_vals) is not None:
        curr_eps, eps_source = last(eps_vals), f"FY{fiscal_year}"
    pe = to_f(info.get("trailingPE"))
    if pe is None and price and curr_eps and curr_eps > 0:
        pe = price / curr_eps

    # Growth: the lower of the company's own history and what analysts expect.
    # History is equity (book value) growth — Rule #1's preferred yardstick —
    # falling back to EPS growth when equity isn't growing (heavy buybacks).
    if gr["bvps"] is not None and gr["bvps"] > 0:
        hist_growth, hist_growth_src = gr["bvps"], "Book value / share"
    elif gr["eps"] is not None:
        hist_growth, hist_growth_src = gr["eps"], "EPS"
    else:
        hist_growth, hist_growth_src = gr["bvps"], ("Book value / share" if gr["bvps"] is not None else None)
    an_growth, an_growth_src = analyst_growth(tk)

    candidates  = [g for g in (hist_growth, an_growth) if g is not None and g > 0]
    growth_used = min(min(candidates), MAX_GROWTH) if candidates else None

    # Historical P/E: price at each fiscal year-end ÷ that year's EPS.
    hist_pes = []
    if comparable and len(close_all):
        for p, e in zip(periods, eps_vals):
            if e is None or e <= 0 or p < close_all.index[0]:
                continue
            px = to_f(close_all.asof(p))
            if px and 0 < px / e <= 200:
                hist_pes.append(px / e)
    hist_pe = statistics.median(hist_pes) if hist_pes else None

    sticker_issue = None
    future_eps = future_pe = default_pe = future_price = sticker_price = mos_price = None
    if curr_eps is None:
        sticker_issue = "no_eps"
    elif curr_eps <= 0:
        sticker_issue = "negative_eps"
    elif growth_used is None:
        sticker_issue = "no_growth" if (hist_growth is not None or an_growth is not None) else "no_growth_data"
    else:
        default_pe    = 2 * growth_used * 100
        future_pe     = min(default_pe, hist_pe) if hist_pe else default_pe
        future_pe     = min(future_pe, MAX_FUTURE_PE)
        future_eps    = curr_eps * (1 + growth_used) ** 10
        future_price  = future_eps * future_pe
        sticker_price = future_price / ((1 + MARR) ** 10)
        mos_price     = sticker_price * (1 - MOS)

    # 3) Payback Time — years of free cash flow, growing at the same
    #    conservative rate as the Sticker Price, needed to add up to market cap.
    payback_yrs = payback_price = None
    payback_over = False
    payback_growth = growth_used if growth_used is not None else 0.0
    if comparable and fcf_now is not None and fcf_now > 0:
        running, cur_fcf, eight_yr = 0.0, fcf_now, 0.0
        for yr_n in range(1, 31):
            cur_fcf *= (1 + payback_growth)
            running += cur_fcf
            if yr_n == PAYBACK_TARGET:
                eight_yr = running
            if mktcap and payback_yrs is None and running >= mktcap:
                payback_yrs = yr_n
        payback_over  = bool(mktcap) and payback_yrs is None
        payback_price = (eight_yr * ps_unit / shares) if shares else None

    # ── Scores ──────────────────────────────────────────────────
    # Moat = the Big 5: ROIC plus sales, EPS, equity and cash growth, each
    # wanted at ≥ 10% a year. 2 pts at ≥ 10%, 1 pt at ≥ 5%, 0 below.
    def growth_pts(key):
        if gr[key] is None and turned_negative(series[key]):
            return 0                      # went from profit to loss: a fail, not "no data"
        return tier(gr[key], BIG5_TARGET, 0.05)

    roic_for_score = min(roic, roic_avg) if (roic is not None and roic_avg is not None) else roic
    cash_key = "fcf" if (gr["fcf"] is not None or turned_negative(fcf)) else "ocf"
    moat_sc = to_score([
        tier(roic_for_score, BIG5_TARGET, 0.05),
        growth_pts("revenue"), growth_pts("eps"), growth_pts("bvps"), growth_pts(cash_key),
    ])

    # Financial health uses the same good / warn / bad bands the page colours by.
    de_pts  = 0 if neg_equity else tier_inv(de_ratio, 0.5, 1.5)
    cov_pts = 2 if no_interest else tier(int_coverage, 5, 2)
    fin_sc = to_score([
        tier(roic, 0.10, 0.05), de_pts, tier(current_ratio, 1.5, 1.0),
        tier(profit_margin, 0.10, 0.05), cov_pts,
    ])

    quant = [s for s in (moat_sc, fin_sc) if s is not None]
    overall = round(sum(quant) / len(quant)) if quant else None

    # ── Verdict ─────────────────────────────────────────────────
    is_strong_fin = fin_sc is not None and fin_sc >= 7
    is_good_moat  = moat_sc is not None and moat_sc >= 7
    reasons = []

    if price is None or sticker_issue in ("no_eps", "no_growth_data"):
        verdict, confidence = "INSUFFICIENT DATA", "Low"
        reasons.append("Yahoo Finance did not return enough data to value this company.")
    elif sticker_issue == "negative_eps":
        verdict, confidence = "AVOID / WAIT", "Medium"
        reasons.append("Earnings are negative, so there is no Sticker Price — Rule #1 wants a "
                       "predictable, profitable business.")
    elif sticker_issue == "no_growth":
        verdict, confidence = "AVOID / WAIT", "Medium"
        reasons.append("Neither the company's history nor analyst estimates show positive growth, "
                       "so there is no Sticker Price.")
    else:
        q_text = ("The business scores 7+ on both moat and financial health."
                  if (is_strong_fin and is_good_moat)
                  else "The business scores 7+ on only one of moat and financial health."
                  if (is_strong_fin or is_good_moat)
                  else "The business scores below 7 on both moat and financial health.")
        if price <= mos_price:
            reasons.append("Price is at or below the Margin of Safety price.")
            reasons.append(q_text)
            if is_strong_fin and is_good_moat:
                verdict, confidence = "STRONG BUY", "High"
            elif is_strong_fin or is_good_moat:
                verdict, confidence = "BUY", "Medium"
            else:
                verdict, confidence = "WATCHLIST", "Low"
        elif price <= sticker_price:
            verdict, confidence = "WATCHLIST", "Medium"
            reasons.append("Price is below the Sticker Price but not yet at the Margin of Safety price.")
            reasons.append(q_text)
        else:
            verdict, confidence = "AVOID / WAIT", "Medium"
            reasons.append("Price is above the Sticker Price.")
            reasons.append(q_text)
    if sticker_issue is None and price is not None:
        # The verdict is driven by the Sticker Price; say where the other two
        # valuation methods agree or disagree with it.
        others = []
        if ten_cap_ps is not None:
            others.append(("Ten Cap", price <= ten_cap_ps))
        if payback_yrs is not None or payback_over:
            others.append(("Payback Time", payback_yrs is not None and payback_yrs <= PAYBACK_TARGET))
        passed = [nm for nm, ok in others if ok]
        failed = [nm for nm, ok in others if not ok]
        if passed:
            reasons.append(f"Also passes: {' and '.join(passed)}.")
        if failed:
            reasons.append(f"Fails: {' and '.join(failed)}.")
    if n < 4 and n > 0:
        reasons.append(f"Only {n} fiscal year(s) of statements were available; growth rates are less reliable.")

    # ── Dividend yield ──────────────────────────────────────────
    # Yahoo's `dividendYield` field is already in percent (0.41 = 0.41%), so
    # work the yield out from the dividend itself and keep it as a fraction.
    div_rate  = to_f(info.get("dividendRate"))
    div_yield = (div_rate / price) if (div_rate is not None and price) else None
    if div_yield is None:
        div_yield = to_f(info.get("trailingAnnualDividendYield"))
    if div_yield is None and to_f(info.get("dividendYield")) is not None:
        div_yield = to_f(info.get("dividendYield")) / 100.0

    # ── Return ──────────────────────────────────────────────────
    return {
        "info": {
            "name": name, "ticker": sym, "price": price,
            "price_change": price_chg, "price_change_pct": price_chg_pct,
            "market_cap": mktcap, "sector": sector, "industry": industry,
            "description": desc, "pe_ratio": pe, "eps": curr_eps,
            "dividend_yield": div_yield,
            "beta": to_f(info.get("beta")),
            "52w_high": to_f(info.get("fiftyTwoWeekHigh")) or high52,
            "52w_low":  to_f(info.get("fiftyTwoWeekLow"))  or low52,
            "shares": shares,
        },
        "currency": {
            "price": cur_code or cur_major, "major": cur_major, "unit": unit,
            "statements": stmt_cur, "per_share": (cur_code or cur_major) if comparable else stmt_cur,
            "comparable": comparable,
        },
        "big5": {
            "year_labels": year_labels,
            "roic": roic_vals, "roic_avg": roic_avg,
            "revenues": revenues, "net_incomes": net_inc,
            "eps_values": eps_vals, "bvps": bvps,
            "ocf": ocf, "fcf": fcf,
            "growth_rates": gr, "growth_years": gr_years,
        },
        "financials": {
            "fiscal_year": fiscal_year,
            "roic": roic, "roe": roe, "de_ratio": de_ratio,
            "negative_equity": neg_equity, "debt_free": debt_free,
            "current_ratio": current_ratio, "profit_margin": profit_margin,
            "fcf_margin": fcf_margin, "operating_margin": operating_margin,
            "interest_coverage": int_coverage, "no_interest": no_interest,
        },
        "valuation": {
            "fiscal_year": fiscal_year,
            "owner_earnings": owner_earnings, "owner_earnings_parts": oe_parts,
            "owner_earnings_method": oe_method,
            "ten_cap_value": ten_cap_val, "ten_cap_per_share": ten_cap_ps,
            "market_cap": mktcap,
            "sticker_price": sticker_price, "mos_price": mos_price,
            "sticker_issue": sticker_issue,
            "current_eps": curr_eps, "eps_source": eps_source,
            "historical_growth": hist_growth, "historical_growth_source": hist_growth_src,
            "analyst_growth": an_growth, "analyst_growth_source": an_growth_src,
            "growth_rate_used": growth_used, "growth_cap": MAX_GROWTH,
            "future_eps": future_eps, "default_pe": default_pe,
            "historical_pe": hist_pe, "future_pe": future_pe,
            "future_price": future_price,
            "fcf": fcf_now if comparable else None,
            "payback_years": payback_yrs, "payback_over_30": payback_over,
            "payback_growth": payback_growth, "payback_price": payback_price,
            "payback_target": PAYBACK_TARGET,
        },
        "technical": {
            "ma50": ma50, "ma200": ma200, "signals": signals,
            "prices": closes, "volumes": volumes, "dates": dt_strs,
            "ma50_series": ma50_series, "ma200_series": ma200_series,
            "52w_high": high52, "52w_low": low52,
        },
        "scores": {
            # Meaning and Management are qualitative judgments that can't be
            # computed from financial data, so they are deliberately unscored.
            "meaning": None, "moat": moat_sc,
            "management": None, "financial": fin_sc, "overall": overall,
        },
        "verdict": {"verdict": verdict, "confidence": confidence, "reasons": reasons},
        "notes": notes,
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
  .verdict-reasons { font-size: 12px; opacity: 0.75; margin-top: 6px; max-width: 460px; line-height: 1.5; }
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
  .score-hint { font-size: 11px; color: var(--muted); opacity: 0.8; margin-top: 6px; line-height: 1.4; }

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
  .big5-star { color: var(--gold); }
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

  /* ── Data notes / disclaimer ── */
  #dataNotes {
    display: none; background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.25);
    border-radius: 12px; padding: 12px 18px; margin-bottom: 20px;
    color: #93c5fd; font-size: 12px; line-height: 1.7;
  }
  .method-note { color: var(--muted); font-size: 12px; line-height: 1.6; margin: -8px 0 16px; }
  .disclaimer { color: var(--muted); font-size: 11px; text-align: center; padding: 8px 0 28px; }

  #result { display: none; }
</style>
</head>
<body>

<header>
  <div class="logo">Rule <span>#</span>1 <span style="color:var(--gold)">▸</span> Dashboard</div>
  <div class="search-wrap">
    <input id="tickerInput" type="text" placeholder="Enter ticker (e.g. AAPL)" maxlength="15" autocomplete="off" spellcheck="false" />
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
        <div class="verdict-reasons" id="verdictReasons"></div>
      </div>
      <div class="verdict-prices">
        <div class="vp-item"><div class="vp-label">Current Price</div><div class="vp-val" id="vpCurrent"></div></div>
        <div class="vp-item"><div class="vp-label">Sticker Price</div><div class="vp-val" id="vpSticker"></div></div>
        <div class="vp-item"><div class="vp-label">MOS Buy Price</div><div class="vp-val" id="vpMOS"></div></div>
        <div class="vp-item"><div class="vp-label">Upside / Downside</div><div class="vp-val" id="vpUpside"></div></div>
      </div>
    </div>

    <!-- Data notes (currency conversion, missing data…) -->
    <div id="dataNotes"></div>

    <!-- Scores -->
    <div class="section-title">Scores <span>Rule #1 Framework — out of 10</span></div>
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
    <div class="section-title">Big 5 Numbers <span>★ = the Big 5 — Rule #1 wants each at 10%+ a year</span></div>
    <div class="card" style="margin-bottom:20px; overflow-x:auto;">
      <table class="big5-table" id="big5Table"></table>
    </div>

    <!-- Financials -->
    <div class="section-title">Financial Health</div>
    <div class="grid2" id="finGrid"></div>

    <!-- Valuation -->
    <div class="section-title">Intrinsic Value Analysis</div>
    <div class="method-note">Growth is the lower of the company's own history and analyst estimates, capped at 15%. The future P/E is the lower of 2 × growth and the stock's own historical P/E. The Sticker Price discounts the 10-year price target at 15% a year; the MOS price is half of that.</div>
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

    <div class="disclaimer">Educational tool, not financial advice. Data from Yahoo Finance and may be delayed or incomplete — verify against company filings before investing.</div>

  </div><!-- #result -->
</main>

<script>
// ── Helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Currency of the ticker on screen. Prices/per-share values are in the quote
// currency (which can be a minor unit such as pence); totals are in the major one.
const SYMBOLS = { USD:'$', EUR:'€', GBP:'£', JPY:'¥', CNY:'CN¥', HKD:'HK$', CAD:'C$', AUD:'A$',
                  INR:'₹', KRW:'₩', CHF:'CHF ', TWD:'NT$', BRL:'R$', MXN:'MX$', SEK:'SEK ', NOK:'NOK ',
                  DKK:'DKK ', SGD:'S$', ZAR:'R', ILS:'₪' };
let CUR = { perShare: 'USD', totals: 'USD' };
const sym = code => SYMBOLS[code] != null ? SYMBOLS[code] : (code ? code + ' ' : '$');

function fmt(n, decimals=2) {
  if (n == null) return 'N/A';
  return n.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}
// Price / per-share amount
function fmtP(n) {
  if (n == null) return 'N/A';
  const s = SYMBOLS[CUR.perShare];
  const body = fmt(Math.abs(n));
  const sign = n < 0 ? '-' : '';
  return s != null ? sign + s + body : `${sign}${body} ${CUR.perShare}`;
}
function fmtPct(n) { return n != null ? (n*100).toFixed(1) + '%' : 'N/A'; }
// Company-level total
function fmtB(n) {
  if (n == null) return 'N/A';
  const a = Math.abs(n), sign = n < 0 ? '-' : '', s = sym(CUR.totals);
  if (a >= 1e12) return sign + s + (a/1e12).toFixed(2) + 'T';
  if (a >= 1e9)  return sign + s + (a/1e9).toFixed(2) + 'B';
  if (a >= 1e6)  return sign + s + (a/1e6).toFixed(2) + 'M';
  return sign + s + fmt(a, 0);
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
let requestSeq = 0;   // only the most recent request is allowed to render

// ── Main analysis ─────────────────────────────────────────────────────────────
async function runAnalysis(ticker) {
  ticker = (ticker || $('tickerInput').value).trim().toUpperCase();
  if (!ticker) return;
  const seq = ++requestSeq;

  $('tickerInput').value = ticker;
  $('welcome').style.display   = 'none';
  $('error').style.display     = 'none';
  $('result').style.display    = 'none';
  $('loading').style.display   = 'block';
  $('loadingTicker').textContent = ticker;
  $('analyzeBtn').disabled = true;

  try {
    const res  = await fetch(`/api/analyze?ticker=${encodeURIComponent(ticker)}`);
    let data;
    try { data = await res.json(); }
    catch (_) { throw new Error(`Server returned an unreadable response (HTTP ${res.status}).`); }
    if (seq !== requestSeq) return;            // a newer search superseded this one
    if (data.error) throw new Error(data.error);
    if (!res.ok)    throw new Error(`HTTP ${res.status}`);

    $('loading').style.display = 'none';
    $('result').style.display  = 'block';

    renderAll(data);
  } catch(e) {
    if (seq !== requestSeq) return;
    $('loading').style.display = 'none';
    $('result').style.display  = 'none';
    $('error').style.display   = 'block';
    $('errorMsg').textContent  = `Could not load data for "${ticker}": ${e.message}`;
  } finally {
    if (seq === requestSeq) $('analyzeBtn').disabled = false;
  }
}

function quickTicker(t) {
  $('tickerInput').value = t;
  runAnalysis(t);
}

$('tickerInput').addEventListener('keydown', e => { if(e.key === 'Enter') runAnalysis(); });

// ── Render ────────────────────────────────────────────────────────────────────
function renderAll(d) {
  CUR = { perShare: d.currency.per_share || 'USD', totals: d.currency.statements || 'USD' };
  renderHero(d.info, d.currency);
  renderVerdict(d.verdict, d.info, d.valuation);
  renderNotes(d.notes);
  renderScores(d.scores);
  renderChart(d.technical);
  renderBig5(d.big5);
  renderFinancials(d.financials);
  renderValuation(d.valuation, d.info);
  renderTechnical(d.technical, d.info.price);
}

function renderHero(info, cur) {
  // Price and market cap are always in the listing currency, even when the
  // statements could not be converted.
  const save = CUR;
  CUR = { perShare: cur.price || 'USD', totals: cur.major || 'USD' };

  $('companyName').textContent = `${info.name} (${info.ticker})`;
  $('companyMeta').textContent = `${info.sector}  •  ${info.industry}`;
  $('priceDisplay').textContent = fmtP(info.price);

  const el = $('priceChange');
  if (info.price_change != null) {
    const sign = info.price_change >= 0 ? '+' : '-';
    const pct  = info.price_change_pct != null
      ? ` (${sign}${fmtPct(Math.abs(info.price_change_pct))})` : '';
    el.textContent = `${sign}${fmtP(Math.abs(info.price_change))}${pct}`;
    el.className   = 'price-change ' + (info.price_change >= 0 ? 'pos' : 'neg');
  } else {
    el.textContent = '';
    el.className   = 'price-change';
  }

  $('heroMktcap').textContent  = fmtB(info.market_cap);
  $('heroPE').textContent      = info.pe_ratio != null ? fmt(info.pe_ratio, 1) + 'x' : 'N/A';
  $('hero52H').textContent     = fmtP(info['52w_high']);
  $('hero52L').textContent     = fmtP(info['52w_low']);
  $('heroBeta').textContent    = info.beta != null ? fmt(info.beta, 2) : 'N/A';
  $('heroDivYield').textContent = info.dividend_yield ? (info.dividend_yield*100).toFixed(2) + '%' : '—';
  $('companyDesc').textContent = info.description || '';
  $('companyDesc').style.display = info.description ? '' : 'none';
  CUR = save;
}

function renderVerdict(verdict, info, val) {
  const v = verdict.verdict;
  const colors = {
    'STRONG BUY': ['#052e16', '#10b981'],
    'BUY':        ['#022c22', '#34d399'],
    'WATCHLIST':  ['#1c1917', '#f59e0b'],
    'AVOID / WAIT': ['#1f0a0a', '#ef4444'],
    'INSUFFICIENT DATA': ['#111827', '#9ca3af'],
  };
  const [bg, fg] = colors[v] || ['#1f2937', '#e5e7eb'];
  const banner = $('verdict-banner');
  banner.style.background = bg;
  banner.style.border     = `1px solid ${fg}33`;

  $('verdictLabel').textContent = v;
  $('verdictLabel').style.color = fg;
  $('verdictSub').textContent   = `Confidence: ${verdict.confidence}`;
  $('verdictReasons').textContent = (verdict.reasons || []).join(' ');

  $('vpCurrent').textContent = fmtP(info.price);
  $('vpSticker').textContent = fmtP(val.sticker_price);
  $('vpMOS').textContent     = fmtP(val.mos_price);

  const el = $('vpUpside');
  if (info.price && val.sticker_price) {
    const upside = (val.sticker_price - info.price) / info.price;
    el.textContent = (upside >= 0 ? '+' : '') + fmtPct(upside);
    el.style.color = upside >= 0 ? '#10b981' : '#ef4444';
  } else {
    el.textContent = 'N/A';
    el.style.color = '';
  }
}

function renderNotes(notes) {
  const box = $('dataNotes');
  box.innerHTML = '';
  (notes || []).forEach(t => {
    const div = document.createElement('div');
    div.textContent = 'ⓘ ' + t;
    box.appendChild(div);
  });
  box.style.display = (notes && notes.length) ? 'block' : 'none';
}

function renderScores(scores) {
  const items = [
    { key: 'meaning',    label: 'Meaning',    qualitative: 'Do you understand and believe in the business? Your call.' },
    { key: 'moat',       label: 'Moat',       hint: 'Big 5 numbers vs the 10% target' },
    { key: 'management', label: 'Management', qualitative: 'Is management honest and owner-oriented? Your call.' },
    { key: 'financial',  label: 'Financial Health', hint: 'Returns, debt, liquidity, margins' },
  ];
  const grid = $('scoresGrid');
  const r = 30, circ = 2 * Math.PI * r;
  grid.innerHTML = items.map(({ key, label, qualitative, hint }) => {
    const val = scores[key];
    const has = val != null;
    const color = !has ? '#6b7280' : val >= 7 ? '#10b981' : val >= 5 ? '#f59e0b' : '#ef4444';
    const pct   = has ? val / 10 : 0;
    const sub   = qualitative ? `Not scored — qualitative. ${qualitative}`
                : has ? hint : 'Not enough data to score';
    return `
      <div class="card score-card">
        <div class="score-ring">
          <svg viewBox="0 0 80 80" width="80" height="80">
            <circle cx="40" cy="40" r="${r}" fill="none" stroke="#1f2937" stroke-width="7"/>
            ${has ? `<circle cx="40" cy="40" r="${r}" fill="none" stroke="${color}" stroke-width="7"
              stroke-dasharray="${circ}" stroke-dashoffset="${circ * (1 - pct)}"
              stroke-linecap="round"/>` : ''}
          </svg>
          <div class="score-num" style="color:${color}">${has ? val : '—'}</div>
        </div>
        <div class="score-name">${label}</div>
        <div class="score-hint">${esc(sub)}</div>
      </div>`;
  }).join('');
}

function renderChart(tech) {
  const ctx = document.getElementById('priceChart').getContext('2d');
  if (priceChartInst) { priceChartInst.destroy(); priceChartInst = null; }

  if (!tech.prices || tech.prices.length === 0) {
    $('maValues').textContent = 'Price data unavailable';
    return;
  }

  // Legend values
  const parts = [];
  if (tech.ma50)  parts.push(`50MA: ${fmtP(tech.ma50)}`);
  if (tech.ma200) parts.push(`200MA: ${fmtP(tech.ma200)}`);
  $('maValues').textContent = parts.join('  •  ');

  const gradient = ctx.createLinearGradient(0, 0, 0, 260);
  gradient.addColorStop(0, 'rgba(59,130,246,0.3)');
  gradient.addColorStop(1, 'rgba(59,130,246,0)');

  const hasSeries = a => Array.isArray(a) && a.some(v => v != null);
  const maLine = (label, data, color) => ({
    label, data, borderColor: color, borderWidth: 1.5, borderDash: [4,3],
    pointRadius: 0, fill: false, spanGaps: false, tension: 0.2,
  });

  if (typeof Chart === 'undefined') {
    $('maValues').textContent = 'Chart library failed to load';
    return;
  }

  priceChartInst = new Chart(ctx, {
    type: 'line',
    data: {
      labels: tech.dates,
      datasets: [
        {
          label: 'Price',
          data: tech.prices,
          borderColor: '#3b82f6',
          backgroundColor: gradient,
          borderWidth: 2,
          pointRadius: 0,
          fill: true,
          tension: 0.2,
        },
        hasSeries(tech.ma50_series)  ? maLine('50-day MA',  tech.ma50_series,  '#f59e0b') : null,
        hasSeries(tech.ma200_series) ? maLine('200-day MA', tech.ma200_series, '#8b5cf6') : null,
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
            label: c => ` ${c.dataset.label}: ${c.parsed.y != null ? fmtP(c.parsed.y) : 'N/A'}`
          }
        }
      },
      scales: {
        x: { grid: { color: '#1f2937' }, ticks: { color: '#6b7280', maxTicksLimit: 8, font: { size: 11 } } },
        y: { grid: { color: '#1f2937' }, ticks: { color: '#6b7280', font: { size: 11 }, callback: v => fmt(v, v < 10 ? 2 : 0) } },
      }
    }
  });
}

function renderBig5(b5) {
  const labels = b5.year_labels;
  const gr = b5.growth_rates, yrs = b5.growth_years || {};

  if (!labels || labels.length === 0) {
    $('big5Table').innerHTML = '<tbody><tr><td style="color:var(--muted)">Financial statements unavailable for this ticker.</td></tr></tbody>';
    return;
  }

  // Rule #1 wants every Big 5 number at 10%+ per year.
  const badgeCls = v => v >= 0.10 ? 'cagr-green' : v >= 0 ? 'cagr-grey' : 'cagr-red';
  function badge(val, suffix) {
    return val != null
      ? `<span class="cagr-badge ${badgeCls(val)}">${val >= 0 ? '+' : ''}${(val*100).toFixed(1)}% ${suffix}</span>`
      : `<span class="cagr-badge cagr-grey">N/A</span>`;
  }
  function row(label, values, fmt_fn, badgeHtml, big5) {
    const cells = values.map(v => `<td style="color:${v == null ? '#4b5563' : v < 0 ? '#ef4444' : '#e5e7eb'}">${fmt_fn(v)}</td>`).join('');
    const star = big5 ? '<span class="big5-star" title="One of the Big 5">★</span> ' : '';
    return `<tr><td>${star}${label} <small style="color:var(--muted)">${badgeHtml}</small></td>${cells}</tr>`;
  }
  const growthRow = (label, values, fmt_fn, key, big5) =>
    row(label, values, fmt_fn, badge(gr[key], yrs[key] ? `${yrs[key]}y CAGR` : 'CAGR'), big5);

  const yearHeaders = labels.map(y => `<th>${esc(y)}</th>`).join('');
  const fmtB2 = v => {
    if (v == null) return '—';
    const a = Math.abs(v), s = v < 0 ? '-' : '';
    return s + (a >= 1e12 ? (a/1e12).toFixed(2)+'T' : a >= 1e9 ? (a/1e9).toFixed(1)+'B' : a >= 1e6 ? (a/1e6).toFixed(1)+'M' : a.toFixed(0));
  };
  const fmtD   = v => v == null ? '—' : fmtP(v);
  const fmtPc  = v => v == null ? '—' : (v*100).toFixed(1) + '%';

  $('big5Table').innerHTML = `
    <thead><tr><th>Metric <span style="font-weight:400">(totals in ${esc(CUR.totals)})</span></th>${yearHeaders}</tr></thead>
    <tbody>
      ${row('ROIC', b5.roic, fmtPc, badge(b5.roic_avg, 'avg'), true)}
      ${growthRow('Revenue',        b5.revenues,    fmtB2, 'revenue', true)}
      ${growthRow('EPS (diluted)',  b5.eps_values,  fmtD,  'eps', true)}
      ${growthRow('Book Value/Sh',  b5.bvps,        fmtD,  'bvps', true)}
      ${growthRow('Free Cash Flow', b5.fcf,         fmtB2, 'fcf', true)}
      ${growthRow('Net Income',     b5.net_incomes, fmtB2, 'net_income', false)}
      ${growthRow('Op. Cash Flow',  b5.ocf,         fmtB2, 'ocf', false)}
    </tbody>`;
}

function renderFinancials(fin) {
  const rows1 = [
    { label: 'ROIC',             val: fin.roic,             fmt: fmtPct, goodA: 0.10, warnA: 0.05 },
    { label: 'ROE',              val: fin.roe,              fmt: fmtPct, goodA: 0.15, warnA: 0.10,
      na: fin.negative_equity ? { text: 'N/A (negative equity)', cls: 'warn' } : null },
    { label: 'Operating Margin', val: fin.operating_margin, fmt: fmtPct, goodA: 0.15, warnA: 0.05 },
    { label: 'Profit Margin',    val: fin.profit_margin,    fmt: fmtPct, goodA: 0.10, warnA: 0.05 },
    { label: 'FCF Margin',       val: fin.fcf_margin,       fmt: fmtPct, goodA: 0.10, warnA: 0.05 },
  ];
  const rows2 = [
    { label: 'Debt / Equity',    val: fin.de_ratio,       fmt: v => fmt(v,2)+'x', inv: true, goodA: 0.5, warnA: 1.5,
      na: fin.negative_equity ? { text: 'Negative equity', cls: 'bad' } : null },
    { label: 'Current Ratio',    val: fin.current_ratio,  fmt: v => fmt(v,2)+'x', goodA: 1.5, warnA: 1.0 },
    { label: 'Interest Coverage',val: fin.interest_coverage, fmt: v => fmt(v,1)+'x', goodA: 5, warnA: 2,
      na: fin.no_interest ? { text: 'No debt', cls: 'good' } : null },
  ];

  function makeCard(rows, title) {
    const inner = rows.map(r => {
      let c = r.inv ? clsInv(r.val, r.goodA, r.warnA) : cls(r.val, r.goodA, r.warnA);
      let text = r.val != null ? r.fmt(r.val) : 'N/A';
      if (r.val == null && r.na) { text = r.na.text; c = r.na.cls; }
      return `<div class="metric-row">
        <span class="metric-name">${r.label}</span>
        <span class="metric-val ${c}">${text}</span>
      </div>`;
    }).join('');
    return `<div class="card"><div class="card-title">${title}</div>${inner}</div>`;
  }
  const fy = fin.fiscal_year ? ` — FY${esc(fin.fiscal_year)}` : '';
  $('finGrid').innerHTML = makeCard(rows1, 'Profitability' + fy) + makeCard(rows2, 'Balance Sheet & Safety' + fy);
}

function renderValuation(val, info) {
  const price = info.price;

  function valCard(title, lines) {
    const inner = lines.filter(Boolean).map(([k, v]) => {
      const isObj = v && typeof v === 'object';
      return `
      <div class="metric-row">
        <span class="metric-name">${k}</span>
        <span class="metric-val ${isObj && v.cls ? v.cls : 'neutral'}">${isObj ? v.text : (v || 'N/A')}</span>
      </div>`;
    }).join('');
    return `<div class="card"><div class="card-title">${title}</div>${inner}</div>`;
  }

  // ── Ten Cap ──
  const tcOk = val.ten_cap_per_share != null && price;
  const tenCapClass = tcOk ? (price <= val.ten_cap_per_share ? 'good' : 'bad')
                    : (val.owner_earnings != null && val.owner_earnings <= 0 ? 'bad' : 'neutral');
  const p = val.owner_earnings_parts;
  const oeLines = p ? [
    ['Net Income',            fmtB(p.net_income)],
    ['+ Depreciation & Amort.', fmtB(p.depreciation)],
    ['+ Δ Receivables / Payables', fmtB(p.chg_receivables + p.chg_payables)],
    ['+ Income Tax',          fmtB(p.income_tax)],
    ['− Capex (as maintenance)', fmtB(p.maintenance_capex)],
  ] : (val.owner_earnings_method === 'ocf_less_capex'
        ? [['Method', 'Operating cash flow − capex']] : []);
  const tenCapAssessment = tcOk
    ? { text: price <= val.ten_cap_per_share ? '✅ At or below Ten Cap price' : '❌ Above Ten Cap price', cls: tenCapClass }
    : (val.owner_earnings != null && val.owner_earnings <= 0
        ? { text: '❌ Negative owner earnings', cls: 'bad' } : 'N/A');

  // ── Sticker ──
  const stickerClass = (val.sticker_price && price)
    ? (price <= val.sticker_price ? 'good' : price <= val.sticker_price * 1.5 ? 'warn' : 'bad') : 'neutral';
  const issueText = {
    no_eps: 'N/A — no EPS data', negative_eps: 'N/A — negative earnings',
    no_growth: 'N/A — no positive growth', no_growth_data: 'N/A — no growth data',
  }[val.sticker_issue];
  const growthUsed = val.growth_rate_used != null
    ? fmtPct(val.growth_rate_used) + (val.growth_rate_used >= val.growth_cap - 1e-9 ? ' (capped)' : '')
    : 'N/A';
  const peNote = val.future_pe != null
    ? val.future_pe.toFixed(1) + 'x' : 'N/A';

  // ── Payback ──
  const paybackClass = val.payback_years != null
    ? (val.payback_years <= val.payback_target ? 'good' : val.payback_years <= 15 ? 'warn' : 'bad')
    : (val.payback_over_30 || (val.fcf != null && val.fcf <= 0) ? 'bad' : 'neutral');
  const paybackText = val.payback_years != null ? val.payback_years + ' years'
    : val.payback_over_30 ? '> 30 years'
    : (val.fcf != null && val.fcf <= 0) ? 'Never (negative FCF)' : 'N/A';
  const paybackAssess = val.payback_years != null
    ? { text: val.payback_years <= val.payback_target ? '✅ Passes' : val.payback_years <= 15 ? '⚠️ Marginal' : '❌ Too Long', cls: paybackClass }
    : (paybackClass === 'bad' ? { text: '❌ Fails', cls: 'bad' } : 'N/A');
  const pbPriceClass = (val.payback_price != null && price)
    ? (price <= val.payback_price ? 'good' : 'neutral') : 'neutral';

  const fy = val.fiscal_year ? ` — FY${esc(val.fiscal_year)}` : '';
  $('valGrid').innerHTML = `
    ${valCard('Ten Cap (Owner Earnings)' + fy, [
      ...oeLines,
      ['= Owner Earnings',  fmtB(val.owner_earnings)],
      ['Ten Cap Value (× 10)', fmtB(val.ten_cap_value)],
      ['Market Cap',        fmtB(val.market_cap)],
      ['Ten Cap Price / Share', { text: fmtP(val.ten_cap_per_share), cls: tenCapClass }],
      ['Assessment',        tenCapAssessment],
    ])}
    ${valCard('Sticker Price (10-year projection)', [
      [`Current EPS (${esc(val.eps_source || 'TTM')})`, fmtP(val.current_eps)],
      [`Historical growth${val.historical_growth_source ? ' — ' + esc(val.historical_growth_source) : ''}`, fmtPct(val.historical_growth)],
      [`Analyst growth${val.analyst_growth_source ? ' — ' + esc(val.analyst_growth_source) : ''}`, fmtPct(val.analyst_growth)],
      ['Growth Rate Used (lower of the two)', growthUsed],
      ['Future EPS (10y)', fmtP(val.future_eps)],
      ['Default P/E (2 × growth)', val.default_pe != null ? val.default_pe.toFixed(1)+'x' : 'N/A'],
      ['Historical P/E (median)', val.historical_pe != null ? val.historical_pe.toFixed(1)+'x' : 'N/A'],
      ['Future P/E (lower of the two)', peNote],
      ['Future Price (10y)', fmtP(val.future_price)],
      ['Sticker Price (15% return)', { text: issueText || fmtP(val.sticker_price), cls: issueText ? 'bad' : stickerClass }],
      ['MOS Price (50%)',  { text: fmtP(val.mos_price), cls: (val.mos_price && price && price <= val.mos_price ? 'good' : 'neutral') }],
    ])}
    ${valCard('Payback Time (FCF)' + fy, [
      ['Free Cash Flow',   fmtB(val.fcf)],
      ['FCF Growth Assumed', fmtPct(val.payback_growth)],
      ['Market Cap',       fmtB(val.market_cap)],
      ['Years to Payback', { text: paybackText, cls: paybackClass }],
      ['Target',           `≤ ${val.payback_target} years`],
      [`${val.payback_target}-Year Payback Price / Share`, { text: fmtP(val.payback_price), cls: pbPriceClass }],
      ['Assessment',       paybackAssess],
    ])}`;
}

function renderTechnical(tech, price) {
  $('tMA50').textContent  = tech.ma50  ? fmtP(tech.ma50)  : 'N/A';
  $('tMA200').textContent = tech.ma200 ? fmtP(tech.ma200) : 'N/A';

  function versus(id, ma) {
    const el = $(id);
    if (price && ma) {
      const pct = (price - ma) / ma * 100;
      el.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
      el.className   = 'metric-val ' + (price > ma ? 'good' : 'bad');
    } else { el.textContent = 'N/A'; el.className = 'metric-val neutral'; }
  }
  versus('tVs50',  tech.ma50);
  versus('tVs200', tech.ma200);

  const signalMeta = {
    'above_50ma':    ['Above 50-Day MA',   'sig-green'],
    'below_50ma':    ['Below 50-Day MA',   'sig-red'],
    'above_200ma':   ['Above 200-Day MA',  'sig-green'],
    'below_200ma':   ['Below 200-Day MA',  'sig-red'],
    'golden_cross':  ['50MA above 200MA (Golden Cross) 🌟', 'sig-green'],
    'death_cross':   ['50MA below 200MA (Death Cross) ☠️', 'sig-red'],
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
_CACHE_MAX = 200  # entries
_CACHE_LOCK = threading.Lock()
_SYMBOL_LOCKS = {}

# Yahoo symbols: letters, digits and . - ^ = (BRK-B, SHOP.TO, ^GSPC, EURUSD=X)
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")

def _is_rate_limit(err):
    if type(err).__name__ == "YFRateLimitError":
        return True
    m = str(err).lower()
    return "too many" in m or "rate limit" in m or "rate-limit" in m or "429" in m

def analyze_cached(sym):
    with _CACHE_LOCK:
        hit = _CACHE.get(sym)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
        lock = _SYMBOL_LOCKS.setdefault(sym, threading.Lock())
    # One fetch per symbol at a time; concurrent requests wait and share it.
    with lock:
        with _CACHE_LOCK:
            hit = _CACHE.get(sym)
            if hit and time.time() - hit[0] < _CACHE_TTL:
                return hit[1]
        try:
            for attempt in range(3):
                try:
                    result = analyze(sym)
                    break
                except Exception as e:
                    if _is_rate_limit(e) and attempt < 2:
                        time.sleep(1.5 * (attempt + 1))  # back off, then retry
                        continue
                    raise
            with _CACHE_LOCK:
                if len(_CACHE) >= _CACHE_MAX:
                    for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:_CACHE_MAX // 4]:
                        _CACHE.pop(old, None)
                _CACHE[sym] = (time.time(), result)
            return result
        finally:
            with _CACHE_LOCK:
                _SYMBOL_LOCKS.pop(sym, None)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))

        elif parsed.path == "/api/analyze":
            params = parse_qs(parsed.query)
            ticker = (params.get("ticker", [""])[0]).upper().strip()
            if not ticker:
                self._json(400, {"error": "Missing ticker"})
                return
            if not _TICKER_RE.match(ticker):
                self._json(400, {"error": "That doesn't look like a valid ticker symbol."})
                return
            try:
                print(f"  Analyzing {ticker}…", flush=True)
                data = analyze_cached(ticker)
                payload = json.dumps(clean_for_json(data), allow_nan=False)
                self._send(200, "application/json", payload.encode("utf-8"))
                print(f"  ✓ {ticker} done", flush=True)
            except AnalysisError as e:
                print(f"  ✗ {ticker}: {e}", flush=True)
                self._json(e.status, {"error": str(e)})
            except Exception as e:
                print(f"  ✗ Error: {e}", flush=True)
                if _is_rate_limit(e):
                    self._json(429, {"error": "Yahoo Finance is rate-limiting requests right now. "
                                              "Wait ~30 seconds and try again."})
                else:
                    self._json(502, {"error": f"Could not analyze {ticker}: {e}"})

        else:
            self._send(404, "text/plain", b"Not found")

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode("utf-8"))

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-response

    def log_message(self, *_):
        pass  # suppress access logs

# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def main():
    cloud = bool(os.environ.get("PORT"))
    # Hosted platforms need every interface; on your own machine the dashboard
    # only listens locally so it isn't exposed to the rest of the network.
    host = os.environ.get("HOST") or ("0.0.0.0" if cloud else "127.0.0.1")

    print("\n" + "─"*56)
    print("  Rule #1 Interactive Dashboard — Yahoo Finance")
    print("─"*56)

    try:
        server = ThreadingHTTPServer((host, PORT), Handler)
    except OSError as e:
        print(f"\n  ✗ Could not start on port {PORT}: {e}")
        print(f"    Is another copy already running? Try:  PORT=8081 python3 {os.path.basename(__file__)}\n")
        sys.exit(1)
    server.daemon_threads = True

    print(f"\n  ✓ Server running on http://localhost:{PORT}")
    if cloud:
        print(f"  ✓ Running in cloud mode on {host}:{PORT}")
    else:
        print("  ✓ Opening browser automatically…")
        def open_browser():
            time.sleep(0.8)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=open_browser, daemon=True).start()
    print("\n  Enter any ticker in the search bar.")
    print("  Press Ctrl+C to stop.\n")
    print("─"*56 + "\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  Server stopped. Goodbye!")

if __name__ == "__main__":
    main()
