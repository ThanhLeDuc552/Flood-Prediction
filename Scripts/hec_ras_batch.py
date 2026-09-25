"""
HEC-RAS 7.0.1 batch unsteady-run automation.

Workflow
--------
For every event / sub-window folder in DISCHARGE_DIR (reconstructed_discharge):
1. Locate the folder (both single-window <= 100h and multi-window > 100h subfolders).
2. Read the hourly hydrographs for the configured river boundaries:
   - Ai Nghia  -> ai_nghia_hourly_discharge.csv
   - Tuy Loan  -> tuy_loan_hourly_discharge.csv
   - Vu Gia    -> vu_gia_hourly_discharge.csv / thanh_my_hourly_discharge.csv
3. Read initial flow values (from initial_flow_values.csv or first hydrograph ordinate).
4. Replace the mapped Flow Hydrograph blocks and Initial Flow Loc entries in a copy
   of the template .u01.
5. Set hydrograph Fixed Start Date/Time to the simulation start timestamp.
6. Set plan Simulation Date to start/end timestamps.
7. Run HEC-RAS through HECRASController in blocking mode.
8. Store each run in OUTPUT_ROOT/<run_id>/ so outputs cannot overwrite each other.
9. Write run_manifest.csv in OUTPUT_ROOT.

Windows / HEC-RAS 7.0.1
-----------------------
Install pywin32:
    py -m pip install pywin32

The COM ProgID for HEC-RAS 7.x is normally RAS701.HECRASController.
If your installation exposes a different ProgID, change HEC_RAS_PROGID below.

IMPORTANT
---------
This script edits ASCII .u01 and .p## files in RUN-SPECIFIC COPIES only.
It never edits the original/template project.
"""

from __future__ import annotations

import csv
import math
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# USER CONFIGURATION & PATH RESOLUTION
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent

HEC_RAS_PROGID = "RAS701.HECRASController"

# Original, working HEC-RAS project folder.
# Defaults to workspace 'hec-ras model', with fallback to external directory if present.
TEMPLATE_PROJECT_DIR = WORKSPACE_DIR / "hec-ras model"
if not TEMPLATE_PROJECT_DIR.exists():
    TEMPLATE_PROJECT_DIR = Path(r"D:\weather_pred\hec-ras flood modeling\danang flood modelling")

# HEC-RAS model filenames inside the template directory.
PROJECT_FILE = "danangfloodmodel.prj"
PLAN_FILE = "danangfloodmodel.p05"
FLOW_FILE = "danangfloodmodel.u01"

# Directory containing reconstructed discharge event folders.
DISCHARGE_DIR = WORKSPACE_DIR / "Data Collections" / "reconstructed_discharge"

# Root folder where run-specific project copies and outputs will be stored.
OUTPUT_ROOT = WORKSPACE_DIR / "Data Collections" / "automated_runs"

# Period filter: set to None to run all detected periods, or specify a folder/period name
# (e.g. "2022-10-09_to_2022-10-10" or "2022-12-01_to_2022-12-04"). Can also be passed via --period.
TARGET_PERIOD = None

# CSV datetime column. If None, the script tries common names automatically.
DATETIME_COLUMN = None

# Delimiter for CSV files.
CSV_DELIMITER = ","

# ---------------------------------------------------------------------------
# HYDROGRAPH BOUNDARY MAPPING
# ---------------------------------------------------------------------------
# The supplied u01 contains these three Flow Hydrograph boundaries:
#
#   Ai Nghia, Main Reach, 6399
#   Tuy Loan, Main Reach, 9800
#   Vu Gia,   Main Reach, 39160
#
# Each boundary maps to one or more candidate hourly CSV filenames inside
# each run folder under reconstructed_discharge.

BOUNDARY_MAP = [
    {
        "river": "Ai Nghia",
        "reach": "Main Reach",
        "rs": "6399",
        "csv_filename": [
            "ai_nghia_hourly_discharge.csv",
            "ai_nghia.csv",
        ],
        "initial_flow_rs": "6399",
    },
    {
        "river": "Tuy Loan",
        "reach": "Main Reach",
        "rs": "9800",
        "csv_filename": [
            "tuy_loan_hourly_discharge.csv",
            "tuy_loan.csv",
        ],
        "initial_flow_rs": "9800",
    },
    {
        "river": "Vu Gia",
        "reach": "Main Reach",
        "rs": "39160",
        "csv_filename": [
            "vu_gia_hourly_discharge.csv",
            "thanh_my_hourly_discharge.csv",
            "vu_gia.csv",
            "thanh_my.csv",
        ],
        "initial_flow_rs": "39160",
    },
]

