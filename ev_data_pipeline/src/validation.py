"""Post-generation self-validation and report for the sessions dataset."""

from __future__ import annotations

import logging

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

EFFICIENCY = config.EFFICIENCY_RANGE[1]


def load_sessions(path: str) -> pd.DataFrame:
    return pd.read_csv(
        path,
        dtype={"session_id": str, "station_id": str, "driver_id": str},
        parse_dates=["started_at", "ended_at"],
    )


def _energy_stats(df: pd.DataFrame) -> pd.DataFrame:
    clean = df[~df["is_anomaly"]]
    return (
        clean.groupby("session_type")["energy_kwh"]
        .agg(["mean", "median", "max"])
        .round(2)
    )


def _top_stations(df: pd.DataFrame, n: int = 5) -> pd.Series:
    return df["station_id"].value_counts().head(n)


def check_invariants(
    df: pd.DataFrame, target_rows: int, anomaly_rate: float
) -> list[str]:
    """Return a list of invariant violations (empty list means all good)."""
    violations: list[str] = []
    total = len(df)
    if total < target_rows:
        violations.append(f"row count {total} is below the requested {target_rows}")

    corrupted_share = df["is_anomaly"].mean()
    target_corrupted = anomaly_rate / 3.0
    if abs(corrupted_share - target_corrupted) > 0.01:
        violations.append(
            f"corrupted share {corrupted_share:.3%} deviates from target "
            f"{target_corrupted:.3%} by more than 1pp"
        )

    dup_share = df["session_id"].duplicated().mean()
    target_dup = anomaly_rate / 3.0
    if abs(dup_share - target_dup) > 0.01:
        violations.append(
            f"duplicate share {dup_share:.3%} deviates from target {target_dup:.3%} by more than 1pp"
        )

    clean = df[~df["is_anomaly"]]
    physical = (
        clean["station_power_kw"] * (clean["duration_minutes"] / 60.0) * EFFICIENCY
    )
    excess = clean[clean["energy_kwh"] > physical * 1.001]
    if len(excess):
        violations.append(
            f"{len(excess)} clean rows exceed energy <= power*duration*efficiency"
        )

    bad_types = set(clean["session_type"]) - set(config.SESSION_TYPE_PRIORS)
    if bad_types:
        violations.append(f"unexpected session_type values: {sorted(bad_types)}")

    if clean["started_at"].isna().any() or clean["ended_at"].isna().any():
        violations.append("clean rows contain unparseable timestamps")
    else:
        reversed_ts = clean[clean["ended_at"] < clean["started_at"]]
        if len(reversed_ts):
            violations.append(
                f"{len(reversed_ts)} clean rows have ended_at < started_at"
            )
    return violations


def build_report(df: pd.DataFrame) -> str:
    total = len(df)
    lines: list[str] = [
        "=" * 60,
        "VALIDATION REPORT",
        "=" * 60,
        f"rows:             {total}",
        f"duplicates:       {df['session_id'].duplicated().mean():.2%}",
        f"zero sessions:    {((df['energy_kwh'] == 0) | (df['duration_minutes'] == 0)).mean():.2%}",
        f"corrupted:        {df['is_anomaly'].mean():.2%}",
        "",
        "energy_kwh by session_type (clean rows):",
        _energy_stats(df).to_string(),
        "",
        "top-5 busiest stations:",
        _top_stations(df).to_string(),
        "",
        f"timestamp range:  {df['started_at'].min()}  ..  {df['started_at'].max()}",
    ]
    return "\n".join(lines)


def run_validation(path: str, target_rows: int, anomaly_rate: float) -> bool:
    """Print the report; return False (and log errors) if invariants are violated."""
    df = load_sessions(path)
    df["is_anomaly"] = df["is_anomaly"].astype(str).str.lower() == "true"
    print(build_report(df))
    violations = check_invariants(df, target_rows, anomaly_rate)
    if violations:
        for violation in violations:
            logger.error("INVARIANT VIOLATED: %s", violation)
        return False
    logger.info("Validation passed: all invariants hold.")
    return True
