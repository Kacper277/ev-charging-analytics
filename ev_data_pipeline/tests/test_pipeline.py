"""Unit tests for generator, anomalies and validation."""

from __future__ import annotations

import hashlib
import random
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src import config
from src.anomalies import corrupt_row, split_anomaly_counts, zero_out_row
from src.generator import (
    StationPools,
    _day_weights,
    generate_clean_chunk,
    generate_sessions,
)
from src.stations import make_synthetic_stations
from src.validation import check_invariants, load_sessions


@pytest.fixture(scope="module")
def stations() -> list[dict]:
    return make_synthetic_stations(120, random.Random(42))


@pytest.fixture(scope="module")
def sessions_df(tmp_path_factory, stations) -> pd.DataFrame:
    out = tmp_path_factory.mktemp("data") / "sessions.csv"
    generate_sessions(stations, 5_000, "2024-01-01", "2024-06-30", 0.03, 42, str(out))
    df = load_sessions(str(out))
    df["is_anomaly"] = df["is_anomaly"].astype(str).str.lower() == "true"
    return df


# ---------------------------------------------------------------- anomalies


def test_anomaly_fractions_approximate_target(sessions_df):
    total = len(sessions_df)
    assert sessions_df["is_anomaly"].mean() == pytest.approx(0.03 / 3, abs=0.012)
    dup_share = sessions_df["session_id"].duplicated().mean()
    zero_share = (
        (sessions_df["energy_kwh"] == 0) | (sessions_df["duration_minutes"] == 0)
    ).mean()
    assert dup_share == pytest.approx(0.03 / 3, abs=0.012)
    assert zero_share == pytest.approx(0.03 / 3, abs=0.012)
    assert total >= 5_000  # duplicates only add rows


def test_zero_sessions_not_flagged(sessions_df):
    zero = sessions_df[
        (sessions_df["energy_kwh"] == 0) | (sessions_df["duration_minutes"] == 0)
    ]
    # Zero sessions are aborted sessions, never auto-flagged.
    assert not zero["is_anomaly"].any()


def test_split_anomaly_counts_sum():
    counts = split_anomaly_counts(10_000, 0.03)
    assert sum(counts.values()) == 300
    assert set(counts) == set(config.ANOMALY_CATEGORY_WEIGHTS)


def test_corruption_types_change_row():
    row = {
        "energy_kwh": 10.0,
        "duration_minutes": 30.0,
        "station_power_kw": 50.0,
        "price_per_kwh": 0.6,
        "started_at": "2024-01-01T10:00:00+00:00",
        "ended_at": "2024-01-01T10:30:00+00:00",
        "is_anomaly": False,
    }
    corrupt_row(row, "negative_energy", random.Random(1))
    assert row["energy_kwh"] < 0 and row["is_anomaly"]

    corrupt_row(row, "reversed_timestamp", random.Random(1))
    assert row["ended_at"] < row["started_at"]


def test_zero_out_never_flags():
    row = {
        "duration_minutes": 30.0,
        "energy_kwh": 5.0,
        "cost_pln": 3.0,
        "started_at": "a",
        "ended_at": "b",
        "is_anomaly": False,
    }
    zero_out_row(row, random.Random(1))
    assert row["is_anomaly"] is False


# ---------------------------------------------------------------- realism


def test_intraday_home_evening_cluster(stations):
    rng_np = np.random.default_rng(7)
    rng_py = random.Random(7)
    days = [
        date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(28)
    ]
    day_weights = {t: _day_weights(days, t) for t in ("home", "work", "public")}
    pool = StationPools(stations)
    from src.generator import _make_driver_pool

    driver_pool, driver_weights = _make_driver_pool(rng_py)
    rows = generate_clean_chunk(
        4_000,
        days,
        day_weights,
        pool,
        rng_np,
        rng_py,
        driver_pool,
        driver_weights,
        date(2024, 1, 1),
    )
    hours = [int(r["started_at"][11:13]) for r in rows if r["session_type"] == "home"]
    evening = sum(1 for h in hours if 18 <= h <= 23)
    assert evening / len(hours) > 0.35  # home clusters 18:00-23:00


def test_energy_within_physical_limit(sessions_df):
    clean = sessions_df[~sessions_df["is_anomaly"]]
    clean = clean[(clean["energy_kwh"] > 0) & (clean["duration_minutes"] > 0)]
    physical = (
        clean["station_power_kw"]
        * (clean["duration_minutes"] / 60.0)
        * config.EFFICIENCY_RANGE[1]
    )
    assert (clean["energy_kwh"] <= physical * 1.001).all()


def test_cost_matches_energy_times_price(sessions_df):
    clean = sessions_df[~sessions_df["is_anomaly"]].dropna(subset=["price_per_kwh"])
    ok = clean[(clean["energy_kwh"] > 0)]
    expected = (ok["energy_kwh"] * ok["price_per_kwh"]).round(2)
    assert (ok["cost_pln"] - expected).abs().le(0.011).all()


def test_session_type_priors(sessions_df):
    shares = sessions_df["session_type"].value_counts(normalize=True)
    for name, prior in config.SESSION_TYPE_PRIORS.items():
        assert shares.get(name, 0) == pytest.approx(prior, abs=0.05)


def test_home_sessions_use_low_power(sessions_df):
    clean = sessions_df[~sessions_df["is_anomaly"]]
    home = clean[clean["session_type"] == "home"]
    assert (home["station_power_kw"] <= 11.0).all()
    public = clean[clean["session_type"] == "public"]
    assert (public["station_power_kw"] >= 22.0).all()


def test_station_ids_resolve(sessions_df, stations):
    valid = {s["station_id"] for s in stations}
    assert set(sessions_df["station_id"]).issubset(valid)


# ---------------------------------------------------------------- determinism


def _md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def test_deterministic_output_for_fixed_seed(tmp_path, stations):
    out1, out2 = tmp_path / "a.csv", tmp_path / "b.csv"
    generate_sessions(stations, 1_000, "2024-01-01", "2024-03-31", 0.03, 42, str(out1))
    generate_sessions(stations, 1_000, "2024-01-01", "2024-03-31", 0.03, 42, str(out2))
    assert _md5(out1) == _md5(out2)


def test_different_seed_changes_output(tmp_path, stations):
    out1, out2 = tmp_path / "a.csv", tmp_path / "b.csv"
    generate_sessions(stations, 1_000, "2024-01-01", "2024-03-31", 0.03, 42, str(out1))
    generate_sessions(stations, 1_000, "2024-01-01", "2024-03-31", 0.03, 43, str(out2))
    assert _md5(out1) != _md5(out2)


# ---------------------------------------------------------------- validation


def test_check_invariants_pass_on_good_data(sessions_df):
    assert check_invariants(sessions_df, 5_000, 0.03) == []


def test_check_invariants_flags_corrupted_share():
    df = pd.DataFrame(
        {
            "session_id": [f"s{i}" for i in range(100)],
            "station_id": "x",
            "started_at": pd.Timestamp("2024-01-01", tz="UTC"),
            "ended_at": pd.Timestamp("2024-01-01", tz="UTC"),
            "duration_minutes": 30.0,
            "energy_kwh": 5.0,
            "station_power_kw": 50.0,
            "session_type": "public",
            "price_per_kwh": 0.6,
            "cost_pln": 3.0,
            "driver_id": "d",
            "is_anomaly": [True] * 30 + [False] * 70,
        }
    )
    violations = check_invariants(df, 100, 0.03)
    assert any("corrupted share" in v for v in violations)
