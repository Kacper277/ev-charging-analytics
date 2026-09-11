-- ============================================================================
-- EV Charging Analytics — Analytical Queries over Gold Layer
-- Databricks (Delta Lake) SQL. Each query is saved as a view; Power BI reads
-- these views (not raw fact/dim tables). Run in order after 01_bronze →
-- 02_silver → 03_gold.
--
-- Results are documented in docs/03-analytics-queries.md.
-- ============================================================================

-- Q1: Cost per operator — where does energy cost the most?
-- Purpose: identifies price differences between operators; the weighted price
-- (cost per delivered kWh, not simple average of session prices) reflects the
-- real revenue side. Basis for the cost page in Power BI.
CREATE OR REPLACE VIEW workspace.default.v_operator_costs AS
SELECT
  s.operator,
  COUNT(*)                                        AS sessions,
  ROUND(SUM(f.energy_kwh), 0)                     AS total_kwh,
  ROUND(SUM(f.cost_pln), 2)                       AS total_cost_pln,
  ROUND(SUM(f.cost_pln) / SUM(f.energy_kwh), 3)   AS weighted_price_per_kwh
FROM workspace.default.fact_sessions f
JOIN workspace.default.dim_station s USING (station_id)
GROUP BY s.operator
ORDER BY total_kwh DESC;

-- Q2: Station utilization — delivered energy vs physically available capacity
-- Purpose: the core operational metric. util_pct (energy) shows whether sessions
-- effectively use the station's power; occupancy_pct_of_time shows what fraction
-- of calendar time the station is busy. The contrast between the two (fast chargers:
-- high energy util, low time occupancy) is the basis for the scatter plot in Power BI.
-- NOTE: bottom performers in Q5 are filtered to stations with >= 100 sessions
-- (small AC stations are excluded by design).
CREATE OR REPLACE VIEW workspace.default.v_station_utilization AS
SELECT
  f.station_id,
  ANY_VALUE(s.operator)                                        AS operator,
  ANY_VALUE(s.charger_class)                                   AS charger_class,
  COUNT(*)                                                     AS sessions,
  ROUND(SUM(f.energy_kwh), 1)                                  AS delivered_kwh,
  ROUND(SUM(f.duration_minutes) / 60.0, 1)                     AS occupied_hours,
  ROUND(100 * SUM(f.energy_kwh) /
        NULLIF(SUM(f.duration_minutes) / 60.0 * ANY_VALUE(s.station_power_kw), 0), 1)
                                                               AS util_pct,
  ROUND(100 * SUM(f.duration_minutes) / (731.0 * 24 * 60), 2)  AS occupancy_pct_of_time
FROM workspace.default.fact_sessions f
JOIN workspace.default.dim_station s USING (station_id)
GROUP BY f.station_id
HAVING COUNT(*) >= 100;

-- Q3: Intra-day demand profile — when is the system under the most load?
-- Purpose: bimodal profile (morning ~09:00 and afternoon ~15:00 peaks) with
-- long home sessions at night (~330 min) vs short public sessions during the day
-- (~140-210 min). Directly feeds the load-shifting recommendation.
CREATE OR REPLACE VIEW workspace.default.v_peak_hours AS
SELECT
  hour_of_day,
  COUNT(*)                          AS sessions,
  ROUND(SUM(energy_kwh), 0)         AS total_kwh,
  ROUND(AVG(duration_minutes), 0)   AS avg_duration_min
FROM workspace.default.fact_sessions
GROUP BY hour_of_day
ORDER BY hour_of_day;

-- Q4: Anomaly review queue — what the Silver layer flagged, for BI drill-down
-- Purpose: makes data quality tangible in the BI layer. 1 600 anomalies,
-- reconciled 1:1 with the generator's anomaly budget (see docs/02).
CREATE OR REPLACE VIEW workspace.default.v_anomaly_review AS
SELECT
  session_id,
  station_id,
  anomaly_reason,
  started_at,
  energy_kwh,
  duration_minutes,
  price_per_kwh
