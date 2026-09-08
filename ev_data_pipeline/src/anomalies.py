"""Anomaly injection logic for the synthetic session generator.

The total anomaly budget (``--anomaly-rate``, default 3%) is split evenly
across three categories (see config.ANOMALY_CATEGORY_WEIGHTS):

* duplicates  - exact duplicate rows (same session_id, full copy)
* zero        - aborted sessions (energy/duration == 0), NOT flagged is_anomaly
* corruption  - explicitly corrupted rows, flagged is_anomaly=true

Corruption sub-types (uniform pick per row):
negative_energy | negative_duration | energy_over_limit | null_value |
reversed_timestamp
"""

from __future__ import annotations

import random
from typing import Any

from . import config

Row = dict[str, Any]


def split_anomaly_counts(n_rows: int, anomaly_rate: float) -> dict[str, int]:
    """Split the anomaly budget across categories for ``n_rows`` base rows."""
    counts: dict[str, int] = {}
    remaining = 1.0
    categories = list(config.ANOMALY_CATEGORY_WEIGHTS.items())
    for index, (name, weight) in enumerate(categories):
        if index == len(categories) - 1:
            share = remaining
        else:
            share = weight
        remaining -= share
        counts[name] = round(n_rows * anomaly_rate * share)
    return counts


def pick_corruption_type(rng: random.Random) -> str:
    return rng.choices(config.CORRUPTION_TYPES, k=1)[0]


def corrupt_row(row: Row, corruption: str, rng: random.Random) -> Row:
    """Apply one corruption sub-type in place and flag the row."""
    row["is_anomaly"] = True
    if corruption == "negative_energy":
        row["energy_kwh"] = -abs(float(row["energy_kwh"] or 0.0)) - rng.uniform(
            0.1, 5.0
        )
    elif corruption == "negative_duration":
        row["duration_minutes"] = -abs(
            float(row["duration_minutes"] or 0.0)
        ) - rng.uniform(1.0, 30.0)
    elif corruption == "energy_over_limit":
        power = float(row["station_power_kw"])
        hours = abs(float(row["duration_minutes"])) / 60.0 or 1.0
        row["energy_kwh"] = round(power * hours * rng.uniform(2.0, 6.0), 3)
    elif corruption == "null_value":
        field_name = rng.choice(["energy_kwh", "duration_minutes", "price_per_kwh"])
        row[field_name] = None
    elif corruption == "reversed_timestamp":
        row["started_at"], row["ended_at"] = row["ended_at"], row["started_at"]
    return row


def zero_out_row(row: Row, rng: random.Random) -> Row:
    """Turn a row into an aborted session (never flagged is_anomaly)."""
    if rng.random() < 0.5:
        row["duration_minutes"] = 0.0
        row["ended_at"] = row["started_at"]
    row["energy_kwh"] = 0.0
    row["cost_pln"] = 0.0
    return row


def duplicate_row(row: Row) -> Row:
    """Exact copy of a row, including session_id (ingestion duplication)."""
    return dict(row)
