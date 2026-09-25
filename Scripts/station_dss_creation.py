"""
Generate multi-variable HEC-DSS files from Open-Meteo location time-series CSVs
and generate HEC-RAS station metadata and summary CSVs.

Workflow:
1. Scans 'Data Collections/openmeteo api' for location*.csv files.
2. For each location:
   - Maps index to station name (e.g., location0 -> station_0000).
   - Projects (latitude, longitude) from WGS84 (EPSG:4326) to Vietnam/Da Nang EPSG:2045.
   - Generates 4 DSS files inside 'Data Collections/new DSSs/station_XXXX/':
     * precipitation.dss
     * evapotranspiration.dss
     * wind_speed.dss
     * wind_direction.dss
3. Generates metadata and summary CSV files in 'Data Collections/extras/':
   - stations.csv:
     station_name,gauge_height,latitude,longitude,project_x,project_y
   - precipitation_summary.csv:
     station_name,dss_filename,dss_pathname
   - wind_speed_summary.csv:
     station_name,dss_filename,dss_pathname
   - wind_direction_summary.csv:
     station_name,dss_filename,dss_pathname
   - evapotranspiration_summary.csv:
     station_name,dss_filename,dss_pathname
"""

from pathlib import Path
import argparse
import functools
import os
import re
import sys
import time
import pandas as pd
import pyproj
from pydsstools.heclib.dss import HecDss
from pydsstools.core import TimeSeriesContainer

# Live unbuffered printing for console / background runners
print = functools.partial(print, flush=True)

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent

DEFAULT_INPUT_DIR = WORKSPACE_DIR / "Data Collections" / "openmeteo api"
DEFAULT_OUTPUT_DIR = WORKSPACE_DIR / "Data Collections" / "new DSSs"
DEFAULT_EXTRAS_DIR = WORKSPACE_DIR / "Data Collections" / "extras"

# DSS time parameters
TIME_INTERVAL = "1HOUR"
INTERVAL_MINUTES = 60

# Variable configurations matching HEC-RAS meteorological boundary conditions
VARIABLE_SETTINGS = {
    "precipitation": {
        "summary_csv": "precipitation_summary.csv",
        "filename": "precipitation.dss",
        "dss_part": "PRECIP",
        "unit": "MM",
        "type": "PER-CUM",
    },
    "evapotranspiration": {
        "summary_csv": "evapotranspiration_summary.csv",
        "filename": "evapotranspiration.dss",
        "dss_part": "ET",
        "unit": "MM",
        "type": "PER-CUM",
    },
    "wind_speed_10m": {
        "summary_csv": "wind_speed_summary.csv",
        "filename": "wind_speed.dss",
        "dss_part": "WIND-SPEED",
        "unit": "M/S",
        "type": "INST-VAL",
    },
    "wind_direction_10m": {
        "summary_csv": "wind_direction_summary.csv",
        "filename": "wind_direction.dss",
        "dss_part": "WIND-DIR",
        "unit": "DEG",
        "type": "INST-VAL",
    },
}


def write_station_dss_file(
    station_name: str,
    station_data: pd.DataFrame,
    variable: str,
    output_file: Path,
    skip_existing: bool = False,
) -> str:
    """
    Write or verify a regular time-series DSS file for a given variable and station,
    and return the stored DSS pathname.
    """
    settings = VARIABLE_SETTINGS[variable]
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Expected standard pathname pattern
    fallback_pathname = f"//{station_name}/{settings['dss_part']}//{TIME_INTERVAL}/OBS/"

    # If already exists and skipping requested, query and return existing pathname
    if skip_existing and output_file.exists():
        try:
            with HecDss.Open(str(output_file)) as dss:
                paths = dss.search_path(f"/*/{station_name}/{settings['dss_part']}/*/*/*/")
                if paths:
                    return paths[0]
        except Exception:
            pass

    if variable not in station_data.columns:
        print(f"    [Warning] Column '{variable}' not found in station data.")
        return fallback_pathname

    valid_data = station_data[["date", variable]].dropna()
    if valid_data.empty:
        print(f"    [Warning] No valid data for '{variable}' in {station_name}.")
        return fallback_pathname

    valid_data = valid_data.sort_values("date")
    dates = pd.to_datetime(valid_data["date"])
    values = valid_data[variable].astype(float).tolist()

    start_time = dates.iloc[0].strftime("%d%b%Y %H%M").upper()

    tsc = TimeSeriesContainer(
        fallback_pathname,
        len(values),
        1,  # 1 indicates regular time-series in pydsstools
        values=values,
        start_time=start_time,
        data_units=settings["unit"],
        data_type=settings["type"],
    )

    with HecDss.Open(str(output_file)) as dss:
        dss.put_ts(tsc)

    # Retrieve exact pathname recorded by DSS library
    recorded_pathname = fallback_pathname
    try:
        with HecDss.Open(str(output_file)) as dss:
            paths = dss.search_path(f"/*/{station_name}/{settings['dss_part']}/*/*/*/")
            if paths:
                recorded_pathname = paths[0]
    except Exception as e:
        print(f"    [Warning] Could not inspect recorded pathname for {output_file.name}: {e}")

    return recorded_pathname


