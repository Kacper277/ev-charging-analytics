# Medallion Architecture — Bronze / Silver / Gold

The Databricks stage transforms two raw CSVs from a Unity Catalog Volume
into a star schema (1 fact + 2 dimensions), enforcing data quality on the way.
Everything runs on Databricks Community Edition; notebooks live in [`databricks/`](../databricks/).

## Architecture at a glance

Unity Catalog Volume Bronze Silver Gold ───────────────────── (raw) (typed+clean) (star schema) ev_data/sessions.csv ──► bronze_sessions ──┐ ├─► silver_clean ──┬─► fact_sessions ev_data/stations.csv ──► bronze_stations ──┼─► │ │ (rules + dedup) └─► dim_station └────────────────────► dim_time

Row counts (reconciled end-to-end):

| Stage | Rows | Note |
|---|---|---|
| `bronze_sessions` | 80 800 | includes ~800 injected duplicates |
| `silver_clean` | 80 000 | deduplicated by business key `session_id` |
| `fact_sessions` | 78 400 | anomalies excluded, moved to review views |

## Bronze — raw, AS-IS

**Rule: zero transformation.** CSVs are loaded with `inferSchema=False`
(everything as STRING) and only get an `ingested_at` audit timestamp.

Why defer type parsing to Silver? A lossless audit trail: if a cast ever fails
downstream, the raw strings are untouched for debugging. Type enforcement is
a *quality decision* made explicitly in Silver — not an accident of CSV inference.

```python
bronze = (
    spark.read.csv(RAW_PATH, header=True, inferSchema=False)
    .withColumn("ingested_at", F.current_timestamp())
)
bronze.write.mode("overwrite").saveAsTable("workspace.default.bronze_sessions")
Silver — type enforcement, dedup, anomaly rules

Three quality gates, in order:

1. Explicit casting — every column gets its intended type (TIMESTAMP, DOUBLE, …) instead of trusting CSV inference. Early failure mode: string '22.2' silently accepted by inference would blow up later as a failed BIGINT cast — the pipeline fails loudly instead.

2. Deduplication by business key — ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY ingested_at DESC) keeps the newest version of each session. (Duplicating by all columns would only catch byte-identical copies and miss conflicting versions of the same event.)

3. Anomaly rules — a single ordered CASE mapping each defect class to a reason (first matching rule wins, so reasons are mutually exclusive):
anomaly_reason	Rule	Target rows
null_critical_field	NULL in energy_kwh / duration_minutes / price_per_kwh	174
reversed_timestamp	ended_at < started_at	151
negative_value	negative energy or duration	300
zero_session_aborted	energy_kwh = 0 OR duration_minutes = 0	800
exceeds_physical_limit	energy_kwh > power_kw × hours × 0.95	175
ok	—	78 400

Every clean record carries anomaly_reason = 'ok' and is_anomaly = false; all records stay in Silver (clean + flagged) for full lineage and debugging.
Anomaly budget reconciliation

The generator injects ~3% anomalies in three categories (~1% each). Reconciliation across layers confirms zero silent leaks:

    Duplicates (~1%) — removed in dedup: 80 800 → 80 000
    Zero sessions (~1%) — flagged by the OR-rule (an early AND-based rule let single-zero sessions leak into ok; caught by cross-layer reconciliation, fixed)
    Corruption (~1%) — split across negative_value (300), exceeds_physical_limit (175), null_critical_field (174), reversed_timestamp (151)

Result: 78 400 clean rows = 80 800 − 800 duplicates − 1 600 flagged. Nothing reached Gold unaccounted for.
Gold — star schema

One fact table, two dimensions (classic relational design, named explicitly):

fact_sessions (78 400) — grain: one clean charging session. FKs: station_id → dim_station, date_key → dim_time. Measures: energy_kwh, price_per_kwh, cost_pln, duration_minutes. Session-scoped attributes kept in the fact: hour_of_day, tariff_period (day = 13:00–21:00, else night — the basis for tariff what-if scenarios in Power BI).

dim_station (200) — operator, power, charger_class derived from power tier (AC home ≤11 kW / AC destination ≤22 kW / DC fast ≤50 kW / DC ultra-fast). Built from Bronze (stations need no cleaning).

dim_time (731) — calendar grain: one row per date (2024-01-01 … 2025-12-31), with year/quarter/month/day-of-week/is_weekend.

    Cardinality bug worth mentioning: the first dim_time build included tariff_period (an hour-derived attribute), producing 2 rows per date — 1 462 instead of 731. That violates the uniqueness of date_key and would double-count every measure in Power BI on the fact↔dim join. Fixed by keeping the date dimension at day grain and moving the tariff attribute into the fact table.

Design decisions
Decision	Choice	Why
Cast strategy	explicit CAST, fail loud	TRY_CAST would mask bad data; a broken ingest should stop the pipeline
Dedup key	session_id	business key, not full-row hash
Anomalies in Gold?	no	Gold = analysis-ready truth; anomalies stay in Silver review views
Tables vs temp views in Silver	tables	intermediate checkpoints are queryable for debugging
Unity Catalog naming	single schema workspace.default with bronze_/silver_/dim_/fact_ prefixes	Community Edition exposes one catalog; prefixes carry the layer semantics
Reproducing

Attach the Community Edition cluster, ensure the CSVs are in /Volumes/workspace/default/ev_data/, then run 01_bronze → 02_silver → 03_gold (Run All in each). Verification queries at the end of each notebook assert the row counts from the table above.