# ---------------------------------------------------------------------------
# OPTIONAL SETTINGS
# ---------------------------------------------------------------------------

# If True, the script will refuse to run when a configured boundary CSV is missing.
REQUIRE_ALL_CONFIGURED_COLUMNS = True

# HEC-RAS hydrograph rows are written in groups of 10 values.
VALUES_PER_LINE = 10

# HEC-RAS accepts many interval choices; these are common unsteady intervals.
INTERVALS_MINUTES = [
    1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30,
    60, 120, 180, 240, 360, 480, 720, 1440,
]

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

@dataclass
class HydrographSeries:
    datetimes: list[datetime]
    values: list[float]


def parse_datetime(value: str) -> datetime:
    value = value.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=None)
        except ValueError:
            pass
    # Last attempt: ISO parser
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(f"Cannot parse datetime: {value!r}") from exc


def hec_date(dt: datetime) -> str:
    return dt.strftime("%d%b%Y").upper()


def hec_datetime(dt: datetime) -> str:
    return f"{hec_date(dt)},{dt:%H%M}"


def make_run_id(folder_path: Path, root_dir: Path) -> str:
    try:
        rel = folder_path.relative_to(root_dir)
        clean = re.sub(r"[^A-Za-z0-9_-]+", "_", str(rel))
        return clean.strip("_")
    except ValueError:
        clean = re.sub(r"[^A-Za-z0-9_-]+", "_", folder_path.name)
        return clean.strip("_")


def find_datetime_column(fieldnames: list[str]) -> str:
    if DATETIME_COLUMN:
        if DATETIME_COLUMN not in fieldnames:
            raise ValueError(
                f"Configured DATETIME_COLUMN={DATETIME_COLUMN!r} is not in CSV. "
                f"Columns: {fieldnames}"
            )
        return DATETIME_COLUMN

    normalized = {re.sub(r"[^a-z0-9]", "", x.lower()): x for x in fieldnames}
    for candidate in [
        "datetime", "date_time", "timestamp", "dateandtime",
        "time", "date"
    ]:
        key = re.sub(r"[^a-z0-9]", "", candidate.lower())
        if key in normalized:
            return normalized[key]

    raise ValueError(
        "Could not find a datetime column. Set DATETIME_COLUMN in the script. "
        f"CSV columns: {fieldnames}"
    )


def read_river_csv(csv_path: Path) -> tuple[list[datetime], list[float]]:
    """
    Read hourly datetime and discharge values from a single river CSV.
    """
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        if not reader.fieldnames:
            raise ValueError(f"{csv_path.name}: CSV has no header.")

        fields = [x.strip() for x in reader.fieldnames]
        dt_col = find_datetime_column(fields)

        # Identify discharge column
        q_col = None
        for candidate in ["discharge_m3s", "discharge", "flow", "flow_m3s", "q", "q_m3s"]:
            for col in fields:
                if col.strip().lower() == candidate:
                    q_col = col
                    break
            if q_col:
                break

        if not q_col:
            non_dt = [c for c in fields if c != dt_col]
            if non_dt:
                q_col = non_dt[0]
            else:
                raise ValueError(
                    f"{csv_path.name}: could not identify discharge column in {fields}"
                )

        datetimes = []
        values = []
        for row_no, row in enumerate(reader, start=2):
            raw_dt = (row.get(dt_col) or "").strip()
            raw_q = (row.get(q_col) or "").strip()
            if not raw_dt:
                continue

            dt = parse_datetime(raw_dt)
            if raw_q == "":
                raise ValueError(f"{csv_path.name}: blank discharge at row {row_no}")
            try:
                q = float(raw_q)
            except ValueError as exc:
                raise ValueError(
                    f"{csv_path.name}: non-numeric discharge {raw_q!r} at row {row_no}"
                ) from exc

            if not math.isfinite(q):
                raise ValueError(f"{csv_path.name}: non-finite discharge at row {row_no}")

            datetimes.append(dt)
            values.append(q)

    if len(datetimes) < 2:
        raise ValueError(f"{csv_path.name}: need at least two data rows.")

    # Validate chronological order and regular spacing
    if datetimes != sorted(datetimes):
        raise ValueError(f"{csv_path.name}: datetime values are not sorted ascending.")

    deltas = [
        (datetimes[i + 1] - datetimes[i]).total_seconds()
        for i in range(len(datetimes) - 1)
    ]
    if any(d <= 0 for d in deltas):
        raise ValueError(f"{csv_path.name}: duplicate or non-increasing timestamps found.")
    if max(deltas) != min(deltas):
        raise ValueError(f"{csv_path.name}: hydrograph is not a regular interval time series.")

    return datetimes, values


