#!/usr/bin/env python3
"""
02_clean.py  -  Cleaning program for the Wildcat Capital extract (HW04, Part C)

Implements the decisions recorded in decision_log.md, one transformation at a time.

    reads   <project>/<table>.csv                    (8 raw files - NEVER modified)
    writes  <project>/data/clean/<table>_clean.csv   (same columns + 0/1 flag columns)
            <project>/data/clean/README_clean.md
            <project>/data/audit/cleaning_log.csv     (table, issue, action, rows affected, note)
            <project>/data/audit/value_changes.csv    (table, key column, key, column, old, new)
            <project>/data/audit/quarantine_<set>.csv (one file per quarantined set)

Rules this program follows
    * Every file is read as TEXT (dtype=str, keep_default_na=False), so nothing is
      converted by accident ("None" stays "None", "07589" keeps its zero).
    * Each step prints rows affected and a before/after check, then ASSERTS the
      after-state. If a fix does not do what it claims, the script stops.
      (Do not run with `python -O`, which disables assert statements.)
    * Where a value can be recomputed from other columns, or two tables describe the
      same fact, the repair is reconciled against that second source.
    * Nothing is deleted silently: removed rows go to a quarantine file with the
      reason, source file and source line, and raw = clean + quarantined is proven
      for every table.

Run from VS Code:   python scripts/02_clean.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name.lower() == "scripts" else HERE   # Wildcat Capital/
RAW_DIR = ROOT
CLEAN_DIR = ROOT / "data" / "clean"
AUDIT_DIR = ROOT / "data" / "audit"

TABLES = ["dim_advisor", "dim_benchmark", "dim_client", "dim_date", "dim_security",
          "fact_fees", "fact_portfolio_snapshot", "fact_transactions"]

PRIMARY_KEYS = {"dim_advisor": "advisor_id", "dim_benchmark": "benchmark_id",
                "dim_client": "client_id", "dim_date": "date_id",
                "dim_security": "security_id", "fact_fees": "fee_id",
                "fact_portfolio_snapshot": "snapshot_id", "fact_transactions": "txn_id"}

# (child table, child column, parent table, parent column)
RELATIONSHIPS = [
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
    ("fact_fees", "fee_date", "dim_date", "date"),
    ("fact_portfolio_snapshot", "snapshot_date", "dim_date", "date"),
    ("fact_transactions", "txn_date", "dim_date", "date"),
]

TRADE_TYPES = ["Buy", "Sell", "Dividend"]
CASH_TYPES = ["Deposit", "Withdrawal", "Advisory Fee"]
AUM_FLOOR = 1000.0
MONEY_TOL = 0.0101          # $0.01 plus float noise
RATE_TOL = 1e-5

# -----------------------------------------------------------------------------
# State and audit trail
# -----------------------------------------------------------------------------
raw: dict[str, pd.DataFrame] = {}
df: dict[str, pd.DataFrame] = {}                 # working (clean) copies
quarantine: dict[str, list[pd.DataFrame]] = {}   # set name -> list of frames
quarantine_counts: dict[str, int] = {t: 0 for t in TABLES}
cleaning_log: list[dict] = []
value_changes: list[dict] = []
added_flags: list[dict] = []
step_no = 0


def num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def dt(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")


def header(title: str):
    global step_no
    step_no += 1
    print(f"\n{'=' * 78}\nSTEP {step_no}: {title}\n{'-' * 78}")


def log(table, issue, action, rows, note="", echo=True):
    cleaning_log.append(dict(step=step_no, table=table, issue=issue, action=action,
                             rows_affected=int(rows), note=note))
    if echo:
        print(f"  -> {action.upper()}: {table} | {issue} | rows affected = {int(rows):,}")


def show(label, before, after):
    print(f"  {label:<44} before = {before!s:<14} after = {after!s}")


def record_changes(table, mask, column, old: pd.Series, new: pd.Series):
    key = PRIMARY_KEYS[table]
    for k, o, n in zip(df[table].loc[mask, key], old[mask], new[mask]):
        value_changes.append(dict(table=table, key_column=key, key=k, column=column,
                                  old_value=o, new_value=n, step=step_no))


def send_to_quarantine(table, set_name, mask, reason):
    rows = df[table].loc[mask].copy()
    rows.insert(0, "quarantine_reason", reason)
    rows.insert(1, "source_file", f"{table}.csv")
    rows.insert(2, "source_line", rows.index + 2)     # +1 header, +1 one-based lines
    quarantine.setdefault(set_name, []).append(rows)
    quarantine_counts[table] += int(mask.sum())
    df[table] = df[table].loc[~mask]


def add_flag(table, column, mask, meaning):
    df[table][column] = np.where(mask, "1", "0")
    added_flags.append(dict(table=table, column=column, rows_flagged=int(mask.sum()),
                            meaning=meaning))


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# =============================================================================
# STEP 1  Load raw files as text and fingerprint them
# =============================================================================
header("Load the 8 raw files as text and record a fingerprint of each")
raw_hash = {}
for t in TABLES:
    path = RAW_DIR / f"{t}.csv"
    assert path.exists(), f"Missing raw file: {path}"
    raw_hash[t] = sha256(path)
    raw[t] = pd.read_csv(path, dtype=str, keep_default_na=False)
    df[t] = raw[t].copy()
    print(f"  {t:<26} {len(raw[t]):>9,} rows x {raw[t].shape[1]:>2} cols   sha256 {raw_hash[t][:12]}...")
    assert raw[t].notna().all().all(), f"{t}: a value was read as NaN - text read failed"
log("all", "load", "read as text", sum(len(x) for x in raw.values()),
    "dtype=str, keep_default_na=False; sha256 recorded to prove raw files are untouched")


# =============================================================================
# STEP 2  Standardize: trim leading/trailing whitespace in every cell
# =============================================================================
header("Standardize - trim leading/trailing whitespace (protects every later comparison)")
total = 0
for t in TABLES:
    for c in df[t].columns:
        old = df[t][c]
        new = old.str.strip()
        mask = old != new
        if mask.any():
            record_changes(t, mask, c, old, new)
            df[t][c] = new
            total += int(mask.sum())
before = total
after = sum(int((df[t][c] != df[t][c].str.strip()).sum()) for t in TABLES for c in df[t].columns)
show("cells with leading/trailing whitespace", before, after)
assert after == 0, "Whitespace still present after trimming"
log("all", "leading/trailing whitespace", "standardize", total,
    "Profile found none in this extract; step kept as a safeguard for joins")


# =============================================================================
# STEP 3  Quarantine exact duplicate rows
# =============================================================================
header("Quarantine - exact duplicate rows (identical in every column)")
for t in TABLES:
    dup = df[t].duplicated(keep="first")
    before = int(dup.sum())
    if before:
        send_to_quarantine(t, "exact_duplicates", dup, "exact duplicate of an earlier row")
    after = int(df[t].duplicated().sum())
    show(f"{t}: duplicate rows", before, after)
    assert after == 0, f"{t}: duplicates remain"
    log(t, "exact duplicate rows", "quarantine", before,
        "Profile found none; step kept as a safeguard")


# =============================================================================
# STEP 4  Quarantine transactions dated after the client departed
#         Decision log #12.  Reconciled: recomputed from dim_client + snapshots
#         must equal the source flag is_post_departure_txn.
# =============================================================================
header("Quarantine - transactions after client departure (decision log #12)")
cl, sn, tx = df["dim_client"], df["fact_portfolio_snapshot"], df["fact_transactions"]
last_snap = dt(sn["snapshot_date"]).groupby(sn["client_id"]).max()
inactive = set(cl.loc[cl["is_active"] == "False", "client_id"])
departure_month = {c: last_snap[c].to_period("M") for c in inactive if c in last_snap.index}
txn_month = dt(tx["txn_date"]).dt.to_period("M")
dep = tx["client_id"].map(departure_month)
recomputed = pd.Series([d is not None and d == d and m > d for m, d in zip(txn_month, dep)],
                       index=tx.index)
source_flag = tx["is_post_departure_txn"] == "1"

disagree = int((recomputed != source_flag).sum())
show("recomputed vs source flag disagreements", "-", disagree)
assert disagree == 0, ("Recomputed post-departure set differs from is_post_departure_txn - "
                       "stop and investigate before quarantining")
before = int(source_flag.sum())
send_to_quarantine("fact_transactions", "post_departure_txn", source_flag,
                   "transaction dated after the inactive client's last snapshot month")
tx = df["fact_transactions"]
after = int((tx["is_post_departure_txn"] == "1").sum())
show("post-departure transactions in clean table", before, after)
assert after == 0, "Post-departure transactions remain in the clean table"
assert len(tx) == len(raw["fact_transactions"]) - before, "Row count after quarantine is wrong"
log("fact_transactions", "transactions after client departure", "quarantine", before,
    f"Recomputed from dim_client.is_active + last snapshot month; matches source flag "
    f"exactly. Mix: {raw['fact_transactions'].loc[source_flag[source_flag].index, 'txn_type'].value_counts().to_dict()}")


# =============================================================================
# STEP 5  Flag AUM floor in snapshots (decision log #2)
# =============================================================================
header("Flag - AUM floor of $1,000 in fact_portfolio_snapshot (decision log #2)")
sn = df["fact_portfolio_snapshot"]
mask = num(sn["aum"]) == AUM_FLOOR
before = "no column"
add_flag("fact_portfolio_snapshot", "is_aum_floor", mask,
         "1 = aum is the $1,000 system floor, not a real balance")
after = int((df["fact_portfolio_snapshot"]["is_aum_floor"] == "1").sum())
show("snapshots flagged is_aum_floor", before, after)
assert after == int(mask.sum()) and after > 0, "is_aum_floor count wrong"
assert (num(sn.loc[sn["is_aum_floor"] == "1", "aum"]) == AUM_FLOOR).all(), \
    "A flagged row is not at the floor"
assert (num(sn.loc[sn["is_aum_floor"] == "0", "aum"]) != AUM_FLOOR).all(), \
    "A floor row was left unflagged"
log("fact_portfolio_snapshot", "AUM placeholder/floor 1000.0", "flag", after,
    f"{sn.loc[mask, 'client_id'].nunique()} clients; value kept (true balance unknowable)")


# =============================================================================
# STEP 6  Flag AUM floor in fees - reconciled against the snapshot table
# =============================================================================
header("Flag - AUM floor in fact_fees, reconciled to the same client-month snapshot")
fe, sn = df["fact_fees"], df["fact_portfolio_snapshot"]
snap_key = sn["client_id"] + "|" + sn["snapshot_date"]
floor_months = set(snap_key[sn["is_aum_floor"] == "1"])
fee_key = fe["client_id"] + "|" + fe["fee_date"]
mask = num(fe["aum_basis"]) == AUM_FLOOR
# Reconciliation: every fee at the floor must sit on a snapshot month at the floor
not_in_snap = int((mask & ~fee_key.isin(floor_months)).sum())
show("fee floor rows without a floor snapshot", "-", not_in_snap)
assert not_in_snap == 0, "Fee AUM floor does not reconcile to snapshot AUM floor"
# Reconciliation: those fees still satisfy amount = basis x bps / 10,000 / 12
recalc = (num(fe["aum_basis"]) * num(fe["bps_charged"]) / 10_000 / 12 - num(fe["amount"])).abs()
assert (recalc[mask] <= MONEY_TOL).all(), "Floor fees do not recompute - basis is not the floor"
add_flag("fact_fees", "is_aum_floor", mask,
         "1 = aum_basis is the $1,000 system floor; fee was charged on the floor")
after = int((df["fact_fees"]["is_aum_floor"] == "1").sum())
show("fees flagged is_aum_floor", "no column", after)
assert after == int(mask.sum()) and after > 0
log("fact_fees", "AUM placeholder/floor 1000.0", "flag", after,
    "Reconciled: every floor fee matches a floor snapshot month and recomputes to the cent; "
    f"{len(floor_months) - after} floor snapshot months have no fee")


# =============================================================================
# STEP 7  Flag dividends booked at full position value (decision log #7)
# =============================================================================
header("Flag - dividend amount equals shares x price (decision log #7)")
tx = df["fact_transactions"]
is_div = tx["txn_type"] == "Dividend"
pos_value = num(tx["shares"]) * num(tx["price"])
mask = is_div & ((pos_value - num(tx["amount"])).abs() <= MONEY_TOL)
add_flag("fact_transactions", "is_dividend_amount_suspect", mask,
         "1 = Dividend amount equals shares x price (100% yield); amount is a position "
         "value, not income")
after = int((df["fact_transactions"]["is_dividend_amount_suspect"] == "1").sum())
show("dividends flagged", "no column", f"{after:,} of {int(is_div.sum()):,}")
assert after == int(mask.sum()), "Flag count does not match rule"
assert not ((df["fact_transactions"]["is_dividend_amount_suspect"] == "1") & ~is_div).any(), \
    "A non-dividend row was flagged"
log("fact_transactions", "dividend = full position value", "flag", after,
    f"Flagged dividends total ${num(tx.loc[mask, 'amount']).sum():,.2f}; amounts not changed")


# =============================================================================
# STEP 8  Flag Buy/Sell on non-trading days (decision log #10) - uses dim_date
# =============================================================================
header("Flag - Buy/Sell dated on a non-trading day (decision log #10)")
tx, dd = df["fact_transactions"], df["dim_date"]
trading = tx["txn_date"].map(dict(zip(dd["date"], dd["is_trading_day"])))
assert trading.notna().all(), "A txn_date is missing from dim_date"
is_trade = tx["txn_type"].isin(["Buy", "Sell"])
mask = is_trade & (trading == "0")
add_flag("fact_transactions", "is_non_trading_day_txn", mask,
         "1 = Buy/Sell dated on a day dim_date marks is_trading_day = 0")
after = int((df["fact_transactions"]["is_non_trading_day_txn"] == "1").sum())
show("trades flagged", "no column", f"{after:,} of {int(is_trade.sum()):,}")
flagged_dates = df["fact_transactions"].loc[mask, "txn_date"]
assert (flagged_dates.map(dict(zip(dd["date"], dd["is_trading_day"]))) == "0").all()
assert after == int(mask.sum())
weekend = int(dt(flagged_dates).dt.dayofweek.ge(5).sum())
log("fact_transactions", "trade on non-trading day", "flag", after,
    f"{weekend:,} on weekends, {after - weekend:,} on weekday holidays; dates not moved")


# =============================================================================
# STEP 9  Verify the existing return flag covers implausible returns (decision log #1)
# =============================================================================
header("Flag (existing) - verify is_return_suspect covers implausible returns (decision log #1)")
sn = df["fact_portfolio_snapshot"]
ret = num(sn["return_ytd"])
implausible = ret.abs() > 1
unflagged = int((implausible & (sn["is_return_suspect"] != "1")).sum())
show("|return_ytd| > 100% not flagged", "-", unflagged)
assert unflagged == 0, "Some implausible returns are not flagged - add a flag before reporting"
over_1000 = int((ret > 10).sum())
log("fact_portfolio_snapshot", "return_ytd > 1,000%", "flag (existing column verified)",
    over_1000, f"{int(implausible.sum()):,} rows with |return| > 100% all carry "
    f"is_return_suspect = 1; values kept")


# =============================================================================
# STEP 10  Leave-as-is items: assert they are still exactly as logged
# =============================================================================
header("Leave as is - confirm untouched values (decision log #3, #4, #6, #8, #9, #11)")
checks = [
    ("dim_client", "risk_profile = 'Unknown'", (df["dim_client"]["risk_profile"] == "Unknown").sum(),
     (raw["dim_client"]["risk_profile"] == "Unknown").sum()),
    ("dim_advisor", "certifications = 'None'", (df["dim_advisor"]["certifications"] == "None").sum(),
     (raw["dim_advisor"]["certifications"] == "None").sum()),
    ("dim_security", "rows sharing a name", df["dim_security"]["name"].duplicated(keep=False).sum(),
     raw["dim_security"]["name"].duplicated(keep=False).sum()),
]
for t, what, a, b in checks:
    show(f"{t}: {what}", int(b), int(a))
    assert a == b, f"{t}: '{what}' changed although the decision was leave as is"
    log(t, what, "leave as is", 0, "Legitimate value/structure - see decision log")
assert df["dim_security"]["ticker"].is_unique, "Tickers must stay unique (duplicate names are separate funds)"
for t, issue in [("fact_transactions", "sells exceeding running position (pre-2020 holdings)"),
                 ("dim_client", "initial_aum vs first snapshot (2 clients)"),
                 ("fact_transactions", "Advisory Fee debits vs fact_fees bills")]:
    log(t, issue, "leave as is", 0, "Documented limitation - see README_clean.md")


# =============================================================================
# STEP 11  Reconciliation checks on the clean tables (no values changed)
# =============================================================================
header("Reconcile - recomputable columns and facts held in two tables")
fe, sn, tx, dd = (df["fact_fees"], df["fact_portfolio_snapshot"], df["fact_transactions"],
                  df["dim_date"])

# R1 fee amount = aum_basis x bps / 10,000 / 12
r1 = int(((num(fe["aum_basis"]) * num(fe["bps_charged"]) / 10_000 / 12
           - num(fe["amount"])).abs() > MONEY_TOL).sum())
show("R1 fee amount != basis x bps / 10,000 / 12", "-", r1)
assert r1 == 0

# R2 alpha = return - benchmark return
r2 = int(((num(sn["return_ytd"]) - num(sn["benchmark_return_ytd"])
           - num(sn["active_alpha_ytd"])).abs() > RATE_TOL).sum())
show("R2 alpha != return - benchmark return", "-", r2)
assert r2 == 0

# R3 fee aum_basis and advisor = same client-month snapshot
m = fe.merge(sn[["client_id", "snapshot_date", "aum", "advisor_id"]],
             left_on=["client_id", "fee_date"], right_on=["client_id", "snapshot_date"],
             how="left", suffixes=("", "_snap"))
r3a = int(m["aum"].isna().sum())
r3b = int(((num(m["aum_basis"]) - num(m["aum"])).abs() > MONEY_TOL).sum())
r3c = int((m["advisor_id"] != m["advisor_id_snap"]).sum())
show("R3 fees without a snapshot month", "-", r3a)
show("R3 fee aum_basis != snapshot aum", "-", r3b)
show("R3 fee advisor != snapshot advisor", "-", r3c)
assert r3a == 0 and r3b == 0 and r3c == 0

# R4 snapshot year/month = dim_date year/month
cal = dd.set_index("date")
r4 = int(((sn["year"] != sn["snapshot_date"].map(cal["year"]))
          | (sn["month"] != sn["snapshot_date"].map(cal["month"]))).sum())
show("R4 snapshot year/month != dim_date", "-", r4)
assert r4 == 0

# R5 Buy/Sell amount = shares x price
trade = tx["txn_type"].isin(["Buy", "Sell"])
r5 = int((trade & ((num(tx["shares"]) * num(tx["price"]) - num(tx["amount"])).abs()
                   > MONEY_TOL)).sum())
show("R5 Buy/Sell amount != shares x price", "-", r5)
assert r5 == 0

# R6 security fields present exactly when the transaction type needs them
r6a = int((tx["txn_type"].isin(TRADE_TYPES) & (tx["security_id"] == "")).sum())
r6b = int((tx["txn_type"].isin(CASH_TYPES) & (tx["security_id"] != "")).sum())
show("R6 trade rows missing security_id", "-", r6a)
show("R6 cash rows carrying a security_id", "-", r6b)
assert r6a == 0 and r6b == 0

# R7 every fact advisor = client's advisor of record
adv = dict(zip(df["dim_client"]["client_id"], df["dim_client"]["advisor_id"]))
for f in ("fact_fees", "fact_portfolio_snapshot", "fact_transactions"):
    r7 = int((df[f]["advisor_id"] != df[f]["client_id"].map(adv)).sum())
    show(f"R7 {f} advisor != client advisor", "-", r7)
    assert r7 == 0
log("all", "reconciliation R1-R7", "verify", 0,
    "Fee formula, alpha formula, fee vs snapshot AUM/advisor, snapshot vs dim_date, "
    "trade amount, txn-type structure, fact vs client advisor - all pass")


# =============================================================================
# STEP 12  Keys and referential integrity on the clean tables
# =============================================================================
header("Verify - primary keys and all 13 relationships on the clean tables")
for t, k in PRIMARY_KEYS.items():
    ok = df[t][k].is_unique and (df[t][k] != "").all()
    show(f"{t}.{k} unique & complete", "-", ok)
    assert ok, f"{t}: primary key broken"
for child, ccol, parent, pcol in RELATIONSHIPS:
    fk = df[child][ccol]
    blank = fk == ""
    if ccol == "security_id":   # blank is legitimate only on cash movements (R6)
        assert not (blank & ~df[child]["txn_type"].isin(CASH_TYPES)).any()
    orphans = int((~blank & ~fk.isin(set(df[parent][pcol]))).sum())
    rate = 100 * (1 - orphans / max(int((~blank).sum()), 1))
    show(f"{child}.{ccol} -> {parent}.{pcol}", "-", f"{rate:.2f}% match")
    assert orphans == 0, f"Orphans in {child}.{ccol}"
log("all", "keys and referential integrity", "verify", 0,
    "8 primary keys unique; 13 relationships at 100% match")


# =============================================================================
# STEP 13  Write outputs, then prove raw = clean + quarantined and raw untouched
# =============================================================================
header("Write clean tables, quarantine files and audit trail; prove row balance")
CLEAN_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

for t in TABLES:
    out = df[t]
    raw_cols = list(raw[t].columns)
    assert list(out.columns[:len(raw_cols)]) == raw_cols, f"{t}: raw columns changed"
    extra = list(out.columns[len(raw_cols):])
    assert all(c.startswith("is_") and set(out[c]) <= {"0", "1"} for c in extra), \
        f"{t}: added columns must be 0/1 flags"
    out.to_csv(CLEAN_DIR / f"{t}_clean.csv", index=False)

for set_name, frames in quarantine.items():
    q = pd.concat(frames)
    q.to_csv(AUDIT_DIR / f"quarantine_{set_name}.csv", index=False)
    print(f"  quarantine_{set_name}.csv  {len(q):,} rows")

print(f"\n  {'table':<26}{'raw':>10}{'clean':>10}{'quarantined':>13}  balanced")
for t in TABLES:
    clean_rows = len(pd.read_csv(CLEAN_DIR / f"{t}_clean.csv", dtype=str, keep_default_na=False))
    ok = len(raw[t]) == clean_rows + quarantine_counts[t]
    print(f"  {t:<26}{len(raw[t]):>10,}{clean_rows:>10,}{quarantine_counts[t]:>13,}  {ok}")
    assert ok, f"{t}: raw != clean + quarantined"
    log(t, "row balance", "verify", quarantine_counts[t],
        f"raw {len(raw[t]):,} = clean {clean_rows:,} + quarantined {quarantine_counts[t]:,}",
        echo=False)

for t in TABLES:
    assert sha256(RAW_DIR / f"{t}.csv") == raw_hash[t], f"Raw file {t}.csv was modified!"
print("  raw files unchanged (sha256 identical before and after)")
log("all", "raw files untouched", "verify", 0, "sha256 of all 8 raw files identical after run")

pd.DataFrame(cleaning_log, columns=["step", "table", "issue", "action", "rows_affected",
                                    "note"]).to_csv(AUDIT_DIR / "cleaning_log.csv", index=False)
pd.DataFrame(value_changes, columns=["table", "key_column", "key", "column", "old_value",
                                     "new_value", "step"]).to_csv(
    AUDIT_DIR / "value_changes.csv", index=False)


# =============================================================================
# README_clean.md  (numbers come from this run, so the README cannot drift)
# =============================================================================
def flag_rows(table, col):
    return next(f["rows_flagged"] for f in added_flags if f["table"] == table and f["column"] == col)


q_post = quarantine_counts["fact_transactions"]
readme = f"""# Wildcat Capital - Clean Data

