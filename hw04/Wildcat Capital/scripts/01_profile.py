#!/usr/bin/env python3
"""
01_profile.py  -  Data-quality audit for the Wildcat Capital extract (HW04, Part A)

WHAT IT DOES
    1. Reads every CSV it is given AS TEXT (dtype=str, keep_default_na=False) so that
       nothing is silently converted (leading zeros, "None", "45.0", "$1,200", ...).
    2. Profiles every column of every table (generic - works on any CSV):
           null/blank count, null-like tokens, distinct count, dominant value shapes,
           inferred (expected) type, share of values that fail to parse as that type,
           whitespace problems, mixed formats, numeric/date min & max, outliers.
    3. Runs data-quality checks in four groups and tags each finding
       ERROR / WARN / INFO:
           1 Column level           - type, allowed values, ranges, key uniqueness
           2 Single-table rules     - duplicates, near-duplicates, row contradictions
           3 Referential integrity  - every foreign key must point to an existing PK
           4 Cross-table rules      - facts that disagree with dimensions / each other
    4. Writes  reports/profile_raw.csv      (one row per table x column)
               reports/dq_summary_raw.csv   (one row per finding)

GENERIC vs. CONFIGURED
    The profiling engine and the generic checks (types, whitespace, mixed formats,
    null tokens, placeholder values, likely typos, duplicate rows, near-duplicates,
    key uniqueness, auto-discovered foreign keys) run on ANY table with no setup.
    Business knowledge (allowed code lists, ranges, row rules, cross-table rules) is
    declared in the TABLE_RULES / FOREIGN_KEYS / CROSS_TABLE_RULES configuration
    below. A table that has no configuration still gets the full generic audit, and a
    rule whose tables were not loaded is skipped.

USAGE
    python scripts/01_profile.py                       # all *.csv in the project folder
    python scripts/01_profile.py path/to/some.csv ...  # any table(s)
    python scripts/01_profile.py --data-dir "Wildcat Capital" --out-dir reports

    Severity:  ERROR = wrong, or will break a join or calculation
               WARN  = probably wrong; investigate
               INFO  = worth knowing
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import re
import sys
import warnings
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# =============================================================================
# 0. CONSTANTS
# =============================================================================
ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
G1, G2, G3, G4 = ("1 Column level", "2 Single-table", "3 Referential integrity",
                  "4 Cross-table")

# Strings that mean "missing" but are NOT blank - a BI tool will treat them as data.
NULL_TOKENS = {"null", "none", "nan", "n/a", "na", "#n/a", "nil", "-", "--", "?",
               "missing", "undefined", "(blank)", "unknown_null"}
BOOL_TRUE = {"true", "t", "yes", "y", "1"}
BOOL_FALSE = {"false", "f", "no", "n", "0"}
BOOL_TOKENS = BOOL_TRUE | BOOL_FALSE

# (name, regex, strptime format or None for ISO8601)
DATE_PATTERNS = [
    ("YYYY-MM-DD", r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    ("YYYY-MM-DD hh:mm[:ss]", r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?", None),
    ("YYYY/MM/DD", r"\d{4}/\d{1,2}/\d{1,2}", "%Y/%m/%d"),
    ("MM/DD/YYYY", r"\d{1,2}/\d{1,2}/\d{4}", "%m/%d/%Y"),
    ("MM/DD/YY", r"\d{1,2}/\d{1,2}/\d{2}", "%m/%d/%y"),
    ("MM-DD-YYYY", r"\d{1,2}-\d{1,2}-\d{4}", "%m-%d-%Y"),
    ("DD-Mon-YYYY", r"\d{1,2}-[A-Za-z]{3}-\d{4}", "%d-%b-%Y"),
    ("Mon DD, YYYY", r"[A-Za-z]{3} \d{1,2}, \d{4}", "%b %d, %Y"),
    ("Month DD, YYYY", r"[A-Za-z]{4,9} \d{1,2}, \d{4}", "%B %d, %Y"),
    ("YYYYMMDD", r"(19|20)\d{6}", "%Y%m%d"),   # only used for date-named columns
]
DATE_NAME_HINT = re.compile(r"(date|_dt$|since|_on$|time|day$)", re.I)
CODE_NAME_HINT = re.compile(r"(zip|postal|phone|ssn|cusip|isin|ticker|sedol|fips)", re.I)

RE_INT = r"[+-]?\d+"
RE_DEC = r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?"
RE_DIRTY_NUM = r"\(?[+-]?\s*[$€£]?\s*[+-]?\d[\d,]*(\.\d+)?\s*%?\)?"

TYPE_THRESHOLD = 0.80        # share of values that must parse for a type to be inferred
SAMPLE_N = 5                 # example values / keys per finding
TODAY = pd.Timestamp(date.today())
AS_OF = pd.Timestamp("2024-12-31")   # extract as-of date (end of the dim_date calendar)

US_STATES = set("""AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN
MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR""".split())


# =============================================================================
# 1. CONFIGURATION  (business knowledge - extend for new tables)
# =============================================================================
# Row rules are functions f(t) -> boolean Series (True = row VIOLATES the rule).
# `t` is a Typed wrapper: t.s(col) stripped text, t.n(col) numeric, t.d(col) datetime,
# t.b(col) boolean (1/0/NaN), t.df raw text frame.  Comparisons against NaN are False,
# so a rule only fires on rows where its inputs are present.

TRADE_TYPES = ["Buy", "Sell", "Dividend"]
CASH_TYPES = ["Deposit", "Withdrawal", "Advisory Fee"]


def _ne(a, b):
    """a != b only where both sides are present (NaN never counts as a contradiction)."""
    return a.notna() & b.notna() & (a != b)


def _blank(t, c):
    return t.s(c) == ""


def _present(t, c):
    return t.s(c) != ""


TABLE_RULES: dict[str, dict] = {
    # ------------------------------------------------------------------ dimensions
    "dim_advisor": {
        "documented_rows": 25,
        "expected_columns": ["advisor_id", "first_name", "last_name", "region",
                             "years_experience", "aum_target_mm", "certifications"],
        "primary_key": ["advisor_id"],
        "natural_keys": [["first_name", "last_name"]],
        "required": ["advisor_id", "first_name", "last_name", "region"],
        "types": {"advisor_id": "integer", "years_experience": "integer",
                  "aum_target_mm": "decimal", "certifications": "text"},
        "allowed": {"region": ["Northeast", "Southeast", "Mid-Atlantic", "Midwest",
                               "West", "Southwest"]},
        "allowed_list_tokens": {"certifications": (",", ["CFA", "CFP", "CIMA", "ChFC",
                                                        "CPA", "CPWA", "CAIA", "None"])},
        "ranges": {"years_experience": (0, 50, ERROR), "aum_target_mm": (0.01, 10_000, ERROR)},
    },
    "dim_benchmark": {
        "documented_rows": 8,
        "expected_columns": ["benchmark_id", "name", "asset_class", "ticker_proxy"],
        "primary_key": ["benchmark_id"],
        "unique": ["name", "ticker_proxy"],
        "required": ["benchmark_id", "name", "asset_class"],
        "types": {"benchmark_id": "integer"},
    },
    "dim_client": {
        "documented_rows": 2525,
        "expected_columns": ["client_id", "first_name", "last_name", "age", "city", "state",
                             "zip_code", "risk_profile", "client_tier",
                             "primary_account_type", "initial_aum", "client_since",
                             "advisor_id", "is_active"],
        "primary_key": ["client_id"],
        "natural_keys": [["first_name", "last_name", "zip_code"]],
        "required": ["client_id", "first_name", "last_name", "state", "risk_profile",
                     "client_tier", "initial_aum", "client_since", "advisor_id",
                     "is_active"],
        "types": {"client_id": "integer", "age": "integer", "zip_code": "code",
                  "initial_aum": "decimal", "client_since": "date",
                  "advisor_id": "integer", "is_active": "boolean"},
        "allowed": {
            "state": sorted(US_STATES),
            "risk_profile": ["Conservative", "Moderately Conservative", "Balanced",
                             "Moderately Aggressive", "Aggressive"],
            "client_tier": ["Retail", "HNW", "UHNW"],
            "primary_account_type": ["Taxable", "IRA", "Roth IRA", "Trust", "529"],
        },
        "placeholder_values": {"risk_profile": ["Unknown"]},
        "patterns": {"zip_code": (r"\d{5}(-\d{4})?", ERROR)},
        "ranges": {"age": (18, 110, ERROR), "initial_aum": (0.01, 1e10, ERROR),
                   "client_since": ("1950-01-01", "today", ERROR)},
        "row_rules": [
            dict(name="client_tier vs initial_aum", severity=WARN,
                 desc="Tier does not match initial AUM band (Retail <$500K, HNW $500K-$5M, "
                      "UHNW >=$5M)",
                 cols=["client_tier", "initial_aum"],
                 rule=lambda t: ((t.s("client_tier") == "Retail") & (t.n("initial_aum") >= 500_000))
                 | ((t.s("client_tier") == "HNW") & ((t.n("initial_aum") < 500_000) | (t.n("initial_aum") >= 5_000_000)))
                 | ((t.s("client_tier") == "UHNW") & (t.n("initial_aum") < 5_000_000))),
            dict(name="529 plan for an elderly owner", severity=INFO,
                 desc="primary_account_type = 529 (education savings) for a client aged 75+",
                 cols=["primary_account_type", "age"],
                 rule=lambda t: (t.s("primary_account_type") == "529") & (t.n("age") >= 75)),
            dict(name="client_since before age 18", severity=WARN,
                 desc="Client relationship started before age 18 (age taken as of 2024-12-31)",
                 cols=["age", "client_since"],
                 rule=lambda t: (t.n("age") - (AS_OF.year - t.d("client_since").dt.year)) < 18),
        ],
    },
    "dim_date": {
        "documented_rows": 1827,
        "expected_columns": ["date_id", "date", "year", "quarter", "month", "month_name",
                             "week_of_year", "day_of_week", "is_month_end",
                             "is_quarter_end", "is_year_end", "is_trading_day"],
        "primary_key": ["date_id"],
        "unique": ["date"],
        "required": ["date_id", "date"],
        "types": {"date_id": "integer", "date": "date", "year": "integer",
                  "quarter": "integer", "month": "integer", "week_of_year": "integer",
                  "is_month_end": "boolean", "is_quarter_end": "boolean",
                  "is_year_end": "boolean", "is_trading_day": "boolean"},
        "ranges": {"quarter": (1, 4, ERROR), "month": (1, 12, ERROR),
                   "week_of_year": (1, 53, ERROR), "year": (2020, 2024, WARN)},
        "allowed": {"day_of_week": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                    "Saturday", "Sunday"],
                    "month_name": ["January", "February", "March", "April", "May", "June",
                                   "July", "August", "September", "October", "November",
                                   "December"]},
        "row_rules": [
            dict(name="calendar attributes vs date", severity=ERROR,
                 desc="year / quarter / month / month_name / day_of_week disagree with `date`",
                 cols=["date", "year", "quarter", "month", "month_name", "day_of_week"],
                 rule=lambda t: t.d("date").notna() & (
                     _ne(t.n("year"), t.d("date").dt.year)
                     | _ne(t.n("quarter"), t.d("date").dt.quarter)
                     | _ne(t.n("month"), t.d("date").dt.month)
                     | ((t.s("month_name") != t.d("date").dt.month_name()) & (t.s("month_name") != ""))
                     | ((t.s("day_of_week") != t.d("date").dt.day_name()) & (t.s("day_of_week") != "")))),
            dict(name="week_of_year vs date", severity=INFO,
                 desc="week_of_year differs from the ISO week of `date` (may be a different "
                      "week convention)",
                 cols=["date", "week_of_year"],
                 rule=lambda t: _ne(t.n("week_of_year"),
                                    t.d("date").dt.isocalendar().week.astype(float))),
            dict(name="period-end flags vs date", severity=ERROR,
                 desc="is_month_end / is_quarter_end / is_year_end disagree with `date`",
                 cols=["date", "is_month_end", "is_quarter_end", "is_year_end"],
                 rule=lambda t: t.d("date").notna() & (
                     _ne(t.b("is_month_end"), t.d("date").dt.is_month_end.astype(float))
                     | _ne(t.b("is_quarter_end"), t.d("date").dt.is_quarter_end.astype(float))
                     | _ne(t.b("is_year_end"), t.d("date").dt.is_year_end.astype(float)))),
            dict(name="trading day on a weekend", severity=ERROR,
                 desc="is_trading_day = 1 on a Saturday or Sunday",
                 cols=["date", "is_trading_day"],
                 rule=lambda t: (t.b("is_trading_day") == 1) & (t.d("date").dt.dayofweek >= 5)),
        ],
        "completeness": {"date_column": "date"},   # check for gaps in the calendar
    },
    "dim_security": {
        "documented_rows": 500,
        "expected_columns": ["security_id", "ticker", "name", "asset_class", "sector",
                             "benchmark_id", "is_etf", "expense_ratio_bps"],
        "primary_key": ["security_id"],
        "unique": ["ticker"],
        "natural_keys": [["name"]],
        "required": ["security_id", "ticker", "name", "asset_class", "benchmark_id", "is_etf"],
        "types": {"security_id": "integer", "ticker": "code", "benchmark_id": "integer",
                  "is_etf": "boolean", "expense_ratio_bps": "decimal"},
        "allowed": {"asset_class": ["Equity", "Fixed Income", "Alternative",
                                    "Cash Equivalent"],
                    "sector": ["Communication Services", "Consumer Discretionary",
                               "Consumer Staples", "Energy", "Financials", "Health Care",
                               "Healthcare", "Industrials", "Materials", "Real Estate",
                               "Technology", "Information Technology", "Utilities"]},
        "patterns": {"ticker": (r"[A-Z][A-Z0-9.\-]{0,9}", WARN)},
        "ranges": {"expense_ratio_bps": (0, 300, ERROR)},
        "row_rules": [
            dict(name="ETF without expense ratio", severity=WARN,
                 desc="is_etf = True but expense_ratio_bps is blank",
                 cols=["is_etf", "expense_ratio_bps"],
                 rule=lambda t: (t.b("is_etf") == 1) & _blank(t, "expense_ratio_bps")),
            dict(name="non-ETF with expense ratio", severity=WARN,
                 desc="is_etf = False but an expense ratio is present",
                 cols=["is_etf", "expense_ratio_bps"],
                 rule=lambda t: (t.b("is_etf") == 0) & _present(t, "expense_ratio_bps")),
            dict(name="equity without sector", severity=WARN,
                 desc="asset_class = Equity but sector is blank",
                 cols=["asset_class", "sector"],
                 rule=lambda t: (t.s("asset_class") == "Equity") & _blank(t, "sector")),
            dict(name="non-equity with sector", severity=INFO,
                 desc="asset_class is not Equity but a sector is populated",
                 cols=["asset_class", "sector"],
                 rule=lambda t: (t.s("asset_class") != "Equity") & _present(t, "sector")),
        ],
        # A determines B: rows that deviate from the dominant B for their A
        "functional_dependencies": [("asset_class", "is_etf")],
    },
    # ------------------------------------------------------------------ facts
    "fact_fees": {
        "documented_rows": 122197,
        "expected_columns": ["fee_id", "client_id", "advisor_id", "fee_date", "fee_type",
                             "aum_basis", "bps_charged", "amount"],
        "primary_key": ["fee_id"],
        "natural_keys": [["client_id", "fee_date", "fee_type"]],
        "required": ["fee_id", "client_id", "advisor_id", "fee_date", "fee_type",
                     "aum_basis", "bps_charged", "amount"],
        "types": {"fee_id": "integer", "client_id": "integer", "advisor_id": "integer",
                  "fee_date": "date", "aum_basis": "decimal", "bps_charged": "decimal",
                  "amount": "decimal"},
        "allowed": {"fee_type": ["Management", "Advisory", "Custodial", "Performance"]},
        "ranges": {"aum_basis": (0.01, 1e10, ERROR), "bps_charged": (0, 300, ERROR),
                   "amount": (0, 1e8, ERROR), "fee_date": ("2020-01-01", "2024-12-31", ERROR)},
        "row_rules": [
            dict(name="fee amount recompute", severity=ERROR,
                 desc="amount != aum_basis x bps_charged / 10,000 / 12 (tolerance $0.01)",
                 cols=["aum_basis", "bps_charged", "amount"],
                 rule=lambda t: ((t.n("aum_basis") * t.n("bps_charged") / 10_000 / 12)
                                 - t.n("amount")).abs() > 0.0101),
            dict(name="fee not dated on 1st of month", severity=WARN,
                 desc="fee_date is not the first day of a month (monthly fee grain)",
                 cols=["fee_date"],
                 rule=lambda t: _ne(t.d("fee_date").dt.day, pd.Series(1, index=t.df.index))),
        ],
    },
    "fact_portfolio_snapshot": {
        "documented_rows": 129547,
        "expected_columns": ["snapshot_id", "client_id", "advisor_id", "benchmark_id",
                             "snapshot_date", "year", "month", "aum", "return_ytd",
                             "benchmark_return_ytd", "active_alpha_ytd", "cash_pct",
                             "account_type", "is_return_suspect"],
        "primary_key": ["snapshot_id"],
        "natural_keys": [["client_id", "snapshot_date"]],
        "required": ["snapshot_id", "client_id", "advisor_id", "benchmark_id",
                     "snapshot_date", "aum"],
        "types": {"snapshot_id": "integer", "client_id": "integer", "advisor_id": "integer",
                  "benchmark_id": "integer", "snapshot_date": "date", "year": "integer",
                  "month": "integer", "aum": "decimal", "return_ytd": "decimal",
                  "benchmark_return_ytd": "decimal", "active_alpha_ytd": "decimal",
                  "cash_pct": "decimal", "is_return_suspect": "boolean"},
        "allowed": {"account_type": ["Taxable", "IRA", "Roth IRA", "Trust", "529"]},
        "ranges": {"aum": (0.01, 1e10, ERROR), "cash_pct": (0, 1, ERROR),
                   "return_ytd": (-1, 10, ERROR), "benchmark_return_ytd": (-1, 2, ERROR),
                   "month": (1, 12, ERROR),
                   "snapshot_date": ("2020-01-01", "2024-12-31", ERROR)},
        "row_rules": [
            dict(name="year/month vs snapshot_date", severity=ERROR,
                 desc="year or month column disagrees with snapshot_date",
                 cols=["snapshot_date", "year", "month"],
                 rule=lambda t: t.d("snapshot_date").notna() & (
                     _ne(t.n("year"), t.d("snapshot_date").dt.year)
                     | _ne(t.n("month"), t.d("snapshot_date").dt.month))),
            dict(name="alpha recompute", severity=ERROR,
                 desc="active_alpha_ytd != return_ytd - benchmark_return_ytd (tolerance 1e-5)",
                 cols=["return_ytd", "benchmark_return_ytd", "active_alpha_ytd"],
                 rule=lambda t: (t.n("return_ytd") - t.n("benchmark_return_ytd")
                                 - t.n("active_alpha_ytd")).abs() > 1e-5),
            dict(name="implausible return not flagged", severity=WARN,
                 desc="|return_ytd| > 100% but is_return_suspect = 0",
                 cols=["return_ytd", "is_return_suspect"],
                 rule=lambda t: (t.n("return_ytd").abs() > 1) & (t.b("is_return_suspect") == 0)),
            dict(name="return flagged suspect", severity=INFO,
                 desc="Source already flags these returns as suspect (is_return_suspect = 1)",
                 cols=["is_return_suspect"],
                 rule=lambda t: t.b("is_return_suspect") == 1),
            dict(name="snapshot not dated on 1st of month", severity=WARN,
                 desc="snapshot_date is not the first day of a month (monthly grain)",
                 cols=["snapshot_date"],
                 rule=lambda t: _ne(t.d("snapshot_date").dt.day, pd.Series(1, index=t.df.index))),
        ],
    },
    "fact_transactions": {
        "documented_rows": 298772,
        "expected_columns": ["txn_id", "client_id", "advisor_id", "security_id", "txn_date",
                             "txn_type", "shares", "price", "amount",
                             "is_post_departure_txn"],
        "primary_key": ["txn_id"],
        "required": ["txn_id", "client_id", "advisor_id", "txn_date", "txn_type", "amount"],
        "types": {"txn_id": "integer", "client_id": "integer", "advisor_id": "integer",
                  "security_id": "integer", "txn_date": "date", "shares": "decimal",
                  "price": "decimal", "amount": "decimal",
                  "is_post_departure_txn": "boolean"},
        "allowed": {"txn_type": TRADE_TYPES + CASH_TYPES},
        "ranges": {"shares": (0, 1e8, ERROR), "price": (0.01, 1e6, ERROR),
                   "amount": (0, 1e9, ERROR), "txn_date": ("2020-01-01", "2024-12-31", ERROR)},
        "row_rules": [
            dict(name="security trade missing security/shares/price", severity=ERROR,
                 desc="Buy / Sell / Dividend row with blank security_id, shares or price",
                 cols=["txn_type", "security_id", "shares", "price"],
                 rule=lambda t: t.s("txn_type").isin(TRADE_TYPES)
                 & (_blank(t, "security_id") | _blank(t, "shares") | _blank(t, "price"))),
            dict(name="cash movement carrying security fields", severity=WARN,
                 desc="Deposit / Withdrawal / Advisory Fee row with security_id, shares or "
                      "price populated",
                 cols=["txn_type", "security_id", "shares", "price"],
                 rule=lambda t: t.s("txn_type").isin(CASH_TYPES)
                 & (_present(t, "security_id") | _present(t, "shares") | _present(t, "price"))),
            dict(name="amount vs shares x price", severity=ERROR,
                 desc="amount != shares x price (tolerance $0.01)",
                 cols=["shares", "price", "amount"],
                 rule=lambda t: ((t.n("shares") * t.n("price")) - t.n("amount")).abs() > 0.0101),
            dict(name="dividend equals full position value", severity=WARN,
                 desc="Dividend amount = shares x price (a 100% yield) - looks like a trade "
                      "value booked as income",
                 cols=["txn_type", "shares", "price", "amount"],
                 rule=lambda t: (t.s("txn_type") == "Dividend")
                 & (((t.n("shares") * t.n("price")) - t.n("amount")).abs() <= 0.0101)),
            dict(name="non-positive amount", severity=WARN,
                 desc="amount <= 0 (sign should be carried by txn_type)",
                 cols=["amount"], rule=lambda t: t.n("amount") <= 0),
        ],
        "position_check": dict(entity=["client_id", "security_id"], order=["txn_date", "txn_id"],
                               type_col="txn_type", qty_col="shares",
                               plus=["Buy"], minus=["Sell"]),
    },
}

# Explicit foreign keys. ID-based keys whose column name equals another table's PK
# are ALSO auto-discovered, so a new table gets RI checks without configuration.
FOREIGN_KEYS = [
    # (child_table, child_col, parent_table, parent_col, nullable_when)
    ("dim_client", "advisor_id", "dim_advisor", "advisor_id", None),
    ("dim_security", "benchmark_id", "dim_benchmark", "benchmark_id", None),
    ("fact_fees", "client_id", "dim_client", "client_id", None),
    ("fact_fees", "advisor_id", "dim_advisor", "advisor_id", None),
    ("fact_portfolio_snapshot", "client_id", "dim_client", "client_id", None),
    ("fact_portfolio_snapshot", "advisor_id", "dim_advisor", "advisor_id", None),
    ("fact_portfolio_snapshot", "benchmark_id", "dim_benchmark", "benchmark_id", None),
    ("fact_transactions", "client_id", "dim_client", "client_id", None),
    ("fact_transactions", "advisor_id", "dim_advisor", "advisor_id", None),
    # security_id is legitimately blank on cash movements
    ("fact_transactions", "security_id", "dim_security", "security_id",
     lambda t: t.s("txn_type").isin(CASH_TYPES)),
    # date role-playing keys
    ("fact_fees", "fee_date", "dim_date", "date", None),
    ("fact_portfolio_snapshot", "snapshot_date", "dim_date", "date", None),
    ("fact_transactions", "txn_date", "dim_date", "date", None),
]


# =============================================================================
# 2. HELPERS
# =============================================================================
class Typed:
    """Raw text frame + cached typed views of its columns."""

    def __init__(self, name: str, df: pd.DataFrame):
        self.name, self.df, self._c = name, df, {}

    def has(self, *cols):
        return all(c in self.df.columns for c in cols)

    def s(self, col):
        k = ("s", col)
        if k not in self._c:
            self._c[k] = self.df[col].astype(str).str.strip()
        return self._c[k]

    def n(self, col):
        k = ("n", col)
        if k not in self._c:
            self._c[k] = pd.to_numeric(self.s(col), errors="coerce")
        return self._c[k]

    def d(self, col):
        k = ("d", col)
        if k not in self._c:
            self._c[k] = to_dates(self.s(col))
        return self._c[k]

    def b(self, col):
        k = ("b", col)
        if k not in self._c:
            low = self.s(col).str.lower()
            out = pd.Series(np.nan, index=low.index)
            out[low.isin(BOOL_TRUE)] = 1.0
            out[low.isin(BOOL_FALSE)] = 0.0
            self._c[k] = out
        return self._c[k]


def to_dates(s: pd.Series) -> pd.Series:
    """Parse text dates. ISO first (fast), then every other known pattern."""
    out = pd.to_datetime(s.where(s.str.fullmatch(r"\d{4}-\d{2}-\d{2}")),
                         format="%Y-%m-%d", errors="coerce")
    rest = out.isna() & (s != "")
    if rest.any():
        for _, rx, fmt in DATE_PATTERNS[1:]:
            m = rest & s.str.fullmatch(rx)
            if m.any():
                out[m] = (pd.to_datetime(s[m], format=fmt, errors="coerce") if fmt
                          else pd.to_datetime(s[m], format="ISO8601", errors="coerce"))
                rest = out.isna() & (s != "")
    return out


def shape_of(u: pd.Series) -> pd.Series:
    """Value shape: letters -> A/a (runs collapsed), digits -> 9 (exact count kept)."""
    x = u.str.replace(r"[A-Z]", "A", regex=True).str.replace(r"[a-z]", "a", regex=True)
    x = x.str.replace(r"\d", "9", regex=True)
    x = x.str.replace(r"A+", "A", regex=True).str.replace(r"a+", "a", regex=True)
    return x.str.replace(r"(Aa)+", "Aa", regex=True)


def fmt_num(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return ""
    if isinstance(x, (pd.Timestamp,)):
        return x.strftime("%Y-%m-%d")
    x = float(x)
    if x.is_integer() and abs(x) < 1e15:
        return f"{int(x)}"
    return f"{x:.6f}".rstrip("0").rstrip(".")


def sample(values, n=SAMPLE_N):
    vals = [str(v) for v in list(dict.fromkeys(values))[:n]]
    return " | ".join(repr(v) if (v != v.strip() or v == "") else v for v in vals)


def classify_values(u: pd.Series, col: str) -> pd.DataFrame:
    """Per unique (stripped, non-null) value: which types it parses as."""
    out = pd.DataFrame(index=u.index)
    out["is_int"] = u.str.fullmatch(RE_INT)
    out["is_dec"] = u.str.fullmatch(RE_DEC)
    out["is_bool"] = u.str.lower().isin(BOOL_TOKENS)
    out["is_dirty_num"] = u.str.fullmatch(RE_DIRTY_NUM) & ~out["is_dec"]
    out["date_fmt"] = ""
    out["date_bad"] = False
    for name, rx, fmt in DATE_PATTERNS:
        if name == "YYYYMMDD" and not DATE_NAME_HINT.search(col):
            continue
        m = (out["date_fmt"] == "") & u.str.fullmatch(rx)
        if not m.any():
            continue
        parsed = (pd.to_datetime(u[m], format=fmt, errors="coerce") if fmt
                  else pd.to_datetime(u[m], format="ISO8601", errors="coerce"))
        out.loc[m, "date_fmt"] = name
        out.loc[m[m].index[parsed.isna().values], "date_bad"] = True
    out["is_date"] = (out["date_fmt"] != "") & ~out["date_bad"]
    return out


def infer_type(cls: pd.DataFrame, w: np.ndarray, col: str, low_vals: pd.Series) -> str:
    total = w.sum()
    if total == 0:
        return "empty"
    f = lambda m: w[m.values.astype(bool)].sum() / total
    if CODE_NAME_HINT.search(col):
        return "code"
    if f(cls["is_bool"]) >= 0.95 and low_vals.nunique() <= 4 and not col.lower().endswith("_id"):
        return "boolean"
    if f(cls["is_date"] | cls["date_bad"]) >= TYPE_THRESHOLD:
        return "date"
    if f(cls["is_int"]) >= TYPE_THRESHOLD:
        return "integer"
    if f(cls["is_dec"] | cls["is_dirty_num"]) >= TYPE_THRESHOLD:
        return "decimal"
    return "text"


# =============================================================================
# 3. THE AUDIT
# =============================================================================
class Audit:
    def __init__(self):
        self.findings: list[dict] = []
        self.profiles: list[dict] = []
        self.tables: dict[str, Typed] = {}
        self.inferred: dict[tuple, str] = {}
        self.pk: dict[str, list] = {}

    # ---------------------------------------------------------------- recording
    def add(self, group, check, table, column, severity, rows_affected, rows_checked,
            detail, examples="", auto_pass=True):
        """Record a finding. A group 2-4 check that finds nothing is kept as an INFO
        'PASS' line so the summary proves the check ran; group 1 passes are dropped
        (the column profile already shows them)."""
        rows_affected = int(rows_affected)
        if rows_affected == 0 and severity != INFO:
            if group == G1 or not auto_pass:
                return
            severity, detail, examples = INFO, "PASS - no rows where: " + detail, ""
        self.findings.append(dict(
            group=group, check=check, table=table, column=column, severity=severity,
            rows_affected=rows_affected, rows_checked=int(rows_checked),
            pct_affected=round(100 * rows_affected / rows_checked, 3) if rows_checked else 0.0,
            detail=detail, examples=examples))

    def keys_of(self, t: Typed, mask) -> str:
        pk = self.pk.get(t.name) or [t.df.columns[0]]
        sub = t.df.loc[mask[mask].index[:SAMPLE_N], pk]
        return " | ".join("/".join(map(str, r)) for r in sub.itertuples(index=False))

    # ---------------------------------------------------------------- loading
    def load(self, path: Path):
        name = path.stem
        raw = path.read_bytes()
        try:
            text, enc = raw.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            text, enc = raw.decode("latin-1"), "latin-1"
            self.add(G1, "file encoding", name, "(file)", WARN, 1, 1,
                     "File is not valid UTF-8; read as latin-1 - accented characters may be "
                     "garbled")
        if raw.startswith(b"\xef\xbb\xbf"):
            self.add(G1, "byte-order mark", name, "(file)", INFO, 1, 1,
                     "File starts with a UTF-8 BOM (handled; some tools show it in the first "
                     "header)")

        records = list(csv.reader(io.StringIO(text, newline="")))
        header, body = records[0], records[1:]
        ncol = len(header)
        blank_lines = [i for i, r in enumerate(body, 2) if not any(x.strip() for x in r)]
        ragged = [i for i, r in enumerate(body, 2)
                  if len(r) != ncol and any(x.strip() for x in r)]
        embedded_nl = sum(any("\n" in v or "\r" in v for v in r) for r in body)
        if ragged:
            self.add(G1, "ragged rows", name, "(file)", ERROR, len(ragged), len(body),
                     f"Rows whose field count differs from the header ({ncol}); values will "
                     "shift into the wrong columns",
                     "line " + ", ".join(map(str, ragged[:SAMPLE_N])))
        if blank_lines:
            self.add(G1, "blank lines", name, "(file)", INFO, len(blank_lines), len(body),
                     "Completely empty lines in the file (dropped on load)")
        if embedded_nl:
            self.add(G1, "embedded line breaks", name, "(file)", WARN, embedded_nl, len(body),
                     "Quoted values contain line breaks - physical line count != row count")
        dup_hdr = [h for h, c in Counter(header).items() if c > 1]
        if dup_hdr:
            self.add(G1, "duplicate header", name, ", ".join(dup_hdr), ERROR, len(dup_hdr),
                     ncol, "Column name appears more than once")
        bad_hdr = [h for h in header if h != h.strip() or not re.fullmatch(r"[a-z0-9_]+", h.strip())]
        if bad_hdr:
            self.add(G1, "header naming", name, ", ".join(map(repr, bad_hdr)), WARN,
                     len(bad_hdr), ncol,
                     "Header has whitespace, capitals or special characters (snake_case "
                     "expected)")

        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in body if any(x.strip() for x in r)]
        cols = [h.strip() for h in header]
        df = pd.DataFrame(rows, columns=cols, dtype=str)
        self.tables[name] = Typed(name, df)

        cfg = TABLE_RULES.get(name, {})
        if "documented_rows" in cfg and cfg["documented_rows"] != len(df):
            self.add(G1, "row count vs data dictionary", name, "(table)", WARN,
                     abs(cfg["documented_rows"] - len(df)), cfg["documented_rows"],
                     f"Data dictionary documents {cfg['documented_rows']:,} rows; file has "
                     f"{len(df):,}")
        if "expected_columns" in cfg:
            missing = [c for c in cfg["expected_columns"] if c not in cols]
            extra = [c for c in cols if c not in cfg["expected_columns"]]
            if missing:
                self.add(G1, "missing columns", name, ", ".join(missing), ERROR, len(missing),
                         len(cfg["expected_columns"]), "Expected columns not found in file")
            if extra:
                self.add(G1, "undocumented columns", name, ", ".join(extra), INFO, len(extra),
                         len(cols), "Columns present in the file but not in the expected "
                         "schema")
        print(f"  loaded {name:<28} {len(df):>9,} rows x {len(cols)} cols  ({enc})")

    # ---------------------------------------------------------------- primary keys
    def resolve_pk(self, name):
        t = self.tables[name]
        cfg = TABLE_RULES.get(name, {})
        if cfg.get("primary_key"):
            self.pk[name] = [c for c in cfg["primary_key"] if c in t.df.columns]
            return
        # Generic guess: <table singular>_id, else the first *_id column, else column 1
        stem = re.sub(r"^(dim|fact|stg|tbl)_", "", name).rstrip("s")
        cands = [f"{stem}_id"] + [c for c in t.df.columns if c.endswith("_id")]
        for c in cands:
            if c in t.df.columns:
                self.pk[name] = [c]
                return
        self.pk[name] = [t.df.columns[0]]

    # ---------------------------------------------------------------- profiling
    def profile_table(self, name):
        t = self.tables[name]
        cfg = TABLE_RULES.get(name, {})
        n = len(t.df)
        for pos, col in enumerate(t.df.columns, 1):
            self.profile_column(t, cfg, col, pos, n)

    def profile_column(self, t: Typed, cfg, col, pos, n):
        name = t.name
        raw = t.df[col].astype(str)
        vc = raw.value_counts(sort=True)
        u_raw = pd.Series(vc.index.astype(str), dtype=str)
        w = vc.values.astype(np.int64)
        u = u_raw.str.strip()
        low = u.str.lower()
        is_blank = (u == "").values
        is_tok = (low.isin(NULL_TOKENS) & (u != "")).values
        placeholders = set(cfg.get("placeholder_values", {}).get(col, []))
        is_ph = u.isin(placeholders).values & ~is_tok
        is_tok = is_tok | is_ph
        present = ~(is_blank | is_tok)

        blank_cnt = int(w[is_blank].sum())
        tok_cnt = int(w[is_tok].sum())
        nb = blank_cnt + tok_cnt
        n_present = int(w[present].sum())
        up, wp = u[present].reset_index(drop=True), w[present]

        # ---- types
        cls = classify_values(up, col) if len(up) else pd.DataFrame(
            columns=["is_int", "is_dec", "is_bool", "is_dirty_num", "date_fmt", "date_bad",
                     "is_date"])
        inferred = infer_type(cls, wp, col, up.str.lower()) if len(up) else "empty"
        expected = cfg.get("types", {}).get(col, inferred)
        self.inferred[(name, col)] = expected
        type_src = "config" if col in cfg.get("types", {}) else "inferred"

        if len(up):
            if expected == "integer":
                integral = cls["is_dec"] & up.where(cls["is_dec"], "nan").astype(float).map(
                    lambda x: float(x).is_integer() if x == x else False)
                fail = ~(cls["is_int"] | integral)
            elif expected == "decimal":
                fail = ~cls["is_dec"]
            elif expected == "boolean":
                fail = ~cls["is_bool"]
            elif expected == "date":
                fail = ~cls["is_date"]
            else:
                fail = pd.Series(False, index=up.index)
            fail = fail.fillna(True).astype(bool)
        else:
            fail = pd.Series(dtype=bool)
        fail_cnt = int(wp[fail.values].sum()) if len(up) else 0

        # ---- shapes
        shp = shape_of(up) if len(up) else pd.Series(dtype=str)
        shape_counts = (pd.Series(wp, index=shp.values).groupby(level=0).sum()
                        .sort_values(ascending=False)) if len(up) else pd.Series(dtype=int)
        top_shapes = " | ".join(f"{s} ({100 * c / n_present:.1f}%)"
                                for s, c in shape_counts.head(5).items())

        # ---- whitespace (raw values, including blanks)
        ws_lt = int(w[(u_raw != u_raw.str.strip()).values & (u != "").values].sum())
        ws_blank_spaces = int(w[(u_raw != "").values & (u == "").values].sum())
        ws_dbl = int(w[u_raw.str.contains("  ", regex=False).values & (u != "").values].sum())
        ws_ctl = int(w[u_raw.str.contains(r"[\t\r\n]", regex=True).values].sum())
        ws_odd = int(w[u_raw.str.contains("[ ​‌‍﻿]",
                                          regex=True).values].sum())

        # ---- mixed formats
        mixed = []
        if len(up):
            if expected == "date":
                fm = pd.Series(wp, index=cls["date_fmt"].replace("", "unparseable").values)
                fm = fm.groupby(level=0).sum().sort_values(ascending=False)
                if len(fm) > 1:
                    mixed.append(("date formats: " + ", ".join(f"{k}={v}" for k, v in fm.items()),
                                  n_present - int(fm.iloc[0])))
            if expected in ("integer", "decimal"):
                sym = int(wp[cls["is_dirty_num"].values].sum())
                if sym:
                    mixed.append((f"numbers with $ , % or () = {sym}", sym))
                sci = int(wp[up.str.contains(r"[eE]").values & cls["is_dec"].values].sum())
                if sci:
                    mixed.append((f"scientific notation = {sci}", sci))
                lz = int(wp[up.str.fullmatch(r"[+-]?0\d+(\.\d*)?").values].sum())
                if lz:
                    mixed.append((f"leading zeros = {lz}", lz))
                if expected == "integer":
                    dot0 = int(wp[up.str.fullmatch(r"[+-]?\d+\.0+").values].sum())
                    plain = int(wp[cls["is_int"].values].sum())
                    if dot0 and plain:
                        mixed.append((f"integers written as 'n.0' = {dot0} vs plain = {plain}",
                                      min(dot0, plain)))
                    elif dot0:
                        mixed.append((f"all integers written as 'n.0' ({dot0})", dot0))
                if expected == "decimal":
                    dot0 = int(wp[up.str.fullmatch(r"[+-]?\d+\.0+").values].sum())
                    if dot0 == n_present and n_present:
                        mixed.append(("whole numbers stored with '.0' (integer-valued decimal)",
                                      dot0))
            if expected == "boolean":
                style = up.str.lower().map(lambda v: "1/0" if v in ("1", "0") else
                                           "Y/N" if v in ("y", "n") else
                                           "Yes/No" if v in ("yes", "no") else
                                           "T/F" if v in ("t", "f") else "True/False")
                st = pd.Series(wp, index=style.values).groupby(level=0).sum()
                spell = pd.Series(wp, index=up.values).groupby(level=0).sum()
                if len(st) > 1 or len(spell) > 2:
                    # rows not written in the dominant True/False spelling pair
                    canon = spell.groupby(spell.index.str.lower().isin(BOOL_TRUE)).idxmax()
                    off = int(spell[~spell.index.isin(canon.values)].sum())
                    mixed.append(("boolean spellings: " + ", ".join(f"{k}={v}" for k, v in spell.items()),
                                  off))
            if expected in ("text", "code"):
                grp = pd.DataFrame({"v": up, "k": up.str.lower().str.replace(r"\s+", " ", regex=True), "w": wp})
                var = grp.groupby("k").filter(lambda g: g["v"].nunique() > 1)
                if len(var):
                    ex = var.groupby("k")["v"].apply(lambda s: "/".join(s.unique()[:3])).head(3)
                    vw = var.groupby(["k", "v"])["w"].sum()
                    off = int(vw.sum() - vw.groupby(level=0).max().sum())
                    mixed.append((f"case/spacing variants: {off} rows off the dominant spelling, "
                                  "e.g. " + "; ".join(ex), off))
            if expected == "code" and len(shape_counts) > 1 and \
                    shape_counts.iloc[0] >= 0.8 * n_present:
                off = n_present - int(shape_counts.iloc[0])
                mixed.append((f"code shapes off the standard '{shape_counts.index[0]}' = {off} ("
                              + ", ".join(f"{s}={c}" for s, c in shape_counts.iloc[1:5].items())
                              + ")", off))

        # ---- numeric / date stats
        nums = pd.to_numeric(t.s(col), errors="coerce") if expected in ("integer", "decimal") \
            else pd.Series(dtype=float)
        mn = mx = mean = med = std = ""
        neg = zero = outl = ""
        if expected in ("integer", "decimal") and nums.notna().any():
            v = nums.dropna()
            mn, mx, mean, med, std = map(fmt_num, (v.min(), v.max(), v.mean(), v.median(), v.std()))
            neg, zero = int((v < 0).sum()), int((v == 0).sum())
            q1, q3 = v.quantile([0.25, 0.75])
            iqr = q3 - q1
            outl = int(((v < q1 - 3 * iqr) | (v > q3 + 3 * iqr)).sum()) if iqr > 0 else 0
        elif expected == "date":
            dts = t.d(col).dropna()
            if len(dts):
                mn, mx = fmt_num(dts.min()), fmt_num(dts.max())

        lens = up.str.len()
        top_vals = " | ".join(f"{v} ({c})" for v, c in zip(u_raw.head(5), w[:5]))
        distinct = int(present.sum())

        self.profiles.append(dict(
            table=name, column=col, position=pos, row_count=n,
            expected_type=expected, type_source=type_src,
            blank_count=blank_cnt, null_token_count=tok_cnt,
            null_tokens_found=", ".join(sorted(set(u[is_tok]))),
            null_or_blank_count=nb, null_or_blank_pct=round(100 * nb / n, 3) if n else 0,
            distinct_count=distinct,
            distinct_pct=round(100 * distinct / n_present, 3) if n_present else 0,
            is_unique=bool(n_present == n and distinct == n),
            top_values=top_vals,
            shape_count=len(shape_counts), dominant_shapes=top_shapes,
            dominant_shape_pct=round(100 * shape_counts.iloc[0] / n_present, 2) if len(shape_counts) else 0,
            parse_fail_count=fail_cnt,
            parse_fail_pct=round(100 * fail_cnt / n_present, 3) if n_present else 0,
            parse_fail_examples=sample(up[fail.values]) if fail_cnt else "",
            ws_leading_trailing=ws_lt, ws_spaces_only=ws_blank_spaces,
            ws_double_internal=ws_dbl, ws_tab_newline=ws_ctl, ws_nonstandard=ws_odd,
            mixed_formats=" ; ".join(m for m, _ in mixed),
            min_value=mn, max_value=mx, mean=mean, median=med, std=std,
            negative_count=neg, zero_count=zero, outliers_3iqr=outl,
            min_length=int(lens.min()) if len(lens) else "",
            max_length=int(lens.max()) if len(lens) else ""))

        # ---------------- Group 1 findings from the profile ----------------
        key_cols = set(self.pk.get(name, [])) | {fk[1] for fk in FOREIGN_KEYS if fk[0] == name}
        req = col in cfg.get("required", [])
        if fail_cnt:
            sev = ERROR if expected in ("integer", "decimal", "date", "boolean") else WARN
            self.add(G1, "type parse failure", name, col, sev, fail_cnt, n_present,
                     f"{fail_cnt:,} values do not parse as {expected}",
                     sample(up[fail.values]))
        ph_cnt = int(w[is_ph].sum())
        if tok_cnt - ph_cnt:
            real = is_tok & ~is_ph
            self.add(G1, "null-like token", name, col, ERROR if req else WARN, tok_cnt - ph_cnt, n,
                     f"Text tokens that usually mean 'missing' ({', '.join(sorted(set(u[real])))}) "
                     "- a BI tool will treat them as real values; decide missing vs. legitimate "
                     "category", "")
        if ph_cnt:
            self.add(G1, "placeholder category", name, col, WARN, ph_cnt, n,
                     f"Placeholder value(s) {sorted(set(u[is_ph]))} stand in for an unknown "
                     "value - excluded from distinct counts and parse checks", "")
        if blank_cnt:
            self.add(G1, "blank values", name, col, ERROR if req else INFO, blank_cnt, n,
                     ("Required column has blanks" if req else
                      "Blanks present - check whether they are structural (legitimately "
                      "not applicable) or missing"), "")
        ws_total = ws_lt + ws_dbl + ws_ctl + ws_odd
        if ws_total:
            self.add(G1, "whitespace", name, col, ERROR if col in key_cols else WARN, ws_total, n,
                     f"leading/trailing={ws_lt}, double spaces={ws_dbl}, tab/newline={ws_ctl}, "
                     f"non-breaking/zero-width={ws_odd}"
                     + (" - breaks joins on this key" if col in key_cols else ""),
                     sample(u_raw[(u_raw != u_raw.str.strip()).values]))
        if ws_blank_spaces:
            self.add(G1, "whitespace-only values", name, col, WARN, ws_blank_spaces, n,
                     "Cells contain only spaces (look non-blank to some tools)")
        for m, affected in mixed:
            sev = WARN
            if m.startswith("date formats") or m.startswith("numbers with"):
                sev = ERROR
            if m.startswith(("whole numbers stored", "all integers written", "scientific")):
                sev = INFO
            if m.startswith("leading zeros") and col in key_cols:
                sev = ERROR
            self.add(G1, "mixed formats", name, col, sev, affected, n_present, m)
        if expected in ("integer", "decimal") and isinstance(outl, int) and outl \
                and col not in key_cols and not col.endswith("_id"):
            self.add(G1, "statistical outliers (3xIQR)", name, col, INFO, outl, n_present,
                     f"Values beyond Q1-3*IQR / Q3+3*IQR (min {mn}, max {mx}); flagged is not "
                     "the same as wrong")
        if n_present and distinct == 1 and n > 1:
            self.add(G1, "constant column", name, col, INFO, n_present, n,
                     f"Only one distinct value ({up.iloc[0]})")

        # placeholder / sentinel values in high-cardinality numeric or date columns
        if expected in ("integer", "decimal", "date") and len(wp) > 20 and col not in key_cols:
            top_c, second = wp[0], (wp[1] if len(wp) > 1 else 0)
            if distinct / max(n_present, 1) > 0.5 and top_c >= 10 and top_c >= 10 * max(second, 1):
                self.add(G1, "placeholder / sentinel value", name, col, WARN, int(top_c), n_present,
                         f"Value '{up.iloc[0]}' repeats {top_c:,}x in an otherwise near-unique "
                         f"column (next most common repeats {second}x) - looks like a default, "
                         "floor or placeholder", up.iloc[0])
        if expected == "date" and len(up):
            sent = up[up.str.match(r"(1900|1899|1970-01-01|9999|2099|0001)")]
            if len(sent):
                self.add(G1, "placeholder / sentinel value", name, col, WARN,
                         int(wp[sent.index].sum()), n_present, "Sentinel dates found",
                         sample(sent))

        # likely typos: rare spelling close to a frequent category
        if expected == "text" and 2 <= distinct <= 60 and n_present >= 50:
            cats = pd.Series(wp, index=up.values).groupby(level=0).sum()
            freq = [c for c, k in cats.items() if k >= max(5, 0.01 * n_present)]
            rare = [c for c, k in cats.items() if k < max(5, 0.01 * n_present)]
            freq_low = {f.lower(): f for f in freq}
            for r in rare:
                if r.lower() in freq_low:
                    continue          # pure case variant - already reported under mixed formats
                close = difflib.get_close_matches(r.lower(), list(freq_low), 1, 0.8)
                if close:
                    self.add(G1, "likely typo / variant category", name, col, WARN, int(cats[r]),
                             n_present, f"Rare value '{r}' ({cats[r]}x) closely resembles "
                             f"frequent value '{freq_low[close[0]]}'", r)

        # ---- configured column rules
        s = t.s(col)
        if col in cfg.get("allowed", {}):
            allowed = set(cfg["allowed"][col])
            bad = (s != "") & ~s.isin(allowed) & ~s.isin(placeholders) & ~s.str.lower().isin(NULL_TOKENS)
            self.add(G1, "allowed values", name, col, ERROR, bad.sum(), n,
                     f"Values outside the allowed list ({len(allowed)} codes)",
                     sample(s[bad]))
        if col in cfg.get("allowed_list_tokens", {}):
            sep, allowed = cfg["allowed_list_tokens"][col]
            toks = s.str.split(sep)
            bad = toks.map(lambda L: any(x.strip() not in allowed for x in L if x.strip()))
            self.add(G1, "allowed values (multi-valued)", name, col, WARN, bad.sum(), n,
                     f"List items outside {sorted(allowed)}", sample(s[bad]))
            multi = s.str.contains(sep, regex=False)
            if multi.any():
                self.add(G1, "multi-valued cell", name, col, INFO, multi.sum(), n,
                         f"Cells hold several '{sep}'-separated values - split before "
                         "filtering or counting", sample(s[multi]))
        if col in cfg.get("patterns", {}):
            rx, sev = cfg["patterns"][col]
            bad = (s != "") & ~s.str.fullmatch(rx)
            self.add(G1, "pattern", name, col, sev, bad.sum(), n,
                     f"Values not matching /{rx}/", sample(s[bad]))
        if col in cfg.get("ranges", {}):
            lo, hi, sev = cfg["ranges"][col]
            if expected == "date":
                lo_v = TODAY if lo == "today" else pd.Timestamp(lo)
                hi_v = TODAY if hi == "today" else pd.Timestamp(hi)
                v = t.d(col)
            else:
                lo_v, hi_v, v = lo, hi, t.n(col)
            bad = (v < lo_v) | (v > hi_v)
            self.add(G1, "range", name, col, sev, bad.sum(), int(v.notna().sum()),
                     f"Values outside [{lo}, {hi}]", sample(s[bad]))

    # ---------------------------------------------------------------- key uniqueness
    def key_checks(self, name):
        t, cfg = self.tables[name], TABLE_RULES.get(name, {})
        n = len(t.df)
        pk = self.pk[name]
        if pk:
            k = t.df[pk].apply(lambda c: c.str.strip())
            blank = (k == "").any(axis=1)
            dup = k.duplicated(keep=False) & ~blank
            self.add(G1, "primary key uniqueness", name, "+".join(pk), ERROR, dup.sum(), n,
                     f"Duplicate primary key values ({k[dup].drop_duplicates().shape[0]} distinct "
                     "keys repeated)", self.keys_of(t, dup))
            self.add(G1, "primary key completeness", name, "+".join(pk), ERROR, blank.sum(), n,
                     "Blank primary key", "")
            # key normalisation collisions (e.g. '7' vs '07' vs '7.0' vs ' 7')
            if len(pk) == 1:
                norm = k[pk[0]].str.lstrip("0").str.replace(r"\.0+$", "", regex=True)
                coll = norm.duplicated(keep=False) & ~k[pk[0]].duplicated(keep=False)
                self.add(G1, "primary key format collision", name, pk[0], ERROR, coll.sum(), n,
                         "Different spellings of the same key (leading zeros / '.0')",
                         self.keys_of(t, coll))
            if not (dup.any() or blank.any()):
                self.add(G1, "primary key uniqueness", name, "+".join(pk), INFO, 0, n,
                         f"Primary key {'+'.join(pk)} is unique and complete")
        for col in cfg.get("unique", []):
            if col in t.df.columns:
                s = t.s(col)
                dup = s.duplicated(keep=False) & (s != "")
                self.add(G1, "alternate key uniqueness", name, col, ERROR, dup.sum(), n,
                         f"{col} should be unique", sample(s[dup]))

    # ---------------------------------------------------------------- Group 2
    def single_table(self, name):
        t, cfg = self.tables[name], TABLE_RULES.get(name, {})
        df, n = t.df, len(t.df)
        pk = self.pk[name]
        non_key = [c for c in df.columns if c not in pk]

        exact = df.duplicated(keep="first")
        self.add(G2, "exact duplicate rows", name, "(all columns)", ERROR, exact.sum(), n,
                 "Rows identical in every column (extra copies counted)", self.keys_of(t, exact))
        if non_key:
            dup_nk = df.duplicated(subset=non_key, keep="first") & ~exact
            self.add(G2, "duplicate rows with different key", name, "(all non-key columns)", WARN,
                     dup_nk.sum(), n, "Same content under a different primary key - likely a "
                     "double load", self.keys_of(t, dup_nk))
            norm = pd.DataFrame({c: t.s(c).str.lower().str.replace(r"\s+", " ", regex=True)
                                 for c in non_key})
            for c in non_key:  # numeric normalisation: '45' == '45.0' == '045'
                if self.inferred.get((name, c)) in ("integer", "decimal"):
                    v = t.n(c)
                    norm[c] = np.where(v.notna(), v.round(6).astype(str), norm[c])
            near = norm.duplicated(keep="first") & ~df.duplicated(subset=non_key, keep="first")
            self.add(G2, "near-duplicate rows", name, "(all non-key columns)", WARN, near.sum(), n,
                     "Rows that become duplicates after trimming, lower-casing and numeric "
                     "normalisation", self.keys_of(t, near))

        for nk in cfg.get("natural_keys", []):
            if all(c in df.columns for c in nk):
                k = pd.DataFrame({c: t.s(c).str.lower() for c in nk})
                dup = k.duplicated(keep=False) & (k != "").all(axis=1)
                sev = ERROR if name.startswith("fact") else WARN
                self.add(G2, "natural key duplicates", name, "+".join(nk), sev, dup.sum(), n,
                         f"More than one row per ({', '.join(nk)}) - grain violation or "
                         "duplicate entity", self.keys_of(t, dup))

        for r in cfg.get("row_rules", []):
            if not t.has(*r["cols"]):
                continue
            try:
                bad = r["rule"](t).fillna(False).astype(bool)
            except Exception as e:  # never let one rule kill the audit
                self.add(G2, r["name"], name, ", ".join(r["cols"]), WARN, 1, 1,
                         f"Rule could not be evaluated: {e}")
                continue
            self.add(G2, r["name"], name, ", ".join(r["cols"]), r["severity"], bad.sum(), n,
                     r["desc"], self.keys_of(t, bad))

        for a, b in cfg.get("functional_dependencies", []):
            if t.has(a, b):
                self.fd_check(G2, name, t.s(a), t.s(b), f"{a} -> {b}", t)

        if "position_check" in cfg:
            p = cfg["position_check"]
            if t.has(*p["entity"], *p["order"], p["type_col"], p["qty_col"]):
                d = df[p["entity"]].copy()
                typ = t.s(p["type_col"])
                q = t.n(p["qty_col"]).fillna(0)
                d["delta"] = np.where(typ.isin(p["plus"]), q, np.where(typ.isin(p["minus"]), -q, 0))
                d["_d"], d["_i"] = t.d(p["order"][0]), t.n(p["order"][1])
                d = d[typ.isin(p["plus"] + p["minus"])].sort_values(p["entity"] + ["_d", "_i"])
                d["pos"] = d.groupby(p["entity"])["delta"].cumsum()
                bad = pd.Series(False, index=df.index)
                bad[d.index[(d["pos"] < -1e-6) & (d["delta"] < 0)]] = True
                ents = d.loc[bad[bad].index, p["entity"]].drop_duplicates().shape[0]
                self.add(G2, "sell exceeds position", name, ", ".join(p["entity"] + [p["qty_col"]]),
                         WARN, bad.sum(), int((d["delta"] < 0).sum()),
                         f"Sells that take the running share position below zero "
                         f"({ents:,} client-security pairs) - short sale, missing opening "
                         "position, or bad data", self.keys_of(t, bad))

        if "completeness" in cfg:
            c = cfg["completeness"]["date_column"]
            d = t.d(c).dropna()
            if len(d):
                full = pd.date_range(d.min(), d.max(), freq="D")
                gaps = full.difference(pd.DatetimeIndex(d))
                self.add(G2, "calendar gaps", name, c, ERROR if len(gaps) else INFO, len(gaps),
                         len(full), f"Missing dates between {d.min():%Y-%m-%d} and "
                         f"{d.max():%Y-%m-%d}" if len(gaps) else
                         f"Calendar continuous {d.min():%Y-%m-%d} to {d.max():%Y-%m-%d}",
                         sample(gaps.strftime("%Y-%m-%d")))

    def fd_check(self, group, table, a: pd.Series, b: pd.Series, label, t: Typed):
        """A should determine B: flag rows whose B is not the dominant B for their A."""
        d = pd.DataFrame({"a": a.values, "b": b.values}, index=a.index)
        d = d[(d.a != "") & (d.b != "")]
        if d.empty:
            return
        mode = d.groupby("a")["b"].agg(lambda s: s.value_counts().index[0])
        bad = d["b"] != d["a"].map(mode)
        multi = d.groupby("a")["b"].nunique()
        mapping = "; ".join(f"{k}->{v}" for k, v in mode.head(8).items())
        if bad.any():
            mask = pd.Series(False, index=t.df.index)
            mask[d.index[bad]] = True
            self.add(group, f"consistency {label}", table, label, WARN, bad.sum(), len(d),
                     f"{(multi > 1).sum()} value(s) of the left side map to more than one value "
                     f"on the right; rows not on the dominant mapping. Dominant: {mapping}",
                     self.keys_of(t, mask))
        else:
            self.add(group, f"consistency {label}", table, label, INFO, 0, len(d),
                     f"Each left value maps to exactly one right value: {mapping}")

    # ---------------------------------------------------------------- Group 3
    def discover_fks(self):
        fks = [fk for fk in FOREIGN_KEYS if fk[0] in self.tables and fk[2] in self.tables]
        known = {(c, cc) for c, cc, *_ in fks}
        single_pks = {self.pk[p][0]: p for p in self.tables if len(self.pk[p]) == 1}
        for child, t in self.tables.items():
            for col in t.df.columns:
                if col.endswith("_id") and col in single_pks and single_pks[col] != child \
                        and (child, col) not in known and col not in self.pk[child]:
                    fks.append((child, col, single_pks[col], col, None))
                    self.add(G3, "auto-discovered foreign key", child, col, INFO, 0, len(t.df),
                             f"{child}.{col} treated as FK to {single_pks[col]}.{col} (name match)")
        return fks

    def referential(self):
        for child, ccol, parent, pcol, nullable in self.discover_fks():
            ct, pt = self.tables[child], self.tables[parent]
            if ccol not in ct.df.columns or pcol not in pt.df.columns:
                continue
            fk_raw, pk_raw = ct.df[ccol].astype(str), pt.df[pcol].astype(str)
            fk, pk = fk_raw.str.strip(), pk_raw.str.strip()
            n = len(fk)
            label = f"{ccol} -> {parent}.{pcol}"
            blank = fk == ""
            if nullable is not None:
                allowed_blank = nullable(ct).fillna(False).astype(bool)
                bad_blank = blank & ~allowed_blank
                self.add(G3, "FK blank (structural)", child, label, INFO,
                         (blank & allowed_blank).sum(), n,
                         "Blank FK where the business rule allows it (not an orphan)")
            else:
                bad_blank = blank
            self.add(G3, "FK blank", child, label, ERROR, bad_blank.sum(), n,
                     "Foreign key is blank where a parent is required", self.keys_of(ct, bad_blank),
                     auto_pass=False)

            pk_set = set(pk_raw)
            is_date = self.inferred.get((child, ccol)) == "date"
            if is_date:  # compare on parsed dates so format differences are separate findings
                fkd, pkd = ct.d(ccol), pt.d(pcol)
                orphan = ~blank & fkd.notna() & ~fkd.isin(set(pkd.dropna()))
                raw_mismatch = ~blank & ~fk_raw.isin(pk_set) & ~orphan
            else:
                fk_norm = fk.str.lstrip("0").str.replace(r"\.0+$", "", regex=True)
                pk_norm = set(pk.str.lstrip("0").str.replace(r"\.0+$", "", regex=True))
                orphan = ~blank & ~fk_norm.isin(pk_norm)
                raw_mismatch = ~blank & ~fk_raw.isin(pk_set) & ~orphan
            matched = (~blank & ~orphan).sum()
            self.add(G3, "FK orphans", child, label, ERROR, orphan.sum(), n,
                     f"FK values with no matching parent key "
                     f"({fk[orphan].nunique()} distinct orphan keys). Match rate "
                     f"{100 * matched / max((~blank).sum(), 1):.2f}% of non-blank",
                     sample(fk[orphan]), auto_pass=False)
            self.add(G3, "FK format mismatch", child, label, ERROR, raw_mismatch.sum(), n,
                     "Key matches only after trimming/normalising - a raw text join will miss "
                     "these rows", sample(fk_raw[raw_mismatch]), auto_pass=False)
            if not (orphan.any() or bad_blank.any() or raw_mismatch.any()):
                self.add(G3, "FK integrity", child, label, INFO, 0, n,
                         f"All {(~blank).sum():,} non-blank keys resolve to {parent}")
            used = set(fk[~blank]) if not is_date else set(ct.d(ccol).dropna())
            parent_vals = pk if not is_date else pt.d(pcol)
            unused = ~parent_vals.isin(used)
            if unused.any() and not is_date:
                self.add(G3, "unused parent keys", parent, f"{pcol} <- {child}.{ccol}", INFO,
                         unused.sum(), len(pk), f"Parent rows never referenced by {child}",
                         sample(pk[unused]))

    # ---------------------------------------------------------------- Group 4
    def cross_table(self):
        for fn in CROSS_TABLE_RULES:
            try:
                fn(self)
            except KeyError:
                continue          # a table or column the rule needs is not loaded
            except Exception as e:
                self.add(G4, fn.__name__, "-", "-", WARN, 1, 1, f"Rule could not run: {e}")

    # ---------------------------------------------------------------- output
    def write(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        prof = pd.DataFrame(self.profiles)
        f = pd.DataFrame(self.findings)
        sev_order = {ERROR: 0, WARN: 1, INFO: 2}
        f["_s"] = f["severity"].map(sev_order)
        f = f.sort_values(["_s", "group", "table", "rows_affected"],
                          ascending=[True, True, True, False]).drop(columns="_s")
        f.insert(0, "finding_id", [f"F{i:04d}" for i in range(1, len(f) + 1)])
        prof.to_csv(out_dir / "profile_raw.csv", index=False)
        f.to_csv(out_dir / "dq_summary_raw.csv", index=False)
        return prof, f


# =============================================================================
# 4. CROSS-TABLE BUSINESS RULES  (each takes the Audit; KeyError => skipped)
# =============================================================================
def _need(a: Audit, *names):
    for nm in names:
        if nm not in a.tables:
            raise KeyError(nm)
    return [a.tables[nm] for nm in names]


def _client_lookup(a, col):
    cl, = _need(a, "dim_client")
    return dict(zip(cl.s("client_id"), cl.s(col)))


def xr_advisor_vs_client(a: Audit):
    """Fact advisor must be the client's advisor of record."""
    lk = _client_lookup(a, "advisor_id")
    for f in ("fact_fees", "fact_portfolio_snapshot", "fact_transactions"):
        if f not in a.tables:
            continue
        t = a.tables[f]
        exp = t.s("client_id").map(lk)
        bad = exp.notna() & (t.s("advisor_id") != exp)
        a.add(G4, "fact advisor vs client advisor", f, "advisor_id vs dim_client.advisor_id",
              WARN, bad.sum(), len(t.df),
              "Fact row credits a different advisor than the client's advisor of record "
              "(advisor change history or misattribution)", a.keys_of(t, bad))


