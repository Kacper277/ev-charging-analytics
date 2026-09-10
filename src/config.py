"""Central configuration for the EV data pipeline (Stage 0).

Every tunable lives here so behaviour is reproducible and testable.
CLI flags override these defaults where exposed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Job A - Open Charge Map
# ---------------------------------------------------------------------------

OCM_BASE_URL = "https://api.openchargemap.io/v3/poi/"
OCM_API_KEY_ENV = "OCM_API_KEY"
OCM_MAX_RETRIES = 3
OCM_BACKOFF_BASE_SECONDS = 2.0
OCM_PAGE_SIZE = 200

# Fallback power assignment (kW) when a station has no usable PowerKW,
# keyed by charger tier (see TIERS below).
TIER_FALLBACK_POWER_KW: dict[str, float] = {
    "home": 11.0,
    "destination": 22.0,
    "dc": 50.0,
}

# ---------------------------------------------------------------------------
# Job B - synthetic session generator
# ---------------------------------------------------------------------------

DEFAULT_ROWS = 80_000
DEFAULT_START = "2024-01-01"
DEFAULT_END = "2025-12-31"
DEFAULT_ANOMALY_RATE = 0.03
CHUNK_SIZE = 20_000
SAMPLE_ROWS = 500

# Month -> volume multiplier (winter high, summer dip). ±20% band.
MONTHLY_SEASONALITY: dict[int, float] = {
    1: 1.20,
    2: 1.15,
    3: 1.05,
    4: 1.00,
    5: 0.95,
    6: 0.90,
    7: 0.85,
    8: 0.85,
    9: 1.00,
    10: 1.05,
    11: 1.10,
    12: 1.20,
}

# EV adoption: overall volume growth per month (compound).
MONTHLY_GROWTH_RATE = 0.03

# Year-over-year tariff drift applied to prices.
YEARLY_PRICE_DRIFT = 0.10

# Session-type priors (must sum to 1.0).
SESSION_TYPE_PRIORS: dict[str, float] = {
    "home": 0.45,
    "work": 0.25,
    "public": 0.30,
}

# Charger tiers by station power (kW). Boundaries are inclusive lower bounds.
TIER_BOUNDS: list[tuple[str, float, float]] = [
    # tier, min power, max power
    ("home", 0.0, 11.0),
    ("destination", 11.0, 22.0),
    ("dc", 22.0, float("inf")),
]

# Intra-day hour weights (24 values) per session type. Not normalised here.
HOUR_WEIGHTS: dict[str, list[float]] = {
    # home: evening 18-23 + overnight
    "home": [
        6,
        5,
        4,
        3,
        3,
        3,
        2,
        2,
        2,
        2,
        2,
        2,
        2,
        2,
        2,
        3,
        4,
        6,
        10,
        12,
        12,
        10,
        8,
        6,
    ],
    # work: 08-10 and 14-16 clusters
    "work": [
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        2,
        8,
        10,
        6,
        3,
        2,
        2,
        8,
        10,
        5,
        2,
        1,
        0,
        0,
        0,
        0,
        0,
    ],
    # public: bimodal (morning commute + late afternoon)
    "public": [
        0,
        0,
        0,
        0,
        0,
        0,
        1,
        2,
        4,
        6,
        5,
        4,
        4,
        4,
        5,
        7,
        8,
        7,
        5,
        4,
        3,
        2,
        1,
        0,
    ],
}

# Extra lunch bump weight applied to DC (>=50 kW) public sessions.
DC_LUNCH_BUMP: dict[int, float] = {11: 3.0, 12: 4.0, 13: 3.0, 14: 2.0}

# Weekday volume multipliers per session type (Mon..Sun).
WEEKDAY_FACTORS: dict[str, list[float]] = {
    "home": [1.0, 1.0, 1.0, 1.0, 1.0, 1.2, 1.2],
    "work": [3.0, 3.0, 3.0, 3.0, 3.0, 0.3, 0.3],
    "public": [0.8, 0.8, 0.8, 0.9, 1.6, 2.0, 2.0],
}

# Session-type business rules: (min_duration, max_duration, min_power, max_power).
# For public sessions power comes from the station; DC hubs (>=50 kW) use the
# tighter DC duration window.
DURATION_MINUTES: dict[str, tuple[float, float]] = {
    "home": (60.0, 600.0),
    "work": (120.0, 480.0),
    "public": (20.0, 90.0),
}
DC_DURATION_MINUTES: tuple[float, float] = (15.0, 60.0)
POWER_RANGE: dict[str, tuple[float, float]] = {
    "home": (3.6, 11.0),
    "work": (11.0, 22.0),
    "public": (22.0, 150.0),
}

# Charging efficiency ceiling: energy_kwh <= power_kw * hours * efficiency.
EFFICIENCY_RANGE: tuple[float, float] = (0.85, 0.95)
# Utilisation of the physical ceiling (battery state of charge spread).
UTILISATION_RANGE: tuple[float, float] = (0.55, 1.0)

# Base price per kWh (PLN) by session type.
BASE_PRICE_PLN: dict[str, float] = {
    "home": 0.35,
    "work": 0.30,
    "public": 0.65,
}
# Public price spread (lognormal sigma) to model operator-dependent pricing.
PUBLIC_PRICE_SIGMA = 0.08
PRICE_SIGMA: dict[str, float] = {
    "home": 0.02,
    "work": 0.02,
    "public": PUBLIC_PRICE_SIGMA,
}

# Size of the persistent driver pool (drivers resampled with Zipf-like weights).
DRIVER_POOL_SIZE = 5_000

# ---------------------------------------------------------------------------
# Anomaly budget
# ---------------------------------------------------------------------------
# The total anomaly_rate (default 3%) is split evenly across:
#   duplicates  (~1/3)  exact duplicate rows (same session_id)
#   zero        (~1/3)  aborted sessions (energy or duration == 0, NOT flagged)
#   corruption  (~1/3)  explicitly injected corrupt rows (is_anomaly=true)
ANOMALY_CATEGORY_WEIGHTS: dict[str, float] = {
    "duplicates": 1.0 / 3.0,
    "zero": 1.0 / 3.0,
    "corruption": 1.0 / 3.0,
}

# Corruption sub-types (chosen uniformly per corrupted row).
CORRUPTION_TYPES: tuple[str, ...] = (
    "negative_energy",
    "negative_duration",
    "energy_over_limit",
    "null_value",
    "reversed_timestamp",
)

CSV_COLUMNS: tuple[str, ...] = (
    "session_id",
    "station_id",
    "started_at",
    "ended_at",
    "duration_minutes",
    "energy_kwh",
    "station_power_kw",
    "session_type",
    "price_per_kwh",
    "cost_pln",
    "driver_id",
    "is_anomaly",
)

SEED = 42