Produced by `scripts/02_clean.py` from the 8 raw files in the project folder. The raw files are
never modified; the script checks their sha256 fingerprints before and after the run.
Every decision implemented here is documented in `decision_log.md`.

| File | Raw rows | Clean rows | Quarantined |
|---|---:|---:|---:|
""" + "\n".join(
    f"| `{t}_clean.csv` | {len(raw[t]):,} | {len(df[t]):,} | {quarantine_counts[t]:,} |"
    for t in TABLES) + f"""

Audit trail: `data/audit/cleaning_log.csv` (one row per step), `data/audit/value_changes.csv`
(one row per changed cell - empty in this run because no stored value needed correcting) and
`data/audit/quarantine_post_departure_txn.csv` ({q_post:,} rows with reason, source file and
source line).

## Relationships

Star schema with a snowflake branch: 3 fact tables and 5 dimensions. Every relationship is
one-to-many (dimension `1` to fact `*`), single-direction filtering from dimension to fact. All 13
were verified at a 100% match rate on the clean data.

| # | From (many) | To (one) | Notes |
|---|---|---|---|
| 1 | `fact_fees[client_id]` | `dim_client[client_id]` | |
| 2 | `fact_fees[advisor_id]` | `dim_advisor[advisor_id]` | |
| 3 | `fact_fees[fee_date]` | `dim_date[date]` | monthly grain, always the 1st |
| 4 | `fact_portfolio_snapshot[client_id]` | `dim_client[client_id]` | |
| 5 | `fact_portfolio_snapshot[advisor_id]` | `dim_advisor[advisor_id]` | |
| 6 | `fact_portfolio_snapshot[benchmark_id]` | `dim_benchmark[benchmark_id]` | |
| 7 | `fact_portfolio_snapshot[snapshot_date]` | `dim_date[date]` | monthly grain, always the 1st |
| 8 | `fact_transactions[client_id]` | `dim_client[client_id]` | |
| 9 | `fact_transactions[advisor_id]` | `dim_advisor[advisor_id]` | |
| 10 | `fact_transactions[security_id]` | `dim_security[security_id]` | blank on Deposit / Withdrawal / Advisory Fee (legitimate) |
| 11 | `fact_transactions[txn_date]` | `dim_date[date]` | daily grain |
| 12 | `dim_client[advisor_id]` | `dim_advisor[advisor_id]` | snowflake - see note |
| 13 | `dim_security[benchmark_id]` | `dim_benchmark[benchmark_id]` | snowflake - see note |

