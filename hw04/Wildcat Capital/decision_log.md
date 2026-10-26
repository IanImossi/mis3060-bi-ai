## 1. Implausible year-to-date returns
| Field | What to write |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.return_ytd` |
| **Evidence** | ERROR — 261 of 129,547 snapshots have `return_ytd` > 10 (over 1,000%); max = 94.668 (≈ 9,467%). All 261 already carry `is_return_suspect = 1`, and **every** row with \|return\| > 100% is flagged (0 unflagged). 3,875 rows are flagged suspect in total. |
| **Dimension violated** | Accuracy/Plausibility |
| **Decision** | **flag** (keep the existing `is_return_suspect` flag; do not change the values) |
| **Justification** | The source system has already identified these rows, and our recomputation agrees with its flag 100%. A correct return cannot be recalculated without cash-flow and valuation data we do not have, so replacing the number would be inventing it. Keeping the rows preserves AUM and alpha history for the client; the committee should exclude flagged rows from performance averages and advisor rankings. |
| **Risk** | If a report forgets the filter, a single 9,467% row will dominate any average return or alpha ranking. If some of these returns are genuine (e.g. a tiny account that grew from a $1,000 floor), excluding them understates performance for those clients. |

## 2. AUM placeholder / floor value of 1,000.0

| Field | What to write |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.aum`; `fact_fees.aum_basis` |
| **Evidence** | WARN — value `1000.0` repeats 420× in `aum` and 386× in `aum_basis`, in columns where no other value repeats more than 2×. Affects 92 clients (~4.6 months each). It follows a crash of about −97% (e.g. client 29: $16,704.59 → $1,000.00, `return_ytd` −0.977). Fee `aum_basis` equals the snapshot AUM for every client-month, and fees on a $1,000 basis ($0.65–$0.80) recompute correctly. |
| **Dimension violated** | Accuracy/Plausibility |
| **Decision** | **flag** — add `is_aum_floor` (0/1) to both tables |
| **Justification** | The value is consistent across two tables and with the fee formula, so it is a system floor (minimum balance) rather than a typing error. The true balance cannot be recovered, and any replacement would be invented. A flag lets the committee see and exclude these months without losing the fee and snapshot records. |
| **Risk** | Firm-wide AUM is understated for these client-months, and returns calculated from a $1,000 base are meaningless. If the floor is actually a real balance, flagging it may make analysts exclude legitimate small accounts. |

## 3. "Unknown" risk profile

| Field | What to write |
|---|---|
| **Table / column** | `dim_client.risk_profile` |
| **Evidence** | WARN — 75 of 2,500 clients (3.0%) have `risk_profile = "Unknown"` (48 Retail, 18 HNW, 9 UHNW). Every one of them maps consistently to benchmark 3 (60/40 Blend) in `fact_portfolio_snapshot`. |
| **Dimension violated** | Completeness, Validity |
| **Decision** | **leave as is** |
| **Justification** | "Unknown" is an explicit category the business already uses, since it consistently drives a benchmark assignment. Inferring a risk profile from age, tier or holdings would invent a suitability decision that only an advisor can make. |
| **Risk** | If "Unknown" really means a missing suitability assessment, 75 clients (including 9 UHNW) may be invested without a documented risk profile. That is a compliance issue to escalate, not something the data can fix. |

## 4. "None" as a certification value

| Field | What to write |
|---|---|
| **Table / column** | `dim_advisor.certifications` |
| **Evidence** | WARN — 6 of 25 advisors have `certifications = "None"`. The profiler flags "None" as a null-like token. 10 other advisors hold multi-valued cells such as "CFP, CFA". |
| **Dimension violated** | Completeness (apparent) |
| **Decision** | **leave as is** |
| **Justification** | "None" here means "holds no professional certification", which is a real answer, not missing data. Converting it to blank would wrongly say we don't know. The text will be read correctly as a category in Power BI as long as the cleaning step reads files with `keep_default_na=False`. |
| **Risk** | A future step that uses default `pd.read_csv` will silently turn "None" into NaN, and those 6 advisors will show as unknown rather than uncertified. Multi-valued cells must be split before counting advisors by certification. |

## 5. Row counts differ from the data dictionary

