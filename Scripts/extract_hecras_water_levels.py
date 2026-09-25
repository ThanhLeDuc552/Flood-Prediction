"""
Extract HEC-RAS 2D unsteady water levels at coordinate pairs.

Inputs
------
1. HEC-RAS .hdf output file, e.g.:
   danangfloodmodel.p05.hdf

2. Coordinate CSV containing at least:
   latitude,longitude

   Optional:
   id
   Example:
       id,latitude,longitude
       P01,16.0542,108.2022
       P02,16.0610,108.2150

Output
------
One CSV per coordinate, all placed in one output folder.

Each CSV contains:
    latitude,longitude,datetime,water_level_m

The script extracts the nearest HEC-RAS 2D cell to each coordinate and
interpolates/resamples the available output to exactly 1-hour intervals.

IMPORTANT:
- HEC-RAS stores projected X/Y coordinates, not necessarily latitude/longitude.
- Set HEC_RAS_CRS below to the CRS used by the HEC-RAS geometry/model.
- The coordinate CSV is assumed to be WGS84 latitude/longitude (EPSG:4326).
"""

from pathlib import Path
import re
import argparse

import h5py
import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree


# ============================================================
# USER SETTINGS
# ============================================================

# CRS of the coordinates in your HEC-RAS geometry.
# CHANGE THIS if your HEC-RAS model uses another CRS.
#
# Examples:
#   EPSG:32648  = WGS84 / UTM zone 48N
#   EPSG:32748  = WGS84 / UTM zone 48S
#   EPSG:6959   = VN-2000 / UTM zone 48N (Da Nang area)
#
HEC_RAS_CRS = "EPSG:2045"

INPUT_HDF = r"danangfloodmodel.p05.hdf"
COORDINATE_FILE = r"d:\weather_pred\datasets\openmeteo api\unique_coordinates.csv"
OUTPUT_FOLDER = r"d:\weather_pred\datasets\openmeteo api\water_level_points"

# Coordinate CSV column names
LAT_COLUMN = "lat"
LON_COLUMN = "lon"

ID_COLUMN = "id"

# Target timestep
TARGET_FREQUENCY = "1h"

# ============================================================
# HEC-RAS HDF helpers
# ============================================================


def print_hdf_structure(hdf):
    """Print HDF structure for debugging if expected paths are not found."""
    print("\nHDF5 structure:")

    def visitor(name, obj):
        print(name)

    hdf.visititems(visitor)


def find_dataset(hdf, possible_names):
    """
    Recursively find dataset whose final path component matches one of possible names,
    prioritized by the order in possible_names.
    """
    target_names = [x.lower() for x in possible_names]
    matched_by_priority = {name: [] for name in target_names}

    def visitor(name, obj):
        if isinstance(obj, h5py.Dataset):
            basename = name.split("/")[-1].lower()
            if basename in matched_by_priority:
                matched_by_priority[basename].append(name)

    hdf.visititems(visitor)

    for target in target_names:
        if matched_by_priority[target]:
            items = matched_by_priority[target]
            items.sort(
                key=lambda x: (
                    "2d flow areas" not in x.lower(),
                    "base output" not in x.lower(),
                    "unsteady" not in x.lower(),
                    len(x),
                )
            )
            return items[0]

    return None


def find_coordinate_dataset(hdf):
    """
    Try to locate the HEC-RAS 2D cell-center coordinate dataset.

    HEC-RAS versions can use slightly different HDF layouts, so this
    searches by dataset name. Cell centers are prioritized over face points.
    """
    candidates = [
        "Cells Center Coordinate",
        "Cells Center Coordinates",
        "Cell Center Coordinate",
        "Cell Center Coordinates",
        "Cell Centers",
        "FacePoints Coordinate",
        "FacePoints Coordinates",
        "2D Face Center Coordinate",
        "2D Face Center Coordinates",
    ]

    path = find_dataset(hdf, candidates)

    if path:
        return path

    # More permissive fallback
    matches = []

    def visitor(name, obj):
        if isinstance(obj, h5py.Dataset):
            low = name.lower()
            if (
                ("coordinate" in low or "coordinates" in low)
                and ("cell" in low or "face" in low)
            ):
                matches.append(name)

    hdf.visititems(visitor)

    if matches:
        matches.sort(
            key=lambda x: (
                "cell" not in x.lower(),
                "center" not in x.lower(),
                len(x),
            )
        )
        return matches[0]

    return None


