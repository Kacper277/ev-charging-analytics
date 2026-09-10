"""Job A - fetch charging stations from the Open Charge Map API."""

from __future__ import annotations

import csv
import logging
import os
import random
import time
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)

CsvRow = dict[str, Any]


class MissingApiKeyError(RuntimeError):
    """Raised when OCM_API_KEY is not set."""


class OcmAuthError(RuntimeError):
    """Raised on 401/403 from the OCM API."""


class OcmFetchError(RuntimeError):
    """Raised when the API keeps failing after retries."""


def require_api_key() -> str:
    key = os.environ.get(config.OCM_API_KEY_ENV, "").strip()
    if not key:
        raise MissingApiKeyError(
            f"Environment variable {config.OCM_API_KEY_ENV} is not set. "
            "Get a free key at https://www.openchargemap.org/develop/api "
            f"and export it: export {config.OCM_API_KEY_ENV}=<your-key>"
        )
    return key


def _station_tier(power_kw: float) -> str:
    for tier, low, high in config.TIER_BOUNDS:
        if low < power_kw <= high:
            return tier
    return "destination"


def normalize_station(poi: dict[str, Any], rng: random.Random) -> CsvRow:
    """Normalize a raw OCM POI payload into our station schema."""
    address = poi.get("AddressInfo") or {}
    connections: list[dict[str, Any]] = poi.get("Connections") or []

    power_kw: float | None = None
    for conn in connections:
        value = conn.get("PowerKW")
        if isinstance(value, (int, float)) and value > 0:
            power_kw = max(power_kw, float(value)) if power_kw else float(value)

    if power_kw is None:
        # Heuristic fallback by charger-type tier.
        joined = " ".join(
            str(c.get("ConnectionType") or {}).upper() for c in connections
        )
        if any(k in joined for k in ("DC", "CHADEMO", "CCS", "TESLA")):
            power_kw = config.TIER_FALLBACK_POWER_KW["dc"]
        elif any(k in joined for k in ("TYPE 2", "MENNEKES", "IEC 62196")):
            power_kw = config.TIER_FALLBACK_POWER_KW["destination"]
        else:
            power_kw = config.TIER_FALLBACK_POWER_KW["home"]
        power_kw *= 1.0 + rng.uniform(-0.1, 0.1)
        power_kw = round(power_kw, 1)

    operator = (poi.get("OperatorInfo") or {}).get("Title") or "unknown"
    town = address.get("Town") or ""
    address_line = address.get("AddressLine1") or address.get("Title") or ""

    return {
        "station_id": str(poi.get("ID", "")),
        "power_kw": power_kw,
        "tier": _station_tier(power_kw),
        "operator": operator,
        "latitude": address.get("Latitude"),
        "longitude": address.get("Longitude"),
        "address": address_line,
        "city": town,
    }


def _get_with_retry(
    session: requests.Session, params: dict[str, Any]
) -> list[dict[str, Any]]:
    delay = config.OCM_BACKOFF_BASE_SECONDS
    last_error = ""
    for attempt in range(1, config.OCM_MAX_RETRIES + 1):
        try:
            response = session.get(config.OCM_BASE_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if response.status_code in (401, 403):
                raise OcmAuthError(
                    f"OCM API returned {response.status_code}. Check that "
                    f"{config.OCM_API_KEY_ENV} contains a valid API key."
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
            else:
                response.raise_for_status()
                return response.json()
        if attempt < config.OCM_MAX_RETRIES:
            logger.warning(
                "OCM request failed (%s), retrying in %.1fs (attempt %d/%d)",
                last_error,
                delay,
                attempt,
                config.OCM_MAX_RETRIES,
            )
            time.sleep(delay)
            delay *= 2
    raise OcmFetchError(
        f"OCM API failed after {config.OCM_MAX_RETRIES} attempts (last error: {last_error}). "
        "OCM is rate-limited; try again later or use --synthetic-stations for Job B."
    )


def fetch_stations(
    api_key: str,
    country: str,
    limit: int,
    rng: random.Random,
    session: requests.Session | None = None,
) -> list[CsvRow]:
    """Fetch and normalize up to ``limit`` stations for ``country``."""
    http = session or requests.Session()
    stations: list[CsvRow] = []
    offset = 0
    while len(stations) < limit:
        page_size = min(config.OCM_PAGE_SIZE, limit - len(stations))
        payload = _get_with_retry(
            http,
            {
                "key": api_key,
                "countrycode": country,
                "maxresults": page_size,
                "offset": offset,
                "compact": True,
                "verbose": False,
            },
        )
        if not payload:
            break
        for poi in payload:
            stations.append(normalize_station(poi, rng))
        offset += len(payload)
        if len(payload) < page_size:
            break
    return stations


def make_synthetic_stations(count: int, rng: random.Random) -> list[CsvRow]:
    """Generate deterministic fake stations so Job B can run standalone."""
    # Imported lazily: faker is only needed for the synthetic fallback.
    from faker import Faker

    fake = Faker()
    # Derive the faker seed from the pipeline rng for full determinism.
    fake.seed_instance(rng.randrange(2**31))
    Faker.seed(rng.randrange(2**31))

    tiers = ["home", "destination", "dc"]
    stations: list[CsvRow] = []
    for i in range(count):
        tier = rng.choices(tiers, weights=[0.55, 0.30, 0.15], k=1)[0]
        if tier == "home":
            power = round(rng.uniform(3.6, 11.0), 1)
        elif tier == "destination":
            power = round(rng.uniform(11.0, 22.0), 1)
        else:
            power = round(rng.uniform(50.0, 150.0), 1)
        stations.append(
            {
                "station_id": f"SYN-{i:06d}",
                "power_kw": power,
                "tier": _station_tier(power),
                "operator": fake.company(),
                "latitude": float(f"{fake.latitude():.6f}"),
                "longitude": float(f"{fake.longitude():.6f}"),
                "address": fake.street_address(),
                "city": fake.city(),
            }
        )
    return stations


def write_stations_csv(stations: list[CsvRow], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fields = [
        "station_id",
        "power_kw",
        "tier",
        "operator",
        "latitude",
        "longitude",
        "address",
        "city",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(stations)


def read_stations_csv(path: str) -> list[CsvRow]:
    """Read the normalized stations snapshot; raises on unreadable input."""
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = [dict(r) for r in csv.DictReader(handle)]
    except OSError as exc:
        raise OSError(
            f"Cannot read stations file '{path}': {exc}. "
            "Run `fetch-stations` first or pass --synthetic-stations N."
        ) from exc
    if not rows:
        raise ValueError(f"Stations file '{path}' is empty.")
    for row in rows:
        row["power_kw"] = float(row["power_kw"])
    return rows


def run_fetch(country: str, limit: int, out_path: str, seed: int) -> int:
    """CLI entrypoint for Job A. Returns number of stations written."""
    started = time.perf_counter()
    api_key = require_api_key()
    rng = random.Random(seed)
    logger.info("Fetching up to %d stations for country=%s", limit, country)
    try:
        stations = fetch_stations(api_key, country, limit, rng)
    except OcmFetchError as exc:
        logger.error("%s", exc)
        stations = []

    if not stations:
        logger.warning(
            "No stations fetched (empty result or rate limited). "
            "Snapshot not written. Job B can run standalone with "
            "`generate-sessions --synthetic-stations N`."
        )
        return 0

    write_stations_csv(stations, out_path)
    duration = time.perf_counter() - started
    logger.info(
        "Fetched %d raw stations, wrote %d normalized stations to %s in %.1fs",
        len(stations),
        len(stations),
        out_path,
        duration,
    )
    return len(stations)
