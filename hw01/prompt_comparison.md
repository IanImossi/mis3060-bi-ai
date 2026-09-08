# Question 1: "What should a BI dashboard for a loan portfolio committee include, and why?"

A loan portfolio dashboard generally needs to cover five areas. Portfolio composition and concentration — breakdown by loan purpose, geography, vintage, and size, both by count and by dollar exposure, because count share and dollar share can diverge sharply and concentration in dollars is what actually drives loss severity. Credit quality distribution — credit score bands, DTI bands, and how the mix is trending over time, since a portfolio can look stable on aggregate default rate while quietly drifting toward riskier originations. Delinquency and default rates — current snapshot plus trend, ideally broken out by segment rather than as one blended number, since a blended rate can hide a deteriorating segment offset by a strong one. Risk-adjusted yield — average interest rate against loss rate by segment, to show which parts of the book are actually

# Takeaway from Strategy 1
- This strategy is good for just gathering data and not for how to present it to others.

# Question 2: You are a senior credit risk analyst presenting to a portfolio committee of three senior executives at Wildcat Capital. What should a BI dashboard for a loan portfolio committee include, and why?"

If I were building this for your committee, I'd structure it so the first screen answers "is the book okay" in under thirty seconds, and everything else is drill-down for whoever wants to dig in. At the top: total portfolio dollars outstanding, blended default and delinquency rate, and dollar-weighted exposure by loan purpose — because in our book, Business is 52% of dollars on just 14% of loan count, so a headline default-rate number that treats all five purposes equally would understate how much a handful of large Business impairments could move losses. Right below that, I'd put concentration explicitly, not buried in an appendix: top-10 and top-20 borrower exposure, and purpose-level dollar share against purpose-level default rate side by side, so the committee can see in one glance that Home Improvement has our highest default rate (6.5%) but only 20% of dollars, while Business has a lower default rate (3.4%)

# Takeaway from Strategy 2
- This strategy is great if the prompter does not know exactly how they want their infomation presented, but knows what they want in terms of information.

# Question 3: Provide a table with three columns (Metric, What It Shows, Why the Committee Cares), grouped under tier headers by how urgently the committee should act on it. Example, for a completely different domain (hospital operations):

TierMetricWhat It ShowsWhy It MattersTier 1 — Act nowDefault rate by loan purpose (dollar-weighted)Where losses are concentrated right nowA blended portfolio default rate can mask a single segment deteriorating; dollar-weighting shows what's actually at stakeTier 1 — Act nowTop-10/top-20 borrower exposureSingle-name concentration riskA handful of large loans defaulting can move losses more than a much larger number of small onesTier 1 — Act nowCurrent-to-Delinquent transition rate (this period)Whether the pipeline of future problem loans is growingThis is a leading indicator; the static delinquency count only tells you where things stand, not where they're headingTier 2 — WatchCredit score / DTI distribution of new originations, by quarterUnderwriting standard drift over timeStandards can loosen gradually without breaching any single threshold, and it won't show up in defaults for monthsTier 2 — WatchRisk-adjusted yield by segment (avg rate minus loss rate)Which segments are actually profitable after credit costA high headline rate on a high-loss segment isn't real return;

- The above information was provided in a table format

# Takeway from Strategy 3
I believed that strategy 3 was good if there is a preconcieved answer and format that was needed. The only caveat is that, because it is specific in what is being asked, claude cannot make as many improvements that could be unknown to the prompter.

## Statement
- Delinquency means a borrower has missed one or more scheduled payments, but the loan is still considered an active, potentially curable obligation
## Fact Check
- Loan delinquency is used to describe a situation in which a borrower misses their due date for a single scheduled payment for a form of financing, like student loans, mortgages, credit card balances, or automobile loans, in addition to unsecured personal loans. There are consequences for delinquency, depending on the type of loan, the duration, and the cause of the delinquency.
    - Investopedia: https://www.investopedia.com/ask/answers/062315/what-are-differences-between-delinquency-and-default.asp
- Claude correctly defined what a loan delinquency is, and was more consise in defining what it was.