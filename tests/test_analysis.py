"""Offline tests for the Rule #1 analysis engine.

Yahoo Finance is replaced with small in-memory fixtures, so these run without
network access:

    python -m unittest discover -s tests -v
"""

import json
import math
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rule1_dashboard as r1  # noqa: E402

B = 1e9
YEARS = [pd.Timestamp(f"{y}-09-30") for y in (2022, 2023, 2024, 2025)]
NAN = float("nan")


def frame(rows, periods=YEARS):
    """Statement frame shaped like yfinance's: newest period first."""
    df = pd.DataFrame(rows, index=periods).T
    return df[sorted(df.columns, reverse=True)]


def price_history(start=130.0, end=300.0, tz="America/New_York"):
    idx = pd.bdate_range(end="2025-11-14", periods=1260, tz=tz)
    step = (end - start) / (len(idx) - 1)
    close = [start + i * step for i in range(len(idx))]
    return pd.DataFrame({
        "Open": close, "High": [c * 1.01 for c in close], "Low": [c * 0.99 for c in close],
        "Close": close, "Adj Close": [c * 0.97 for c in close],
        "Volume": [50_000_000] * len(idx),
    }, index=idx)


class FakeTicker:
    def __init__(self, info=None, inc=None, bal=None, cf=None, hist=None, growth=None, fast=None):
        self._fast = fast
        self.info = info or {}
        self.income_stmt = inc if inc is not None else pd.DataFrame()
        self.balance_sheet = bal if bal is not None else pd.DataFrame()
        self.cash_flow = cf if cf is not None else pd.DataFrame()
        self._hist = hist if hist is not None else pd.DataFrame()
        self.growth_estimates = growth if growth is not None else pd.DataFrame()
        self.history_calls = []

    def __getattr__(self, name):
        if name == "fast_info" and self._fast is not None:
            return self._fast
        raise AttributeError(name)

    def history(self, **kw):
        self.history_calls.append(kw)
        return self._hist


class FakeYF:
    def __init__(self, tickers):
        self.tickers = tickers

    def Ticker(self, sym):
        return self.tickers.get(sym, FakeTicker())


def growth_frame(**periods):
    return pd.DataFrame({"stockTrend": periods}).rename(index=lambda p: p.replace("p", "+"))


# ── A large, profitable, buyback-heavy company (figures in the style of AAPL) ──
def base_info(**over):
    info = {
        "quoteType": "EQUITY", "longName": "Example Corp", "currency": "USD",
        "financialCurrency": "USD", "currentPrice": 300.0, "previousClose": 298.5,
        "marketCap": 300.0 * 14.7 * B, "sharesOutstanding": 14.7 * B,
        "trailingEps": 8.0, "trailingPE": 37.5, "beta": 1.1,
        "dividendRate": 1.04, "dividendYield": 0.35,       # Yahoo: already in percent
        "trailingAnnualDividendYield": 0.0034,
        "earningsGrowth": 0.91,                            # quarterly YoY — must be ignored
        "sector": "Technology", "industry": "Consumer Electronics",
    }
    info.update(over)
    return info


def base_inc(**over):
    rows = {
        "Total Revenue":          [394.3 * B, 383.3 * B, 391.0 * B, 416.2 * B],
        "Net Income":             [99.8 * B, 97.0 * B, 93.7 * B, 112.0 * B],
        "Operating Income":       [119.4 * B, 114.3 * B, 123.2 * B, 133.1 * B],
        "Pretax Income":          [119.1 * B, 113.7 * B, 123.5 * B, 132.7 * B],
        "Tax Provision":          [19.3 * B, 16.7 * B, 29.7 * B, 20.7 * B],
        "Tax Rate For Calcs":     [0.162, 0.147, 0.241, 0.156],
        "Interest Expense":       [2.93 * B, 3.93 * B, NAN, NAN],
        "Diluted EPS":            [6.11, 6.13, 6.08, 7.46],
        "Diluted Average Shares": [16.326 * B, 15.813 * B, 15.408 * B, 15.005 * B],
    }
    rows.update(over)
    return frame(rows)