def xr_account_type(a: Audit):
    s, = _need(a, "fact_portfolio_snapshot")
    exp = s.s("client_id").map(_client_lookup(a, "primary_account_type"))
    bad = exp.notna() & (s.s("account_type") != exp)
    a.add(G4, "snapshot account_type vs client", "fact_portfolio_snapshot",
          "account_type vs dim_client.primary_account_type", WARN, bad.sum(), len(s.df),
          "Snapshot account type disagrees with the client's primary account type",
          a.keys_of(s, bad))


def xr_before_client_since(a: Audit):
    cl, = _need(a, "dim_client")
    since = dict(zip(cl.s("client_id"), cl.d("client_since")))
    for f, dcol in (("fact_fees", "fee_date"), ("fact_portfolio_snapshot", "snapshot_date"),
                    ("fact_transactions", "txn_date")):
        if f not in a.tables:
            continue
        t = a.tables[f]
        sd = pd.to_datetime(t.s("client_id").map(since))
        # monthly facts are dated on the 1st: compare by month for them
        fd = t.d(dcol)
        if f != "fact_transactions":
            bad = fd.dt.to_period("M") < sd.dt.to_period("M")
        else:
            bad = fd < sd
        bad = bad.fillna(False)
        clients = t.s("client_id")[bad].nunique()
        a.add(G4, "fact dated before client_since", f, f"{dcol} vs dim_client.client_since", WARN,
              bad.sum(), len(t.df),
              f"Activity recorded before the client relationship began ({clients} clients) - "
              "client_since or the fact date is wrong", a.keys_of(t, bad))


