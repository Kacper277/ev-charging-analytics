# EV Charging Analytics — Stage 0 (Data)

Production-quality data pipeline producing two artefacts for the downstream
Databricks / Power BI stages:

1. **Job A — `fetch-stations`**: fetch and normalize charging stations from the
   free [Open Charge Map](https://www.openchargemap.org/) API into
   `data/stations.csv`.
2. **Job B — `generate-sessions`**: generate a realistic, reproducible synthetic
   charging-sessions dataset (`data/sessions.csv`, default 80,000 rows; 100k+
   handled comfortably via chunked writes).

Python 3.10+, fully typed, tested with pytest.

## Setup

```bash
cd ev_data_pipeline
python -m venv .venv && .venv\Scripts\activate      # or source .venv/bin/activate
pip install -r requirements.txt
```

## CLI usage

```bash
# Job A — fetch stations (API key required)
export OCM_API_KEY=<your-free-key>                  # https://www.openchargemap.org/develop/api
python -m src.cli fetch-stations --country PL --limit 200 --out data/stations.csv

# Job B — generate sessions from the fetched snapshot
python -m src.cli generate-sessions --stations data/stations.csv \
    --rows 80000 --start 2024-01-01 --end 2025-12-31 \
    --anomaly-rate 0.03 --seed 42 --out data/sessions.csv

# Job B standalone — no OCM key / rate-limited? Generate fake stations instead
python -m src.cli generate-sessions --synthetic-stations 300 \
    --rows 80000 --seed 42 --out data/sessions.csv
```

`fetch-stations` retries with exponential backoff on 429/5xx (max 3), fails
fast with an actionable message on 401/403 or a missing `OCM_API_KEY`, and on
an empty result prints a warning and exits 0 (use `--synthetic-stations` for
Job B). If a snapshot exists it can be reused indefinitely; Job B never needs
the network.

## Reproducibility (single seed)

Every random component — session-type priors, day/hour sampling, durations,
efficiency, prices, driver pool, anomaly selection, synthetic stations, Faker
ids — is driven by the single `--seed` flag. Re-running the same command with
the same seed produces **byte-identical** CSV output (verified via MD5 in the
unit tests). Omit `--seed` for a default of 42.

## Anomaly budget (~3% by default)

`--anomaly-rate` (default `0.03`) is split **evenly** across three categories
(≈1% each):

| Category    | Share | Flagged `is_anomaly`? | Description |
|-------------|-------|-----------------------|-------------|
| duplicates  | ~1/3  | no (exact copies)     | exact duplicate rows, same `session_id` — simulates ingestion duplication |
| zero        | ~1/3  | **no**                | aborted sessions (`energy_kwh == 0` and/or `duration_minutes == 0`) — deliberately left unflagged so the Silver layer can catch them with rules |
| corruption  | ~1/3  | **yes**               | one of, chosen uniformly per row: negative `energy_kwh`, negative `duration_minutes`, `energy_kwh > power_kw * hours` (physically impossible), NULL in `energy_kwh`/`duration_minutes`/`price_per_kwh`, `ended_at < started_at` |

So the total anomalous share ≈ `--anomaly-rate`, and the explicitly corrupted
share (the `is_anomaly=true` rows) ≈ `rate / 3`.

## Realism model

- **Seasonality**: ±20% monthly multiplier — winter high (Dec/Jan ≈ 1.20),
  summer dip (Jul/Aug ≈ 0.85).
- **Weekday effect**: work sessions Mon–Fri heavy / weekend light; public
  sessions peak Fri–Sun; home slightly weekend-boosted.
- **Intra-day**: home clusters 18:00–23:00 + overnight; work clusters 08:00–10:00
  and 14:00–16:00; public is bimodal (morning commute + late afternoon) with a
  lunch bump at DC hubs (≥50 kW).
- **Growth**: overall volume compounds ~3%/month (EV adoption curve).
- **Durations/power**: home 60–600 min at 3.6–11 kW; work 120–480 min at
  11–22 kW; public 20–90 min at 22–150 kW; DC hubs 15–60 min.
- **Energy**: `energy_kwh = power_kw * hours * efficiency(0.85–0.95) * utilisation(0.55–1.0)`
  — always ≤ the physical ceiling.
- **Prices** (PLN): home ≈ 0.35, work ≈ 0.30, public ≈ 0.65 ± operator spread;
  all drift +10% YoY so tariff scenarios have signal.

## Data dictionary — `sessions.csv`

| Column             | Type          | Description |
|--------------------|---------------|-------------|
| `session_id`       | string        | uuid4 hex (duplicated rows share it intentionally) |
| `station_id`       | string        | FK into `stations.csv` (big DC hubs weighted heavier; resampled if the station list is small) |
| `started_at`       | ISO 8601 UTC  | timezone-aware, e.g. `2024-07-05T19:12:00+00:00` |
| `ended_at`         | ISO 8601 UTC  | `started_at + duration_minutes` (corrupt rows may invert it) |
| `duration_minutes` | float (min)   | whole minutes; NULL/negative only on corrupt rows |
| `energy_kwh`       | float         | correlated with power & duration; 0 on aborted sessions |
| `station_power_kw` | float         | denormalized from `stations.csv` |
| `session_type`     | string        | `home` / `work` / `public`, priors ≈ 45/25/30, tied to power tier |
| `price_per_kwh`    | float (PLN)   | base by type, +10% YoY drift, operator-dependent spread |
| `cost_pln`         | float (PLN)   | `energy_kwh * price_per_kwh` for clean rows (anomalies may violate it) |
| `driver_id`        | string        | persistent pseudonymized id (`DRV_…`) — same driver keeps the same id; Zipf-like usage |
| `is_anomaly`       | bool          | `true` only for explicitly injected corruption |

## Data dictionary — `stations.csv`

| Column       | Description |
|--------------|-------------|
| `station_id` | OCM POI id (or `SYN-…` for synthetic stations) |
| `power_kw`   | max usable power across connections; tier heuristics when missing |
| `tier`       | `home` (≤11 kW) / `destination` (11–22 kW) / `dc` (>22 kW) |
| `operator`   | operator name |
| `latitude` / `longitude` / `address` / `city` | location fields when available |

## Validation report

After generation the pipeline re-reads the output and prints: row count,
% duplicates, % zero-sessions, % corrupted, energy mean/median/max per
`session_type`, top-5 busiest stations and the timestamp range. It **exits
non-zero** if any invariant is violated (e.g. corrupted share deviating >1pp
from target, clean rows exceeding the physical energy limit, reversed
timestamps on clean rows).

A 500-row preview is written to `data/samples/sample_sessions.csv` (committed
to the repo).

## Tests

```bash
python -m pytest tests -v
```

Covers: anomaly fractions, zero-sessions not auto-flagged, intra-day home
evening clustering, energy ≤ power·duration·efficiency for clean rows,
cost consistency, session-type priors/power tiers, station FK integrity,
byte-identical determinism for a fixed seed (and divergence for a different
seed), and validation invariant checks.