def base_bal(**over):
    rows = {
        "Stockholders Equity":    [50.67 * B, 62.15 * B, 56.95 * B, 73.73 * B],
        "Total Debt":             [132.5 * B, 123.9 * B, 119.1 * B, 112.4 * B],
        "Invested Capital":       [170.7 * B, 173.2 * B, 163.6 * B, 172.3 * B],
        "Current Assets":         [135.4 * B, 143.6 * B, 153.0 * B, 148.0 * B],
        "Current Liabilities":    [154.0 * B, 145.3 * B, 176.4 * B, 165.6 * B],
        "Ordinary Shares Number": [15.943 * B, 15.55 * B, 15.117 * B, 14.773 * B],
    }
    rows.update(over)
    return frame(rows)


def base_cf(**over):
    rows = {
        "Operating Cash Flow":            [122.2 * B, 110.5 * B, 118.3 * B, 111.5 * B],
        "Capital Expenditure":            [-10.7 * B, -10.96 * B, -9.45 * B, -12.7 * B],
        "Free Cash Flow":                 [111.4 * B, 99.6 * B, 108.8 * B, 98.8 * B],
        "Depreciation And Amortization":  [11.1 * B, 11.5 * B, 11.4 * B, 11.7 * B],
        "Changes In Account Receivables": [-1.8 * B, -1.7 * B, -3.8 * B, -4.6 * B],
        "Change In Account Payable":      [9.4 * B, -1.9 * B, 6.0 * B, 0.9 * B],
    }
    rows.update(over)
    return frame(rows)


def base_ticker(**kw):
    kw.setdefault("info", base_info())
    kw.setdefault("inc", base_inc())
    kw.setdefault("bal", base_bal())
    kw.setdefault("cf", base_cf())
    kw.setdefault("hist", price_history())
    kw.setdefault("growth", growth_frame(LTG=0.105, p1y=0.12))
    return FakeTicker(**kw)


class AnalysisCase(unittest.TestCase):
    def run_analysis(self, sym="TEST", extra=None, **kw):
        tk = kw.pop("ticker", None) or base_ticker(**kw)
        tickers = {sym: tk}
        tickers.update(extra or {})
        old = r1.yf
        r1.yf = FakeYF(tickers)
        r1._FX_CACHE.clear()
        try:
            return r1.analyze(sym)
        finally:
            r1.yf = old


class TestHelpers(unittest.TestCase):
    def test_cagr_basic(self):
        rate, years = r1.cagr([100, None, None, 133.1])
        self.assertAlmostEqual(rate, 0.10, places=4)   # 3 years, not 1
        self.assertEqual(years, 3)

    def test_cagr_uses_period_dates(self):
        rate, years = r1.cagr([100, 121], [pd.Timestamp("2021-12-31"), pd.Timestamp("2023-12-31")])
        self.assertAlmostEqual(rate, 0.10, places=3)
        self.assertEqual(years, 2)

    def test_cagr_undefined_for_negative_endpoints(self):
        self.assertIsNone(r1.cagr([-5, 10, 20])[0])
        self.assertIsNone(r1.cagr([5, 10, -20])[0])
        self.assertIsNone(r1.cagr([5])[0])

    def test_cagr_never_hides_a_loss_behind_an_older_profit(self):
        # Old behaviour dropped the negative year and reported healthy growth.
        self.assertIsNone(r1.cagr([10, 20, 30, -40])[0])
        self.assertTrue(r1.turned_negative([10, 20, 30, -40]))

    def test_score_ignores_missing_metrics(self):
        self.assertEqual(r1.to_score([2, 2, None, None, 2]), 10)
        self.assertEqual(r1.to_score([0, 0, 0]), 1)
        self.assertIsNone(r1.to_score([2, None, None]))

    def test_equivalent_shares_handles_splits_and_adrs(self):
        s, exact = r1.equivalent_shares([100, 100, 1000, 1000], 1000)
        self.assertEqual(s, [1000, 1000, 1000, 1000])
        self.assertFalse(exact)
        s, exact = r1.equivalent_shares([26e9, 26e9], 5.2e9)       # 5 ordinary shares per ADR
        self.assertAlmostEqual(s[-1], 5.2e9)
        s, exact = r1.equivalent_shares([105, 103, 101, 100], 99)  # ordinary buybacks
        self.assertTrue(exact)
        self.assertEqual(s, [105, 103, 101, 100])