def xr_activity_vs_is_active(a: Audit):
    """Inactive clients should stop having snapshots; active ones should reach the last month."""
    cl, s = _need(a, "dim_client", "fact_portfolio_snapshot")
    last = s.d("snapshot_date").groupby(s.s("client_id")).max()
    end = s.d("snapshot_date").max()
    act = pd.Series(cl.b("is_active").values, index=cl.s("client_id").values)
    lastc = last.reindex(act.index)
    active_stopped = (act == 1) & (lastc < end)
    inactive_running = (act == 0) & (lastc == end)
    no_snap = lastc.isna()
    a.add(G4, "active client without current snapshot", "dim_client",
          "is_active vs fact_portfolio_snapshot", WARN, active_stopped.sum(), len(act),
          f"is_active = True but last snapshot is before {end:%Y-%m-%d}",
          sample(act.index[active_stopped.values]))
    a.add(G4, "inactive client with current snapshot", "dim_client",
          "is_active vs fact_portfolio_snapshot", WARN, inactive_running.sum(), len(act),
          f"is_active = False but snapshots continue to {end:%Y-%m-%d}",
          sample(act.index[inactive_running.values]))
    a.add(G4, "client without any snapshot", "dim_client", "client_id", WARN, no_snap.sum(),
          len(act), "Client has no portfolio snapshot at all", sample(act.index[no_snap.values]))

    if "fact_transactions" in a.tables:
        t = a.tables["fact_transactions"]
        if not t.has("is_post_departure_txn"):
            return
        cid = t.s("client_id")
        dep = cid.map(lastc.where(act == 0).to_dict())     # departure month for inactive clients
        after = (t.d("txn_date").dt.to_period("M") > pd.to_datetime(dep).dt.to_period("M")).fillna(False)
        flag = t.b("is_post_departure_txn")
        a.add(G4, "post-departure transactions", "fact_transactions",
              "txn_date vs last snapshot of inactive client", WARN, after.sum(), len(t.df),
              "Transactions for inactive clients dated after their last snapshot month - the "
              "account should be closed", a.keys_of(t, after))
        miss = after & (flag == 0)
        wrong = ~after & (flag == 1)
        a.add(G4, "is_post_departure_txn flag vs data", "fact_transactions",
              "is_post_departure_txn", WARN, miss.sum() + wrong.sum(), len(t.df),
              f"Flag disagrees with recomputation: {miss.sum()} unflagged post-departure, "
              f"{wrong.sum()} flagged but not after departure", a.keys_of(t, miss | wrong))


