"""
Fetch historical meteorological data from Open-Meteo API for HEC-RAS / LSTM modeling.

Workflow:
1. Reads all coordinate points from 'Data Collections/extras/unique_coordinates.csv'.
2. Queries Open-Meteo Historical Forecast API for hourly weather data
   from 2022-10-01 to 2026-08-10:
   - precipitation (mm)
   - evapotranspiration (mm)
   - wind_speed_10m (m/s)
   - wind_direction_10m (deg)
3. Cleans out old event-based folders in 'Data Collections/openmeteo api'.
4. Saves continuous time-series files as 'location{index}.csv' (location0.csv, location1.csv, ...).
5. Implements intelligent batching, rate-limit testing, and automatic 429 cooldown handling.
"""

from pathlib import Path
import argparse
import shutil
import sys
import time
import pandas as pd
import requests
import openmeteo_requests
import requests_cache
from retry_requests import retry


import functools
# Ensure unbuffered live console output for background logs
print = functools.partial(print, flush=True)

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent

DEFAULT_COORD_FILE = WORKSPACE_DIR / "Data Collections" / "extras" / "unique_coordinates.csv"
DEFAULT_OUTPUT_DIR = WORKSPACE_DIR / "Data Collections" / "openmeteo api"

API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
START_DATE = "2022-10-01"
END_DATE = "2026-08-10"

DEFAULT_BATCH_SIZE = 8
DEFAULT_BATCH_DELAY = 4.0  # seconds between successful requests
RATE_LIMIT_COOLDOWN = 65.0  # seconds to sleep when 429 rate limit is hit


# ============================================================
# API CLIENT SETUP
# ============================================================

def get_openmeteo_client():
    """Create and return a cached and retry-enabled Open-Meteo API client."""
    cache_session = requests_cache.CachedSession(".cache", expire_after=86400)
    retry_session = retry(cache_session, retries=5, backoff_factor=0.5)
    return openmeteo_requests.Client(session=retry_session)


# ============================================================
# RATE LIMIT DIAGNOSTICS & TESTING
# ============================================================

def test_api_limits(test_lat: float = 16.005888, test_lon: float = 108.195165):
    """
    Test and report Open-Meteo rate limits, response payload structure,
    and batching behavior.
    """
    print("=" * 65)
    print("OPEN-METEO API RATE LIMIT DIAGNOSTICS")
    print("=" * 65)
    print(f"API Endpoint : {API_URL}")
    print(f"Test Period  : {START_DATE} to {END_DATE} (~33,840 hourly points)")
    print(f"Test Location: ({test_lat:.6f}, {test_lon:.6f})\n")

    # 1. Official Free Tier Guidelines
    print("1. Official Open-Meteo Non-Commercial Limits:")
    print("   - Daily Limit   : 10,000 API calls / day")
    print("   - Hourly Limit  : 5,000 API calls / hour")
    print("   - Minutely Limit: 600 API calls / minute")
    print("   - Multi-location: Each location in a batch counts as 1 call.")
    print("   - Weight penalty: Queries spanning >1,000 days incur heavy compute cost.")
    print()

    # 2. Probe direct response and headers
    print("2. Testing Single Location Query...")
    t0 = time.time()
    params = {
        "latitude": test_lat,
        "longitude": test_lon,
        "start_date": "2022-10-01",
        "end_date": "2022-10-07",
        "hourly": ["precipitation", "evapotranspiration", "wind_speed_10m", "wind_direction_10m"],
        "models": "best_match"
    }

    try:
        resp = requests.get(API_URL, params=params, timeout=15)
        dt = time.time() - t0
        print(f"   HTTP Status: {resp.status_code} ({dt:.2f}s)")
        print(f"   Response Headers:")
        relevant_headers = [
            "date", "content-type", "content-length", "x-ratelimit-limit",
            "x-ratelimit-remaining", "retry-after"
        ]
        for h in relevant_headers:
            if h in resp.headers:
                print(f"     {h}: {resp.headers[h]}")

        if resp.status_code == 200:
            data = resp.json()
            elevation = data.get("elevation", "N/A")
            gen_time = data.get("generationtime_ms", "N/A")
            print(f"   Response Details: Elevation={elevation}m, GenTime={gen_time}ms")
        elif resp.status_code == 429:
            print(f"   Rate Limit Reached: {resp.text}")
    except Exception as e:
        print(f"   Single probe failed: {e}")

    # 3. Test Full Multi-Year Query via SDK
    print("\n3. Testing 4-Year Full Range Query (2022-10-01 to 2026-08-10)...")
    client = get_openmeteo_client()
    full_params = {
        "latitude": [test_lat],
        "longitude": [test_lon],
        "start_date": START_DATE,
        "end_date": END_DATE,
        "hourly": ["precipitation", "evapotranspiration", "wind_speed_10m", "wind_direction_10m"],
        "models": "best_match"
    }
    t0 = time.time()
    try:
        results = client.weather_api(API_URL, params=full_params)
        dt = time.time() - t0
        hourly = results[0].Hourly()
        total_pts = len(hourly.Variables(0).ValuesAsNumpy())
        print(f"   Full-range query SUCCESS in {dt:.2f}s!")
        print(f"   Returned {total_pts:,} hourly timestamps per variable.")
        print(f"   Estimated payload size: ~{total_pts * 4 * 4 / 1024:.1f} KB binary (FlatBuffers)")
    except Exception as e:
        print(f"   Full-range query failed: {e}")

    print("\n4. Throttling & Batching Recommendations:")
    print(f"   - Optimal batch size: {DEFAULT_BATCH_SIZE} locations per batch.")
    print(f"   - For 428 locations: ceil(428 / {DEFAULT_BATCH_SIZE}) = {-( -428 // DEFAULT_BATCH_SIZE)} batches.")
    print(f"   - Inter-batch delay : {DEFAULT_BATCH_DELAY}s (prevents minutely compute limit).")
    print(f"   - 429 Auto-recovery : Sleep {RATE_LIMIT_COOLDOWN}s on HTTP 429 before retrying.")
    print("=" * 65 + "\n")


