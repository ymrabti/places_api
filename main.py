"""
Collect Google Places photos for every place inside a polygon.

Places Nearby Search only accepts a circle, so the polygon is covered with a
grid of small circles, each circle is searched (with pagination), results are
de-duplicated by place_id, and anything outside the polygon is discarded.

Requirements:  pip install requests shapely
Usage:         set GOOGLE_MAPS_API_KEY, put your polygon in perimeter.geojson
               (WGS84, [lon, lat] order), then run the script.

NOTE: Google Maps Platform terms restrict bulk downloading/storing Places
content and require photo attribution. Check they fit your use case.
"""
import json
import math
import os
import time

import requests
from shapely.geometry import Point, box, shape
from shapely.ops import unary_union

# 1. Configuration
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

POLYGON_FILE = (
    "perimeter.geojson"  # Polygon / MultiPolygon / Feature / FeatureCollection
)
SEARCH_RADIUS = 200  # radius (m) of each search circle; smaller = fewer missed places
OUTPUT_DIR = "user_photos"
TIMEOUT = 20

NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PHOTO_URL = "https://maps.googleapis.com/maps/api/place/photo"


# ---------- polygon handling ----------
def load_polygon(path):
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    if gj["type"] == "FeatureCollection":
        geoms = [shape(ft["geometry"]) for ft in gj["features"]]
    elif gj["type"] == "Feature":
        geoms = [shape(gj["geometry"])]
    else:
        geoms = [shape(gj)]
    poly = unary_union(geoms)
    if poly.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Expected a polygon, got {poly.geom_type}")
    return poly


def tile_centers(poly, radius_m):
    """Grid of circle centres whose circles fully cover the polygon.
    A square cell of side s is covered by a circle of radius s/sqrt(2),
    so s = radius * 1.4 is safe."""
    minx, miny, maxx, maxy = poly.bounds
    lat0 = (miny + maxy) / 2
    side = radius_m * 1.4
    dlat = side / 111_320
    dlng = side / (111_320 * math.cos(math.radians(lat0)))

    centers = []
    y = miny
    while y < maxy + dlat:
        x = minx
        while x < maxx + dlng:
            cell = box(x, y, x + dlng, y + dlat)
            if poly.intersects(cell):
                centers.append((y + dlat / 2, x + dlng / 2))  # (lat, lng)
            x += dlng
        y += dlat
    return centers


# ---------- Google API ----------
def nearby_search(lat, lng, radius, api_key):
    """All results for one circle, following next_page_token (max 3 pages)."""
    params = {"location": f"{lat},{lng}", "radius": radius, "key": api_key}
    results = []
    for _ in range(3):
        r = requests.get(NEARBY_URL, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(
                f"Nearby Search failed: {status} {data.get('error_message', '')}"
            )
        results.extend(data.get("results", []))
        token = data.get("next_page_token")
        if not token:
            break
        time.sleep(2)  # token needs a moment before it becomes valid
        params = {"pagetoken": token, "key": api_key}
    return results


def collect_places(poly, api_key):
    places = {}
    centers = tile_centers(poly, SEARCH_RADIUS)
    print(f"Searching {len(centers)} circles to cover the polygon...")
    for i, (lat, lng) in enumerate(centers, 1):
        for place in nearby_search(lat, lng, SEARCH_RADIUS, api_key):
            loc = place.get("geometry", {}).get("location", {})
            if "lat" not in loc:
                continue
            pid = place["place_id"]
            if pid in places:
                continue
            if not poly.covers(Point(loc["lng"], loc["lat"])):  # outside the polygon
                continue
            places[pid] = place
        print(f"  circle {i}/{len(centers)} -> {len(places)} places inside polygon")
    return places


_review_cache = {}


def latest_review_time(place_id, api_key):
    """Relative time of the newest review. NOT the photo date; only a rough activity hint."""
    if place_id in _review_cache:
        return _review_cache[place_id]
    value = None
    try:
        r = requests.get(
            DETAILS_URL,
            params={"place_id": place_id, "fields": "reviews", "key": api_key},
            timeout=TIMEOUT,
        )
        reviews = r.json().get("result", {}).get("reviews", [])
        if reviews:
            value = reviews[0].get("relative_time_description")
    except requests.RequestException:
        pass
    _review_cache[place_id] = value
    return value


def download_photo(photo, place, index, api_key):
    r = requests.get(
        PHOTO_URL,
        params={
            "maxwidth": 6000,
            "photoreference": photo["photo_reference"],
            "key": api_key,
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith(
        "image/"
    ):
        print(f"  skipped photo {index}: HTTP {r.status_code}")
        return False

    base = os.path.join(OUTPUT_DIR, f"photo_{index}")
    with open(base + ".jpg", "wb") as f:
        f.write(r.content)

    loc = place["geometry"]["location"]
    metadata = {
        "place_name": place.get("name"),
        "place_id": place["place_id"],
        "place_coordinates": {"latitude": loc["lat"], "longitude": loc["lng"]},
        "latest_review_relative_time": latest_review_time(place["place_id"], api_key),
        "photo_reference": photo["photo_reference"],
        "attribution": photo.get(
            "html_attributions", []
        ),  # must be shown with the photo
    }
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4, ensure_ascii=False)
    return True


def main():
    if not API_KEY:
        raise SystemExit("Set the GOOGLE_MAPS_API_KEY environment variable.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    poly = load_polygon(POLYGON_FILE)
    places = collect_places(poly, API_KEY)

    index = 0
    for place in places.values():
        for photo in place.get("photos", []):
            if download_photo(photo, place, index, API_KEY):
                index += 1
    print(f"Done. {index} photos from {len(places)} places saved in '{OUTPUT_DIR}'.")


if __name__ == "__main__":
    main()
