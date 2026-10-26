#!/usr/bin/env python3
"""
03_validate.py  -  Referential integrity and validation for Wildcat Capital (HW04, Part D)

Runs the SAME checks on two stages and compares them:
    raw    <project>/<table>.csv
    clean  <project>/data/clean/<table>_clean.csv

Contents
    1. check_referential_integrity(fact_df, fact_key, dim_df, dim_key)
         reusable (Assignment 5): match rate, orphan rows, distinct orphan keys, null-key rate
         applied to all 13 relationships (10 ID-based + 3 to dim_date)
    2. Rule-based checks for every column with a natural business bound
         (valid ranges, allowed code lists, sign rules, required fields, recomputable values)
    3. Cross-table reconciliations (5)
    4. IQR outlier flags on 4 numeric columns that have no natural upper bound
    5. Scorecard: one row per table per stage with
         % completeness, % key uniqueness, % type validity, % rule pass, % referential match
         (+ % rule pass excluding rows that 02_clean.py flagged as known exceptions)

Outputs
    reports/data_quality_scorecard.csv   - the scorecard
    reports/validation_details.csv       - every individual check behind the scorecard

Run from VS Code:   python scripts/03_validate.py
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name.lower() == "scripts" else HERE
REPORTS = ROOT / "reports"
STAGES = {"raw": lambda t: ROOT / f"{t}.csv",
          "clean": lambda t: ROOT / "data" / "clean" / f"{t}_clean.csv"}

TABLES = ["dim_advisor", "dim_benchmark", "dim_client", "dim_date", "dim_security",
          "fact_fees", "fact_portfolio_snapshot", "fact_transactions"]

IQR_EXPLANATION = (
    "An IQR flag means a value is statistically unusual compared with the rest of its "
    "column, not that it is wrong - a wealth manager with UHNW clients is expected to have "
    "very large accounts, trades and fees, so flagged rows are for review, not for deletion."
)


# =============================================================================
# 1. REUSABLE REFERENTIAL-INTEGRITY FUNCTION   (keep for Assignment 5)
# =============================================================================
def check_referential_integrity(fact_df: pd.DataFrame, fact_key: str,
                                dim_df: pd.DataFrame, dim_key: str) -> dict:
    """
    Check that every value of fact_df[fact_key] exists in dim_df[dim_key].

    Keys are compared as trimmed text, so it works on frames read with dtype=str and on
    typed frames alike ("7" and 7 both become "7"; "7.0" is normalised to "7").
    Null / blank keys are not orphans: they are reported separately as the null-key rate,
    and the match rate is computed over non-null keys only.

    Returns a dict:
        rows                    total fact rows
        null_keys               rows with a null/blank key
        null_key_rate           % of rows with a null/blank key
        matched_rows            non-null keys found in the dimension
        match_rate              % of non-null keys found in the dimension
        orphan_row_count        non-null keys NOT found in the dimension
        orphan_rows             DataFrame of those fact rows
        distinct_orphan_keys    sorted list of the distinct orphan key values
    """
    def norm(s: pd.Series) -> pd.Series:
        s = s.astype("string").str.strip()
        return s.str.replace(r"^(-?\d+)\.0+$", r"\1", regex=True)

    fk = norm(fact_df[fact_key])
    pk = set(norm(dim_df[dim_key]).dropna())
    is_null = fk.isna() | (fk == "")
    is_orphan = ~is_null & ~fk.isin(pk)
    rows = len(fk)
    non_null = int((~is_null).sum())
    matched = non_null - int(is_orphan.sum())
    return {
        "rows": rows,
        "null_keys": int(is_null.sum()),
        "null_key_rate": round(100 * is_null.sum() / rows, 4) if rows else 0.0,
        "matched_rows": matched,
        "match_rate": round(100 * matched / non_null, 4) if non_null else 100.0,
        "orphan_row_count": int(is_orphan.sum()),
        "orphan_rows": fact_df.loc[is_orphan.fillna(False).values],
        "distinct_orphan_keys": sorted(fk[is_orphan].unique().tolist()),
    }


# The 13 relationships: (fact/child table, key, dimension/parent table, key)
RELATIONSHIPS = [
    # 10 ID-based
    ("dim_client", "advisor_id", "dim_advisor", "advisor_id"),
    ("dim_security", "benchmark_id", "dim_benchmark", "benchmark_id"),
    ("fact_fees", "client_id", "dim_client", "client_id"),
    ("fact_fees", "advisor_id", "dim_advisor", "advisor_id"),
    ("fact_portfolio_snapshot", "client_id", "dim_client", "client_id"),
    ("fact_portfolio_snapshot", "advisor_id", "dim_advisor", "advisor_id"),
    ("fact_portfolio_snapshot", "benchmark_id", "dim_benchmark", "benchmark_id"),
    ("fact_transactions", "client_id", "dim_client", "client_id"),
    ("fact_transactions", "advisor_id", "dim_advisor", "advisor_id"),
    ("fact_transactions", "security_id", "dim_security", "security_id"),
    # 3 joins to the date dimension (on the calendar date, not date_id)
    ("fact_fees", "fee_date", "dim_date", "date"),
    ("fact_portfolio_snapshot", "snapshot_date", "dim_date", "date"),
    ("fact_transactions", "txn_date", "dim_date", "date"),
]

PRIMARY_KEYS = {"dim_advisor": "advisor_id", "dim_benchmark": "benchmark_id",
                "dim_client": "client_id", "dim_date": "date_id",
                "dim_security": "security_id", "fact_fees": "fee_id",
                "fact_portfolio_snapshot": "snapshot_id", "fact_transactions": "txn_id"}


# =============================================================================
# 2. COLUMN TYPES, STRUCTURAL BLANKS, BUSINESS RULES
# =============================================================================
CASH_TYPES = ["Deposit", "Withdrawal", "Advisory Fee"]
TRADE_TYPES = ["Buy", "Sell", "Dividend"]

# Declared types. Columns not listed are text (always valid). Added 0/1 flag columns
# (is_*) in the clean stage are validated as booleans automatically.
TYPES = {
    "dim_advisor": {"advisor_id": "int", "years_experience": "int", "aum_target_mm": "num"},
    "dim_benchmark": {"benchmark_id": "int"},
    "dim_client": {"client_id": "int", "age": "int", "initial_aum": "num",
                   "client_since": "date", "advisor_id": "int", "is_active": "bool"},
    "dim_date": {"date_id": "int", "date": "date", "year": "int", "quarter": "int",
                 "month": "int", "week_of_year": "int", "is_month_end": "bool",
                 "is_quarter_end": "bool", "is_year_end": "bool", "is_trading_day": "bool"},
    "dim_security": {"security_id": "int", "benchmark_id": "int", "is_etf": "bool",
                     "expense_ratio_bps": "num"},
    "fact_fees": {"fee_id": "int", "client_id": "int", "advisor_id": "int",
                  "fee_date": "date", "aum_basis": "num", "bps_charged": "num",
                  "amount": "num"},
    "fact_portfolio_snapshot": {"snapshot_id": "int", "client_id": "int", "advisor_id": "int",
                                "benchmark_id": "int", "snapshot_date": "date", "year": "int",
                                "month": "int", "aum": "num", "return_ytd": "num",
                                "benchmark_return_ytd": "num", "active_alpha_ytd": "num",
                                "cash_pct": "num", "is_return_suspect": "bool"},
    "fact_transactions": {"txn_id": "int", "client_id": "int", "advisor_id": "int",
                          "security_id": "int", "txn_date": "date", "shares": "num",
                          "price": "num", "amount": "num", "is_post_departure_txn": "bool"},
}

# Blanks that are legitimate structure, not missing data: (table, column) -> rule(df) True
# where a blank is allowed. Used by completeness.
STRUCTURAL_BLANKS = {
    ("dim_security", "sector"): lambda d: d["asset_class"] != "Equity",
    ("dim_security", "expense_ratio_bps"): lambda d: d["is_etf"] == "False",
    ("fact_transactions", "security_id"): lambda d: d["txn_type"].isin(CASH_TYPES),
    ("fact_transactions", "shares"): lambda d: d["txn_type"].isin(CASH_TYPES),
    ("fact_transactions", "price"): lambda d: d["txn_type"].isin(CASH_TYPES),
}

US_STATES = set("""AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN
MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY""".split())
ACCOUNT_TYPES = ["Taxable", "IRA", "Roth IRA", "Trust", "529"]


def n(d, c):
    return pd.to_numeric(d[c], errors="coerce")


def dt(d, c):
    return pd.to_datetime(d[c], format="%Y-%m-%d", errors="coerce")


def rng(c, lo=None, hi=None):
    """Numeric range rule; checked only where the value is present."""
    def f(d):
        v = n(d, c)
        ok = pd.Series(True, index=d.index)
        if lo is not None:
            ok &= v >= lo
        if hi is not None:
            ok &= v <= hi
        return d[c] != "", ok
    return f


def allowed(c, values):
    vals = set(values)
    return lambda d: (d[c] != "", d[c].isin(vals))


def pattern(c, rx):
    return lambda d: (d[c] != "", d[c].str.fullmatch(rx))


def date_rng(c, lo, hi):
    return lambda d: (d[c] != "", dt(d, c).between(pd.Timestamp(lo), pd.Timestamp(hi)))


def tol(a, b, t):
    return (a - b).abs() <= t


# RULES[table] = list of (column(s), rule type, description, fn(df) -> (applies_mask, pass_mask)
#                         [, flag column])
# The optional flag column names a 0/1 column added by 02_clean.py. Rows where it is 1 are
# known, documented exceptions: they still count as failures in pct_rule_pass, and are
# excluded only from pct_rule_pass_unflagged.
RULES = {
    "dim_advisor": [
        ("years_experience", "range", "0-60 years", rng("years_experience", 0, 60)),
        ("aum_target_mm", "sign", "> 0", rng("aum_target_mm", 0.000001)),
        ("region", "code list", "known US region",
         allowed("region", ["Northeast", "Southeast", "Mid-Atlantic", "Midwest", "West",
                            "Southwest"])),
        ("certifications", "code list", "each item in CFA/CFP/CIMA/ChFC/CPA/None",
         lambda d: (d["certifications"] != "", d["certifications"].str.split(",").map(
             lambda L: all(x.strip() in {"CFA", "CFP", "CIMA", "ChFC", "CPA", "None"}
                           for x in L)))),
    ],
    "dim_benchmark": [
        ("asset_class", "code list", "known benchmark class",
         allowed("asset_class", ["Equity", "Fixed Income", "Multi-Asset", "International",
                                 "Small Cap", "Emerging", "Cash"])),
    ],
    "dim_client": [
        ("age", "range", "18-110", rng("age", 18, 110)),
        ("state", "code list", "US state code", allowed("state", US_STATES)),
        ("zip_code", "pattern", "5 digits", pattern("zip_code", r"\d{5}")),
        ("risk_profile", "code list", "5 profiles + Unknown",
         allowed("risk_profile", ["Conservative", "Moderately Conservative", "Balanced",
                                  "Moderately Aggressive", "Aggressive", "Unknown"])),
        ("client_tier", "code list", "Retail/HNW/UHNW",
         allowed("client_tier", ["Retail", "HNW", "UHNW"])),
        ("primary_account_type", "code list", "5 account types",
         allowed("primary_account_type", ACCOUNT_TYPES)),
        ("initial_aum", "sign", "> 0", rng("initial_aum", 0.01)),
        ("client_since", "range", "1950-01-01 to 2024-12-31",
         date_rng("client_since", "1950-01-01", "2024-12-31")),
        ("is_active", "code list", "True/False", allowed("is_active", ["True", "False"])),
        ("client_tier+initial_aum", "row rule", "tier matches AUM band (<500K / 500K-5M / >=5M)",
         lambda d: (d["client_tier"] != "",
                    ((d["client_tier"] == "Retail") & (n(d, "initial_aum") < 500_000))
                    | ((d["client_tier"] == "HNW") & n(d, "initial_aum").between(500_000, 5_000_000))
                    | ((d["client_tier"] == "UHNW") & (n(d, "initial_aum") >= 5_000_000)))),
    ],
    "dim_date": [
        ("quarter", "range", "1-4", rng("quarter", 1, 4)),
        ("month", "range", "1-12", rng("month", 1, 12)),
        ("week_of_year", "range", "1-53", rng("week_of_year", 1, 53)),
        ("day_of_week", "code list", "weekday name",
         allowed("day_of_week", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                 "Saturday", "Sunday"])),
        ("is_month_end,is_quarter_end,is_year_end,is_trading_day", "code list", "0/1",
         lambda d: (pd.Series(True, index=d.index),
                    d[["is_month_end", "is_quarter_end", "is_year_end",
                       "is_trading_day"]].isin(["0", "1"]).all(axis=1))),
        ("date+year/quarter/month/day_of_week", "row rule", "calendar attributes match date",
         lambda d: (d["date"] != "",
                    (n(d, "year") == dt(d, "date").dt.year)
                    & (n(d, "quarter") == dt(d, "date").dt.quarter)
                    & (n(d, "month") == dt(d, "date").dt.month)
                    & (d["day_of_week"] == dt(d, "date").dt.day_name()))),
        ("is_trading_day", "row rule", "no trading on weekends",
         lambda d: (d["is_trading_day"] == "1", dt(d, "date").dt.dayofweek < 5)),
    ],
    "dim_security": [
        ("asset_class", "code list", "4 asset classes",
         allowed("asset_class", ["Equity", "Fixed Income", "Alternative", "Cash Equivalent"])),
        ("ticker", "pattern", "1-5 capital letters", pattern("ticker", r"[A-Z]{1,5}")),
        ("is_etf", "code list", "True/False", allowed("is_etf", ["True", "False"])),
        ("expense_ratio_bps", "range", "0-300 bps", rng("expense_ratio_bps", 0, 300)),
        ("is_etf+expense_ratio_bps", "row rule", "ETFs have an expense ratio",
         lambda d: (d["is_etf"] == "True", d["expense_ratio_bps"] != "")),
    ],
    "fact_fees": [
        ("fee_type", "code list", "4 fee types",
         allowed("fee_type", ["Management", "Advisory", "Custodial", "Performance"])),
        ("aum_basis", "sign", "> 0", rng("aum_basis", 0.01)),
        ("bps_charged", "range", "0-300 bps", rng("bps_charged", 0, 300)),
        ("amount", "sign", ">= 0", rng("amount", 0)),
        ("fee_date", "range", "2020-01-01 to 2024-12-31",
         date_rng("fee_date", "2020-01-01", "2024-12-31")),
        ("amount (recompute)", "row rule", "amount = aum_basis x bps / 10,000 / 12 (+/- $0.01)",
         lambda d: (d["amount"] != "", tol(n(d, "aum_basis") * n(d, "bps_charged") / 120_000,
                                           n(d, "amount"), 0.0101))),
    ],
    "fact_portfolio_snapshot": [
        ("aum", "sign", "> 0", rng("aum", 0.01)),
        ("cash_pct", "range", "0-1", rng("cash_pct", 0, 1)),
        ("return_ytd", "range", ">= -100% (cannot lose more than everything)",
         rng("return_ytd", -1)),
        ("benchmark_return_ytd", "range", ">= -100%", rng("benchmark_return_ytd", -1)),
        ("month", "range", "1-12", rng("month", 1, 12)),
        ("account_type", "code list", "5 account types", allowed("account_type", ACCOUNT_TYPES)),
        ("is_return_suspect", "code list", "0/1", allowed("is_return_suspect", ["0", "1"])),
        ("snapshot_date", "range", "2020-01-01 to 2024-12-31",
         date_rng("snapshot_date", "2020-01-01", "2024-12-31")),
        ("active_alpha_ytd (recompute)", "row rule", "alpha = return - benchmark (+/- 1e-5)",
         lambda d: (d["active_alpha_ytd"] != "",
                    tol(n(d, "return_ytd") - n(d, "benchmark_return_ytd"),
                        n(d, "active_alpha_ytd"), 1e-5))),
        ("return_ytd+is_return_suspect", "row rule", "returns beyond +/-100% are flagged suspect",
         lambda d: (n(d, "return_ytd").abs() > 1, d["is_return_suspect"] == "1")),
        ("aum", "row rule", "aum is a real balance, not the $1,000 system floor",
         lambda d: (d["aum"] != "", n(d, "aum") != 1000), "is_aum_floor"),
    ],
    "fact_transactions": [
        ("txn_type", "code list", "6 transaction types",
         allowed("txn_type", TRADE_TYPES + CASH_TYPES)),
        ("shares", "sign", "> 0", rng("shares", 0.000001)),
        ("price", "sign", "> 0", rng("price", 0.01)),
        ("amount", "sign", "> 0 (direction is carried by txn_type)", rng("amount", 0.01)),
        ("txn_date", "range", "2020-01-01 to 2024-12-31",
         date_rng("txn_date", "2020-01-01", "2024-12-31")),
        ("is_post_departure_txn", "code list", "0/1",
         allowed("is_post_departure_txn", ["0", "1"])),
        ("security_id/shares/price", "row rule", "Buy/Sell/Dividend carry security, shares, price",
         lambda d: (d["txn_type"].isin(TRADE_TYPES),
                    (d["security_id"] != "") & (d["shares"] != "") & (d["price"] != ""))),
        ("security_id/shares/price", "row rule", "cash movements carry no security fields",
         lambda d: (d["txn_type"].isin(CASH_TYPES),
                    (d["security_id"] == "") & (d["shares"] == "") & (d["price"] == ""))),
        ("amount (recompute)", "row rule", "Buy/Sell amount = shares x price (+/- $0.01)",
         lambda d: (d["txn_type"].isin(["Buy", "Sell"]),
                    tol(n(d, "shares") * n(d, "price"), n(d, "amount"), 0.0101))),
        ("amount (dividend)", "row rule",
         "a dividend is less than the value of the position (yield < 100%)",
         lambda d: (d["txn_type"] == "Dividend",
                    n(d, "amount") < n(d, "shares") * n(d, "price") - 0.0101),
         "is_dividend_amount_suspect"),
    ],
}

# Numeric columns with NO natural upper bound -> IQR outlier flags
IQR_COLUMNS = [("dim_client", "initial_aum"), ("fact_fees", "amount"),
               ("fact_portfolio_snapshot", "aum"), ("fact_transactions", "amount")]


# =============================================================================
# 3. CHECK RUNNERS
# =============================================================================
details: list[dict] = []


def detail(stage, kind, table, column, desc, checked, failed, extra=""):
    checked, failed = int(checked), int(failed)
    details.append(dict(stage=stage, check_type=kind, table=table, column=column,
                        description=desc, rows_checked=checked, rows_failed=failed,
                        pct_pass=round(100 * (checked - failed) / checked, 4) if checked else 100.0,
                        extra=extra))


def is_valid_type(s: pd.Series, typ: str) -> pd.Series:
    if typ == "int":
        return s.str.fullmatch(r"[+-]?\d+")
    if typ == "num":
        return s.str.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
    if typ == "bool":
        return s.isin(["True", "False", "0", "1"])
    if typ == "date":
        return pd.to_datetime(s, format="%Y-%m-%d", errors="coerce").notna()
    return pd.Series(True, index=s.index)


def completeness(stage, t, d):
    total = blanks = 0
    for c in d.columns:
        blank = d[c].str.strip() == ""
        if (t, c) in STRUCTURAL_BLANKS:
            blank &= ~STRUCTURAL_BLANKS[(t, c)](d)
        total += len(d)
        blanks += int(blank.sum())
    detail(stage, "completeness", t, "(all columns)",
           "non-blank cells; blanks that are structural (not applicable) count as complete",
           total, blanks)
    return 100 * (total - blanks) / total


def key_uniqueness(stage, t, d):
    k = d[PRIMARY_KEYS[t]].str.strip()
    bad = k.duplicated(keep=False) | (k == "")
    detail(stage, "key uniqueness", t, PRIMARY_KEYS[t], "primary key unique and non-blank",
           len(k), bad.sum())
    return 100 * (len(k) - bad.sum()) / len(k)


def type_validity(stage, t, d):
    types = dict(TYPES.get(t, {}))
    for c in d.columns:                       # flag columns added by 02_clean.py
        if c.startswith("is_") and c not in types:
            types[c] = "bool"
    checked = failed = 0
    for c, typ in types.items():
        if c not in d.columns:
            continue
        present = d[c] != ""
        ok = is_valid_type(d[c], typ).fillna(False)
        c_checked, c_failed = int(present.sum()), int((present & ~ok).sum())
        checked += c_checked
        failed += c_failed
        if c_failed:
            detail(stage, "type validity", t, c, f"values parse as {typ}", c_checked, c_failed,
                   " | ".join(d.loc[present & ~ok, c].head(5)))
    detail(stage, "type validity", t, "(all typed columns)", "non-blank values parse as "
           "their declared type", checked, failed)
    return 100 * (checked - failed) / checked if checked else 100.0


def flagged_rows(d, flag):
    return d[flag] == "1" if flag and flag in d.columns else pd.Series(False, index=d.index)


def rule_pass(stage, t, d):
    """Single-table rules. Returns (rows checked, rows failed, failed and not flagged)."""
    checked = failed = failed_unflagged = 0
    for rule in RULES.get(t, []):
        col, kind, desc, fn = rule[:4]
        flag = rule[4] if len(rule) > 4 else None
        applies, ok = fn(d)
        applies = applies.fillna(False).astype(bool)
        ok = ok.fillna(False).astype(bool)
        bad = applies & ~ok
        c_checked, c_failed = int(applies.sum()), int(bad.sum())
        c_unflagged = int((bad & ~flagged_rows(d, flag)).sum())
        checked += c_checked
        failed += c_failed
        failed_unflagged += c_unflagged
        key = PRIMARY_KEYS[t]
        note = ("e.g. " + key + " " + ", ".join(d.loc[bad, key].head(5))) if c_failed else ""
        if flag:
            note += f" | {c_failed - c_unflagged:,} of the failures carry {flag} = 1" \
                if flag in d.columns else f" | no {flag} column at this stage"
        detail(stage, f"rule: {kind}", t, col, desc, c_checked, c_failed, note)
    return checked, failed, failed_unflagged


def referential(stage, data):
    """Apply check_referential_integrity to all 13 relationships."""
    per_table: dict[str, list] = {}
    print(f"\n  Referential integrity ({stage})")
    print(f"  {'relationship':<62}{'match %':>9}{'orphans':>9}{'null %':>9}")
    for child, ck, parent, pk in RELATIONSHIPS:
        r = check_referential_integrity(data[child], ck, data[parent], pk)
        label = f"{child}.{ck} -> {parent}.{pk}"
        print(f"  {label:<62}{r['match_rate']:>9.2f}{r['orphan_row_count']:>9,}"
              f"{r['null_key_rate']:>9.2f}")
        # nulls on security_id are structural (cash movements); others count as non-matches
        structural_null = ck == "security_id"
        denom = r["rows"] - (r["null_keys"] if structural_null else 0)
        good = r["matched_rows"]
        per_table.setdefault(child, []).append((good, denom))
        detail(stage, "referential integrity", child, f"{ck} -> {parent}.{pk}",
               f"match rate {r['match_rate']}% | null-key rate {r['null_key_rate']}% "
               f"| distinct orphan keys {len(r['distinct_orphan_keys'])}",
               denom, denom - good,
               ", ".join(map(str, r["distinct_orphan_keys"][:10])))
    return {t: 100 * sum(g for g, _ in v) / sum(dn for _, dn in v) for t, v in per_table.items()}


def reconciliations(stage, data):
    """Cross-table reconciliations - two tables describing the same fact."""
    fe, sn, tx = data["fact_fees"], data["fact_portfolio_snapshot"], data["fact_transactions"]
    cl, dd = data["dim_client"], data["dim_date"]
    out = []

    # X1 fee AUM basis = same client-month snapshot AUM
    m = fe.merge(sn[["client_id", "snapshot_date", "aum"]], how="left",
                 left_on=["client_id", "fee_date"], right_on=["client_id", "snapshot_date"])
    bad = m["aum"].isna() | ~tol(n(m, "aum_basis"), n(m, "aum"), 0.0101)
    out.append(("fact_fees vs fact_portfolio_snapshot", "aum_basis = snapshot aum (same client-month)",
                len(m), bad.sum()))

    # X2 fact advisor = client's advisor of record in dim_client
    adv = dict(zip(cl["client_id"], cl["advisor_id"]))
    for f in ("fact_fees", "fact_portfolio_snapshot", "fact_transactions"):
        d = data[f]
        out.append((f"{f} vs dim_client", "advisor_id = client's advisor of record",
                    len(d), (d["advisor_id"] != d["client_id"].map(adv)).sum()))

    # X3 snapshot year/month = dim_date year/month for the snapshot date
    cal = dd.set_index("date")
    bad = (sn["year"] != sn["snapshot_date"].map(cal["year"])) | \
          (sn["month"] != sn["snapshot_date"].map(cal["month"]))
    out.append(("fact_portfolio_snapshot vs dim_date", "year/month = dim_date year/month",
                len(sn), bad.sum()))

    # X4 no activity for inactive clients after their last snapshot month
    last = dt(sn, "snapshot_date").groupby(sn["client_id"]).max().dt.to_period("M")
    inactive = set(cl.loc[cl["is_active"] == "False", "client_id"])
    dep = tx["client_id"].map({c: p for c, p in last.items() if c in inactive})
    tm = dt(tx, "txn_date").dt.to_period("M")
    after = pd.Series([d == d and d is not None and m > d for m, d in zip(tm, dep)], index=tx.index)
    out.append(("fact_transactions vs dim_client + snapshots",
                "no transactions after an inactive client's last snapshot month",
                len(tx), after.sum()))

    # X5 Advisory Fee debits (transactions) = fees billed (fact_fees), client-month totals
    af = tx[tx["txn_type"] == "Advisory Fee"]
    a = n(af, "amount").groupby([af["client_id"], af["txn_date"].str[:7]]).sum()
    b = n(fe, "amount").groupby([fe["client_id"], fe["fee_date"].str[:7]]).sum()
    j = pd.concat([a.rename("debited"), b.rename("billed")], axis=1).dropna()
    out.append(("fact_transactions vs fact_fees",
                "Advisory Fee debited = fee billed (client-month, both present)",
                len(j), (~tol(j["debited"], j["billed"], 0.0101)).sum(), None))

    # X6 Buy/Sell happen on a trading day according to dim_date
    trading = tx["txn_date"].map(dict(zip(dd["date"], dd["is_trading_day"])))
    trade = tx["txn_type"].isin(["Buy", "Sell"])
    bad = trade & (trading == "0")
    out.append(("fact_transactions vs dim_date", "Buy/Sell dated on a trading day",
                int(trade.sum()), bad, "is_non_trading_day_txn"))

    print(f"\n  Cross-table reconciliations ({stage})")
    per_table: dict[str, list] = {}
    for item in out:
        name, desc, checked, failed = item[:4]
        flag = item[4] if len(item) > 4 else None
        table = name.split(" vs ")[0]
        if isinstance(failed, pd.Series):          # row mask -> can honour a flag column
            mask = failed
            failed = int(mask.sum())
            unflagged = int((mask & ~flagged_rows(data[table], flag)).sum())
        else:
            failed = unflagged = int(failed)
        print(f"  {name:<48}{desc:<62} fail {failed:>7,} / {int(checked):,}")
        detail(stage, "reconciliation", table, name, desc, checked, failed,
               f"{failed - unflagged:,} of the failures carry {flag} = 1" if flag and failed else "")
        per_table.setdefault(table, []).append((int(checked), failed, unflagged))
    return per_table


def iqr_flags(stage, data):
    print(f"\n  IQR outlier flags ({stage}) - 1.5 x IQR fences")
    for t, c in IQR_COLUMNS:
        v = n(data[t], c).dropna()
        q1, q3 = v.quantile([0.25, 0.75])
        lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
        flagged = int(((v < lo) | (v > hi)).sum())
        print(f"  {t + '.' + c:<36} fences [{lo:,.2f}, {hi:,.2f}]  flagged {flagged:,} "
              f"of {len(v):,} ({100 * flagged / len(v):.1f}%)")
        detail(stage, "IQR outlier flag", t, c,
               f"outside [{lo:,.2f}, {hi:,.2f}] (Q1 {q1:,.2f}, Q3 {q3:,.2f}); flagged != wrong",
               len(v), flagged, IQR_EXPLANATION)
    print(f"  Note: {IQR_EXPLANATION}")


# =============================================================================
# 4. MAIN
# =============================================================================
def load(stage):
    data = {}
    for t in TABLES:
        p = STAGES[stage](t)
        if not p.exists():
            raise SystemExit(f"Missing {stage} file {p} - run 02_clean.py first")
        data[t] = pd.read_csv(p, dtype=str, keep_default_na=False)
    return data


def main():
    REPORTS.mkdir(exist_ok=True)
    score = []
    for stage in STAGES:
        print(f"\n{'=' * 90}\nSTAGE: {stage.upper()}\n{'=' * 90}")
        data = load(stage)
        ri = referential(stage, data)
        recon = reconciliations(stage, data)
        iqr_flags(stage, data)
        for t in TABLES:
            d = data[t]
            rc, rf, ru = rule_pass(stage, t, d)
            for xc, xf, xu in recon.get(t, []):     # cross-table rules count toward the table
                rc, rf, ru = rc + xc, rf + xf, ru + xu
            score.append(dict(
                table=t, stage=stage, rows=len(d),
                pct_completeness=round(completeness(stage, t, d), 3),
                pct_key_uniqueness=round(key_uniqueness(stage, t, d), 3),
                pct_type_validity=round(type_validity(stage, t, d), 3),
                pct_rule_pass=round(100 * (rc - rf) / rc, 3) if rc else 100.0,
                pct_rule_pass_unflagged=round(100 * (rc - ru) / rc, 3) if rc else 100.0,
                pct_referential_match=round(ri[t], 3) if t in ri else np.nan,
                rules_evaluated=len(RULES.get(t, [])) + len(recon.get(t, [])),
                rule_checks=rc, rule_failures=rf,
                foreign_keys=sum(1 for r in RELATIONSHIPS if r[0] == t),
            ))

    sc = pd.DataFrame(score).sort_values(["table", "stage"], ascending=[True, False])
    sc["notes"] = sc["foreign_keys"].map(
        lambda k: "" if k else "no outgoing foreign keys - referential match not applicable")
    sc.loc[sc["stage"] == "raw", "notes"] = sc["notes"].where(sc["notes"] == "", sc["notes"] + "; ") \
        + "raw has no cleaning flags, so pct_rule_pass_unflagged = pct_rule_pass"
    sc.to_csv(REPORTS / "data_quality_scorecard.csv", index=False)
    pd.DataFrame(details).to_csv(REPORTS / "validation_details.csv", index=False)

    print(f"\n{'=' * 90}\nSCORECARD  (reports/data_quality_scorecard.csv)\n{'=' * 90}")
    cols = ["table", "stage", "rows", "pct_completeness", "pct_key_uniqueness",
            "pct_type_validity", "pct_rule_pass", "pct_rule_pass_unflagged",
            "pct_referential_match"]
    print(sc[cols].to_string(index=False))

    print("\nRules that fail in the CLEAN stage (values kept on purpose - see decision_log.md):")
    dd = pd.DataFrame(details)
    fails = dd[(dd.stage == "clean") & dd.check_type.str.startswith(("rule", "reconciliation"))
               & (dd.rows_failed > 0)]
    for r in fails.itertuples():
        print(f"  {r.table}.{r.column}: {r.description} - {r.rows_failed:,} of {r.rows_checked:,}")
    print("\npct_rule_pass counts every failure. pct_rule_pass_unflagged leaves out failures on "
          "rows 02_clean.py\nflagged as known, documented exceptions (values kept on purpose).")
    print(f"\nWrote {REPORTS / 'data_quality_scorecard.csv'}")
    print(f"Wrote {REPORTS / 'validation_details.csv'} ({len(dd)} checks)")


if __name__ == "__main__":
    main()