# ============================================================
# DIRECTORY CLEANUP
# ============================================================

def clean_output_dir(output_dir: Path):
    """
    Remove all existing files and subdirectories inside output_dir,
    leaving a clean, empty directory.
    """
    output_dir = Path(output_dir)
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)
        return

    print(f"Cleaning existing contents in: {output_dir.resolve()}")
    items = list(output_dir.iterdir())
    removed_files = 0
    removed_dirs = 0

    for item in items:
        try:
            if item.is_dir():
                shutil.rmtree(item)
                removed_dirs += 1
            else:
                item.unlink()
                removed_files += 1
        except Exception as e:
            print(f"  Warning: could not remove {item.name}: {e}")

    print(f"  Cleaned {removed_dirs} directories and {removed_files} files.")


# ============================================================
# MAIN DATA EXTRACTION
# ============================================================

def fetch_and_save_locations(
    coord_file: Path,
    output_dir: Path,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    delay_between_batches: float = DEFAULT_BATCH_DELAY,
    max_locations: int | None = None,
    clean: bool = True,
    skip_existing: bool = False,
):
    """
    Fetch all locations in batches and save individual location{index}.csv files.
    """
    coord_file = Path(coord_file).resolve()
    output_dir = Path(output_dir).resolve()

    if not coord_file.exists():
        raise FileNotFoundError(f"Coordinate file not found: {coord_file}")

    coords_df = pd.read_csv(coord_file)
    if "lat" not in coords_df.columns or "lon" not in coords_df.columns:
        raise ValueError(f"Coordinate file must contain 'lat' and 'lon' columns. Found: {list(coords_df.columns)}")

    if max_locations is not None and max_locations > 0:
        coords_df = coords_df.iloc[:max_locations].copy()

    total_locations = len(coords_df)
    print(f"Loaded {total_locations} coordinates from: {coord_file.name}")
    print(f"Target date range: {start_date} to {end_date}")
    print(f"Output directory : {output_dir}")
    print(f"Batch size       : {batch_size}")
    print(f"Batch delay      : {delay_between_batches}s")

    if clean:
        clean_output_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    client = get_openmeteo_client()

    total_batches = -(-total_locations // batch_size)
    saved_count = 0
    start_time_all = time.time()

    print("\nStarting Open-Meteo download pipeline...")
    print("=" * 65)

    for batch_idx in range(total_batches):
        batch_start_idx = batch_idx * batch_size
        batch_end_idx = min(batch_start_idx + batch_size, total_locations)

        indices = list(range(batch_start_idx, batch_end_idx))

        # Check if all files in this batch already exist
        if skip_existing:
            all_exist = all((output_dir / f"location{idx}.csv").exists() for idx in indices)
            if all_exist:
                print(f"[Batch {batch_idx + 1}/{total_batches}] Locations {batch_start_idx}..{batch_end_idx - 1} already exist. Skipping.")
                saved_count += len(indices)
                continue

        batch_lats = coords_df.loc[indices, "lat"].astype(float).tolist()
        batch_lons = coords_df.loc[indices, "lon"].astype(float).tolist()

        params = {
            "latitude": batch_lats,
            "longitude": batch_lons,
            "start_date": start_date,
            "end_date": end_date,
            "hourly": ["precipitation", "evapotranspiration", "wind_speed_10m", "wind_direction_10m"],
            "models": "best_match"
        }

        print(f"\n[Batch {batch_idx + 1}/{total_batches}] Requesting locations {batch_start_idx} to {batch_end_idx - 1} ({len(indices)} locs)...")

        # Retry loop for rate-limiting (429)
        max_retries = 5
        responses = None

        for attempt in range(1, max_retries + 1):
            try:
                t0 = time.time()
                responses = client.weather_api(API_URL, params=params)
                elapsed = time.time() - t0
                print(f"  Received {len(responses)} responses in {elapsed:.2f}s.")
                break
            except Exception as e:
                err_str = str(e)
                if "limit exceeded" in err_str.lower() or "429" in err_str:
                    print(f"  [Rate Limit Hit] {err_str}")
                    print(f"  Cooling down for {RATE_LIMIT_COOLDOWN}s before retry {attempt}/{max_retries}...")
                    time.sleep(RATE_LIMIT_COOLDOWN)
                else:
                    print(f"  [Error] {e} (attempt {attempt}/{max_retries})")
                    time.sleep(5 * attempt)

        if responses is None or len(responses) != len(indices):
            print(f"  Failed to retrieve batch {batch_idx + 1}. Skipping.")
            continue

        # Process and save each location's dataframe
        for i, response in enumerate(responses):
            loc_index = indices[i]
            hourly = response.Hourly()

            time_index = pd.date_range(
                start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
                end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
                freq=pd.Timedelta(seconds=hourly.Interval()),
                inclusive="left"
            )

            hourly_data = {
                "date": time_index,
                "latitude": batch_lats[i],
                "longitude": batch_lons[i],
                "precipitation": hourly.Variables(0).ValuesAsNumpy(),
                "evapotranspiration": hourly.Variables(1).ValuesAsNumpy(),
                "wind_speed_10m": hourly.Variables(2).ValuesAsNumpy(),
                "wind_direction_10m": hourly.Variables(3).ValuesAsNumpy()
            }

            df_location = pd.DataFrame(data=hourly_data)
            output_csv_path = output_dir / f"location{loc_index}.csv"
            df_location.to_csv(output_csv_path, index=False)
            saved_count += 1

        print(f"  Saved location{indices[0]}.csv through location{indices[-1]}.csv ({len(time_index):,} rows each)")

        # Polite delay to prevent minutely rate limit
        if batch_idx < total_batches - 1 and delay_between_batches > 0:
            time.sleep(delay_between_batches)

    total_time = time.time() - start_time_all
    print("\n" + "=" * 65)
    print("DOWNLOAD PIPELINE COMPLETED")
    print("=" * 65)
    print(f"Total locations processed: {saved_count}/{total_locations}")
    print(f"Output directory         : {output_dir.resolve()}")
    print(f"Total time elapsed       : {total_time / 60:.1f} minutes ({total_time:.1f}s)")
    print("=" * 65)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Fetch multi-year Open-Meteo weather data for all 2D flow area coordinates."
    )
    parser.add_argument(
        "--coord-file",
        "-c",
        type=str,
        default=str(DEFAULT_COORD_FILE),
        help=f"Path to unique_coordinates.csv (default: {DEFAULT_COORD_FILE})",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Target output directory for location*.csv (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=START_DATE,
        help=f"Start date YYYY-MM-DD (default: {START_DATE})",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=END_DATE,
        help=f"End date YYYY-MM-DD (default: {END_DATE})",
    )
    parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Number of locations per API request (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--delay",
        "-d",
        type=float,
        default=DEFAULT_BATCH_DELAY,
        help=f"Delay in seconds between API batches (default: {DEFAULT_BATCH_DELAY}s)",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=None,
        help="Optional: Limit total locations to download (e.g. -n 5 for a test run)",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not wipe existing contents in output-dir before downloading",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip locations whose CSV file already exists",
    )
    parser.add_argument(
        "--test-limits",
        action="store_true",
        help="Run API rate limit diagnostics and exit",
    )

    args = parser.parse_args()

    if args.test_limits:
        test_api_limits()
        return

    try:
        fetch_and_save_locations(
            coord_file=Path(args.coord_file),
            output_dir=Path(args.output_dir),
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            delay_between_batches=args.delay,
            max_locations=args.limit,
            clean=not args.no_clean,
            skip_existing=args.skip_existing,
        )
    except Exception as e:
        print(f"\nFatal Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
