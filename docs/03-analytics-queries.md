# Analytics — Queries over the Gold Layer

Eight analytical queries, each persisted as a view in Databricks.
Power BI reads these views (Import mode), not the raw fact/dim tables —
this keeps the BI model thin and decouples dashboards from schema changes.
Full SQL lives in [`analytics/queries.sql`](../analytics/queries.sql);
run it after the medallion pipeline (01 → 02 → 03).

## Query catalogue

| # | View | Business question |
|---|---|---|
| Q1 | `v_operator_costs` | Where does energy cost the most — do operators differ on price? |
| Q2 | `v_station_utilization` | How intensively is each station actually used? |
| Q3 | `v_peak_hours` | When is the system under the most load? |
| Q4 | `v_anomaly_review` | Which records did Silver flag — the review queue behind the numbers? |
| Q5 | drill-down on Q2 | Where to invest and where to cut? |
| Q6 | `v_session_mix` | Which segment dominates demand and cost? |
| Q7 | `v_monthly_trend` | Is demand growing, and does it seasonally fluctuate? |
| Q8 | `v_tariff_split` | How much energy sits in the expensive "day" window? (what-if basis) |

## Headline results

### Portfolio totals (Q1, Q6)

| Metric | Value |
|---|---|
| Clean sessions | 78 400 |
| Total energy delivered | ~4.16 GWh |
| Total cost | ~2.25M PLN |
| Weighted price | 0.54 PLN/kWh |

Session-type mix ≈ 45/30/25% (home/public/work) — matching the generator priors.
The cost concentration is the key finding:

| Session type | Sessions | kWh | Cost (PLN) | Cost/kWh |
|---|---|---|---|---|
| home | 35 230 | 983 447 | 383 855 | 0.390 |
| public | 23 627 | 2 048 434 | 1 488 232 | 0.727 |
| work | 19 543 | 1 131 056 | 377 851 | 0.334 |

> **The public segment generates ~66% of total cost from ~30% of sessions.**
> Any tariff scenario is fundamentally a public-segment story.

### Intra-day profile (Q3)

Clearly bimodal: morning peak at 08:00–10:00 (~359k kWh at 09:00 hour bucket)
and afternoon peak at 14:00–16:00 (~392k kWh at 15:00), night valley at 03:00
(~25k kWh). Average session duration flips the narrative between segments:
night sessions average ~330 min (slow home charging), daytime sessions
140–210 min (fast public/work). The night valley is exactly where surplus
grid capacity lives — the target of load shifting.

### Utilization — two metrics, two stories (Q2)

Fast-charger stations (DC ultra-fast) top the *energy* utilization ranking
(util_pct ~69–73%) while sitting near the bottom of *time* occupancy
(0.3–1.8% of calendar hours). Interpretation: their sessions are short and
energy-dense; the hardware is efficient but idle most of the day — headroom
exists. Note that both metrics are computed against the maximum observed
session power (fact-derived); a production model would use nominal per-port
ratings from the asset registry.

### Top/bottom asymmetry (Q5)

Top-10 by delivered volume are exclusively DC ultra-fast hubs (400–500+
sessions each, ~66–88 MWh). Bottom-10 are small DC fast stations near the
activity floor (≥100 sessions filter). Notably, bottom stations show
*similar energy utilization* (~68–70%) to the top ones — the difference is
pure volume, not efficiency. High utilization ≠ high volume.

### Monthly trend (Q7)

Winter peaks, summer dips, strong growth:

| Period | Energy |
|---|---|
| Jul 2024 (minimum) | 118 620 kWh |
| Dec 2024 | 200 313 kWh |
| Jul 2025 | 176 540 kWh |
| Dec 2025 (maximum) | 274 720 kWh |

Year-over-year volume growth: **+36% (2025 vs 2024)** — the EV-adoption curve
is clearly visible in the analytics, consistent with the generator's model.

### Tariff potential (Q8)

The day/night split is remarkably stable: 52.6–56.9% of energy charged in
the day window (13:00–21:00) across all 24 months, averaging ~55%.

| Year | Day kWh | Night kWh | Day share |
|---|---|---|---|
| 2024 | 924 738 | 773 395 | 54.5% |
| 2025 | 1 306 524 | 1 205 273 | 54.8% |

This stability is what makes the what-if scenario trustworthy: shifting
*X percentage points of volume from day to night is a structural estimate,
not a bet on volatile behaviour. The Power BI scenario page quantifies it:
shift share × night discount × affected volume = monthly PLN savings.

### Data quality in the BI layer (Q4)

1 600 flagged records enter the review queue, reconciled 1:1 with the
generator's anomaly budget (details in
[02-medallion-architecture.md](02-medallion-architecture.md)):
800 aborted sessions, 300 negative values, 175 physical-limit violations,
174 NULLs, 151 reversed timestamps. Zero silent leaks reached Gold.

## Design notes

- **Views, not tables**: analytic results are non-materialized views over Gold —
  cheap to recompute, no staleness risk, and Power BI Import pulls them like tables.
- **Q4 reads from Silver**, not Gold — by design: the anomaly queue *is* the
  non-clean population; Gold holds only analysis-ready truth.
- **Q5 is a drill-down**, not a persisted view — ranking top/bottom from Q2
  avoids duplicating the utilization logic.
- The utilization activity floor (≥100 sessions) is deliberate: utilization
  percentages on tiny samples mislead more than inform.