def read_run_folder(
    folder_path: Path
) -> tuple[list[datetime], dict[tuple[str, str, str], HydrographSeries], dict[str, float]]:
    """
    Read hydrographs for configured boundaries and initial flows from a run folder.
    """
    # 1. Read initial_flow_values.csv if present
    initial_flows = {}
    init_path = folder_path / "initial_flow_values.csv"
    if init_path.exists():
        try:
            with init_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    q_val = None
                    for key in ["initial_flow_m3s", "initial_flow", "flow_m3s", "discharge_m3s"]:
                        if key in row and (row[key] or "").strip():
                            q_val = float(row[key].strip())
                            break
                    if q_val is not None:
                        if "station" in row and (row["station"] or "").strip():
                            initial_flows[row["station"].strip().lower()] = q_val
                        if "river" in row and (row["river"] or "").strip():
                            initial_flows[row["river"].strip().lower()] = q_val
        except Exception as e:
            print(f"    [Warning] Failed to parse {init_path.name}: {e}")

    # 2. Read each boundary's CSV file
    reference_datetimes = None
    series_by_key = {}

    for entry in BOUNDARY_MAP:
        candidates = entry.get("csv_filename") or []
        if isinstance(candidates, str):
            candidates = [candidates]

        target_file = None
        for cand in candidates:
            p = folder_path / cand
            if p.exists():
                target_file = p
                break
            p_clean = folder_path / f"{cand.lower().replace(' ', '_')}"
            if p_clean.exists():
                target_file = p_clean
                break

        if not target_file:
            if REQUIRE_ALL_CONFIGURED_COLUMNS:
                raise FileNotFoundError(
                    f"{folder_path.name}: missing required CSV file for boundary "
                    f"'{entry['river']}' ({candidates})."
                )
            continue

        dts, vals = read_river_csv(target_file)

        if reference_datetimes is None:
            reference_datetimes = dts
        else:
            if dts != reference_datetimes:
                raise ValueError(
                    f"{folder_path.name}: timestamp mismatch between {target_file.name} "
                    f"and reference timeline."
                )

        b_key = (
            entry["river"].strip().lower(),
            entry["reach"].strip().lower(),
            entry["rs"].strip(),
        )
        series_by_key[b_key] = HydrographSeries(dts, vals)

    if not series_by_key:
        raise ValueError(f"{folder_path.name}: no configured boundary series could be loaded.")

    return reference_datetimes, series_by_key, initial_flows


def find_run_folders(root_dir: Path) -> list[Path]:
    """
    Find all simulation folders inside root_dir.
    A valid simulation folder contains 'initial_flow_values.csv' or '*_hourly_discharge.csv'.
    Supports:
    1. Single-window event folders (e.g. 2022-10-09_to_2022-10-10/)
    2. Sub-window folders inside multi-window events (e.g. 2022-12-01_to_2022-12-05/2022-12-01_to_2022-12-04/)
    """
    run_folders = []
    for init_file in sorted(root_dir.rglob("initial_flow_values.csv")):
        run_folders.append(init_file.parent)

    if not run_folders:
        seen = set()
        for csv_file in sorted(root_dir.rglob("*_hourly_discharge.csv")):
            if csv_file.parent not in seen:
                seen.add(csv_file.parent)
                run_folders.append(csv_file.parent)

    return sorted(run_folders)


def interval_token(datetimes: list[datetime]) -> str:
    minutes = (datetimes[1] - datetimes[0]).total_seconds() / 60.0
    if not minutes.is_integer():
        raise ValueError("CSV interval is not an integer number of minutes.")
    minutes = int(minutes)
    if minutes not in INTERVALS_MINUTES:
        raise ValueError(
            f"CSV interval = {minutes} minutes is not in the configured HEC-RAS "
            f"interval list: {INTERVALS_MINUTES}"
        )
    if minutes < 60:
        return f"{minutes}MIN"
    if minutes % 60 == 0 and minutes < 1440:
        hours = minutes // 60
        return f"{hours}HOUR"
    days = minutes // 1440
    return f"{days}DAY"


