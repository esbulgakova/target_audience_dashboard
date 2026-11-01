"""Map correctness matters more than snapshots of presentation-only widgets."""

from frontend.geo import aggregate_regions, load_regions


def test_map_counts_missing_and_unknown_locations_without_guessing():
    rows = [
        {"country_code": "RU", "region": "Московская область", "count": 4},
        {"country_code": "RU", "region": "", "city": "Москва", "count": 3},
        {"country_code": "RU", "region": "", "city": "Нижний Новгород", "count": 2},
        {"country_code": "US", "region": "Московская область", "count": 7},
        {"country": "Не указано", "region": "Свердловская область", "count": 5},
        {"country": "Не указано", "city": "Москва", "count": 6},
    ]
    counts, unmapped = aggregate_regions(rows)
    assert sum(counts.values()) == 12
    assert unmapped == 15
    assert counts["RUS-2364"] == 4  # Oblast and city are different regions.
    assert counts["RUS-2365"] == 3


def test_ambiguous_region_is_not_arbitrarily_assigned():
    features = [{"id": str(i), "properties": {
        "country_code": code, "type": "Region", "aliases": ["Одинаковая область"],
    }} for i, code in enumerate(["RU", "BY"])]
    rows = [{"country": "Не указано", "region": "Одинаковая область", "count": 10}]
    assert aggregate_regions(rows, {"features": features}) == ({}, 10)


def test_bundled_regions_have_unique_ids_and_attribution():
    regions = load_regions()
    assert len(regions["features"]) == len({f["id"] for f in regions["features"]})
    assert regions["metadata"]["license"] == "Public domain"
    assert {f["properties"]["country_code"] for f in regions["features"]} == {
        "RU", "BY", "UA", "KZ", "KG", "UZ", "TJ", "TM", "MD", "AM", "AZ", "GE",
    }


def test_iso3_and_alternative_region_names():
    rows = [
        {"country_code": "KAZ", "region": "Алматы", "count": 5},
        {"country_code": "KAZ", "region": "Астана", "count": 7},
        {"country_code": "RUS", "region": "Республика Татарстан", "count": 11},
    ]
    assert aggregate_regions(rows) == ({"KAZ-4829": 5, "KAZ-4830": 7, "RUS-2394": 11}, 0)


def test_changing_chart_does_not_collect_again(monkeypatch):
    import pytest

    pytest.importorskip("streamlit")
    pytest.importorskip("plotly")
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import requests

    calls = []
    result = {
        "query": "Фотография", "mode": "demo", "modes": ["groups"], "cache_hit": False,
        "collected_at": "2026-11-01T10:00:00Z", "expires_at": "2026-11-02T10:00:00Z",
        "total_users": 10, "limited": False, "warnings": [],
        "coverage": {"gender": 100, "age": 100, "geography": 100},
        "gender": [{"label": "Женщины", "count": 10, "percentage": 100}],
        "age": [{"label": "25–34", "count": 10, "percentage": 100}],
        "geography": [{"country": "Россия", "country_code": "RUS", "region": "Москва", "city": "Москва", "count": 10}],
        "sources": {"groups": 10}, "groups": [],
    }

    class Response:
        ok = True

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/health"):
            return Response({"status": "ok", "mode": "demo"})
        if url.endswith("/api/suggestions"):
            return Response({"suggestions": ["Фотография"]})
        return Response(result)

    monkeypatch.setattr(requests, "request", request)
    app_path = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=15).run()
    assert not app.exception
    app.selectbox[0].select("Фотография")
    app.button[0].click().run()
    assert not app.exception
    assert sum(method == "POST" for method, _ in calls) == 1
    app.toggle[0].set_value(True).run()
    assert not app.exception
    assert sum(method == "POST" for method, _ in calls) == 1
