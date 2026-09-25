"""
Extract 2D flow area coordinates from a HEC-RAS geometry HDF file.

This script:
1. Reads in the HEC-RAS geometry HDF5 file (e.g. danangfloodmodel.g01.hdf).
2. Extracts the boundary/perimeter coordinates of all 2D Flow Areas
   (e.g., Perimeter 1, Perimeter 2).
3. Exports and overwrites the target CSV file (e.g., flow_area_coords.csv).

Default paths:
- Input HDF : d:\\flood prediction\\hec-ras model\\danangfloodmodel.g01.hdf
- Output CSV: d:\\flood prediction\\Data Collections\\extras\\flow_area_coords.csv
"""

from pathlib import Path
import argparse
import sys
import h5py
import numpy as np
import pandas as pd


# ============================================================
# DEFAULT PATH RESOLUTION
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent

DEFAULT_HDF_PATH = WORKSPACE_DIR / "hec-ras model" / "danangfloodmodel.g01.hdf"
DEFAULT_OUTPUT_CSV = WORKSPACE_DIR / "Data Collections" / "extras" / "flow_area_coords.csv"


def extract_flow_area_coordinates(
    hdf_path: Path,
    output_csv: Path,
    flow_area_filter: str = "all",
    include_area_column: bool = True,
    use_xy_header: bool = False,
    reproject_wgs84: bool = False,
    epsg_source: int = 2045,
) -> pd.DataFrame:
    """
    Extract 2D Flow Area coordinates from HEC-RAS geometry HDF5 file
    and write to CSV.
    """
    hdf_path = Path(hdf_path)
    output_csv = Path(output_csv)

    if not hdf_path.exists():
        raise FileNotFoundError(f"HEC-RAS geometry HDF file not found: {hdf_path}")

    print(f"Opening HEC-RAS HDF file: {hdf_path.resolve()}")

    with h5py.File(hdf_path, "r") as f:
        geom_path = "Geometry/2D Flow Areas"
        if geom_path not in f:
            raise KeyError(f"'{geom_path}' group not found in {hdf_path}")

        g_flow = f[geom_path]

        # 1. Discover 2D Flow Area names
        flow_area_names = []
        if "Attributes" in g_flow:
            attrs = g_flow["Attributes"]
            if "Name" in attrs.dtype.names:
                for name_entry in attrs["Name"]:
                    if isinstance(name_entry, bytes):
                        name = name_entry.decode("utf-8", errors="ignore").strip()
                    else:
                        name = str(name_entry).strip()
                    if name:
                        flow_area_names.append(name)

        # Fallback discovery by scanning sub-groups with Perimeter dataset
        if not flow_area_names:
            for k in g_flow.keys():
                if isinstance(g_flow[k], h5py.Group) and "Perimeter" in g_flow[k]:
                    flow_area_names.append(k)

        if not flow_area_names:
            raise ValueError(f"No 2D Flow Areas detected under '{geom_path}'.")

        print(f"Detected {len(flow_area_names)} 2D Flow Area(s): {', '.join(flow_area_names)}")

        # 2. Extract polygon perimeter coordinates for each flow area
        extracted_data = []

        for area_name in flow_area_names:
            if flow_area_filter.lower() != "all" and flow_area_filter.lower() != area_name.lower():
                print(f"  Skipping '{area_name}' (filtered out)")
                continue

            pts = None
            # Method A: Dedicated Perimeter dataset inside the flow area group
            perimeter_dataset_path = f"{area_name}/Perimeter"
            if perimeter_dataset_path in g_flow:
                pts = g_flow[perimeter_dataset_path][:]
            # Method B: Slice from Polygon Points using Polygon Info
            elif "Polygon Points" in g_flow and "Polygon Info" in g_flow:
                poly_pts = g_flow["Polygon Points"][:]
                poly_info = g_flow["Polygon Info"][:]
                idx = flow_area_names.index(area_name)
                if idx < len(poly_info):
                    start_idx = int(poly_info[idx, 0])
                    count = int(poly_info[idx, 1])
                    pts = poly_pts[start_idx : start_idx + count]

            if pts is None or len(pts) == 0:
                print(f"  Warning: No perimeter coordinates found for '{area_name}'")
                continue

            print(f"  Extracted {len(pts):,} coordinate vertices for '{area_name}'")
            print(f"    X range: [{pts[:, 0].min():.3f}, {pts[:, 0].max():.3f}]")
            print(f"    Y range: [{pts[:, 1].min():.3f}, {pts[:, 1].max():.3f}]")

            for x_val, y_val in pts:
                row = {
                    "lat": float(x_val),  # col 0 (Easting / X in EPSG:2045)
                    "lon": float(y_val),  # col 1 (Northing / Y in EPSG:2045)
                }
                if include_area_column:
                    row["flow_area"] = area_name
                extracted_data.append(row)

    if not extracted_data:
        raise ValueError("No coordinate points were extracted.")

    df = pd.DataFrame(extracted_data)

    if use_xy_header:
        df = df.rename(columns={"lat": "x", "lon": "y"})

    # Optional reprojection to WGS84
    if reproject_wgs84:
        try:
            import pyproj
            transformer = pyproj.Transformer.from_crs(epsg_source, 4326, always_xy=True)
            col_x = "x" if use_xy_header else "lat"
            col_y = "y" if use_xy_header else "lon"
            lons, lats = transformer.transform(df[col_x].values, df[col_y].values)
            df["wgs84_lon"] = lons
            df["wgs84_lat"] = lats
            print(f"  Reprojected to WGS84 (EPSG:4326):")
            print(f"    Longitude: [{lons.min():.6f}, {lons.max():.6f}]")
            print(f"    Latitude : [{lats.min():.6f}, {lats.max():.6f}]")
        except ImportError:
            print("  pyproj not installed, skipping WGS84 reprojection columns.")

    # 3. Export to CSV (overwrite)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)
    print(f"\nSuccessfully exported and overwritten:")
    print(f"  Output path: {output_csv.resolve()}")
    print(f"  Total coordinate rows: {len(df):,}")
    print(f"  Columns: {', '.join(df.columns)}")

    return df


