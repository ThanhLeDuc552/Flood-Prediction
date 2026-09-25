"""
Extract unique flood coordinates from Da Nang government API dataset
and filter points that fall within HEC-RAS 2D Flow Areas (Perimeter 1, Perimeter 2, etc.).

Workflow:
1. Load 2D flow area perimeter points from flow_area_coords.csv (defined in EPSG:2045).
2. Reproject each flow area's boundary from EPSG:2045 to EPSG:4326 (WGS84).
3. Build polygons for each flow area and compute their unified coverage.
4. Load flood event coordinates from danang_flood_data.csv / danang_flood_raw.json (EPSG:4326).
5. Query and tag points falling within the 2D flow areas.
6. Export the queried unique coordinate pairs to unique_coordinates.csv.
"""

from pathlib import Path
import argparse
import json
import sys
import pandas as pd
import pyproj
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union


# ============================================================
# DEFAULT PATH CONFIGURATION
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent

DEFAULT_FLOW_AREA_FILE = WORKSPACE_DIR / "Data Collections" / "extras" / "flow_area_coords.csv"
DEFAULT_DATA_DIR = WORKSPACE_DIR / "Data Collections" / "dananggov api"
DEFAULT_OUTPUT_FILE = WORKSPACE_DIR / "Data Collections" / "extras" / "unique_coordinates.csv"


def load_flow_areas(
    flow_area_path: Path,
    source_epsg: int = 2045,
    target_epsg: int = 4326,
):
    """
    Load 2D flow area perimeter points, group by flow_area if multiple perimeters exist,
    reproject from source CRS to WGS84, and return a dictionary of {area_name: Polygon}
    and a unified shapely geometry.
    """
    if not flow_area_path.exists():
        raise FileNotFoundError(f"Could not find flow area file: {flow_area_path.resolve()}")

    flow_df = pd.read_csv(flow_area_path)
    transformer = pyproj.Transformer.from_crs(source_epsg, target_epsg, always_xy=True)

    flow_polygons = {}

    if "flow_area" in flow_df.columns and flow_df["flow_area"].nunique() > 0:
        groups = flow_df.groupby("flow_area", sort=False)
    else:
        groups = [("2D Flow Area", flow_df)]

    print(f"Loading 2D Flow Area perimeters from: {flow_area_path.name}")
    for area_name, group in groups:
        x = group.iloc[:, 0].astype(float).values
        y = group.iloc[:, 1].astype(float).values

        poly_lons, poly_lats = transformer.transform(x, y)
        poly = Polygon(zip(poly_lons, poly_lats))

        if not poly.is_valid:
            poly = poly.buffer(0)

        flow_polygons[area_name] = poly

        b = poly.bounds
        print(f"  [{area_name}] Vertices: {len(group):,}")
        print(f"    Longitude: [{b[0]:.6f}, {b[2]:.6f}]")
        print(f"    Latitude : [{b[1]:.6f}, {b[3]:.6f}]")

    combined_polygon = unary_union(list(flow_polygons.values()))
    cb = combined_polygon.bounds
    print(f"\nUnified 2D Flow Area Coverage ({combined_polygon.geom_type}):")
    print(f"  Longitude: [{cb[0]:.6f}, {cb[2]:.6f}]")
    print(f"  Latitude : [{cb[1]:.6f}, {cb[3]:.6f}]")

    return flow_polygons, combined_polygon