class TestVerdict(unittest.TestCase):
    """decide_verdict(price, sticker, mos, ten_cap_ok, payback_ok, strong_fin, good_moat)"""
    STICKER, MOS = 100.0, 50.0

    def v(self, price, ten_cap, payback, fin=True, moat=True):
        verdict, confidence, reasons = r1.decide_verdict(
            price, self.STICKER, self.MOS, ten_cap, payback, fin, moat)
        self.assertTrue(reasons)
        return verdict, confidence

    # ── at or below the Margin of Safety price ──
    def test_mos_quality_and_agreement_is_strong_buy(self):
        self.assertEqual(self.v(45, True, True), ("STRONG BUY", "High"))
        self.assertEqual(self.v(45, True, None), ("STRONG BUY", "High"))
        self.assertEqual(self.v(45, None, None), ("STRONG BUY", "High"))   # no result ≠ disagreement

    def test_mos_quality_but_disagreement_holds_back_strong_buy(self):
        self.assertEqual(self.v(45, False, True), ("BUY", "Medium"))
        self.assertEqual(self.v(45, False, False), ("BUY", "Medium"))

    def test_mos_with_one_quality_score(self):
        self.assertEqual(self.v(45, True, True, moat=False), ("BUY", "Medium"))
        self.assertEqual(self.v(45, None, None, fin=False), ("BUY", "Medium"))
        self.assertEqual(self.v(45, False, None, moat=False), ("BUY", "Low"))

    def test_other_methods_never_buy_a_poor_business(self):
        self.assertEqual(self.v(45, True, True, fin=False, moat=False), ("WATCHLIST", "Low"))

    # ── between MOS and Sticker ──
    def test_below_sticker_is_watchlist_with_confidence_from_other_methods(self):
        self.assertEqual(self.v(80, True, True), ("WATCHLIST", "High"))
        self.assertEqual(self.v(80, True, None), ("WATCHLIST", "High"))
        self.assertEqual(self.v(80, True, False), ("WATCHLIST", "Medium"))
        self.assertEqual(self.v(80, None, None), ("WATCHLIST", "Medium"))
        self.assertEqual(self.v(80, False, False), ("WATCHLIST", "Low"))
        self.assertEqual(self.v(80, None, False), ("WATCHLIST", "Low"))

    # ── above Sticker ──
    def test_above_sticker_lifted_only_by_both_methods_or_a_near_miss(self):
        self.assertEqual(self.v(300, True, True), ("WATCHLIST", "Low"))      # both pass
        self.assertEqual(self.v(115, True, False), ("WATCHLIST", "Low"))     # within 20% + one passes
        self.assertEqual(self.v(115, True, None), ("WATCHLIST", "Low"))
        self.assertEqual(self.v(125, True, False), ("AVOID / WAIT", "Medium"))   # not near, only one
        self.assertEqual(self.v(115, None, None), ("AVOID / WAIT", "Medium"))    # near, nothing agrees
        self.assertEqual(self.v(115, False, False), ("AVOID / WAIT", "High"))
        self.assertEqual(self.v(300, False, None), ("AVOID / WAIT", "High"))

    def test_other_methods_alone_never_produce_a_buy(self):
        for price in (51, 80, 115, 300):
            for fin in (True, False):
                for moat in (True, False):
                    verdict, _ = self.v(price, True, True, fin=fin, moat=moat)
                    self.assertNotIn("BUY", verdict, (price, fin, moat))


