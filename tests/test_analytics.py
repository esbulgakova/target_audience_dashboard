from datetime import date
from types import SimpleNamespace

import pytest

from app.analytics import age_from_bdate, aggregate


@pytest.mark.parametrize("bdate,expected", [("2.11.2000", 25), ("1.11.2000", 26), ("29.2.2000", 26), ("1.5", None),
                                            ("31.2.2000", None), ("1.1.2027", None), (None, None), ("1.1.1800", None)])
def test_age_requires_valid_full_date(bdate, expected):
    assert age_from_bdate(bdate, date(2026, 11, 1)) == expected


def test_unknowns_and_percentages_use_whole_sample():
    users = [SimpleNamespace(sex=sex, bdate=bdate, country=None, country_code=None, region=None, city=None)
             for sex, bdate in [(1, "1.1.2000"), (2, "1.5"), (0, None)]]
    result = aggregate(users, date(2026, 11, 1))
    assert sum(row["count"] for row in result["gender"]) == 3
    assert result["age"][-1]["count"] == 2
    assert result["geography"][0]["percentage"] == 100
    assert result["coverage"]["geography"] == 0
    assert aggregate([])["total_users"] == 0
