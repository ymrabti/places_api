"""
Merge the per-photo metadata JSON files into a single GeoJSON file.

Every ``photo_<n>.json`` in ``user_photos/`` becomes one Point
feature. ``latest_review_relative_time`` ("4 months ago", "a year ago", ...)
is converted to an approximate absolute timestamp: Google expressed that value
relative to the moment the API was called, so each file's modification time is
used as the reference and the stated offset is subtracted from it.

The result is deliberately coarse - "months ago" carries no day, let alone a
time of day - so derived timestamps are truncated to 00:00:00 UTC of the
computed day. Passing ``--as-of YYYY-MM-DD`` forces one shared reference date
instead of the per-file mtimes. Values that cannot be parsed stay null rather
than being guessed.

Requirements:  none (standard library only)
Usage:         python merge_photos_to_geojson.py
               python merge_photos_to_geojson.py --as-of 2026-11-03
               python merge_photos_to_geojson.py --output photos.geojson --indent 0
"""
import argparse
import calendar
import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 1. Configuration
INPUT_DIR = "user_photos"
OUTPUT_FILE = "places.geojson"

NAME_RE = re.compile(r"photo_(\d+)\.json$")
RELATIVE_RE = re.compile(
    r"^(?:(?P<num>\d+)|(?P<article>a|an|one))\s+"
    r"(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago$"
)
ANCHOR_RE = re.compile(r"<a[^>]*>(?P<name>.*?)</a>", re.IGNORECASE | re.DOTALL)
FIXED_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}


# ---------- relative time -> absolute timestamp ----------
def parse_relative(text):
    """Split 'a month ago' / '3 weeks ago' / 'just now' into (count, unit).
    Returns None when the wording is not understood."""
    normalized = " ".join(text.replace("\u00a0", " ").split()).lower()
    normalized = re.sub(r"^(?:about|over|almost)\s+", "", normalized)
    normalized = re.sub(r"^a few\s+", "a ", normalized)

    if normalized in ("just now", "now"):
        return 0, "second"
    if normalized in ("yesterday",):
        return 1, "day"

    match = RELATIVE_RE.match(normalized)
    if not match:
        return None
    count = int(match.group("num")) if match.group("num") else 1
    return count, match.group("unit")


def shift_back(moment, count, unit):
    """Subtract count units from moment; months/years use calendar arithmetic
    with the day clamped to the shorter month (31 Mar - 1 month = 28 Feb)."""
    if unit in FIXED_SECONDS:
        return moment - timedelta(seconds=count * FIXED_SECONDS[unit])

    months = count * 12 if unit == "year" else count
    index = moment.year * 12 + (moment.month - 1) - months
    year, month = index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def convert_relative(relative, anchor):
    """Return (iso_utc, epoch_seconds) for a relative string, or (None, None)."""
    if not relative or not isinstance(relative, str):
        return None, None
    parsed = parse_relative(relative)
    if parsed is None:
        return None, None

    moment = shift_back(anchor, *parsed)
    moment = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ"), int(moment.timestamp())


# ---------- input handling ----------
def metadata_files(input_dir):
    """photo_*.json sorted numerically (photo_9 before photo_10), not by name."""

    def sort_key(path):
        match = NAME_RE.search(path.name)
        return (0, int(match.group(1)), path.name) if match else (1, 0, path.name)

    return sorted(input_dir.glob("photo_*.json"), key=sort_key)


def anchor_for(path, as_of):
    if as_of is not None:
        return as_of
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def contributor_name(attribution):
    """'<a href="...">R K</a>' -> 'R K' (HTML entities unescaped)."""
    for entry in attribution or []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        match = ANCHOR_RE.search(entry)
        name = match.group("name") if match else entry
        name = " ".join(html.unescape(name).split())
        if name:
            return name
    return None


def attribution_text(attribution):
    if not isinstance(attribution, list):
        return None
    joined = " ".join(e.strip() for e in attribution if isinstance(e, str) and e.strip())
    return joined or None


