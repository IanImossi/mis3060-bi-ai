# Wildcat Capital - Clean Data

Produced by `scripts/02_clean.py` from the 8 raw files in the project folder. The raw files are
never modified; the script checks their sha256 fingerprints before and after the run.
Every decision implemented here is documented in `decision_log.md`.

| File | Raw rows | Clean rows | Quarantined |
|---|---:|---:|---:|
| `dim_advisor_clean.csv` | 25 | 25 | 0 |
| `dim_benchmark_clean.csv` | 8 | 8 | 0 |
| `dim_client_clean.csv` | 2,500 | 2,500 | 0 |
| `dim_date_clean.csv` | 1,827 | 1,827 | 0 |
| `dim_security_clean.csv` | 500 | 500 | 0 |
| `fact_fees_clean.csv` | 122,197 | 122,197 | 0 |
| `fact_portfolio_snapshot_clean.csv` | 129,547 | 129,547 | 0 |
| `fact_transactions_clean.csv` | 298,572 | 296,162 | 2,410 |

Audit trail: `data/audit/cleaning_log.csv` (one row per step), `data/audit/value_changes.csv`
(one row per changed cell - empty in this run because no stored value needed correcting) and
`data/audit/quarantine_post_departure_txn.csv` (2,410 rows with reason, source file and
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
| `fact_portfolio_snapshot` | `is_aum_floor` | 420 | AUM is the $1,000 system floor, not a real balance. Exclude from AUM trends and return calculations. |
| `fact_fees` | `is_aum_floor` | 386 | Fee was charged on the $1,000 floor. Reconciled to the floor snapshot month for the same client. |
| `fact_transactions` | `is_dividend_amount_suspect` | 53,387 | Dividend amount equals shares x price (a 100% yield). Do **not** sum as income. |
| `fact_transactions` | `is_non_trading_day_txn` | 40,291 | Buy/Sell dated on a weekend or market holiday. Treat the date as a booking date. |

Existing source flags kept and verified:
- `fact_portfolio_snapshot.is_return_suspect` - covers every return beyond +/-100%, including
  261 rows above 1,000%. Filter it out of performance measures.
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