def process_all_locations(
    input_dir: Path,
    output_dir: Path,
    extras_dir: Path,
    station_prefix: str = "station_",
    limit: int | None = None,
    skip_existing: bool = False,
):
    """
    Process all location*.csv files, write multi-variable DSS files,
    and save stations.csv and summary CSVs.
    """
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    extras_dir = Path(extras_dir).resolve()

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    extras_dir.mkdir(parents=True, exist_ok=True)

    # Discover and sort location*.csv files
    pattern = re.compile(r"location(\d+)\.csv$", re.IGNORECASE)
    csv_entries = []

    for item in input_dir.iterdir():
        if item.is_file():
            m = pattern.match(item.name)
            if m:
                idx = int(m.group(1))
                csv_entries.append((idx, item))
            elif item.name.endswith(".csv"):
                # Non-indexed CSV fallback
                csv_entries.append((999999, item))

    csv_entries.sort(key=lambda x: x[0])

    if not csv_entries:
        raise ValueError(f"No location*.csv files found in {input_dir}")

    if limit is not None and limit > 0:
        csv_entries = csv_entries[:limit]

    total_stations = len(csv_entries)
    print("=" * 65)
    print("MULTI-STATION DSS & METADATA GENERATOR")
    print("=" * 65)
    print(f"Input location files : {total_stations} (from {input_dir.name})")
    print(f"Target DSS directory : {output_dir}")
    print(f"Target Extras folder : {extras_dir}")
    print(f"Skip existing DSS    : {skip_existing}")
    print("=" * 65 + "\n")

    # Coordinate transformation: WGS84 (EPSG:4326) -> Vietnam EPSG:2045
    transformer = pyproj.Transformer.from_crs(4326, 2045, always_xy=True)

    stations_records = []
    summary_records = {var: [] for var in VARIABLE_SETTINGS}

    t0_all = time.time()

    for pos, (loc_idx, csv_path) in enumerate(csv_entries, start=1):
        if loc_idx != 999999:
            station_name = f"{station_prefix}{loc_idx:04d}"
        else:
            station_name = csv_path.stem

        print(f"[{pos}/{total_stations}] Processing {csv_path.name} -> {station_name}...")

        df = pd.read_csv(csv_path)

        if "latitude" not in df.columns or "longitude" not in df.columns:
            raise ValueError(f"File {csv_path.name} is missing 'latitude' or 'longitude' columns.")

        lat = float(df["latitude"].iloc[0])
        lon = float(df["longitude"].iloc[0])

        # Reproject to EPSG:2045 (always_xy=True takes lon, lat -> X, Y)
        proj_x, proj_y = transformer.transform(lon, lat)

        # Record for stations.csv:
        # station_name,gauge_height,latitude,longitude,project_x,project_y
        stations_records.append({
            "station_name": station_name,
            "gauge_height": 10,
            "latitude": "",
            "longitude": "",
            "project_x": f"{proj_x:.2f}",
            "project_y": f"{proj_y:.2f}",
        })

        # Station output folder: new DSSs/<station_name>/
        station_folder = output_dir / station_name

        # Create 4 DSS files and collect summary metadata
        for var, settings in VARIABLE_SETTINGS.items():
            dss_file = station_folder / settings["filename"]

            recorded_pathname = write_station_dss_file(
                station_name=station_name,
                station_data=df,
                variable=var,
                output_file=dss_file,
                skip_existing=skip_existing,
            )

            summary_records[var].append({
                "station_name": station_name,
                "dss_filename": str(dss_file.resolve()),
                "dss_pathname": recorded_pathname,
            })

    # ============================================================
    # WRITE METADATA AND SUMMARY CSV FILES
    # ============================================================
    print("\n" + "-" * 65)
    print("Writing metadata and summary CSV files...")
    print("-" * 65)

    # 1. stations.csv
    stations_csv_path = extras_dir / "stations.csv"
    df_stations = pd.DataFrame(stations_records)
    df_stations.to_csv(stations_csv_path, index=False)
    print(f"  Exported: {stations_csv_path.name} ({len(df_stations)} rows)")

    # 2. 4 Variable Summary CSVs
    for var, settings in VARIABLE_SETTINGS.items():
        summary_csv_path = extras_dir / settings["summary_csv"]
        df_summary = pd.DataFrame(summary_records[var])
        df_summary.to_csv(summary_csv_path, index=False)
        print(f"  Exported: {summary_csv_path.name} ({len(df_summary)} rows)")

    total_time = time.time() - t0_all
    print("\n" + "=" * 65)
    print("PROCESSING COMPLETE")
    print(f"Total stations processed : {len(stations_records)}")
    print(f"Total DSS files generated: {len(stations_records) * len(VARIABLE_SETTINGS)}")
    print(f"Time elapsed             : {total_time:.2f}s ({total_time / 60:.1f} min)")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Convert Open-Meteo CSV weather datasets to HEC-DSS files and generate HEC-RAS stations metadata."
    )
    parser.add_argument(
        "-i", "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing location*.csv files (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for generated station DSS folders (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "-e", "--extras-dir",
        type=Path,
        default=DEFAULT_EXTRAS_DIR,
        help=f"Directory for stations.csv and summary CSVs (default: {DEFAULT_EXTRAS_DIR})",
    )
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=None,
        help="Optional: limit number of locations to process (e.g. -n 2 for testing)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip re-writing DSS files that already exist on disk",
    )
    parser.add_argument(
        "--station-prefix",
        type=str,
        default="station_",
        help="Prefix for station naming format (default: station_ -> station_0000)",
    )

    args = parser.parse_args()

    try:
        process_all_locations(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            extras_dir=args.extras_dir,
            station_prefix=args.station_prefix,
            limit=args.limit,
            skip_existing=args.skip_existing,
        )
    except Exception as e:
        print(f"\nFatal Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()