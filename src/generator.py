"""Job B - synthetic EV charging session generator.

Fully deterministic: all randomness flows from a single seed (numpy
``default_rng`` plus a ``random.Random`` instance, both seeded with the same
value; Faker is seeded from that rng for driver ids / synthetic stations).
"""

from __future__ import annotations

import csv
import logging
import os
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
from numpy.random import Generator

from . import config
from .anomalies import (
    corrupt_row,
    duplicate_row,
    pick_corruption_type,
    split_anomaly_counts,
    zero_out_row,
)
from .stations import make_synthetic_stations, read_stations_csv

logger = logging.getLogger(__name__)

Row = dict[str, Any]

TYPE_NAMES = ("home", "work", "public")
TYPE_INDEX = {name: i for i, name in enumerate(TYPE_NAMES)}


def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {flag} date '{value}'. Expected ISO format YYYY-MM-DD (e.g. 2024-01-01)."
        ) from exc


def _validate_range(start: date, end: date) -> None:
    if start >= end:
        raise ValueError(f"Invalid date range: start {start} must be before end {end}.")


class StationPools:
    """Tier-indexed station pools with hub weighting for resampling."""

    def __init__(self, stations: list[Row]):
        self.stations = stations
        pools: dict[str, list[int]] = {"home": [], "destination": [], "dc": []}
        for idx, station in enumerate(stations):
            power = float(station["power_kw"])
            for tier, low, high in config.TIER_BOUNDS:
                if low < power <= high:
                    pools[tier].append(idx)
                    break
        self.pools = pools

    def _nearest_tier(self, tier: str) -> str:
        if self.pools[tier]:
            return tier
        order = ["home", "destination", "dc"]
        idx = order.index(tier)
        for offset in range(1, len(order)):
            for candidate in (idx - offset, idx + offset):
                if 0 <= candidate < len(order) and self.pools[order[candidate]]:
                    return order[candidate]
        raise ValueError("Station list contains no usable stations.")

    def sample(
        self, tier: str, rng: Generator, size: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (station_idx, power_kw) samples for a tier.

        DC hubs get proportionally more sessions (weighted by power).
        """
        actual = self._nearest_tier(tier)
        pool = np.asarray(self.pools[actual], dtype=np.int64)
        powers = np.asarray(
            [float(self.stations[i]["power_kw"]) for i in self.pools[actual]],
            dtype=np.float64,
        )
        weights = 1.0 + powers / 50.0
        weights = weights / weights.sum()
        picks = rng.choice(len(pool), size=size, p=weights)
        return pool[picks], powers[picks]


def _day_weights(days: list[date], session_type: str) -> np.ndarray:
    weights = np.empty(len(days), dtype=np.float64)
    weekday_factor = config.WEEKDAY_FACTORS[session_type]
    for i, day in enumerate(days):
        growth = (1.0 + config.MONTHLY_GROWTH_RATE) ** ((day - days[0]).days / 30.44)
        weights[i] = (
            config.MONTHLY_SEASONALITY[day.month]
            * weekday_factor[day.weekday()]
            * growth
        )
    return weights


def _hour_weights_matrix(session_types: np.ndarray, powers: np.ndarray) -> np.ndarray:
    """(n, 24) hour weight matrix, with a lunch bump for DC public sessions."""
    matrix = np.empty((len(session_types), 24), dtype=np.float64)
    for name in TYPE_NAMES:
        mask = session_types == TYPE_INDEX[name]
        matrix[mask] = config.HOUR_WEIGHTS[name]
    dc_public = (session_types == TYPE_INDEX["public"]) & (powers >= 50.0)
    if dc_public.any():
        for hour, bump in config.DC_LUNCH_BUMP.items():
            matrix[dc_public, hour] += bump
    return matrix


def _inverse_cdf_rows(cumulative: np.ndarray, uniforms: np.ndarray) -> np.ndarray:
    """Per-row inverse-CDF sampling: returns category indices.

    ``uniforms`` is shape (n,); ``cumulative`` is (n, k) per-row CDFs
    (or broadcastable, e.g. (k,) for a shared distribution).
    """
    return (uniforms[:, None] > cumulative).sum(axis=1)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _make_driver_pool(rng: random.Random) -> tuple[list[str], np.ndarray]:
    from faker import Faker

    fake = Faker()
    fake.seed_instance(rng.randrange(2**31))
    Faker.seed(rng.randrange(2**31))
    pool = [f"DRV_{fake.uuid4()[:13]}" for _ in range(config.DRIVER_POOL_SIZE)]
    # Zipf-like weights: a few heavy users, many light users.
    weights = 1.0 / np.arange(1, len(pool) + 1, dtype=np.float64) ** 0.8
    return pool, weights / weights.sum()


def _new_session_id(rng: random.Random) -> str:
    return uuid.UUID(int=rng.getrandbits(128), version=4).hex


def generate_clean_chunk(
    size: int,
    days: list[date],
    day_weights: dict[str, np.ndarray],
    pools: StationPools,
    rng_np: Generator,
    rng_py: random.Random,
    driver_pool: list[str],
    driver_weights: np.ndarray,
    start_date: date,
) -> list[Row]:
    """Generate ``size`` clean session rows (vectorised, deterministic)."""
    prior = np.asarray([config.SESSION_TYPE_PRIORS[t] for t in TYPE_NAMES])
    type_idx = rng_np.choice(len(TYPE_NAMES), size=size, p=prior / prior.sum())

    station_idx = np.empty(size, dtype=np.int64)
    powers = np.empty(size, dtype=np.float64)
    day = np.empty(size, dtype=np.int64)
    hour = np.empty(size, dtype=np.int64)
    duration = np.empty(size, dtype=np.float64)

    day_cum = {t: np.cumsum(day_weights[t] / day_weights[t].sum()) for t in TYPE_NAMES}

    u_day = rng_np.random(size)
    u_hour = rng_np.random(size)
    u_min = rng_np.random(size)
    u_eff = rng_np.random(size)
    u_util = rng_np.random(size)

    for name in TYPE_NAMES:
        mask = type_idx == TYPE_INDEX[name]
        count = int(mask.sum())
        if count == 0:
            continue
        tier = "home" if name == "home" else ("destination" if name == "work" else "dc")
        s_idx, s_power = pools.sample(tier, rng_np, count)
        station_idx[mask] = s_idx
        powers[mask] = s_power
        day[mask] = _inverse_cdf_rows(
            np.broadcast_to(day_cum[name], (count, len(days))), u_day[mask]
        )

        # power for home/work is sampled in the tier band; public uses station power
        if name == "home":
            low, high = config.POWER_RANGE["home"]
            powers[mask] = rng_np.uniform(low, high, size=count)
        elif name == "work":
            low, high = config.POWER_RANGE["work"]
            powers[mask] = rng_np.uniform(low, high, size=count)

        duration[mask] = rng_np.uniform(*config.DURATION_MINUTES[name], size=count)
        # DC hubs get the tighter fast-charge duration window.
        if name == "public":
            positions = np.flatnonzero(mask)
            dc_positions = positions[s_power >= 50.0]
            if len(dc_positions):
                duration[dc_positions] = rng_np.uniform(
                    *config.DC_DURATION_MINUTES, size=len(dc_positions)
                )

    hour_matrix = _hour_weights_matrix(type_idx, powers)
    hour_cum = np.cumsum(hour_matrix / hour_matrix.sum(axis=1, keepdims=True), axis=1)
    hour = _inverse_cdf_rows(hour_cum, u_hour)
    minute = (u_min * 60).astype(np.int64) % 60

    rows: list[Row] = []
    for i in range(size):
        started = datetime(
            days[int(day[i])].year,
            days[int(day[i])].month,
            days[int(day[i])].day,
            int(hour[i]),
            int(minute[i]),
            tzinfo=timezone.utc,
        )
        duration_r = float(round(duration[i]))  # rounded to whole minutes
        ended = started + timedelta(minutes=duration_r)
        power_r = round(float(powers[i]), 1)
        hours = duration_r / 60.0
        efficiency = config.EFFICIENCY_RANGE[0] + u_eff[i] * (
            config.EFFICIENCY_RANGE[1] - config.EFFICIENCY_RANGE[0]
        )
        utilisation = config.UTILISATION_RANGE[0] + u_util[i] * (
            config.UTILISATION_RANGE[1] - config.UTILISATION_RANGE[0]
        )
        energy = power_r * hours * efficiency * utilisation

        session_type = TYPE_NAMES[int(type_idx[i])]
        years_elapsed = (started.date() - start_date).days / 365.25
        drift = (1.0 + config.YEARLY_PRICE_DRIFT) ** years_elapsed
        base = config.BASE_PRICE_PLN[session_type]
        sigma = config.PRICE_SIGMA[session_type]
        spread = float(rng_py.lognormvariate(0.0, sigma))
        price = base * drift * spread

        rows.append(
            {
                "session_id": _new_session_id(rng_py),
                "station_id": str(pools.stations[int(station_idx[i])]["station_id"]),
                "started_at": _iso(started),
                "ended_at": _iso(ended),
                "duration_minutes": duration_r,
                "energy_kwh": round(float(energy), 3),
                "station_power_kw": power_r,
                "session_type": session_type,
                "price_per_kwh": round(price, 4),
                "cost_pln": round(float(energy) * price, 2),
                "driver_id": driver_pool[
                    int(rng_np.choice(len(driver_pool), p=driver_weights))
                ],
                "is_anomaly": False,
            }
        )
    return rows


def _write_rows(writer: csv.DictWriter, rows: list[Row]) -> None:
    for row in rows:
        out = dict(row)
        out["is_anomaly"] = "true" if row["is_anomaly"] else "false"
        out["duration_minutes"] = (
            "" if row["duration_minutes"] is None else row["duration_minutes"]
        )
        out["energy_kwh"] = "" if row["energy_kwh"] is None else row["energy_kwh"]
        out["price_per_kwh"] = (
            "" if row["price_per_kwh"] is None else row["price_per_kwh"]
        )
        writer.writerow(out)


def _anomaly_counts_for_chunk(size: int, anomaly_rate: float) -> dict[str, int]:
    rates = config.ANOMALY_CATEGORY_WEIGHTS
    return {name: round(size * anomaly_rate * weight) for name, weight in rates.items()}


def generate_sessions(
    stations: list[Row],
    n_rows: int,
    start_str: str,
    end_str: str,
    anomaly_rate: float,
    seed: int,
    out_path: str,
) -> int:
    """Generate the sessions CSV. Returns the number of rows written."""
    start_date = _parse_date(start_str, "--start")
    end_date = _parse_date(end_str, "--end")
    _validate_range(start_date, end_date)
    if anomaly_rate < 0 or anomaly_rate >= 0.5:
        raise ValueError(f"--anomaly-rate must be in [0, 0.5), got {anomaly_rate}")

    rng_py = random.Random(seed)
    rng_np = np.random.default_rng(seed)
    pools = StationPools(stations)
    driver_pool, driver_weights = _make_driver_pool(rng_py)

    days = [
        start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
    ]
    day_weights = {t: _day_weights(days, t) for t in TYPE_NAMES}

    counts = split_anomaly_counts(n_rows, anomaly_rate)
    logger.info(
        "Anomaly budget for %d rows: %s (total %.1f%%)",
        n_rows,
        counts,
        100.0 * sum(counts.values()) / max(n_rows, 1),
    )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    sample_dir = os.path.join(os.path.dirname(out_path) or ".", "samples")
    os.makedirs(sample_dir, exist_ok=True)
    sample_path = os.path.join(sample_dir, "sample_sessions.csv")

    written = 0
    pending_dups: list[Row] = []
    first_chunk_rows: list[Row] = []
    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(config.CSV_COLUMNS))
        writer.writeheader()
        for offset in range(0, n_rows, config.CHUNK_SIZE):
            size = min(config.CHUNK_SIZE, n_rows - offset)
            chunk_counts = _anomaly_counts_for_chunk(size, anomaly_rate)

            rows = generate_clean_chunk(
                size,
                days,
                day_weights,
                pools,
                rng_np,
                rng_py,
                driver_pool,
                driver_weights,
                start_date,
            )

            # Disjoint anomaly index sets (duplicates applied after corruption).
            zero_idx = set(
                rng_np.choice(size, size=chunk_counts["zero"], replace=False).tolist()
            )
            remaining = [i for i in range(size) if i not in zero_idx]
            corr_idx = set(
                rng_py.sample(
                    remaining, min(chunk_counts["corruption"], len(remaining))
                )
            )
            for i in zero_idx:
                rows[i] = zero_out_row(rows[i], rng_py)
            for i in corr_idx:
                rows[i] = corrupt_row(rows[i], pick_corruption_type(rng_py), rng_py)
            dup_idx = rng_py.sample(range(size), min(chunk_counts["duplicates"], size))
            dup_rows = [duplicate_row(rows[i]) for i in dup_idx]

            if not first_chunk_rows:
                first_chunk_rows = rows

            _write_rows(writer, pending_dups)
            written += len(pending_dups)
            pending_dups = []
            _write_rows(writer, rows)
            written += len(rows)
            pending_dups = dup_rows

        _write_rows(writer, pending_dups)
        written += len(pending_dups)

    with open(sample_path, "w", newline="", encoding="utf-8") as sample_handle:
        sample_writer = csv.DictWriter(
            sample_handle, fieldnames=list(config.CSV_COLUMNS)
        )
        sample_writer.writeheader()
        _write_rows(sample_writer, first_chunk_rows[: config.SAMPLE_ROWS])

    logger.info("Wrote %d rows to %s (sample: %s)", written, out_path, sample_path)
    return written


def run_generate(
    stations_path: str | None,
    synthetic_stations: int,
    n_rows: int,
    start: str,
    end: str,
    anomaly_rate: float,
    seed: int,
    out_path: str,
) -> int:
    """CLI entrypoint for Job B. Returns number of rows written."""
    if stations_path:
        stations = read_stations_csv(stations_path)
        logger.info("Loaded %d stations from %s", len(stations), stations_path)
    elif synthetic_stations > 0:
        stations = make_synthetic_stations(synthetic_stations, random.Random(seed))
        logger.info("Generated %d synthetic stations", len(stations))
    else:
        raise ValueError(
            "No station source provided. Either pass --stations <path> (from fetch-stations) "
            "or --synthetic-stations N to generate fake stations."
        )

    from .validation import run_validation

    written = generate_sessions(
        stations, n_rows, start, end, anomaly_rate, seed, out_path
    )
    report_ok = run_validation(out_path, n_rows, anomaly_rate)
    if not report_ok:
        raise SystemExit(1)
    return written