class TestAnalysis(AnalysisCase):
    def test_verdict_lists_each_method(self):
        d = self.run_analysis()                       # price 300 vs sticker ~113
        m = {x["name"]: x["result"] for x in d["verdict"]["methods"]}
        self.assertEqual(m, {"Margin of Safety": "fail", "Ten Cap": "fail", "Payback Time": "fail"})
        self.assertEqual((d["verdict"]["verdict"], d["verdict"]["confidence"]), ("AVOID / WAIT", "High"))

    def test_both_other_methods_passing_lifts_to_watchlist(self):
        # Trades at ~6x earnings: above its (P/E-capped) Sticker Price, but
        # owner earnings and free cash flow say it is cheap.
        d = self.run_analysis(hist=price_history(30.0, 45.0),
                              info=base_info(currentPrice=45.0, previousClose=44.5,
                                             marketCap=45.0 * 14.7 * B, trailingPE=5.6))
        self.assertGreater(45.0, d["valuation"]["sticker_price"])
        m = {x["name"]: x["result"] for x in d["verdict"]["methods"]}
        self.assertEqual(m, {"Margin of Safety": "fail", "Ten Cap": "pass", "Payback Time": "pass"})
        self.assertEqual((d["verdict"]["verdict"], d["verdict"]["confidence"]), ("WATCHLIST", "Low"))

    def test_methods_without_a_result_are_not_counted(self):
        # No exchange rate → Ten Cap and Payback can't be compared with the price.
        d = self.run_analysis(info=base_info(financialCurrency="TWD"))
        m = {x["name"]: x["result"] for x in d["verdict"]["methods"]}
        self.assertEqual(m["Ten Cap"], "na")
        self.assertEqual(m["Payback Time"], "na")
        self.assertEqual((d["verdict"]["verdict"], d["verdict"]["confidence"]), ("AVOID / WAIT", "Medium"))
        # Negative free cash flow is "no result" for Payback Time, not a fail.
        cf = base_cf(**{"Free Cash Flow": [5 * B, 2 * B, -1 * B, -4 * B]})
        m = {x["name"]: x["result"] for x in self.run_analysis(cf=cf)["verdict"]["methods"]}
        self.assertEqual(m["Payback Time"], "na")

    def test_no_sticker_price_is_never_overridden(self):
        inc = base_inc(**{"Diluted EPS": [0.33, 0.13, -0.2, -0.53]})
        d = self.run_analysis(inc=inc, hist=price_history(30.0, 45.0),
                              info=base_info(currentPrice=45.0, marketCap=45.0 * 14.7 * B,
                                             trailingEps=-0.53, trailingPE=None))
        m = {x["name"]: x["result"] for x in d["verdict"]["methods"]}
        self.assertEqual(m["Margin of Safety"], "na")
        self.assertEqual(m["Ten Cap"], "pass")
        self.assertEqual(d["verdict"]["verdict"], "AVOID / WAIT")
        self.assertIn("does not buy without a Sticker Price", " ".join(d["verdict"]["reasons"]))

    def test_payload_is_strict_json(self):
        d = self.run_analysis()
        json.dumps(r1.clean_for_json(d), allow_nan=False)

    def test_dividend_yield_is_a_fraction(self):
        d = self.run_analysis()
        self.assertAlmostEqual(d["info"]["dividend_yield"], 1.04 / 300.0, places=6)
        d = self.run_analysis(info=base_info(dividendRate=None, trailingAnnualDividendYield=None))
        self.assertAlmostEqual(d["info"]["dividend_yield"], 0.0035, places=6)

    def test_eps_and_bvps_use_each_years_share_count(self):
        d = self.run_analysis()
        self.assertEqual(d["big5"]["eps_values"], [6.11, 6.13, 6.08, 7.46])
        self.assertAlmostEqual(d["big5"]["bvps"][0], 50.67 / 15.943, places=3)
        self.assertAlmostEqual(d["big5"]["bvps"][-1], 73.73 / 14.773, places=3)

    def test_growth_rates(self):
        gr = self.run_analysis()["big5"]["growth_rates"]
        self.assertAlmostEqual(gr["revenue"], (416.2 / 394.3) ** (1 / 3) - 1, places=3)
        self.assertAlmostEqual(gr["eps"], (7.46 / 6.11) ** (1 / 3) - 1, places=3)
        self.assertLess(gr["fcf"], 0)

    def test_roic_is_nopat_over_invested_capital(self):
        d = self.run_analysis()
        self.assertAlmostEqual(d["financials"]["roic"], 133.1 * (1 - 0.156) / 172.3, places=4)
        self.assertEqual(len(d["big5"]["roic"]), 4)

    def test_sticker_price_math(self):
        v = self.run_analysis()["valuation"]
        # Growth: lower of BVPS growth (~16%) and analysts' long-term 10.5%;
        # the quarterly `earningsGrowth` field (91%) must play no part.
        self.assertAlmostEqual(v["analyst_growth"], 0.105)
        self.assertEqual(v["analyst_growth_source"], "long-term")
        self.assertAlmostEqual(v["growth_rate_used"], 0.105)
        self.assertAlmostEqual(v["default_pe"], 21.0)
        self.assertLessEqual(v["future_pe"], v["default_pe"])
        fut_eps = 8.0 * 1.105 ** 10
        self.assertAlmostEqual(v["future_eps"], fut_eps, places=6)
        self.assertAlmostEqual(v["sticker_price"], fut_eps * v["future_pe"] / 1.15 ** 10, places=6)
        self.assertAlmostEqual(v["mos_price"], v["sticker_price"] / 2, places=6)

    def test_future_pe_capped_by_historical_pe(self):
        # Stock that has always traded around 9–10x earnings.
        d = self.run_analysis(hist=price_history(start=40.0, end=75.0),
                              info=base_info(currentPrice=75.0, marketCap=75.0 * 14.7 * B))
        v = d["valuation"]
        self.assertIsNotNone(v["historical_pe"])
        self.assertLess(v["historical_pe"], v["default_pe"])
        self.assertAlmostEqual(v["future_pe"], v["historical_pe"])

    def test_growth_is_capped_at_15_percent(self):
        v = self.run_analysis(growth=growth_frame(LTG=0.40))["valuation"]
        self.assertAlmostEqual(v["growth_rate_used"], 0.15)
        self.assertLessEqual(v["future_pe"], 30.0)

    def test_owner_earnings_follow_town_formula(self):
        v = self.run_analysis()["valuation"]
        expect = (112.0 + 11.7 - 4.6 + 0.9 + 20.7 - 12.7) * B
        self.assertAlmostEqual(v["owner_earnings"], expect, delta=1e6)
        self.assertAlmostEqual(v["ten_cap_value"], expect * 10, delta=1e7)
        self.assertAlmostEqual(v["ten_cap_per_share"], expect * 10 / (14.7 * B), places=4)

    def test_payback_time(self):
        v = self.run_analysis()["valuation"]
        self.assertAlmostEqual(v["fcf"], 98.8 * B, delta=1e6)
        g, cum, years = v["payback_growth"], 0.0, None
        f = 98.8 * B
        for y in range(1, 31):
            f *= 1 + g
            cum += f
            if cum >= v["market_cap"]:
                years = y
                break
        self.assertEqual(v["payback_years"], years)
        self.assertAlmostEqual(g, v["growth_rate_used"])       # not the raw FCF CAGR
        eight = sum(98.8 * B * (1 + g) ** y for y in range(1, 9))
        self.assertAlmostEqual(v["payback_price"], eight / (14.7 * B), places=4)

    def test_placeholder_scores_are_gone(self):
        s = self.run_analysis()["scores"]
        self.assertIsNone(s["meaning"])
        self.assertIsNone(s["management"])
        self.assertTrue(1 <= s["moat"] <= 10)
        self.assertTrue(1 <= s["financial"] <= 10)

    def test_statements_with_different_years_stay_aligned(self):
        five = [pd.Timestamp("2021-09-30")] + YEARS
        inc = frame({
            "Total Revenue": [NAN, 394.3 * B, 383.3 * B, 391.0 * B, 416.2 * B],
            "Net Income":    [NAN, 99.8 * B, 97.0 * B, 93.7 * B, 112.0 * B],
            "Diluted EPS":   [NAN, 6.11, 6.13, 6.08, 7.46],
            "Diluted Average Shares": [NAN, 16.326 * B, 15.813 * B, 15.408 * B, 15.005 * B],
        }, five)
        cf = frame({
            "Operating Cash Flow": [110.5 * B, 118.3 * B, 111.5 * B],
            "Capital Expenditure": [-10.96 * B, -9.45 * B, -12.7 * B],
        }, YEARS[1:])
        b5 = self.run_analysis(inc=inc, cf=cf)["big5"]
        self.assertEqual(b5["year_labels"], ["2022", "2023", "2024", "2025"])   # empty 2021 stub dropped
        for key in ("revenues", "eps_values", "bvps", "ocf", "fcf", "roic"):
            self.assertEqual(len(b5[key]), 4, key)
        self.assertIsNone(b5["ocf"][0])                     # 2022 cash flow wasn't reported…
        self.assertAlmostEqual(b5["ocf"][1], 110.5 * B)     # …so 2023 stays under 2023
        self.assertAlmostEqual(b5["fcf"][-1], (111.5 - 12.7) * B)
        self.assertEqual(b5["growth_years"]["ocf"], 2)

    def test_debt_free_company_is_not_penalised(self):
        bal = base_bal(**{"Total Debt": [0.0] * 4,
                          "Invested Capital": [50.67 * B, 62.15 * B, 56.95 * B, 73.73 * B],
                          "Current Assets": [300 * B] * 4})
        inc = base_inc(**{"Interest Expense": [NAN] * 4})
        f = self.run_analysis(bal=bal, inc=inc)["financials"]
        self.assertIsNotNone(f["roic"])
        self.assertEqual(f["de_ratio"], 0.0)
        self.assertTrue(f["no_interest"])
        self.assertEqual(self.run_analysis(bal=bal, inc=inc)["scores"]["financial"], 10)

    def test_negative_earnings_produce_no_sticker_price(self):
        inc = base_inc(**{"Net Income": [5 * B, 2 * B, -3 * B, -8 * B],
                          "Operating Income": [6 * B, 3 * B, -2 * B, -7 * B],
                          "Diluted EPS": [0.33, 0.13, -0.2, -0.53]})
        d = self.run_analysis(inc=inc, info=base_info(trailingEps=-0.53, trailingPE=None))
        v = d["valuation"]
        self.assertEqual(v["sticker_issue"], "negative_eps")
        self.assertIsNone(v["sticker_price"])
        self.assertIsNone(v["mos_price"])
        self.assertEqual(d["verdict"]["verdict"], "AVOID / WAIT")
        self.assertIsNone(d["big5"]["growth_rates"]["eps"])
        self.assertLess(d["financials"]["roic"], 0)

    def test_no_default_growth_when_business_is_shrinking(self):
        bal = base_bal(**{"Stockholders Equity": [80 * B, 70 * B, 60 * B, 50 * B],
                          "Ordinary Shares Number": [15 * B] * 4})
        inc = base_inc(**{"Diluted EPS": [8.0, 7.0, 6.0, 5.0]})
        d = self.run_analysis(bal=bal, inc=inc, growth=growth_frame(LTG=-0.03))
        self.assertIsNone(d["valuation"]["growth_rate_used"])     # used to silently assume 10%
        self.assertEqual(d["valuation"]["sticker_issue"], "no_growth")
        self.assertIsNone(d["valuation"]["sticker_price"])

    def test_short_term_analyst_estimates_are_annualised_over_two_years(self):
        v = self.run_analysis(growth=growth_frame(LTG=NAN, p1y=0.02, **{"0y": 0.10}))["valuation"]
        self.assertAlmostEqual(v["analyst_growth"], math.sqrt(1.10 * 1.02) - 1, places=6)
        self.assertEqual(v["analyst_growth_source"], "next 2 fiscal years")
        self.assertAlmostEqual(v["growth_rate_used"], v["analyst_growth"], places=6)

    def test_growth_is_strictly_the_lower_of_history_and_analysts(self):
        # History is strong, but analysts expect earnings to fall: no flattering fallback.
        d = self.run_analysis(growth=growth_frame(LTG=-0.05))
        self.assertIsNone(d["valuation"]["growth_rate_used"])
        self.assertEqual(d["valuation"]["sticker_issue"], "no_growth")
        self.assertIn("analysts expect earnings to shrink", " ".join(d["verdict"]["reasons"]))

    def test_depreciation_line_is_picked_per_year(self):
        cf = base_cf(**{"Depreciation And Amortization": [11.1 * B, 11.5 * B, NAN, NAN],
                        "Depreciation": [NAN, NAN, 11.4 * B, 11.7 * B]})
        v = self.run_analysis(cf=cf)["valuation"]
        self.assertEqual(v["owner_earnings_method"], "town")
        self.assertAlmostEqual(v["owner_earnings_parts"]["depreciation"], 11.7 * B)

    def test_pence_quoted_stock(self):
        # Price in pence; market cap, EPS and dividend reported in pounds.
        info = base_info(currency="GBp", financialCurrency="GBP", currentPrice=30000.0,
                         previousClose=29850.0, trailingEps=8.0, trailingPE=37.5,
                         dividendRate=1.04, marketCap=300.0 * 14.7 * B)
        d = self.run_analysis(info=info, hist=price_history(13000.0, 30000.0))
        self.assertAlmostEqual(d["info"]["shares"], 14.7 * B, delta=1e3)
        self.assertAlmostEqual(d["info"]["dividend_yield"], 1.04 / 300.0, places=6)
        self.assertAlmostEqual(d["valuation"]["current_eps"], 800.0)
        self.assertAlmostEqual(d["big5"]["eps_values"][-1], 746.0)
        self.assertAlmostEqual(d["big5"]["revenues"][-1], 416.2 * B)        # totals stay in pounds
        self.assertAlmostEqual(d["valuation"]["ten_cap_per_share"],
                               128.0 * B * 10 * 100 / (14.7 * B), places=2)
        self.assertEqual(d["currency"]["per_share"], "GBp")
        self.assertEqual(d["currency"]["statements"], "GBP")

    def test_multi_class_shares_use_market_cap_over_price(self):
        # Yahoo reports only one share class in sharesOutstanding (as for GOOGL).
        d = self.run_analysis(info=base_info(sharesOutstanding=7.0 * B))
        self.assertAlmostEqual(d["info"]["shares"], 14.7 * B, delta=1e3)
        self.assertEqual(d["big5"]["eps_values"][-1], 7.46)

    def test_adr_statements_are_converted_to_the_price_currency(self):
        # Reports in TWD, trades in USD as an ADR worth 5 ordinary shares.
        fx = FakeTicker(hist=pd.DataFrame({"Close": [0.031, 0.032]},
                                          index=pd.to_datetime(["2025-11-13", "2025-11-14"])))
        adr_shares = 14.7 * B / 5
        info = base_info(financialCurrency="TWD", marketCap=300.0 * adr_shares,
                         sharesOutstanding=adr_shares)
        d = self.run_analysis(info=info, extra={"TWDUSD=X": fx})
        self.assertTrue(d["currency"]["comparable"])
        self.assertEqual(d["currency"]["statements"], "USD")
        self.assertAlmostEqual(d["big5"]["revenues"][-1], 416.2 * B * 0.032, delta=1e6)
        self.assertAlmostEqual(d["big5"]["eps_values"][-1],
                               112.0 * B * 0.032 / (15.005 * B / 5), places=3)
        self.assertTrue(any("converted" in n for n in d["notes"]))
        # Growth rates don't depend on the exchange rate.
        self.assertAlmostEqual(d["big5"]["growth_rates"]["revenue"],
                               (416.2 / 394.3) ** (1 / 3) - 1, places=3)

    def test_missing_fx_rate_disables_price_comparisons(self):
        d = self.run_analysis(info=base_info(financialCurrency="TWD"))
        self.assertFalse(d["currency"]["comparable"])
        self.assertIsNone(d["valuation"]["ten_cap_per_share"])
        self.assertIsNone(d["valuation"]["payback_years"])

    def test_technicals_use_unadjusted_closes_and_full_ma_series(self):
        tk = base_ticker()
        t = self.run_analysis(ticker=tk)["technical"]
        self.assertEqual(tk.history_calls[0].get("auto_adjust"), False)
        self.assertEqual(len(t["prices"]), len(t["dates"]))
        self.assertEqual(len(t["ma50_series"]), len(t["prices"]))
        self.assertEqual(len(t["ma200_series"]), len(t["prices"]))
        self.assertTrue(all(v is not None for v in t["ma200_series"]))   # drawn across the whole year
        self.assertTrue(240 <= len(t["prices"]) <= 270)
        self.assertAlmostEqual(t["prices"][-1], 300.0)
        self.assertAlmostEqual(t["52w_high"], 303.0)                     # day high, not close
        self.assertIn("golden_cross", t["signals"])

    def test_unknown_ticker_is_an_error_not_a_verdict(self):
        with self.assertRaises(r1.AnalysisError) as cm:
            self.run_analysis(ticker=FakeTicker())
        self.assertEqual(cm.exception.status, 404)

    def test_funds_are_rejected(self):
        with self.assertRaises(r1.AnalysisError) as cm:
            self.run_analysis(ticker=FakeTicker(info={"quoteType": "ETF", "regularMarketPrice": 500}))
        self.assertEqual(cm.exception.status, 422)

    def test_price_only_ticker_reports_insufficient_data(self):
        d = self.run_analysis(ticker=FakeTicker(info={"quoteType": "EQUITY", "currentPrice": 10.0,
                                                      "currency": "USD"}))
        self.assertEqual(d["verdict"]["verdict"], "INSUFFICIENT DATA")
        self.assertIsNone(d["scores"]["moat"])
        self.assertIsNone(d["scores"]["financial"])


