# news-flags (data branch)

Written once a day by a scheduled Claude agent; read by the Rule #1 dashboard
(`rule1_dashboard.py` on the main code branch). Only `news_flags.json` lives here.

Each flag is keyed by Yahoo Finance ticker:

| field | meaning |
|---|---|
| `severity` | `critical` (integrity/legal crisis at the top: CEO ousted for misconduct, fraud, accounting restatement, criminal probe) → Management capped at 3/10 and no Buy; `major` (unplanned CEO/CFO exit, regulator investigation, big lawsuit over conduct, auditor resignation) → capped at 6/10; `minor` (shown, small score effect) |
| `headline`, `summary` | one line, and 1–3 sentences |
| `first_seen`, `last_confirmed` | YYYY-MM-DD; the dashboard ignores flags not confirmed for 90 days |
| `sources` | list of `{title, url}` |

Edit by hand any time: delete an entry to clear a flag.
