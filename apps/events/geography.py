"""Small deterministic geographic preference scorer.

Country centroids are copied from Google's DSPL example dataset:
https://github.com/google/dspl/blob/master/samples/google/canonical/countries.csv
The DSPL example files are distributed under Google's 2010 BSD license. The
required notice, conditions, and disclaimer are retained here:

Copyright (c) 2010, Google Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
* Neither the name of Google Inc. nor the names of its contributors may be
  used to endorse or promote products derived from this software without
  specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

Centroids provide rough country-level proximity only. They are never treated
as city coordinates. Country codes absent from this source (AX, BL, BQ, CW,
MF, SS, SX, UM) get neutral scores for cross-country distance comparisons.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

MAX_SCORE = 1_000_000
SAME_COUNTRY_SCORE = 750_000
FOREIGN_MAX_SCORE = 500_000
EARTH_RADIUS_KM = 6371.0088


def _load_centroids() -> dict[str, tuple[float, float]]:
    path = Path(__file__).with_name("data") / "country_coordinates.csv"
    centroids: dict[str, tuple[float, float]] = {}
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            code = (row.get("country") or "").strip().upper()
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                len(code) != 2
                or not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                continue
            centroids[code] = (latitude, longitude)
    return centroids


COUNTRY_CENTROIDS = _load_centroids()


def _normalize_country(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _normalize_city(value: Any) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    hav = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    # Protect asin from tiny floating point overshoots.
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, hav))))


def geographic_score(participant_a: Any, participant_b: Any) -> int:
    """Return a deterministic geography preference score in [0, 1,000,000].

    Inputs need only expose ``country`` and ``city`` attributes. Equal cities
    score highest only when both participants share a recognized country.
    Unknown or missing country coordinates provide the neutral score zero.
    """
    country_a = _normalize_country(getattr(participant_a, "country", ""))
    country_b = _normalize_country(getattr(participant_b, "country", ""))
    if not country_a or not country_b:
        return 0
    if country_a == country_b:
        city_a = _normalize_city(getattr(participant_a, "city", ""))
        city_b = _normalize_city(getattr(participant_b, "city", ""))
        if city_a and city_a == city_b:
            return MAX_SCORE
        return SAME_COUNTRY_SCORE

    point_a = COUNTRY_CENTROIDS.get(country_a)
    point_b = COUNTRY_CENTROIDS.get(country_b)
    if point_a is None or point_b is None:
        return 0
    distance = _haversine_km(point_a, point_b)
    # Scale proximity monotonically beneath same-country preference.
    max_distance = math.pi * EARTH_RADIUS_KM
    return max(0, min(FOREIGN_MAX_SCORE, round(FOREIGN_MAX_SCORE * (1 - distance / max_distance))))
