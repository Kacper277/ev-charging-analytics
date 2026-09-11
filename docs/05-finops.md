# FinOps — Cost Model & Optimization

Estimating what this pipeline costs on the free tier vs what it would cost
in production (Azure Databricks + ADLS Gen2), and where the money actually goes.
Rates below are indicative list prices (Premium tier, verified against public
sources Sep 2026); the *method* is the point — assumptions → formula →
per-component split → optimizations. Verify in the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/details/databricks/)
for your region before quoting real numbers.

## Workload assumptions

| Parameter | Assumption | Basis |
|---|---|---|
| Data volume (all layers) | ~0.3–0.5 GB Delta (raw + silver + gold + snapshots) | 80.8k sessions × 11 cols + stations; CSV source ~18 MB |
| Daily refresh | 1×/day, 3 notebooks (Bronze→Silver→Gold), ~15 min on a 2-worker small cluster | Current Run All takes minutes at this scale |
| Interactive development | ~2 h/day, 20 working days | Notebook exploration, debugging, analytics |
| BI serving | Scheduled refresh 1×/day, Import mode, small SQL warehouse | Dashboard reads pre-aggregated views |
| Retention | Bronze/silver history 90 days, gold full | Trade-off: audit trail vs storage/snapshot bloat |

## Cost model: free tier vs production

| Component | Community Edition (today) | Production (monthly, indicative) |
|---|---|---|
| Compute — pipeline | $0 (free cluster) | Jobs Compute, ~5–8 DBU/day → **$2–4** |
| Compute — interactive dev | $0 | All-purpose, ~40 h/mo × 2 DBU/h × $0.55/DBU → **~$45** |
| Compute — BI SQL warehouse | $0 (CSV hand-off) | Serverless SQL, light usage → **$10–20** |
| Storage (ADLS Gen2) | $0 (Volume included) | <1 GB hot → **<$0.05** |
| Power BI | Desktop only, $0 | Pro per user → **$10–14** |
| **Total** | **$0** | **~$70–80** |

Breakdown insight: **compute (pipeline + interactive + SQL) accounts for
~85–90% of the production bill** — storage at this scale is a rounding error.
Within compute, the *interactive* part alone is ~3–4× the pipeline cost:
this is the typical FinOps finding, and the biggest lever (see below).

Two structural notes from the 2026 pricing landscape: (1) All-Purpose Compute
costs ~2–3× more per DBU than Jobs Compute — scheduling pipelines on
interactive clusters is the single most common overspend; (2) the Standard
tier is being retired on Azure (Oct 2026) — workspaces still on it face
automatic Premium-tier migration, i.e. a budget line that rises without any
workload change.

## Optimization recommendations

1. **Right-size the compute type + enforce auto-termination.**
   All scheduled work on Jobs Compute (~3× cheaper per DBU than All-Purpose);
   interactive clusters with a 30-min `autoTerminate` so forgotten clusters
   don't bill overnight. Industry analyses attribute ~40% of avoidable Databricks
   spend to workloads running on the wrong compute type or left-idling clusters.

2. **Serve BI from aggregates, not raw facts.**
   Power BI reads narrow pre-computed views (`v_monthly_trend`, `v_tariff_split`,
   …) — scans shrink from 78k rows to dozens, enabling the smallest SQL
   warehouse SKU and scheduled (not continuous) refresh. At production scale
   this is the difference between a Small and an XX-Large warehouse.

3. **Partition by date + housekeeping.**
   Delta tables partitioned on `date_key`, `OPTIMIZE`/`VACUUM` on schedule —
   less data scanned per run means fewer DBUs *and* smaller storage. Drop
   Bronze duplicates after the 90-day audit window.

4. **Make spend visible from day one.**
   Tag every cluster/job/warehouse (`team`, `project`, `env`) — tags flow into
   Azure Cost Management; monitor DBU consumption via `system.billing.usage`
   with budgets and alerts. Untagged consumption is unbudgetable.

5. **Committed pricing when steady-state.**
   DBCU pre-purchase (1y/3y) cuts DBU rates by up to 33–37% once the daily
   pattern is stable — but only after the workload stabilizes; committing to
   an unstable workload wastes the discount.