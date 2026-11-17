from datetime import timezone
from zoneinfo import ZoneInfo


def tz_of(name: str) -> "timezone | ZoneInfo":
    return timezone.utc if name == "UTC" else ZoneInfo(name)  # UTC needs no tz database