# ---------- feature building ----------
def build_feature(record, source_path, anchor, photo_dir):
    """Return (feature, None) or (None, reason) when the record is unusable."""
    coords = record.get("place_coordinates") or {}
    lat, lon = coords.get("latitude"), coords.get("longitude")
    for value in (lat, lon):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "missing or non-numeric place_coordinates"
        if not math.isfinite(value):
            return None, "non-finite place_coordinates"
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        return None, "place_coordinates out of range"

    relative = record.get("latest_review_relative_time")
    iso, epoch = convert_relative(relative, anchor)

    feature = {
        "type": "Feature",
        # GeoJSON positions are [longitude, latitude] (RFC 7946)
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "place_name": record.get("place_name"),
            "place_id": record.get("place_id"),
            "source_file": source_path.name,
            "photo_file": f"{photo_dir}/{source_path.stem}.jpg",
            "attribution": attribution_text(record.get("attribution")),
            "contributor": contributor_name(record.get("attribution")),
            "latest_review_relative_time": relative,
            "latest_review_time": iso,
            "latest_review_epoch": epoch,
        },
    }
    return feature, None


def build_collection(input_dir, as_of):
    files = metadata_files(input_dir)
    if not files:
        raise SystemExit(f"No photo_*.json files found in '{input_dir}'.")

    features, skipped, unparsed = [], [], []
    for source_path in files:
        try:
            record = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append((source_path.name, f"unreadable JSON ({exc})"))
            continue
        if not isinstance(record, dict):
            skipped.append((source_path.name, "not a JSON object"))
            continue

        feature, reason = build_feature(
            record, source_path, anchor_for(source_path, as_of), input_dir.name
        )
        if feature is None:
            skipped.append((source_path.name, reason))
            continue
        features.append(feature)

        props = feature["properties"]
        if props["latest_review_time"] is None and props["latest_review_relative_time"]:
            unparsed.append((source_path.name, props["latest_review_relative_time"]))

    collection = {
        "type": "FeatureCollection",
        "name": "places_photos",
        "features": features,
    }
    return collection, files, skipped, unparsed


# ---------- CLI ----------
def parse_as_of(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM-DD, got {value!r}"
        ) from exc


def main():
    parser = argparse.ArgumentParser(
        description="Merge photo metadata JSON files into one GeoJSON file."
    )
    parser.add_argument(
        "--input-dir", default=INPUT_DIR, help=f"default: {INPUT_DIR}"
    )
    parser.add_argument(
        "--output", default=OUTPUT_FILE, help=f"default: {OUTPUT_FILE}"
    )
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=None,
        help="reference date (YYYY-MM-DD) for the relative review times; "
        "defaults to each JSON file's modification time",
    )
    parser.add_argument(
        "--indent", type=int, default=2, help="JSON indentation, 0 for compact (default: 2)"
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).expanduser()
    if not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: '{input_dir}'")
    output_path = Path(args.output).expanduser()

    collection, files, skipped, unparsed = build_collection(input_dir, args.as_of)
    if not collection["features"]:
        raise SystemExit("No usable records found; nothing written.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    indent = None if args.indent <= 0 else args.indent
    payload = json.dumps(
        collection, indent=indent, ensure_ascii=False, allow_nan=False
    )
    try:
        output_path.write_text(payload + "\n", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Could not write '{output_path}': {exc}") from exc

    # ---------- summary ----------
    props = [f["properties"] for f in collection["features"]]
    stamped = [p for p in props if p["latest_review_time"] is not None]
    anchor_label = (
        f"--as-of {args.as_of:%Y-%m-%d}" if args.as_of else "each file's mtime"
    )

    print(f"Read {len(files)} JSON file(s) from '{input_dir}'.")
    print(f"Wrote {len(props)} feature(s) to '{output_path}'.")
    print(f"Anchor for relative times: {anchor_label}.")
    print(
        f"Review timestamps: {len(stamped)} converted, "
        f"{len(props) - len(stamped)} null "
        f"({len(props) - len(stamped) - len(unparsed)} empty, {len(unparsed)} unparsed)."
    )
    if stamped:
        times = sorted(p["latest_review_time"] for p in stamped)
        print(f"Review timestamp range: {times[0]} .. {times[-1]}")

    for name, value in unparsed:
        print(f"  unrecognised relative time in {name}: {value!r}")
    for name, reason in skipped:
        print(f"  skipped {name}: {reason}")


if __name__ == "__main__":
    main()
