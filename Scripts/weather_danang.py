import requests
import json
import csv
from datetime import datetime, timezone, timedelta


# ============================================================
# Configuration
# ============================================================

API_URL = "https://muangap-api.danang.gov.vn/v1/flood/reports"

START_DATE = "2020-01-01"
END_DATE = "2026-08-08"

RAW_JSON_FILE = "danang_flood_raw.json"
CLEAN_JSON_FILE = "danang_flood_data.json"
CSV_FILE = "danang_flood_data.csv"


# Vietnam timezone
VN_TZ = timezone(timedelta(hours=7))


# ============================================================
# Helper functions
# ============================================================

def date_to_timestamp(date_string, end_of_day=False):
    """
    Convert YYYY-MM-DD to Unix timestamp.

    Args:
        date_string: Date in YYYY-MM-DD format.
        end_of_day: If True, use 23:59:59 of that date.

    Returns:
        Unix timestamp.
    """

    if end_of_day:
        dt = datetime.strptime(
            date_string, "%Y-%m-%d"
        ).replace(
            hour=23,
            minute=59,
            second=59,
            tzinfo=VN_TZ
        )
    else:
        dt = datetime.strptime(
            date_string, "%Y-%m-%d"
        ).replace(
            hour=0,
            minute=0,
            second=0,
            tzinfo=VN_TZ
        )

    return int(dt.timestamp())


def timestamp_to_vietnam(timestamp):
    """
    Convert Unix timestamp to Vietnam local time.
    """

    if timestamp is None:
        return None

    return datetime.fromtimestamp(
        timestamp,
        tz=VN_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# Fetch flood data
# ============================================================

def fetch_flood_data(start_date, end_date):
    """
    Fetch flood reports from the Da Nang Mưa Ngập API.
    """

    from_time = date_to_timestamp(start_date)
    to_time = date_to_timestamp(
        end_date,
        end_of_day=True
    )

    params = {
        "from_time": from_time,
        "to_time": to_time
    }

    print("Fetching flood data...")
    print(f"Start date : {start_date}")
    print(f"End date   : {end_date}")
    print(f"From time  : {from_time}")
    print(f"To time    : {to_time}")
    print()

    response = requests.get(
        API_URL,
        params=params,
        timeout=120
    )

    print("HTTP status:", response.status_code)

    response.raise_for_status()

    result = response.json()

    print("API status:", result.get("status"))

    if result.get("status") != "success":
        raise RuntimeError(
            f"API returned unexpected status: "
            f"{result.get('status')}"
        )

    records = result.get("data", [])

    print("Records returned:", len(records))

    return result, records


# ============================================================
# Save raw API response
# ============================================================

def save_raw_json(result):
    """
    Save the complete API response.
    """

    with open(
        RAW_JSON_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            result,
            f,
            ensure_ascii=False,
            indent=4
        )

    print(f"Raw JSON saved to: {RAW_JSON_FILE}")


# ============================================================
# Clean the records
# ============================================================

def clean_records(records):
    """
    Extract useful fields from each flood report.
    """

    cleaned = []

    for record in records:

        location = record.get("location", {})
        coordinates = location.get("coordinates", [])

        # GeoJSON uses [longitude, latitude]
        longitude = None
        latitude = None

        if len(coordinates) >= 2:
            longitude = coordinates[0]
            latitude = coordinates[1]

        flood_time = record.get(
            "flood_time",
            {}
        )

        clean_record = {
            "id": record.get("id"),

            "latitude": latitude,
            "longitude": longitude,

            "address": location.get("address"),
            "street_name": location.get("street_name"),
            "ward_name": location.get("ward_name"),
            "ward_id": location.get("ward_id"),
            "district_name": location.get("district_name"),
            "district_id": location.get("district_id"),

            "flood_type": record.get("flood_type"),
            "flood_unit": record.get("flood_unit"),
            "water_level": record.get("water_level"),

            "flood_start": timestamp_to_vietnam(
                flood_time.get("start_time")
            ),

            "flood_end": timestamp_to_vietnam(
                flood_time.get("end_time")
            ),

            "is_frequent": record.get("is_frequent"),

            "flood_description": record.get(
                "flood_description"
            ),

            "create_time": timestamp_to_vietnam(
                record.get("create_time")
            )
        }

        cleaned.append(clean_record)

    return cleaned


# ============================================================
# Save clean JSON
# ============================================================

def save_clean_json(records):
    """
    Save cleaned flood records as JSON.
    """

    with open(
        CLEAN_JSON_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            records,
            f,
            ensure_ascii=False,
            indent=4
        )

    print(
        f"Clean JSON saved to: {CLEAN_JSON_FILE}"
    )


# ============================================================
# Save CSV
# ============================================================

def save_csv(records):
    """
    Save cleaned flood records as CSV.
    """

    if not records:
        print("No records to save.")
        return

    fieldnames = list(records[0].keys())

    with open(
        CSV_FILE,
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(records)

    print(f"CSV saved to: {CSV_FILE}")


# ============================================================
# Summary
# ============================================================

def print_summary(records):
    """
    Print basic information about the downloaded data.
    """

    print()
    print("=" * 50)
    print("DOWNLOAD SUMMARY")
    print("=" * 50)

    print("Total records:", len(records))

    if not records:
        return

    # Number of records with coordinates
    coordinate_count = sum(
        1
        for r in records
        if r["latitude"] is not None
        and r["longitude"] is not None
    )

    print(
        "Records with coordinates:",
        coordinate_count
    )

    # Number of unique streets
    streets = {
        r["street_name"]
        for r in records
        if r["street_name"]
    }

    print(
        "Unique streets:",
        len(streets)
    )

    # Number of unique wards
    wards = {
        r["ward_name"]
        for r in records
        if r["ward_name"]
    }

    print(
        "Unique wards:",
        len(wards)
    )

    # Number of unique districts
    districts = {
        r["district_name"]
        for r in records
        if r["district_name"]
    }

    print(
        "Unique districts:",
        len(districts)
    )

    print("=" * 50)


# ============================================================
# Main
# ============================================================

def main():

    # Fetch data
    raw_result, records = fetch_flood_data(
        START_DATE,
        END_DATE
    )

    # Save original API response
    save_raw_json(raw_result)

    # Clean data
    cleaned_records = clean_records(records)

    # Save cleaned data
    save_clean_json(cleaned_records)
    save_csv(cleaned_records)

    # Print summary
    print_summary(cleaned_records)


if __name__ == "__main__":
    main()