FROM workspace.default.silver_clean
WHERE anomaly_reason <> 'ok';

-- Q5: Top & bottom performers — where to invest vs where to cut
-- Purpose: rank_top ranks by delivered volume (big DC hubs dominate);
-- rank_bottom surfaces the smallest active stations. Worth noting: bottom
-- stations often have HIGHER util_pct than top ones — high utilization ≠ high
-- volume. Drill-down from Q2 (uses the same view; not materialized separately).
SELECT * FROM (
  SELECT *,
         ROW_NUMBER() OVER (ORDER BY delivered_kwh DESC) AS rank_top,
         ROW_NUMBER() OVER (ORDER BY delivered_kwh ASC)  AS rank_bottom
  FROM workspace.default.v_station_utilization
)
WHERE rank_top <= 10 OR rank_bottom <= 10
ORDER BY delivered_kwh DESC;

-- Q6: Demand structure by session type — which segment do tariff scenarios hit hardest?
-- Purpose: home/public/work mix ≈ 45/30/25% of sessions. Public pays ~0.73 PLN/kWh
-- vs ~0.39 home and ~0.33 work → public segment generates ~66% of total cost
-- from 30% of sessions. Central insight for the tariff scenario page.
CREATE OR REPLACE VIEW workspace.default.v_session_mix AS
SELECT
  session_type,
  COUNT(*)                                  AS sessions,
  ROUND(SUM(energy_kwh), 0)                 AS total_kwh,
  ROUND(SUM(cost_pln), 0)                   AS total_cost_pln,
  ROUND(SUM(cost_pln) / SUM(energy_kwh), 3) AS avg_cost_per_kwh
FROM workspace.default.fact_sessions
GROUP BY session_type;

-- Q7: Monthly trend — seasonality + EV adoption growth
-- Purpose: winter peaks (Dec ~275k kWh/mo) and summer dips (Jul ~119k) on top of
-- a strong growth trend (+36% volume 2025 vs 2024). Verifies the generator's
-- seasonality model against the analytics.
CREATE OR REPLACE VIEW workspace.default.v_monthly_trend AS
SELECT
  t.year, t.month,
  COUNT(*)                      AS sessions,
  ROUND(SUM(f.energy_kwh), 0)   AS total_kwh,
  ROUND(SUM(f.cost_pln), 0)     AS total_cost_pln
FROM workspace.default.fact_sessions f
JOIN workspace.default.dim_time t ON f.date_key = t.date_key
GROUP BY t.year, t.month
ORDER BY t.year, t.month;

-- Q8: Day/night energy split per month — feeds the Power BI what-if parameter
-- Purpose: on average ~55% of energy is charged during the 'day' window
-- (13:00-21:00), stable at 52.6-56.9% across the whole period. This table is
-- the factual basis for the tariff what-if scenario in Power BI (shifting X pp
-- of volume from day to night at a given discount = measurable PLN savings).
CREATE OR REPLACE VIEW workspace.default.v_tariff_split AS
SELECT
  t.year, t.month,
  ROUND(SUM(f.energy_kwh), 0)                                            AS total_kwh,
  ROUND(100.0 * SUM(CASE WHEN f.tariff_period = 'day' THEN f.energy_kwh ELSE 0 END)
        / SUM(f.energy_kwh), 1)                                          AS day_pct,
  ROUND(SUM(CASE WHEN f.tariff_period = 'day' THEN f.energy_kwh ELSE 0 END), 0)   AS day_kwh,
  ROUND(SUM(CASE WHEN f.tariff_period = 'night' THEN f.energy_kwh ELSE 0 END), 0) AS night_kwh
FROM workspace.default.fact_sessions f
JOIN workspace.default.dim_time t ON f.date_key = t.date_key
GROUP BY t.year, t.month
ORDER BY t.year, t.month;