def xr_fees_vs_snapshot(a: Audit):
    """Fee AUM basis and advisor should equal the same client-month snapshot."""
    fe, s = _need(a, "fact_fees", "fact_portfolio_snapshot")
    snap = pd.DataFrame({"c": s.s("client_id"), "m": s.d("snapshot_date").dt.to_period("M"),
                         "aum": s.n("aum"), "adv": s.s("advisor_id")}).drop_duplicates(["c", "m"])
    f = pd.DataFrame({"c": fe.s("client_id"), "m": fe.d("fee_date").dt.to_period("M"),
                      "basis": fe.n("aum_basis"), "adv": fe.s("advisor_id")}, index=fe.df.index)
    m = f.reset_index().merge(snap, on=["c", "m"], how="left", suffixes=("", "_s")).set_index("index")
    nosnap = m["aum"].isna()
    diff = (m["basis"] - m["aum"]).abs() > 0.01
    adv = m["adv_s"].notna() & (m["adv"] != m["adv_s"])
    a.add(G4, "fee without snapshot", "fact_fees", "client_id+fee_date vs snapshot", WARN,
          nosnap.sum(), len(m), "Fee charged in a month with no portfolio snapshot for the client",
          a.keys_of(fe, nosnap.reindex(fe.df.index)))
    a.add(G4, "fee aum_basis vs snapshot aum", "fact_fees", "aum_basis vs snapshot.aum", ERROR,
          (diff & ~nosnap).sum(), (~nosnap).sum(),
          "Fee AUM basis differs from the same month's snapshot AUM (> $0.01)",
          a.keys_of(fe, (diff & ~nosnap).reindex(fe.df.index)))
    a.add(G4, "fee advisor vs snapshot advisor", "fact_fees", "advisor_id vs snapshot.advisor_id",
          WARN, adv.sum(), len(m), "Fee and snapshot for the same client-month credit different "
          "advisors", a.keys_of(fe, adv.reindex(fe.df.index)))
    # snapshots with no fee
    fk = set(zip(f["c"], f["m"]))
    nofee = pd.Series([(c, p) not in fk for c, p in zip(snap["c"], snap["m"])], index=snap.index)
    a.add(G4, "snapshot month without fee", "fact_portfolio_snapshot",
          "client_id+snapshot_date vs fact_fees", INFO, nofee.sum(), len(snap),
          "Client-months with a snapshot but no fee charged (fee waiver, small accounts, or "
          "missing fee rows)")