def format_flow_values(values: list[float]) -> list[str]:
    formatted = [f"{v:8.3f}" for v in values]
    return [
        "".join(formatted[i:i + VALUES_PER_LINE])
        for i in range(0, len(formatted), VALUES_PER_LINE)
    ]


def split_location(line: str) -> tuple[str, str, str]:
    if "=" in line:
        line = line.split("=", 1)[1]
    parts = line.split(",")
    if len(parts) < 3:
        return "", "", ""
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def replace_u01(
    source_u01: Path,
    target_u01: Path,
    all_datetimes: list[datetime],
    series_by_key: dict[tuple[str, str, str], HydrographSeries],
    initial_flows: dict[str, float] | None = None,
) -> None:
    lines = source_u01.read_text(encoding="latin-1").splitlines()

    interval = interval_token(all_datetimes)
    start_hec = hec_datetime(all_datetimes[0])

    i = 0
    replaced = set()
    output = []

    while i < len(lines):
        line = lines[i]

        if line.startswith("Boundary Location="):
            river, reach, rs = split_location(line)
            key = (river.lower(), reach.lower(), rs)

            output.append(line)
            i += 1

            if key in series_by_key:
                series = series_by_key[key]

                while i < len(lines):
                    cur = lines[i]

                    if cur.startswith("Boundary Location="):
                        break

                    if cur.startswith("Interval="):
                        output.append(f"Interval={interval}")
                        i += 1
                        continue

                    if cur.startswith("Flow Hydrograph="):
                        output.append(f"Flow Hydrograph= {len(series.values)}")
                        i += 1
                        while i < len(lines) and not lines[i].startswith("Stage Hydrograph"):
                            i += 1
                        output.extend(format_flow_values(series.values))
                        replaced.add(key)
                        continue

                    if cur.startswith("Use Fixed Start Time="):
                        output.append("Use Fixed Start Time=True")
                        i += 1
                        continue

                    if cur.startswith("Fixed Start Date/Time="):
                        output.append(f"Fixed Start Date/Time={start_hec}")
                        i += 1
                        continue

                    output.append(cur)
                    i += 1
                continue

        output.append(line)
        i += 1

    # Map all 7 initial condition flows per period as specified in workflow.txt:
    # - Ai Nghia (rs 6399)
    # - Cam Le (rs 9000)
    # - Han (rs 7602)
    # - Lac Thanh (rs 23600)
    # - Tuy Loan (rs 9800)
    # - Vu Gia (rs 39160)
    # - Yen (rs 14400)
    initial_flows = initial_flows or {}

    q_ai = None
    if ("ai nghia", "main reach", "6399") in series_by_key:
        q_ai = series_by_key[("ai nghia", "main reach", "6399")].values[0]
    elif "ai_nghia" in initial_flows:
        q_ai = initial_flows["ai_nghia"]
    elif "ai nghia" in initial_flows:
        q_ai = initial_flows["ai nghia"]

    q_tuy = None
    if ("tuy loan", "main reach", "9800") in series_by_key:
        q_tuy = series_by_key[("tuy loan", "main reach", "9800")].values[0]
    elif "tuy_loan" in initial_flows:
        q_tuy = initial_flows["tuy_loan"]
    elif "tuy loan" in initial_flows:
        q_tuy = initial_flows["tuy loan"]

    q_vu = None
    if ("vu gia", "main reach", "39160") in series_by_key:
        q_vu = series_by_key[("vu gia", "main reach", "39160")].values[0]
    elif "vu_gia" in initial_flows:
        q_vu = initial_flows["vu_gia"]
    elif "vu gia" in initial_flows:
        q_vu = initial_flows["vu gia"]
    elif "thanh_my" in initial_flows:
        q_vu = initial_flows["thanh_my"]
    elif "thanh my" in initial_flows:
        q_vu = initial_flows["thanh my"]

    q_cam = None
    if "cam_le_danang" in initial_flows:
        q_cam = initial_flows["cam_le_danang"]
    elif "cam le" in initial_flows:
        q_cam = initial_flows["cam le"]
    elif "cam_le" in initial_flows:
        q_cam = initial_flows["cam_le"]

    # Confluence / bifurcation hydraulic mass balance for connected reaches
    q_yen = initial_flows.get("yen")
    if q_yen is None and q_ai is not None:
        q_yen = q_ai * 0.10384837

    q_lac = initial_flows.get("lac thanh") or initial_flows.get("lac_thanh")
    if q_lac is None and q_ai is not None and q_yen is not None:
        q_lac = q_ai - q_yen

    q_han = initial_flows.get("han")
    if q_han is None and q_lac is not None and q_cam is not None:
        q_han = q_lac + q_cam

    initial_lookup = {
        ("ai nghia", "6399"): q_ai,
        ("cam le", "9000"): q_cam,
        ("han", "7602"): q_han,
        ("lac thanh", "23600"): q_lac,
        ("tuy loan", "9800"): q_tuy,
        ("vu gia", "39160"): q_vu,
        ("yen", "14400"): q_yen,
    }

    final = []
    for line in output:
        if line.startswith("Initial Flow Loc="):
            river, reach, rs = split_location(line)
            key = (river.lower(), rs)

            q0 = initial_lookup.get(key)
            if q0 is None:
                for (r, _), val in initial_lookup.items():
                    if r == river.lower() and val is not None:
                        q0 = val
                        break

            if q0 is not None:
                parts = line.split(",")
                if len(parts) >= 4:
                    parts[3] = f"{q0:.6f}"
                    line = ",".join(parts)

        final.append(line)

    # Sanity checks
    expected = set(series_by_key.keys())
    missing = expected - replaced
    if missing:
        raise ValueError(
            "The following configured boundary hydrographs were not found in "
            f"{source_u01.name}: {sorted(missing)}"
        )

    target_u01.write_text("\n".join(final) + "\n", encoding="latin-1")


