from collections import Counter
from datetime import date, datetime, timezone


def age_from_bdate(value: str | None, today: date) -> int | None:
    if not value or len(value.split(".")) != 3:
        return None
    try:
        day, month, year = map(int, value.split("."))
        birthday = date(year, month, day)
    except (ValueError, TypeError):
        return None
    age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
    return age if 0 <= age <= 110 else None


def age_label(age):
    if age is None:
        return "Не указан"
    for bound, label in [(17, "До 18"), (24, "18–24"), (34, "25–34"), (44, "35–44"), (54, "45–54")]:
        if age <= bound:
            return label
    return "55+"


def distribution(counts, total, order=None):
    keys = order if order is not None else counts.keys()
    return [{"label": key, "count": counts[key], "percentage": round(counts[key] * 100 / total, 2) if total else 0.0}
            for key in keys]


def aggregate(users, today=None):
    today = today or datetime.now(timezone.utc).date()
    total = len(users)
    gender, age, geo = Counter(), Counter(), Counter()
    for user in users:
        gender[{1: "Женский", 2: "Мужской"}.get(user.sex, "Не указан")] += 1
        age[age_label(age_from_bdate(user.bdate, today))] += 1
        geo[(user.country or "Не указана", user.country_code or "", user.region or "Не указан", user.city or "Не указан")] += 1
    geography = [dict(country=key[0], country_code=key[1], region=key[2], city=key[3], count=count,
                      percentage=round(count * 100 / total, 2) if total else 0.0)
                 for key, count in sorted(geo.items(), key=lambda item: (-item[1], item[0]))]
    known_geo = sum(count for key, count in geo.items() if key[3] != "Не указан")
    return {
        "total_users": total,
        "gender": distribution(gender, total, ["Женский", "Мужской", "Не указан"]),
        "age": distribution(age, total, ["До 18", "18–24", "25–34", "35–44", "45–54", "55+", "Не указан"]),
        "geography": geography,
        "coverage": {
            "gender": round((total - gender["Не указан"]) * 100 / total, 1) if total else 0,
            "age": round((total - age["Не указан"]) * 100 / total, 1) if total else 0,
            "geography": round(known_geo * 100 / total, 1) if total else 0,
        },
    }