def xr_fee_txn_reconcile(a: Audit):
    """Advisory Fee debits in transactions vs fees billed in fact_fees (client-month totals)."""
    fe, t = _need(a, "fact_fees", "fact_transactions")
    tx = t.df[t.s("txn_type") == "Advisory Fee"]
    if tx.empty:
        return
    tt = pd.DataFrame({"c": t.s("client_id")[tx.index], "m": t.d("txn_date")[tx.index].dt.to_period("M"),
                       "amt": t.n("amount")[tx.index]}).groupby(["c", "m"])["amt"].sum()
    ff = pd.DataFrame({"c": fe.s("client_id"), "m": fe.d("fee_date").dt.to_period("M"),
                       "amt": fe.n("amount")}).groupby(["c", "m"])["amt"].sum()
    j = pd.concat([tt.rename("txn"), ff.rename("fee")], axis=1)
    both = j.dropna()
    match = (both["txn"] - both["fee"]).abs() <= 0.01
    a.add(G4, "fee debits vs fees billed", "fact_transactions",
          "Advisory Fee amount vs fact_fees.amount (client-month)", WARN, (~match).sum(), len(both),
          f"Client-months where the fee debited in transactions differs from the fee billed. "
          f"Matched {match.sum():,}/{len(both):,}; months with a debit but no bill "
          f"{j['fee'].isna().sum():,}; billed but never debited {j['txn'].isna().sum():,}. Total "
          f"debited ${j['txn'].sum():,.0f} vs billed ${j['fee'].sum():,.0f}")