class TestThrottledQuote(AnalysisCase):
    """Yahoo throttling makes yfinance return an empty quote without raising."""
    FAST = {"lastPrice": 300.0, "previousClose": 298.5, "marketCap": 300.0 * 14.7 * B,
            "shares": 14.7 * B, "currency": "USD", "quoteType": "EQUITY"}

    def throttled(self):
        return base_ticker(info={}, fast=dict(self.FAST), growth=pd.DataFrame())

    def test_empty_quote_is_a_retryable_error(self):
        with self.assertRaises(r1.IncompleteData) as cm:
            self.run_analysis(ticker=self.throttled())
        self.assertTrue(r1._is_rate_limit(cm.exception))

    def test_partial_result_is_labelled_and_not_cached(self):
        old_yf, old_sleep = r1.yf, r1.time.sleep
        r1.yf = FakeYF({"SLOW": self.throttled()})
        r1.time.sleep = lambda *_: None
        r1._CACHE.clear()
        try:
            d = r1.analyze_cached("SLOW")
            self.assertTrue(d["partial"])
            self.assertEqual(d["info"]["price"], 300.0)
            self.assertTrue(any("rate-limiting" in n for n in d["notes"]))
            self.assertEqual(d["valuation"]["eps_source"], "FY2025")   # no TTM EPS → latest fiscal year
            self.assertNotIn("SLOW", r1._CACHE)
        finally:
            r1.yf, r1.time.sleep = old_yf, old_sleep


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = r1.yf
        r1.yf = FakeYF({"GOOD": base_ticker()})
        r1._CACHE.clear()
        cls.server = r1.ThreadingHTTPServer(("127.0.0.1", 0), r1.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        r1.yf = cls.old
        r1._CACHE.clear()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as res:
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Rule #1 Dashboard", body)

    def test_analyze_ok(self):
        status, body = self.get("/api/analyze?ticker=good")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["info"]["ticker"], "GOOD")
        self.assertFalse(any(isinstance(v, float) and math.isnan(v) for v in data["technical"]["prices"]))

    def test_bad_requests(self):
        self.assertEqual(self.get("/api/analyze")[0], 400)
        self.assertEqual(self.get("/api/analyze?ticker=%3Cscript%3E")[0], 400)
        status, body = self.get("/api/analyze?ticker=NOPE")
        self.assertEqual(status, 404)
        self.assertIn("No data found", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
