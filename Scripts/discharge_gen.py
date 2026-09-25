from pathlib import Path
import pandas as pd
import numpy as np


# ============================================================
# CONFIGURATION
# ============================================================

# ------------------------------------------------------------
# Input & Output folders
# ------------------------------------------------------------

METEO_FOLDER = Path(
    r"D:\flood prediction\Data Collections\openmeteo api"
)

DISCHARGE_FOLDER = Path(
    r"D:\flood prediction\Data Collections\discharge data"
)

OUTPUT_FOLDER = Path(
    r"D:\flood prediction\Data Collections\reconstructed_discharge"
)

OUTPUT_FOLDER.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# RIVER CONFIGURATION
# ============================================================

RIVER_FILES = {
    "Ai_Nghia":
        "Ai_Nghia_discharge_daily.csv",

    "Cam_Le_DaNang":
        "Cam_Le_DaNang_discharge_daily.csv",

    "Cau_Lau":
        "Cau_Lau_discharge_daily.csv",

    "Giao_Thuy":
        "Giao_Thuy_discharge_daily.csv",

    "Nong_Son":
        "Nong_Son_discharge_daily.csv",

    "Thanh_My":
        "Thanh_My_discharge_daily.csv",

    "Tuy_Loan":
        "Tuy_Loan_discharge_daily.csv",
}

# Standard filename prefix per river according to data format.txt
RIVER_PREFIXES = {
    "Ai_Nghia": "ai_nghia",
    "Cam_Le_DaNang": "cam_le",
    "Cau_Lau": "cau_lau",
    "Giao_Thuy": "giao_thuy",
    "Nong_Son": "nong_son",
    "Thanh_My": "thanh_my",
    "Tuy_Loan": "tuy_loan",
}

# Formal river names for initial flow table
RIVER_NAMES = {
    "Ai_Nghia": "Ai Nghia",
    "Cam_Le_DaNang": "Cam Le",
    "Cau_Lau": "Cau Lau",
    "Giao_Thuy": "Giao Thuy",
    "Nong_Son": "Nong Son",
    "Thanh_My": "Thanh My",
    "Tuy_Loan": "Tuy Loan",
}


# ============================================================
# EVENT DETECTION & WINDOW PARAMETERS
# ============================================================

# Maximum rainfall-event duration search constraint (hours)
MAX_EVENT_HOURS = 72

# Minimum rainfall required for an event (accumulated daily mm)
RAINFALL_THRESHOLD_MM = 20.0

# Hours added before and after each rainfall event.
# Set to 0 to strictly keep only the flood period.
BUFFER_BEFORE_HOURS = 0
BUFFER_AFTER_HOURS = 0

# Maximum window duration for HEC-RAS unsteady flow analysis (hours)
MAX_HECRAS_WINDOW_HOURS = 100

# Sub-window step duration in days when splitting events > MAX_HECRAS_WINDOW_HOURS.
# 3 days step creates overlapping 4-day (96h <= 100h) sub-windows.
CHUNK_STEP_DAYS = 3


# ============================================================
# RAINFALL / DISCHARGE RELATIONSHIP
# ============================================================

# Delay between rainfall peak and discharge peak (hours)
DEFAULT_LAG_HOURS = 6

# Daily mean calibration factor
PEAK_FACTOR = 1.0

# Baseflow ratio relative to daily discharge
BASEFLOW_RATIO = 0.40


# ============================================================
# METEOROLOGICAL CSV FORMAT
# ============================================================

METEO_COLUMNS = [
    "date",
    "latitude",
    "longitude",
    "precipitation",
    "evapotranspiration",
    "wind_speed_10m",
    "wind_direction_10m",
]


# ============================================================
# DATE PARSER
# ============================================================

def parse_mixed_date(value):
    """
    Parse the date formats found in the supplied datasets.
    Supported examples:
        2022-10-01
        2022-10-01 00:00:00+00:00
        14/10/2022
        14/10/2022 00:00:00
    """
    if pd.isna(value):
        return pd.NaT

    value = str(value).strip()

    # ISO format
    if "-" in value:
        return pd.to_datetime(
            value,
            errors="coerce",
            utc=True
        )

    # DD/MM/YYYY format
    if "/" in value:
        return pd.to_datetime(
            value,
            dayfirst=True,
            errors="coerce",
            utc=True
        )

    return pd.to_datetime(
        value,
        errors="coerce",
        utc=True
    )


