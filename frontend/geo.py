"""Conservative name matching against a bundled Natural Earth region map.

No network geocoding, inferred locations or approximate string matching: an
ambiguous or unknown region stays in the table and in the unmapped count.
"""

from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


COUNTRY_CODES = {
    "россия": "RU", "russia": "RU", "rus": "RU",
    "беларусь": "BY", "белоруссия": "BY", "belarus": "BY", "blr": "BY",
    "казахстан": "KZ", "kazakhstan": "KZ", "kaz": "KZ",
    "украина": "UA", "україна": "UA", "ukraine": "UA", "ukr": "UA",
    "армения": "AM", "armenia": "AM", "arm": "AM",
    "азербайджан": "AZ", "azerbaijan": "AZ", "aze": "AZ",
    "грузия": "GE", "georgia": "GE", "geo": "GE",
    "кыргызстан": "KG", "киргизия": "KG", "kyrgyzstan": "KG", "kgz": "KG",
    "молдова": "MD", "молдавия": "MD", "moldova": "MD", "mda": "MD",
    "таджикистан": "TJ", "tajikistan": "TJ", "tjk": "TJ",
    "туркменистан": "TM", "turkmenistan": "TM", "tkm": "TM",
    "узбекистан": "UZ", "uzbekistan": "UZ", "uzb": "UZ",
}
SUPPORTED_COUNTRIES = set(COUNTRY_CODES.values())
CITY_REGION_TYPES = {"City", "Independent City", "Federal City"}


def normalize_name(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = value.replace("ё", "е")
    value = re.sub(r"[\s\-–—]+", " ", value)
    return value.strip(" .")


def country_code(row):
    value = str(row.get("country_code") or "").upper()
    if value in SUPPORTED_COUNTRIES:
        return value
    return COUNTRY_CODES.get(normalize_name(value)) or COUNTRY_CODES.get(
        normalize_name(row.get("country"))
    )


@lru_cache(maxsize=1)
def load_regions():
    return json.loads((Path(__file__).parent / "data" / "regions.geojson").read_text())


def build_lookup(geojson):
    """Retain all matches, so conflicting aliases never pick an arbitrary region."""
    regions = defaultdict(set)
    cities = defaultdict(set)
    for feature in geojson["features"]:
        props = feature["properties"]
        code = props["country_code"]
        for alias in props["aliases"]:
            name = normalize_name(alias)
            if not name:
                continue
            regions[(code, name)].add(feature["id"])
            regions[(None, name)].add(feature["id"])
            if props["type"] in CITY_REGION_TYPES:
                cities[(code, name)].add(feature["id"])
    return regions, cities


def aggregate_regions(rows, geojson=None):
    """Return region counts and number of people whose region was not matched.

    With no country, only a globally unique, exact region name is accepted. A
    country explicitly outside this map never falls back to another country's
    region. City names are accepted only for documented city-level regions.
    """
    geojson = geojson or load_regions()
    regions, cities = build_lookup(geojson)
    counts = defaultdict(int)
    unmapped = 0
    unknown_labels = {"", "не указано", "не указана", "нет данных", "неизвестно"}
    for row in rows:
        count = int(row.get("count", 0))
        code = country_code(row)
        raw_country = normalize_name(row.get("country"))
        if code is None and (
            row.get("country_code") or raw_country not in unknown_labels
        ):
            unmapped += count
            continue
        candidates = regions.get((code, normalize_name(row.get("region"))), set())
        if len(candidates) != 1 and code:
            candidates = cities.get((code, normalize_name(row.get("city"))), set())
        if len(candidates) == 1:
            counts[next(iter(candidates))] += count
        else:
            unmapped += count
    return dict(counts), unmapped


def regional_map(rows, total_users):
    import plotly.graph_objects as go

    geojson = load_regions()
    counts, unmapped = aggregate_regions(rows, geojson)
    features = {feature["id"]: feature for feature in geojson["features"]}
    ids = list(counts)
    # The GeoJSON is local. A MapLibre map without raster tiles also works when
    # the browser has no access to an external map or Plotly's topojson CDN.
    fig = go.Figure()
    fig.add_trace(go.Choroplethmap(
        geojson=geojson,
        locations=list(features),
        z=[0] * len(features),
        featureidkey="id",
        colorscale=[[0, "#E9EEF2"], [1, "#E9EEF2"]],
        showscale=False,
        marker={"line": {"color": "#FFFFFF", "width": 0.6}},
        hoverinfo="skip",
    ))
    if ids:
        fig.add_trace(go.Choroplethmap(
            geojson=geojson,
            locations=ids,
            featureidkey="id",
            z=[counts[key] for key in ids],
            customdata=[[
                features[key]["properties"]["name"],
                features[key]["properties"]["country_code"],
                round(counts[key] / total_users * 100, 1) if total_users else 0,
            ] for key in ids],
            colorscale=[[0, "#BCE5E2"], [0.5, "#259DA3"], [1, "#07515D"]],
            marker={"line": {"color": "#FFFFFF", "width": 0.8}},
            colorbar={"title": "Человек", "thickness": 12},
            hovertemplate=("<b>%{customdata[0]}</b> · %{customdata[1]}<br>"
                           "%{z} человек<br>%{customdata[2]}% выборки<extra></extra>"),
        ))
    fig.update_layout(
        map={"style": "white-bg", "center": {"lat": 56, "lon": 65}, "zoom": 1.65},
        height=510,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        paper_bgcolor="rgba(0,0,0,0)",
        font={"family": "sans-serif"},
    )
    return fig, sum(counts.values()), unmapped