def find_water_level_dataset(hdf):
    """
    Locate the water-surface elevation dataset.

    HEC-RAS commonly stores 2D results under paths containing
    'Unsteady Time Series' / '2D Flow Areas'.
    """
    candidates = [
        "Water Surface",
        "Water Surface Elevation",
        "Water Surface Elevation (m)",
        "Water Surface (m)",
    ]

    # Prefer datasets under an unsteady-results branch.
    preferred = []

    def visitor(name, obj):
        if isinstance(obj, h5py.Dataset):
            low = name.lower()
            base = name.split("/")[-1].lower()

            if base in {x.lower() for x in candidates}:
                preferred.append(name)

    hdf.visititems(visitor)

    if not preferred:
        return None

    # Prefer 2D-related paths
    preferred.sort(
        key=lambda x: (
            "2d" not in x.lower(),
            "unsteady" not in x.lower(),
            len(x),
        )
    )

    return preferred[0]


def find_time_dataset(hdf):
    """
    Locate the HEC-RAS output time information.
    Prioritize actual datetime stamp datasets over relative numeric 'Time'.
    """
    candidates = [
        "Time Date Stamp",
        "Time Date Stamp (ms)",
        "Date Time",
        "DateTime",
        "Time Dates",
        "Times",
        "Time",
    ]

    return find_dataset(hdf, candidates)


def decode_hdf_strings(values):
    """Convert HDF byte/string arrays to normal Python strings."""
    result = []

    for value in values:
        if isinstance(value, bytes):
            result.append(value.decode("utf-8", errors="ignore"))
        else:
            result.append(str(value))

    return result


def read_times(hdf):
    """
    Read HEC-RAS time values.

    This handles string datetime datasets (e.g. '14OCT2022 00:00:00')
    as well as relative numeric time arrays using simulation start time.
    """
    time_path = find_time_dataset(hdf)

    if time_path is None:
        raise RuntimeError(
            "Could not find a Time/DateTime dataset in the HDF file."
        )

    values = hdf[time_path][()]

    # Flatten simple arrays
    values = np.asarray(values).reshape(-1)

    # If the time dataset is numeric (e.g. relative days or hours)
    if np.issubdtype(values.dtype, np.number):
        start_time = None
        for plan_info_path in [
            "Plan Data/Plan Information",
            "Plan Information",
        ]:
            if plan_info_path in hdf and "Simulation Start Time" in hdf[plan_info_path].attrs:
                raw_start = hdf[plan_info_path].attrs["Simulation Start Time"]
                if isinstance(raw_start, bytes):
                    raw_start = raw_start.decode("utf-8", errors="ignore")
                try:
                    start_time = pd.to_datetime(raw_start, format="%d%b%Y %H:%M:%S")
                except Exception:
                    start_time = pd.to_datetime(raw_start)
                break

        if start_time is not None:
            # HEC-RAS numeric Time is typically days since start
            times = start_time + pd.to_timedelta(values, unit="D")
            return pd.DatetimeIndex(times), time_path

    strings = decode_hdf_strings(values)

    # Try standard HEC-RAS datetime string format first to avoid warnings and speed up parsing
    for fmt in ["%d%b%Y %H:%M:%S", "%d%b%Y %H:%M", "%Y-%m-%d %H:%M:%S"]:
        try:
            times = pd.to_datetime(strings, format=fmt)
            if times.notna().all():
                return pd.DatetimeIndex(times), time_path
        except Exception:
            pass

    # Try normal pandas parsing
    times = pd.to_datetime(strings, errors="coerce")

    if times.notna().all():
        return pd.DatetimeIndex(times), time_path

    raise RuntimeError(
        f"Found time dataset at '{time_path}', but could not parse all "
        "values as datetime."
    )