**Ambiguous paths.** Each fact reaches `dim_advisor` both directly and through `dim_client`, and
`fact_portfolio_snapshot` reaches `dim_benchmark` directly while securities reach it through
`dim_security`. Power BI will make one path inactive. Keep the direct fact-to-dimension
relationships active, and set #12 (and #13 if it conflicts) as inactive; use `USERELATIONSHIP`
when a measure needs it. The data supports this: every fact row's advisor equals the client's
advisor of record (reconciliation R7).

**Join on `dim_date[date]`, not `date_id`.** The facts store calendar dates, not date IDs. Mark
`dim_date` as the date table using its `date` column.

## Column types for the BI tool

Files are plain CSV, so set types explicitly instead of trusting auto-detection.

| Table | Whole Number | Decimal Number / Fixed Decimal | Date | Text | True/False |
|---|---|---|---|---|---|
| `dim_advisor` | advisor_id, years_experience | aum_target_mm (millions) | | first_name, last_name, region, certifications | |
| `dim_benchmark` | benchmark_id | | | name, asset_class, ticker_proxy | |
| `dim_client` | client_id, age, advisor_id | initial_aum (currency) | client_since | first_name, last_name, city, state, **zip_code**, risk_profile, client_tier, **primary_account_type** | is_active |
| `dim_date` | date_id, year, quarter, month, week_of_year | | date | month_name, day_of_week | is_month_end, is_quarter_end, is_year_end, is_trading_day (0/1) |
| `dim_security` | security_id, benchmark_id | expense_ratio_bps | | ticker, name, asset_class, sector | is_etf |
| `fact_fees` | fee_id, client_id, advisor_id | aum_basis, amount (currency); bps_charged | fee_date | fee_type | is_aum_floor (0/1) |
| `fact_portfolio_snapshot` | snapshot_id, client_id, advisor_id, benchmark_id, year, month | aum (currency); return_ytd, benchmark_return_ytd, active_alpha_ytd, cash_pct (percent) | snapshot_date | **account_type** | is_return_suspect, is_aum_floor (0/1) |
| `fact_transactions` | txn_id, client_id, advisor_id, security_id | shares, price, amount (currency) | txn_date | txn_type | is_post_departure_txn, is_dividend_amount_suspect, is_non_trading_day_txn (0/1) |

