# EV Charging Analytics
End-to-end data platform for EV charging networks: synthetic-but-realistic data
generation → medallion architecture in Databricks → SQL analytics → Power BI
dashboard with tariff what-if scenarios.

![Dashboard](assets/dashboard_overview.png)

## Architecture
[diagram draw.io: OCM+generator → Delta Bronze → Silver → Gold → SQL views → Power BI]
+ one line per layer with numbers: 80.8k raw → 80.0k deduped → 78.4k clean,
  3% anomaly budget reconciled across layers.

## Key insights
1. ... 
2. ...
3. ...

## What's inside
| Component | Where | Docs |
|---|---|---|
| Data generation (seeded, tested) | `src/` | [docs/01-data-pipeline.md](docs/01-data-pipeline.md) |
| Medallion pipeline (Bronze/Silver/Gold) | `databricks/` | [docs/02-...](docs/...) |
| Analytical SQL views | `analytics/` | [docs/03-...](docs/...) |
| Power BI dashboard | `assets/` | — |
| FinOps estimate | `docs/04-finops.md` | — |

## Production considerations