def get_numeric_water_level(hdf, water_path):
    """Read the water-level result array."""
    data = np.asarray(hdf[water_path][()])

    if not np.issubdtype(data.dtype, np.number):
        raise RuntimeError(
            f"Water-level dataset '{water_path}' is not numeric."
        )

    return data.astype(float)


# ============================================================
# Coordinate handling
# ============================================================


def read_coordinates(path):
    df = pd.read_csv(path)

    required = {LAT_COLUMN, LON_COLUMN}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            f"Coordinate CSV is missing columns: {sorted(missing)}"
        )

    df[LAT_COLUMN] = pd.to_numeric(df[LAT_COLUMN], errors="coerce")
    df[LON_COLUMN] = pd.to_numeric(df[LON_COLUMN], errors="coerce")

    if df[[LAT_COLUMN, LON_COLUMN]].isna().any().any():
        raise ValueError("Coordinate CSV contains invalid latitude/longitude.")

    if ID_COLUMN not in df.columns:
        df[ID_COLUMN] = [f"point_{i+1:04d}" for i in range(len(df))]
    else:
        df[ID_COLUMN] = df[ID_COLUMN].astype(str)

    return df


def transform_coordinates(df):
    """
    Convert WGS84 lat/lon to the projected CRS used by HEC-RAS.
    """
    transformer = Transformer.from_crs(
        "EPSG:4326",
        HEC_RAS_CRS,
        always_xy=True,
    )

    x, y = transformer.transform(
        df[LON_COLUMN].to_numpy(),
        df[LAT_COLUMN].to_numpy(),
    )

    return np.column_stack([x, y])


# ============================================================
# Main extraction
# ============================================================