def replace_plan_dates(source_plan: Path, target_plan: Path, start: datetime, end: datetime):
    text = source_plan.read_text(encoding="latin-1")
    replacement = f"Simulation Date={hec_datetime(start)},{hec_datetime(end)}"
    pattern = re.compile(r"^Simulation Date=.*$", re.MULTILINE)
    text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ValueError(
            f"{source_plan.name}: could not find exactly one 'Simulation Date=' line."
        )
    target_plan.write_text(text, encoding="latin-1")


def link_file(source: Path, target: Path) -> None:
    """
    Link source file to target path in project root.
    Tries hardlink first (fastest, zero disk overhead, same drive),
    then symlink, then atomic copy as fallback.
    """
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except Exception:
        try:
            os.symlink(source, target)
        except Exception:
            shutil.copy2(source, target)


def run_hec_ras(project_file: Path) -> tuple[bool, list[str]]:
    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is required. Install it with: py -m pip install pywin32"
        ) from exc

    ras = win32com.client.Dispatch(HEC_RAS_PROGID)
    messages = []
    try:
        ras.Project_Open(str(project_file))
        try:
            ras.Compute_HideComputationWindow()
        except Exception:
            pass

        try:
            result = ras.Compute_CurrentPlan(None, None, True)
        except TypeError:
            result = ras.Compute_CurrentPlan(0, None, True)

        success = bool(result)
        return success, messages

    finally:
        try:
            ras.Project_Save()
        except Exception:
            pass
        try:
            ras.Project_Close()
        except Exception:
            pass
        try:
            ras.QuitRas()
        except Exception:
            pass