def load_flood_coordinates(data_dir: Path) -> pd.DataFrame:
    """
    Load raw flood coordinate records from CSV or JSON and return unique (lat, lon) pairs.
    """
    csv_file = data_dir / "danang_flood_data.csv"
    json_file = data_dir / "danang_flood_raw.json"

    coord_dfs = []

    if csv_file.exists():
        print(f"\nReading flood records from: {csv_file.name}")
        df_flood = pd.read_csv(csv_file, skipinitialspace=True, on_bad_lines="skip")
        df_flood.columns = df_flood.columns.str.strip()

        if "latitude" in df_flood.columns and "longitude" in df_flood.columns:
            df_flood["latitude"] = pd.to_numeric(df_flood["latitude"], errors="coerce")
            df_flood["longitude"] = pd.to_numeric(df_flood["longitude"], errors="coerce")

            csv_coords = df_flood[["latitude", "longitude"]].dropna().rename(
                columns={"latitude": "lat", "longitude": "lon"}
            )
            coord_dfs.append(csv_coords)

    if json_file.exists() and not coord_dfs:
        print(f"\nReading flood records from: {json_file.name}")
        with open(json_file, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        extracted = []
        for r in raw_data.get("data", []):
            coords = r.get("location", {}).get("coordinates", [])
            if len(coords) >= 2 and coords[0] is not None and coords[1] is not None:
                lon, lat = float(coords[0]), float(coords[1])
                extracted.append({"lat": lat, "lon": lon})

        if extracted:
            json_coords = pd.DataFrame(extracted)
            coord_dfs.append(json_coords)

    if not coord_dfs:
        raise FileNotFoundError(f"No flood coordinates found in: {data_dir.resolve()}")

    all_coords = pd.concat(coord_dfs, ignore_index=True)
    unique_raw_coords = (
        all_coords.drop_duplicates(subset=["lat", "lon"])
        .sort_values(by=["lat", "lon"])
        .reset_index(drop=True)
    )

    print(f"Total raw flood records loaded               : {len(all_coords):,}")
    print(f"Total unique coordinate pairs across province: {len(unique_raw_coords):,}")

    return unique_raw_coords


def filter_and_export_coordinates(
    flow_polygons: dict,
    combined_polygon,
    unique_raw_coords: pd.DataFrame,
    output_file: Path,
    include_area_column: bool = True,
):
    """
    Filter coordinates by checking whether they fall within the 2D flow areas,
    tag which perimeter they belong to, and export to CSV.
    """
    area_matches = []
    for _, row in unique_raw_coords.iterrows():
        pt = Point(row["lon"], row["lat"])
        matched = [name for name, poly in flow_polygons.items() if poly.covers(pt)]
        area_matches.append("; ".join(matched) if matched else None)

    unique_raw_coords = unique_raw_coords.copy()
    unique_raw_coords["flow_area"] = area_matches

    inside_coords = unique_raw_coords[unique_raw_coords["flow_area"].notna()].reset_index(drop=True)
    outside_coords = unique_raw_coords[unique_raw_coords["flow_area"].isna()].reset_index(drop=True)

    print("\n" + "=" * 60)
    print("QUERY RESULTS (HEC-RAS 2D Flow Areas)")
    print("=" * 60)
    print(f"Total unique coordinates across Da Nang : {len(unique_raw_coords):,}")
    for area_name in flow_polygons.keys():
        count_area = sum(1 for m in area_matches if m and area_name in m.split("; "))
        pct_area = (count_area / len(unique_raw_coords)) * 100
        print(f"  - Within {area_name:<20}: {count_area:,} ({pct_area:.1f}%)")

    pct_inside = (len(inside_coords) / len(unique_raw_coords)) * 100
    pct_outside = (len(outside_coords) / len(unique_raw_coords)) * 100
    print(f"Total coordinates WITHIN ANY 2D area    : {len(inside_coords):,} ({pct_inside:.1f}%)")
    print(f"Coordinates OUTSIDE 2D flow areas       : {len(outside_coords):,} ({pct_outside:.1f}%)")
    print("=" * 60)

    # Prepare export dataframe
    if include_area_column:
        export_df = inside_coords[["lat", "lon", "flow_area"]]
    else:
        export_df = inside_coords[["lat", "lon"]]

    output_file.parent.mkdir(parents=True, exist_ok=True)
    export_df.to_csv(output_file, index=False)
    print(f"\nExported {len(export_df):,} queried coordinate pairs to:")
    print(f"  {output_file.resolve()}")
    print(f"  Columns: {', '.join(export_df.columns)}")

    return export_df


def main():
    parser = argparse.ArgumentParser(
        description="Filter Da Nang flood coordinates by HEC-RAS 2D flow area perimeters."
    )
    parser.add_argument(
        "--flow-area-file",
        "-f",
        type=str,
        default=str(DEFAULT_FLOW_AREA_FILE),
        help=f"Path to flow_area_coords.csv (default: {DEFAULT_FLOW_AREA_FILE})",
    )
    parser.add_argument(
        "--data-dir",
        "-d",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help=f"Directory containing dananggov API datasets (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=str(DEFAULT_OUTPUT_FILE),
        help=f"Output CSV path (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--no-area-column",
        action="store_true",
        help="Export strictly 'lat,lon' without 'flow_area' column",
    )
    parser.add_argument(
        "--crs",
        type=int,
        default=2045,
        help="Source EPSG code for flow area coordinates (default: 2045)",
    )

    args = parser.parse_args()

    flow_area_file = Path(args.flow_area_file)
    data_dir = Path(args.data_dir)
    output_file = Path(args.output)

    # Fallback path resolution if default relative paths differ
    if not flow_area_file.exists():
        alt_paths = [
            WORKSPACE_DIR / "Data Collections" / "extras" / "flow_area_coords.csv",
            data_dir / "flow_area_coords.csv",
            Path("../Data Collections/extras/flow_area_coords.csv"),
        ]
        for alt in alt_paths:
            if alt.exists():
                flow_area_file = alt
                break

    try:
        flow_polygons, combined_polygon = load_flow_areas(
            flow_area_path=flow_area_file,
            source_epsg=args.crs,
        )
        unique_raw_coords = load_flood_coordinates(data_dir=data_dir)
        filter_and_export_coordinates(
            flow_polygons=flow_polygons,
            combined_polygon=combined_polygon,
            unique_raw_coords=unique_raw_coords,
            output_file=output_file,
            include_area_column=not args.no_area_column,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()