| Field | What to write |
|---|---|
| **Table / column** | `dim_client` (table); `fact_transactions` (table) |
| **Evidence** | WARN — `dim_client` has 2,500 rows vs 2,525 documented (−25). `fact_transactions` has 298,572 rows vs 298,772 documented (−200). All foreign keys still resolve (100% match rate on all 13 relationships). |
| **Dimension violated** | Completeness |
| **Decision** | **leave as is** |
| **Justification** | Missing rows cannot be recreated without inventing them, and nothing in the extract is broken: no fact row points to a missing client. The gap is documented so the committee knows the totals are based on the file actually received. |
| **Risk** | If the official extract contains 25 more clients and 200 more transactions, client counts, AUM and fee totals are understated. If those extra rows were duplicates that someone already removed, the two duplicate-detection checks will not find them in this version. |

## 6. Duplicate security names

| Field | What to write |
|---|---|
| **Table / column** | `dim_security.name` |
| **Evidence** | WARN — 6 rows (3 names) share a name: "Davis PLC Fund" (RONV / YMS), "Graham Group Inc." (PPJV / UFA), "Jones Inc Fund" (OLAB / YXPS). Each pair has a different `security_id`, ticker, benchmark, and expense ratio. `security_id` and `ticker` are both 100% unique. |
| **Dimension violated** | Uniqueness (apparent) |
| **Decision** | **leave as is** |
| **Justification** | These are different products from the same issuer (e.g. two funds from one sponsor), not duplicate records. Ticker is the real business identifier. Merging them would lose transactions belonging to one of the two securities. |
| **Risk** | Any visual that groups or slices by `name` instead of `ticker`/`security_id` will merge two different securities and misstate holdings. If they are true duplicates, transactions are split across two IDs. |

## 7. Dividend amount equals the full position value

| Field | What to write |
|---|---|
| **Table / column** | `fact_transactions.amount` (where `txn_type = "Dividend"`) |
| **Evidence** | WARN — all 53,826 dividends satisfy `amount = shares × price` (±$0.01), which implies a 100% yield. Dividends total ≈ $3.45B (mean $64,082 per payment). |
| **Dimension violated** | Accuracy/Plausibility |
| **Decision** | **flag** — add `is_dividend_amount_suspect` (0/1) |
| **Justification** | The true dividend cannot be recomputed without a yield per security, so changing the amount would be invention. Quarantining would delete every dividend in the data. The flag keeps the event (which security paid, when, to whom) while warning that the dollar amount is a position value, not income. |
| **Risk** | Income reports that sum this field overstate investment income by roughly $3.4B. If the amount is actually correct under some other convention (e.g. a reinvested position value), flagging it could hide real income. |

## 8. Sells that exceed the running position

| Field | What to write |
|---|---|
| **Table / column** | `fact_transactions.shares` (Buy / Sell by `client_id` + `security_id`) |
| **Evidence** | WARN — 42,975 of 59,720 sells (72%) take the running share position below zero, across 22,678 client-security pairs. Transactions begin 2020-01-01, but `client_since` goes back to 2015-01-01. |
| **Dimension violated** | Consistency, Completeness |
| **Decision** | **leave as is** (documented limitation) |
| **Justification** | The extract starts in 2020, so positions bought before then are missing from the data rather than wrong. Clients don't short-sell in wealth accounts, so the negative positions reflect the extract window. Removing sells would understate realized activity. |
| **Risk** | Holdings cannot be rebuilt from transactions alone; anyone who tries will get negative positions. Holdings and AUM must come from `fact_portfolio_snapshot`. If some sells really are errors, they stay in the data undetected. |

## 9. Initial AUM disagrees with the first snapshot

| Field | What to write |
|---|---|
| **Table / column** | `dim_client.initial_aum` vs `fact_portfolio_snapshot.aum` |
| **Evidence** | WARN — 2 of 968 clients onboarded inside the data window (client_id 1171 and 1571) have first-month AUM more than 50% different from `initial_aum`. |
| **Dimension violated** | Consistency |
| **Decision** | **leave as is** |
| **Justification** | Only 2 clients are affected, and there is no third source to decide which value is right. Early deposits, withdrawals or market moves in the first month can legitimately explain the gap. |
| **Risk** | If `initial_aum` is wrong, tier assignment for these 2 clients (Retail / HNW / UHNW is based on initial AUM) may be wrong, along with any growth-since-onboarding metric. |