def extract():

    input_hdf = Path(INPUT_HDF)
    coordinate_file = Path(COORDINATE_FILE)
    output_dir = Path(OUTPUT_FOLDER)

    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_hdf.exists():
        raise FileNotFoundError(
            f"HDF5 file not found: {input_hdf}"
        )

    if not coordinate_file.exists():
        raise FileNotFoundError(
            f"Coordinate file not found: {coordinate_file}"
        )

    print("=" * 70)
    print("HEC-RAS 2D WATER LEVEL EXTRACTION")
    print("=" * 70)

    print(f"HDF5 file       : {input_hdf}")
    print(f"Coordinate file : {coordinate_file}")
    print(f"Output folder   : {output_dir}")
    print(f"HEC-RAS CRS     : {HEC_RAS_CRS}")
    print(f"Output timestep : {TARGET_FREQUENCY}")

    coordinates = read_coordinates(coordinate_file)

    with h5py.File(input_hdf, "r") as hdf:

        # ----------------------------------------------------
        # Locate HEC-RAS datasets
        # ----------------------------------------------------

        print("\nSearching HDF5 structure...")

        coordinate_path = find_coordinate_dataset(hdf)
        water_path = find_water_level_dataset(hdf)

        if coordinate_path is None or water_path is None:
            print("\nCould not automatically identify the required datasets.")
            print_hdf_structure(hdf)

            raise RuntimeError(
                "\nPlease inspect the printed HDF structure and set the "
                "dataset paths manually."
            )

        print(f"\nCoordinate dataset:")
        print(f"  {coordinate_path}")

        print(f"\nWater-level dataset:")
        print(f"  {water_path}")

        # ----------------------------------------------------
        # Read cell coordinates
        # ----------------------------------------------------

        cell_coordinates = np.asarray(
            hdf[coordinate_path][()]
        )

        print(
            f"\nCoordinate array shape: "
            f"{cell_coordinates.shape}"
        )

        # Make sure we have X/Y pairs
        if cell_coordinates.ndim != 2 or cell_coordinates.shape[1] < 2:
            raise RuntimeError(
                "The coordinate dataset does not appear to contain X/Y pairs."
            )

        cell_xy = cell_coordinates[:, :2].astype(float)

        # ----------------------------------------------------
        # Read water-level results
        # ----------------------------------------------------

        water_level = get_numeric_water_level(
            hdf,
            water_path,
        )

        print(
            f"Water-level array shape: "
            f"{water_level.shape}"
        )

        times, time_path = read_times(hdf)

        print(f"Time dataset: {time_path}")
        print(f"Number of timestamps: {len(times)}")

        # ----------------------------------------------------
        # Determine orientation of water-level array
        # ----------------------------------------------------

        n_time = len(times)
        n_cells = len(cell_xy)

        if water_level.ndim == 1:
            if water_level.size == n_time:
                raise RuntimeError(
                    "Water-level dataset is one-dimensional. "
                    "It does not appear to contain spatially distributed "
                    "2D results."
                )

        elif water_level.ndim == 2:

            if water_level.shape == (n_time, n_cells):
                # Correct orientation
                water_level_time_cell = water_level

            elif water_level.shape == (n_cells, n_time):
                # Transpose
                water_level_time_cell = water_level.T

            else:
                raise RuntimeError(
                    "Water-level array shape does not match the number "
                    f"of timestamps ({n_time}) and cells ({n_cells}).\n"
                    f"Actual shape: {water_level.shape}"
                )

        else:
            raise RuntimeError(
                "Water-level dataset has an unsupported number of dimensions: "
                f"{water_level.ndim}"
            )

        # ----------------------------------------------------
        # Build nearest-cell search tree
        # ----------------------------------------------------

        tree = cKDTree(cell_xy)

        point_xy = transform_coordinates(coordinates)

        distances, nearest_indices = tree.query(point_xy)

        print("\nCoordinate matching:")
        for i, row in coordinates.iterrows():
            print(
                f"  {row[ID_COLUMN]}: "
                f"nearest cell = {nearest_indices[i]}, "
                f"distance = {distances[i]:.2f} map units"
            )

        # ----------------------------------------------------
        # Extract each coordinate
        # ----------------------------------------------------

        for i, row in coordinates.iterrows():

            point_id = str(row[ID_COLUMN])

            cell_index = int(nearest_indices[i])

            values = water_level_time_cell[:, cell_index]

            result = pd.DataFrame(
                {
                    "latitude": row[LAT_COLUMN],
                    "longitude": row[LON_COLUMN],
                    "datetime": times,
                    "water_level_m": values,
                }
            )

            # ------------------------------------------------
            # Resample to exactly 1-hour timestep
            # ------------------------------------------------

            result["datetime"] = pd.to_datetime(
                result["datetime"]
            )

            result = result.set_index("datetime")

            # Numeric interpolation onto an exact hourly grid.
            hourly_index = pd.date_range(
                start=result.index.min(),
                end=result.index.max(),
                freq=TARGET_FREQUENCY,
            )

            hourly = (
                result[["water_level_m"]]
                .reindex(hourly_index)
                .interpolate(method="time")
            )

            hourly["latitude"] = row[LAT_COLUMN]
            hourly["longitude"] = row[LON_COLUMN]

            hourly = hourly.reset_index()
            hourly = hourly.rename(
                columns={"index": "datetime"}
            )

            hourly = hourly[
                [
                    "latitude",
                    "longitude",
                    "datetime",
                    "water_level_m",
                ]
            ]

            # ------------------------------------------------
            # Safe filename
            # ------------------------------------------------

            safe_id = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                point_id,
            )

            output_file = output_dir / f"{safe_id}.csv"

            hourly.to_csv(
                output_file,
                index=False,
                float_format="%.6f",
            )

            print(
                f"Saved: {output_file} "
                f"({len(hourly)} hourly records)"
            )

    print("\n" + "=" * 70)
    print("DONE")
    print(f"Output folder: {output_dir.resolve()}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract HEC-RAS 2D water levels at coordinate points."
    )

    parser.add_argument(
        "--hdf",
        default=INPUT_HDF,
        help="HEC-RAS .hdf output file",
    )

    parser.add_argument(
        "--coordinates",
        default=COORDINATE_FILE,
        help="CSV containing latitude and longitude",
    )

    parser.add_argument(
        "--output",
        default=OUTPUT_FOLDER,
        help="Output directory",
    )

    args = parser.parse_args()

    INPUT_HDF = args.hdf
    COORDINATE_FILE = args.coordinates
    OUTPUT_FOLDER = args.output

    extract()