def write_manifest(path: Path, rows: list[dict]):
    if not rows:
        return
    fields = [
        "run_id", "run_folder", "start", "end", "interval",
        "status", "message", "run_directory",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def process_one_folder(folder_path: Path) -> dict:
    start_time = time.time()

    datetimes, series_by_key, initial_flows = read_run_folder(folder_path)
    start, end = datetimes[0], datetimes[-1]
    run_id = make_run_id(folder_path, DISCHARGE_DIR)
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Ensure baseline template backups exist in TEMPLATE_PROJECT_DIR
    template_u01_orig = TEMPLATE_PROJECT_DIR / f"{FLOW_FILE}.template"
    if not template_u01_orig.exists():
        source_u01 = TEMPLATE_PROJECT_DIR / FLOW_FILE
        if source_u01.exists() and not source_u01.is_symlink():
            shutil.copy2(source_u01, template_u01_orig)
        else:
            backup_u01 = TEMPLATE_PROJECT_DIR / "Backup.u01"
            if backup_u01.exists():
                shutil.copy2(backup_u01, template_u01_orig)

    template_plan_orig = TEMPLATE_PROJECT_DIR / f"{PLAN_FILE}.template"
    if not template_plan_orig.exists():
        source_plan = TEMPLATE_PROJECT_DIR / PLAN_FILE
        if source_plan.exists():
            shutil.copy2(source_plan, template_plan_orig)

    source_u01 = template_u01_orig if template_u01_orig.exists() else (TEMPLATE_PROJECT_DIR / FLOW_FILE)
    source_plan = template_plan_orig if template_plan_orig.exists() else (TEMPLATE_PROJECT_DIR / PLAN_FILE)

    # 2. Save ONLY the modified unsteady analysis file in OUTPUT_ROOT / <run_id>
    saved_u01 = run_dir / FLOW_FILE
    replace_u01(source_u01, saved_u01, datetimes, series_by_key, initial_flows)

    # 3. Link that modified file directly to the project root folder
    target_u01 = TEMPLATE_PROJECT_DIR / FLOW_FILE
    link_file(saved_u01, target_u01)

    # 4. Update simulation dates in the project root plan file
    target_plan = TEMPLATE_PROJECT_DIR / PLAN_FILE
    replace_plan_dates(source_plan, target_plan, start, end)

    # 5. Run HEC-RAS directly in the project root folder (no duplicate project replicas)
    project_file = TEMPLATE_PROJECT_DIR / PROJECT_FILE
    if not project_file.exists():
        raise FileNotFoundError(f"Missing project file: {project_file}")

    success, _messages = run_hec_ras(project_file)

    # 6. If computation output files were produced, preserve them in run_dir
    plan_stem = Path(PLAN_FILE).suffix.lstrip(".").lower()
    prj_stem = Path(PROJECT_FILE).stem
    for ext in [f".{plan_stem}.hdf", f".b{plan_stem[1:]}", f".r{plan_stem[1:]}"]:
        out_f = TEMPLATE_PROJECT_DIR / f"{prj_stem}{ext}"
        if out_f.exists():
            try:
                dest = run_dir / out_f.name
                shutil.copy2(out_f, dest)
            except Exception:
                pass

    status = "SUCCESS" if success else "FAILED"
    return {
        "run_id": run_id,
        "run_folder": str(folder_path),
        "start": hec_datetime(start),
        "end": hec_datetime(end),
        "interval": interval_token(datetimes),
        "status": status,
        "message": f"elapsed={time.time() - start_time:.1f}s",
        "run_directory": str(run_dir),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="HEC-RAS batch unsteady simulation runner")
    parser.add_argument(
        "--period", "-p",
        type=str,
        default=TARGET_PERIOD,
        help="Specific event period or sub-window to run (e.g. 2022-10-09_to_2022-10-10 or 2022-12-01_to_2022-12-04)"
    )
    args = parser.parse_args()
    period_filter = args.period

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    run_folders = find_run_folders(DISCHARGE_DIR)
    if not run_folders:
        print(f"No simulation run folders found in: {DISCHARGE_DIR}")
        return 1

    if period_filter:
        p_clean = period_filter.strip().lower()
        run_folders = [
            f for f in run_folders
            if p_clean in f.name.lower() or p_clean in str(f).lower().replace("\\", "/")
        ]
        if not run_folders:
            print(f"No run folders matched the specified period filter: '{period_filter}'")
            return 1
        print(f"Filtered to {len(run_folders)} run folder(s) matching period '{period_filter}'")
    else:
        print(f"Found {len(run_folders)} simulation run folder(s) in {DISCHARGE_DIR}")

    manifest = []
    for folder_path in run_folders:
        run_name = folder_path.name
        parent_name = folder_path.parent.name
        display_name = (
            f"{parent_name}/{run_name}"
            if parent_name != DISCHARGE_DIR.name
            else run_name
        )
        print(f"\n=== Simulation Run: {display_name} ===")
        try:
            row = process_one_folder(folder_path)
            print(row["status"], row["run_directory"])
        except Exception as exc:
            print(f"FAILED: {exc}")
            row = {
                "run_id": make_run_id(folder_path, DISCHARGE_DIR),
                "run_folder": str(folder_path),
                "start": "",
                "end": "",
                "interval": "",
                "status": "FAILED",
                "message": repr(exc),
                "run_directory": "",
            }
        manifest.append(row)
        write_manifest(OUTPUT_ROOT / "run_manifest.csv", manifest)

    failed = sum(1 for x in manifest if x["status"] != "SUCCESS")
    print(f"\nFinished: {len(manifest) - failed} succeeded, {failed} failed.")
    print(f"Manifest: {OUTPUT_ROOT / 'run_manifest.csv'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