def xr_benchmark_mapping(a: Audit):
    """Each risk profile should map to one benchmark; benchmark returns must agree per date."""
    s, = _need(a, "fact_portfolio_snapshot")
    rp = s.s("client_id").map(_client_lookup(a, "risk_profile"))
    a.fd_check(G4, "fact_portfolio_snapshot", rp.fillna(""), s.s("benchmark_id"),
               "dim_client.risk_profile -> snapshot.benchmark_id", s)
    k = pd.DataFrame({"b": s.s("benchmark_id"), "d": s.s("snapshot_date"),
                      "r": s.n("benchmark_return_ytd")})
    nun = k.groupby(["b", "d"])["r"].transform("nunique")
    bad = nun > 1
    a.add(G4, "benchmark return consistency", "fact_portfolio_snapshot",
          "benchmark_return_ytd per benchmark_id+date", ERROR, bad.sum(), len(k),
          "Same benchmark on the same date carries different YTD returns", a.keys_of(s, bad))


# Which benchmark asset classes are a sensible yardstick for each security asset class
SECURITY_BENCHMARK_FIT = {
    "Equity": {"Equity", "International", "Small Cap", "Emerging"},
    "Fixed Income": {"Fixed Income"},
    "Cash Equivalent": {"Cash"},
    "Alternative": {"Multi-Asset"},
}