## 10. Trades on non-trading days

| Field | What to write |
|---|---|
| **Table / column** | `fact_transactions.txn_date` vs `dim_date.is_trading_day` |
| **Evidence** | WARN — 40,630 of 143,218 Buy/Sell trades (28.4%) are dated on days `dim_date` marks as non-trading (weekends/holidays). Transactions occur on 1,826 distinct dates, essentially every calendar day. |
| **Dimension violated** | Consistency, Accuracy/Plausibility |
| **Decision** | **flag** — add `is_non_trading_day_txn` (0/1) |
| **Justification** | Exchanges are closed on these days, so the date is probably a booking or settlement date. Moving trades to the next trading day would mean choosing a date the data doesn't support. A flag keeps the trade and lets analysts treat these dates with caution. |
| **Risk** | Daily trading-volume and price-at-date analyses will be wrong for 28% of trades. If the dates really are booking dates, flagging them creates noise around valid records. |

## 11. Advisory fee debits do not match fees billed

| Field | What to write |
|---|---|
| **Table / column** | `fact_transactions.amount` (`txn_type = "Advisory Fee"`) vs `fact_fees.amount` |
| **Evidence** | WARN — 0 of 28,746 client-months where both exist match (±$0.01). There are 1,909 months with a fee debit but no bill. Advisory Fee transactions: 35,742 rows, mean $7,378. Fee bills: 122,197 rows. |
| **Dimension violated** | Consistency |
| **Decision** | **leave as is** — designate `fact_fees` as the source of truth for fee revenue |
| **Justification** | The two tables describe different processes: `fact_fees` is the monthly bill, while the transaction is a cash debit on its own schedule and amount. Forcing them to match would mean rewriting one table to fit the other. A documented rule ("fee revenue = `fact_fees`") gives one consistent number. |
| **Risk** | A report that adds both together double-counts fees. If the transaction debits are actually correct and the bills are wrong, reported fee revenue is wrong. That is a billing-process question to escalate. |

## 12. Transactions after a client departed

| Field | What to write |
|---|---|
| **Table / column** | `fact_transactions` (rows) vs `dim_client.is_active` / last snapshot date |
| **Evidence** | WARN — 2,410 transactions belong to inactive clients and are dated after their last snapshot month. The source flag `is_post_departure_txn = 1` matches this recalculation exactly (0 disagreements). Mix: 689 Buy, 482 Sell, 439 Dividend, 284 Advisory Fee, 278 Deposit, 238 Withdrawal. |
| **Dimension violated** | Consistency, Accuracy/Plausibility |
| **Decision** | **quarantine** to `data/audit/quarantine_post_departure_txn.csv` (all columns kept) |
| **Justification** | These are not just closing withdrawals. They include new buys and dividends on accounts that are already closed, which cannot be real activity, and the source system already marks them. Quarantine removes them from reporting while keeping every row traceable, so raw rows = clean rows + quarantined rows. |
| **Risk** | If a client's departure date is wrong (e.g. snapshots stopped early but the account stayed open), real activity, including final liquidation trades, is excluded from reporting. The quarantine file keeps it recoverable. |

---

### Checked and found correct (no action)

| Check | Result |
|---|---|
| Referential integrity | All 13 relationships (10 ID-based, 3 to `dim_date`) resolve with a 100% match rate. The 101,528 blank `security_id` values are all on cash transactions (Deposit / Withdrawal / Advisory Fee), which is legitimate structure. |
| Security benchmark fit | Every security is benchmarked to an index of a compatible asset class. Equity securities benchmarked to MSCI EAFE or Russell 2000 are legitimate. |
| Fee recalculation | `amount = aum_basis × bps_charged / 10,000 / 12` holds for all 122,197 fee rows. |
| Alpha recalculation | `active_alpha_ytd = return_ytd − benchmark_return_ytd` holds for all 129,547 snapshots. |
| Keys | Every primary key is unique and complete. There are no exact duplicates, double loads or near-duplicate rows in any table. |