# ============================================================
# LOAD ONE METEOROLOGICAL FILE
# ============================================================

def load_meteorological_file(path):
    df = pd.read_csv(path)

    missing = [
        column
        for column in METEO_COLUMNS
        if column not in df.columns
    ]

    if missing:
        raise ValueError(
            f"{path.name}: missing columns: {missing}"
        )

    df["date"] = pd.to_datetime(
        df["date"],
        errors="coerce",
        utc=True
    )

    df["precipitation"] = pd.to_numeric(
        df["precipitation"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["date"]
    )

    df = df.sort_values(
        "date"
    )

    return df


# ============================================================
# LOAD ALL METEOROLOGICAL LOCATIONS
# ============================================================

def load_all_meteorological_data():
    files = sorted(
        METEO_FOLDER.glob("*.csv")
    )

    print(
        f"Found {len(files)} meteorological files."
    )

    locations = {}

    for index, path in enumerate(
        files,
        start=1
    ):
        try:
            df = load_meteorological_file(path)
            locations[path.stem] = df
            if index % 50 == 0 or index == len(files):
                print(
                    f"[{index}/{len(files)}] loaded: {path.name}"
                )
        except Exception as error:
            print(
                f"WARNING: Failed to load {path.name}: {error}"
            )

    return locations


# ============================================================
# COMBINE RAINFALL FROM ALL LOCATIONS
# ============================================================

def combine_rainfall(locations):
    rainfall_series = []

    for station_name, df in locations.items():
        temp = df[
            [
                "date",
                "precipitation"
            ]
        ].copy()

        temp = temp.rename(
            columns={
                "precipitation": station_name
            }
        )

        temp = temp.set_index("date")
        rainfall_series.append(temp)

    if not rainfall_series:
        raise RuntimeError("No meteorological data available.")

    rainfall = pd.concat(
        rainfall_series,
        axis=1
    )

    rainfall_mean = rainfall.mean(axis=1, skipna=True)
    rainfall_max = rainfall.max(axis=1, skipna=True)
    station_count = rainfall.count(axis=1)

    summary_rainfall = pd.DataFrame(
        {
            "rainfall_mean": rainfall_mean,
            "rainfall_max": rainfall_max,
            "station_count": station_count,
        },
        index=rainfall.index
    )

    return summary_rainfall


# ============================================================
# DETECT RAINFALL EVENTS
# ============================================================

def detect_rainfall_events(rainfall):
    hourly = rainfall["rainfall_mean"].copy()

    # Daily rainfall
    daily = hourly.resample("D").sum(min_count=1)

    events = []
    i = 0

    while i < len(daily):
        current_value = daily.iloc[i]

        if pd.isna(current_value) or current_value < RAINFALL_THRESHOLD_MM:
            i += 1
            continue

        start = daily.index[i]
        end_time = start + pd.Timedelta(hours=MAX_EVENT_HOURS)

        window = daily.loc[start:end_time]
        valid = window.dropna()

        if valid.empty:
            i += 1
            continue

        event_days = []
        for timestamp, value in valid.items():
            if value >= RAINFALL_THRESHOLD_MM:
                event_days.append(timestamp)
            elif event_days:
                break

        if not event_days:
            i += 1
            continue

        event_start = event_days[0]
        event_end = event_days[-1]

        event_rainfall = daily.loc[event_start:event_end]
        total_rainfall = event_rainfall.sum()
        peak_daily_rainfall = event_rainfall.max()

        events.append(
            {
                "event_start": event_start,
                "event_end": event_end,
                "total_rainfall_mm": total_rainfall,
                "peak_daily_rainfall_mm": peak_daily_rainfall
            }
        )

        i = daily.index.searchsorted(
            event_end + pd.Timedelta(days=1)
        )

    return pd.DataFrame(events)


# ============================================================
# MERGE NEARBY EVENTS
# ============================================================

def merge_events(events):
    if events.empty:
        return events

    events = (
        events
        .sort_values("event_start")
        .reset_index(drop=True)
    )

    merged = []
    current = events.iloc[0].copy()

    for i in range(1, len(events)):
        next_event = events.iloc[i]

        # If two events are adjacent within 1 day, merge them
        if (
            next_event["event_start"]
            <= current["event_end"] + pd.Timedelta(days=1)
        ):
            current["event_end"] = max(
                current["event_end"],
                next_event["event_end"]
            )
            current["total_rainfall_mm"] += (
                next_event["total_rainfall_mm"]
            )
            current["peak_daily_rainfall_mm"] = max(
                current["peak_daily_rainfall_mm"],
                next_event["peak_daily_rainfall_mm"]
            )
        else:
            merged.append(current)
            current = next_event.copy()

    merged.append(current)
    return pd.DataFrame(merged)


# ============================================================
# LOAD RIVER DISCHARGE FILE
# ============================================================

def load_discharge_file(path):
    df = pd.read_csv(path)

    required = [
        "date",
        "discharge_m3s"
    ]

    missing = [
        column
        for column in required
        if column not in df.columns
    ]

    if missing:
        raise ValueError(
            f"{path.name}: missing columns {missing}"
        )

    df["date"] = df["date"].apply(parse_mixed_date)
    df["discharge_m3s"] = pd.to_numeric(
        df["discharge_m3s"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["date", "discharge_m3s"]
    )

    df["date"] = df["date"].dt.normalize()
    df = (
        df
        .sort_values("date")
        .reset_index(drop=True)
    )

    return df


def load_all_discharge_data():
    rivers_data = {}
    for river_key, filename in RIVER_FILES.items():
        path = DISCHARGE_FOLDER / filename
        if not path.exists():
            print(f"WARNING: Missing discharge file: {path}")
            continue
        try:
            df = load_discharge_file(path)
            rivers_data[river_key] = df
            print(f"Loaded {river_key}: {len(df):,} records")
        except Exception as error:
            print(f"WARNING: Failed to load {path.name}: {error}")
    return rivers_data


# ============================================================
# DISCHARGE ANCHORS & EVENT RECONSTRUCTION
# ============================================================

def get_event_discharge(discharge, event_start, event_end):
    start = event_start.tz_convert("UTC").normalize()
    end = event_end.tz_convert("UTC").normalize()

    mask = (
        (discharge["date"] >= start)
        & (discharge["date"] <= end)
    )

    return discharge.loc[mask].copy()


def find_daily_rainfall_peak(rainfall, date):
    day_start = date
    day_end = date + pd.Timedelta(days=1) - pd.Timedelta(hours=1)

    window = rainfall.loc[day_start:day_end]
    if window.empty:
        return None

    valid = window["rainfall_mean"].dropna()
    if valid.empty:
        return None

    peak_time = valid.idxmax()
    peak_value = valid.loc[peak_time]

    return peak_time, peak_value


def create_discharge_anchors(
    rainfall,
    daily_discharge,
    lag_hours,
    peak_factor
):
    anchors = []

    for _, row in daily_discharge.iterrows():
        discharge_date = row["date"]
        daily_q = row["discharge_m3s"]

        rainfall_peak = find_daily_rainfall_peak(
            rainfall,
            discharge_date
        )

        if rainfall_peak is not None:
            rainfall_peak_time, rainfall_value = rainfall_peak
            discharge_time = rainfall_peak_time + pd.Timedelta(hours=lag_hours)
        else:
            discharge_time = discharge_date + pd.Timedelta(hours=12)
            rainfall_value = np.nan

        q_anchor = daily_q * peak_factor

        anchors.append(
            {
                "date": discharge_time,
                "discharge_m3s": q_anchor,
                "daily_mean_q": daily_q,
                "rainfall_peak_mm": rainfall_value,
                "source_date": discharge_date
            }
        )

    anchors = pd.DataFrame(anchors)
    if anchors.empty:
        return anchors

    anchors = (
        anchors
        .sort_values("date")
        .drop_duplicates(subset="date", keep="first")
        .reset_index(drop=True)
    )

    return anchors


def reconstruct_event(
    rainfall,
    discharge,
    event,
    lag_hours=DEFAULT_LAG_HOURS,
    peak_factor=PEAK_FACTOR
):
    """
    Reconstruct hourly discharge hydrograph strictly within the flood period
    (from event_start 00:00:00 to event_end 23:00:00).
    """
    event_start = event["event_start"]
    event_end = event["event_end"]

    # Flood period start and end (no 1-day buffer before or after)
    output_start = event_start - pd.Timedelta(hours=BUFFER_BEFORE_HOURS)
    output_end = (
        event_end
        + pd.Timedelta(hours=BUFFER_AFTER_HOURS)
        + pd.Timedelta(hours=23)
    )

    # Daily discharge data strictly within event
    event_discharge = get_event_discharge(
        discharge,
        event_start,
        event_end
    )

    if event_discharge.empty:
        return None

    anchors = create_discharge_anchors(
        rainfall=rainfall,
        daily_discharge=event_discharge,
        lag_hours=lag_hours,
        peak_factor=peak_factor
    )

    if anchors.empty:
        return None

    # Baseflow anchors at boundary of the flood period
    first_q = anchors.iloc[0]["discharge_m3s"]
    last_q = anchors.iloc[-1]["discharge_m3s"]

    base_before = first_q * BASEFLOW_RATIO
    base_after = last_q * BASEFLOW_RATIO

    boundary_rows = []
    if anchors.iloc[0]["date"] > output_start:
        boundary_rows.append(
            pd.DataFrame([
                {
                    "date": output_start,
                    "discharge_m3s": base_before,
                    "daily_mean_q": np.nan,
                    "rainfall_peak_mm": np.nan,
                    "source_date": pd.NaT
                }
            ])
        )

    boundary_rows.append(anchors)

    if anchors.iloc[-1]["date"] < output_end:
        boundary_rows.append(
            pd.DataFrame([
                {
                    "date": output_end,
                    "discharge_m3s": base_after,
                    "daily_mean_q": np.nan,
                    "rainfall_peak_mm": np.nan,
                    "source_date": pd.NaT
                }
            ])
        )

    anchors = pd.concat(boundary_rows, ignore_index=True)
    anchors = (
        anchors
        .sort_values("date")
        .drop_duplicates(subset="date", keep="first")
        .reset_index(drop=True)
    )

    # Hourly output timeline
    hourly_index = pd.date_range(
        start=output_start,
        end=output_end,
        freq="1h",
        tz="UTC"
    )

    x = hourly_index.astype("int64")
    xp = anchors["date"].astype("int64").to_numpy()
    fp = anchors["discharge_m3s"].astype(float).to_numpy()

    interpolated_q = np.interp(x, xp, fp)

    result = pd.DataFrame(
        {
            "date": hourly_index,
            "discharge_m3s": np.clip(interpolated_q, 0, None)
        }
    )

    return result


# ============================================================
# SAVE WINDOW DATA & INITIAL FLOWS
# ============================================================

def save_window_data(folder_path, rivers_hourly, window_start, window_end):
    """
    Save per-river hourly CSV files and initial_flow_values.csv for a time window.
    """
    folder_path.mkdir(parents=True, exist_ok=True)
    initial_flows = []

    for river_key, river_df in rivers_hourly.items():
        mask = (
            (river_df["date"] >= window_start)
            & (river_df["date"] <= window_end)
        )
        sub_df = river_df.loc[mask].copy().reset_index(drop=True)

        if sub_df.empty:
            continue

        prefix = RIVER_PREFIXES.get(river_key, river_key.lower())
        file_path = folder_path / f"{prefix}_hourly_discharge.csv"
        sub_df.to_csv(file_path, index=False)

        initial_flows.append(
            {
                "station": river_key,
                "river": RIVER_NAMES.get(river_key, river_key),
                "initial_flow_m3s": round(float(sub_df.iloc[0]["discharge_m3s"]), 4),
            }
        )

    if initial_flows:
        init_df = pd.DataFrame(initial_flows)
        init_path = folder_path / "initial_flow_values.csv"
        init_df.to_csv(init_path, index=False)


# ============================================================
# PROCESS ALL FLOOD EVENTS
# ============================================================

def process_all_events(events, rainfall, rivers_discharge):
    """
    Format output folders strictly matching data format.txt:
    1. If duration <= 100 hours:
       OUTPUT_FOLDER / <start>_to_<end>/
           |_ <prefix>_hourly_discharge.csv
           |_ initial_flow_values.csv
    2. If duration > 100 hours:
       OUTPUT_FOLDER / <start>_to_<end>/
           |_ <sub_start>_to_<sub_end>/
               |_ <prefix>_hourly_discharge.csv
               |_ initial_flow_values.csv
    """
    print()
    print("=" * 70)
    print("PROCESSING FLOOD EVENTS")
    print("=" * 70)

    reconstructed_count = 0

    for event_id, event in events.iterrows():
        event_start = event["event_start"]
        event_end = event["event_end"]

        start_str = event_start.strftime("%Y-%m-%d")
        end_str = event_end.strftime("%Y-%m-%d")
        event_folder_name = f"{start_str}_to_{end_str}"

        # Reconstruct hourly flow for all available rivers
        rivers_hourly = {}
        for river_key, discharge_df in rivers_discharge.items():
            res = reconstruct_event(
                rainfall=rainfall,
                discharge=discharge_df,
                event=event,
                lag_hours=DEFAULT_LAG_HOURS,
                peak_factor=PEAK_FACTOR
            )
            if res is not None and not res.empty:
                rivers_hourly[river_key] = res

        if not rivers_hourly:
            continue

        # Total duration in hours (full calendar days)
        total_hours = int((event_end - event_start).total_seconds() / 3600) + 24
        parent_folder = OUTPUT_FOLDER / event_folder_name

        if total_hours <= MAX_HECRAS_WINDOW_HOURS:
            window_start = event_start
            window_end = event_end + pd.Timedelta(hours=23)
            save_window_data(
                parent_folder,
                rivers_hourly,
                window_start,
                window_end
            )
            print(
                f"[Event {event_id + 1}] {event_folder_name} "
                f"({total_hours}h <= {MAX_HECRAS_WINDOW_HOURS}h): "
                f"saved {len(rivers_hourly)} rivers"
            )
        else:
            parent_folder.mkdir(parents=True, exist_ok=True)
            print(
                f"[Event {event_id + 1}] {event_folder_name} "
                f"({total_hours}h > {MAX_HECRAS_WINDOW_HOURS}h): "
                f"splitting into sub-windows"
            )

            cur_start = event_start
            while cur_start < event_end:
                cur_end = min(
                    cur_start + pd.Timedelta(days=CHUNK_STEP_DAYS),
                    event_end
                )
                sub_name = (
                    f"{cur_start.strftime('%Y-%m-%d')}_to_"
                    f"{cur_end.strftime('%Y-%m-%d')}"
                )
                sub_folder = parent_folder / sub_name

                window_start = cur_start
                window_end = cur_end + pd.Timedelta(hours=23)
                save_window_data(
                    sub_folder,
                    rivers_hourly,
                    window_start,
                    window_end
                )

                sub_hours = int((cur_end - cur_start).total_seconds() / 3600) + 24
                print(f"    |_ {sub_name} ({sub_hours}h)")
                cur_start = cur_end

        reconstructed_count += 1

    print()
    print(f"Reconstructed {reconstructed_count} flood events.")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70)
    print("DANANG HOURLY RIVER DISCHARGE RECONSTRUCTION")
    print("=" * 70)

    # STEP 1: Load meteorological locations
    print("\nSTEP 1: Loading meteorological data")
    locations = load_all_meteorological_data()
    if not locations:
        raise RuntimeError("No meteorological files found.")

    # STEP 2: Combine rainfall
    print("\nSTEP 2: Combining rainfall")
    rainfall = combine_rainfall(locations)
    print(f"Combined hourly records: {len(rainfall):,}")

    rainfall_output = OUTPUT_FOLDER / "combined_rainfall.csv"
    rainfall.to_csv(rainfall_output)
    print(f"Saved: {rainfall_output}")

    # STEP 3: Detect rainfall events
    print("\nSTEP 3: Detecting rainfall events")
    events = detect_rainfall_events(rainfall)
    events = merge_events(events)
    print(f"Detected events: {len(events)}")

    event_output = OUTPUT_FOLDER / "detected_rainfall_events.csv"
    events.to_csv(event_output, index=False)
    print(f"Saved: {event_output}")

    # STEP 4: Load river discharge datasets
    print("\nSTEP 4: Loading river discharge data")
    rivers_discharge = load_all_discharge_data()
    if not rivers_discharge:
        raise RuntimeError("No river discharge data found.")

    # STEP 5: Process and reconstruct all events
    print("\nSTEP 5: Generating event folders and hourly discharge hydrographs")
    process_all_events(
        events=events,
        rainfall=rainfall,
        rivers_discharge=rivers_discharge
    )

    print("\n" + "=" * 70)
    print("RECONSTRUCTION COMPLETE")
    print("=" * 70)
    print(f"Output folder:\n{OUTPUT_FOLDER}")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()