def main():
    parser = argparse.ArgumentParser(
        description="Extract 2D Flow Area coordinates from HEC-RAS geometry HDF file to CSV."
    )
    parser.add_argument(
        "--hdf",
        "-i",
        type=str,
        default=str(DEFAULT_HDF_PATH),
        help=f"Path to HEC-RAS .g##.hdf geometry file (default: {DEFAULT_HDF_PATH})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=str(DEFAULT_OUTPUT_CSV),
        help=f"Output CSV path (default: {DEFAULT_OUTPUT_CSV})",
    )
    parser.add_argument(
        "--flow-area",
        "-a",
        type=str,
        default="all",
        help="Flow area name to extract, or 'all' to extract all 2D flow areas (default: 'all')",
    )
    parser.add_argument(
        "--no-area-column",
        action="store_true",
        help="Exclude 'flow_area' column from CSV output (producing strictly lat,lon)",
    )
    parser.add_argument(
        "--xy-header",
        action="store_true",
        help="Use 'x,y' header names instead of 'lat,lon'",
    )
    parser.add_argument(
        "--reproject-wgs84",
        action="store_true",
        help="Also include WGS84 longitude/latitude columns (using pyproj)",
    )
    parser.add_argument(
        "--crs",
        type=int,
        default=2045,
        help="EPSG code of the HEC-RAS model geometry (default: 2045 - Hanoi 1972 / GK Zone 19)",
    )

    args = parser.parse_args()

    try:
        extract_flow_area_coordinates(
            hdf_path=Path(args.hdf),
            output_csv=Path(args.output),
            flow_area_filter=args.flow_area,
            include_area_column=not args.no_area_column,
            use_xy_header=args.xy_header,
            reproject_wgs84=args.reproject_wgs84,
            epsg_source=args.crs,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