def xr_security_benchmark(a: Audit):
    sec, bm = _need(a, "dim_security", "dim_benchmark")
    bac = sec.s("benchmark_id").map(dict(zip(bm.s("benchmark_id"), bm.s("asset_class"))))
    ac = sec.s("asset_class")
    fit = pd.Series([b in SECURITY_BENCHMARK_FIT.get(x, {b}) for x, b in zip(ac, bac)],
                    index=sec.df.index)
    bad = bac.notna() & ~fit
    a.add(G4, "security benchmark fit", "dim_security",
          "asset_class vs dim_benchmark.asset_class", WARN, bad.sum(), len(sec.df),
          "Security is benchmarked against an index of a different asset class",
          a.keys_of(sec, bad))


def xr_snapshot_vs_dim_date(a: Audit):
    s, dd = _need(a, "fact_portfolio_snapshot", "dim_date")
    cal = pd.DataFrame({"y": dd.n("year").values, "m": dd.n("month").values},
                       index=dd.s("date").values)
    cal = cal[~cal.index.duplicated()]
    y = s.s("snapshot_date").map(cal["y"])
    m = s.s("snapshot_date").map(cal["m"])
    bad = y.notna() & ((s.n("year") != y) | (s.n("month") != m))
    a.add(G4, "snapshot year/month vs dim_date", "fact_portfolio_snapshot",
          "year, month vs dim_date", ERROR, bad.sum(), len(s.df),
          "Fact year/month disagree with the calendar dimension for the same date",
          a.keys_of(s, bad))