Watch-outs:
- **`zip_code` must be Text** - numeric typing drops leading zeros (e.g. `03897` becomes `3897`).
- **`account_type` / `primary_account_type` must be Text** - the value `529` (education plan) looks
  numeric and auto-detection will turn the column into numbers and fail on "Roth IRA".
- **Returns and `cash_pct` are fractions** (0.0325 = 3.25%) - format as percent, do not divide by 100.
  Some return and alpha values are written in scientific notation (e.g. `1e-06`); they parse
  correctly as Decimal Number.
- **`certifications = "None"`** means "no certification" and must stay text, not blank.
  Several cells hold two values ("CFP, CFA") - split before counting by certification.
- **`security_id` and `sector`** are blank by design for cash movements and non-equity securities.
- **`expense_ratio_bps`** is blank for non-ETFs (281 rows) by design.

## Flag columns added (0/1)

| Table | Column | Rows = 1 | Meaning / how to use it |
|---|---|---:|---|
| `fact_portfolio_snapshot` | `is_aum_floor` | {flag_rows('fact_portfolio_snapshot', 'is_aum_floor'):,} | AUM is the $1,000 system floor, not a real balance. Exclude from AUM trends and return calculations. |
| `fact_fees` | `is_aum_floor` | {flag_rows('fact_fees', 'is_aum_floor'):,} | Fee was charged on the $1,000 floor. Reconciled to the floor snapshot month for the same client. |
| `fact_transactions` | `is_dividend_amount_suspect` | {flag_rows('fact_transactions', 'is_dividend_amount_suspect'):,} | Dividend amount equals shares x price (a 100% yield). Do **not** sum as income. |
| `fact_transactions` | `is_non_trading_day_txn` | {flag_rows('fact_transactions', 'is_non_trading_day_txn'):,} | Buy/Sell dated on a weekend or market holiday. Treat the date as a booking date. |