def xr_trades_on_non_trading_days(a: Audit):
    t, dd = _need(a, "fact_transactions", "dim_date")
    trading = dict(zip(dd.s("date"), dd.b("is_trading_day")))
    td = t.s("txn_date").map(trading)
    bad = t.s("txn_type").isin(["Buy", "Sell"]) & (td == 0)
    a.add(G4, "trade on non-trading day", "fact_transactions",
          "txn_date vs dim_date.is_trading_day", WARN, bad.sum(),
          int(t.s("txn_type").isin(["Buy", "Sell"]).sum()),
          "Buy/Sell executed on a day dim_date marks as non-trading (weekend/holiday)",
          a.keys_of(t, bad))


def xr_trades_by_asset_class(a: Audit):
    t, sec = _need(a, "fact_transactions", "dim_security")
    ac = t.s("security_id").map(dict(zip(sec.s("security_id"), sec.s("asset_class"))))
    bad = (t.s("txn_type") == "Dividend") & (ac == "Cash Equivalent")
    a.add(G4, "dividend on cash-equivalent", "fact_transactions",
          "txn_type vs dim_security.asset_class", INFO, bad.sum(), int((t.s("txn_type") == "Dividend").sum()),
          "Dividends booked on cash-equivalent securities (usually interest)", a.keys_of(t, bad))


def xr_initial_aum_vs_first_snapshot(a: Audit):
    cl, s = _need(a, "dim_client", "fact_portfolio_snapshot")
    d = pd.DataFrame({"c": s.s("client_id"), "d": s.d("snapshot_date"), "aum": s.n("aum")})
    first = d.sort_values("d").groupby("c").first()
    since = dict(zip(cl.s("client_id"), cl.d("client_since")))
    ini = pd.Series(cl.n("initial_aum").values, index=cl.s("client_id").values)
    j = first.join(ini.rename("ini"), how="inner")
    # only meaningful when the first snapshot is the client's first month
    j = j[[pd.Timestamp(since.get(c)).to_period("M") == dt.to_period("M")
           if pd.notna(since.get(c)) else False for c, dt in zip(j.index, j["d"])]]
    if j.empty:
        return
    ratio = j["aum"] / j["ini"]
    bad = (ratio > 1.5) | (ratio < 0.5)
    a.add(G4, "initial_aum vs first snapshot", "dim_client", "initial_aum vs first snapshot aum",
          WARN, bad.sum(), len(j), "For clients onboarded inside the data window, first-month "
          "AUM differs from initial_aum by more than 50%", sample(j.index[bad]))


CROSS_TABLE_RULES = [
    xr_advisor_vs_client, xr_account_type, xr_before_client_since, xr_activity_vs_is_active,
    xr_fees_vs_snapshot, xr_fee_txn_reconcile, xr_benchmark_mapping, xr_security_benchmark,
    xr_snapshot_vs_dim_date, xr_trades_on_non_trading_days, xr_trades_by_asset_class,
    xr_initial_aum_vs_first_snapshot,
]


# =============================================================================
# 5. MAIN
# =============================================================================
def main(argv=None):
    here = Path(__file__).resolve().parent
    root = here.parent if here.name.lower() == "scripts" else here
    ap = argparse.ArgumentParser(description="Profile and data-quality audit for CSV tables")
    ap.add_argument("files", nargs="*", help="CSV files to audit (default: all *.csv in --data-dir)")
    ap.add_argument("--data-dir", default=str(root), help="folder holding the raw CSVs")
    ap.add_argument("--out-dir", default=str(root / "reports"), help="where reports are written")
    args = ap.parse_args(argv)

    files = [Path(f) for f in args.files] or sorted(Path(args.data_dir).glob("*.csv"))
    if not files:
        sys.exit(f"No CSV files found in {args.data_dir}")

    a = Audit()
    print(f"Reading {len(files)} table(s) as text ...")
    for f in files:
        a.load(f)
    for nm in a.tables:
        a.resolve_pk(nm)
    print("Profiling columns and running column-level checks ...")
    for nm in a.tables:
        a.profile_table(nm)
        a.key_checks(nm)
    print("Single-table rules ...")
    for nm in a.tables:
        a.single_table(nm)
    print("Referential integrity ...")
    a.referential()
    print("Cross-table business rules ...")
    a.cross_table()

    prof, f = a.write(Path(args.out_dir))
    print(f"\nWrote {len(prof)} column profiles -> {Path(args.out_dir) / 'profile_raw.csv'}")
    print(f"Wrote {len(f)} findings        -> {Path(args.out_dir) / 'dq_summary_raw.csv'}\n")
    pv = (f.pivot_table(index="group", columns="severity", values="finding_id", aggfunc="count",
                        fill_value=0).reindex(columns=[ERROR, WARN, INFO], fill_value=0))
    print(pv.to_string())
    print("\nTop ERROR / WARN findings:")
    top = f[f.severity != INFO].head(25)
    for r in top.itertuples():
        print(f"  [{r.severity:<5}] {r.table}.{r.column}: {r.check} - {r.rows_affected:,} rows")


if __name__ == "__main__":
    main()