Existing source flags kept and verified:
- `fact_portfolio_snapshot.is_return_suspect` - covers every return beyond +/-100%, including
  {over_1000} rows above 1,000%. Filter it out of performance measures.
- `fact_transactions.is_post_departure_txn` - all flagged rows were quarantined, so the column is
  0 for every clean row; it is kept so the schema matches the raw file.

## Known limitations (chosen not to fix)

| Limitation | Reason it was not fixed |
|---|---|
| `return_ytd` values up to ~9,467% remain in the table | Correct returns need cash-flow data we don't have; the rows are flagged instead. |
| AUM of $1,000 for 92 clients is not the real balance | True balance cannot be recovered; replacing it would invent data. Flagged. |
| Dividend amounts are position values, not income | No yield data to recompute the real dividend. Flagged; deleting would remove every dividend. |
| 28% of trades are dated on non-trading days | No evidence of the real execution date; moving dates would be a guess. Flagged. |
| 75 clients have `risk_profile = "Unknown"` | A real category already used for benchmark assignment; inferring a profile would invent a suitability decision. |
| Sells exceed the running position for many client-securities | Holdings bought before the 2020 start of the extract are missing. Use snapshots, not transactions, for holdings. |
| Advisory Fee debits in transactions never equal fees billed in `fact_fees` | Different processes (cash debit vs monthly bill). `fact_fees` is the source of truth for fee revenue; never add both. |
| 2 clients' `initial_aum` differs >50% from first-month AUM | Too few to justify a rule and no third source to decide which is right. |
| 3 pairs of securities share a name | Different tickers, IDs, benchmarks and fees - separate funds. Slice by ticker, not name. |
| Row counts differ from the assignment's data dictionary (clients 2,500 vs 2,525; transactions 298,572 vs 298,772) | Rows cannot be recreated; all keys resolve. Totals reflect the file received. |
"""
(CLEAN_DIR / "README_clean.md").write_text(readme, encoding="utf-8")

print(f"\nDone. Clean tables -> {CLEAN_DIR}")
print(f"      Audit trail  -> {AUDIT_DIR}")
print(f"      {len(cleaning_log)} log entries, {len(value_changes)} value changes, "
      f"{sum(quarantine_counts.values()):,} rows quarantined, "
      f"{len(added_flags)} flag columns added")
