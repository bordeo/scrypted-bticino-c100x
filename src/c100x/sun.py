"""Local sunrise/sunset (no network) for day/night snapshot choice.

Uses the NOAA solar calculator (zenith 90.833°) with a fixed timezone.
Defaults: Peraga, Vigonza (45.42°N, 11.96°E, Europe/Rome).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Peraga / Vigonza (PD), Italy.
DEFAULT_LATITUDE = 45.42
DEFAULT_LONGITUDE = 11.96
DEFAULT_TIMEZONE = "Europe/Rome"

# Official sunrise/sunset (accounts for refraction).
_ZENITH_DEG = 90.833


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def solar_events(
    on_date: date,
    *,
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
    tz_name: str = DEFAULT_TIMEZONE,
) -> tuple[datetime, datetime]:
    """Return (sunrise, sunset) as timezone-aware datetimes for ``on_date``."""
    tz = ZoneInfo(tz_name)
    # NOAA algorithm (degrees throughout until trig).
    n = on_date.timetuple().tm_yday
    lng_hour = longitude / 15.0

    def _event(rising: bool) -> datetime:
        t = n + ((6 - lng_hour) / 24.0) if rising else n + ((18 - lng_hour) / 24.0)
        m = (0.9856 * t) - 3.289
        l = (
            m
            + (1.916 * math.sin(math.radians(m)))
            + (0.020 * math.sin(math.radians(2 * m)))
            + 282.634
        ) % 360.0
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360.0
        l_quad = math.floor(l / 90.0) * 90.0
        ra_quad = math.floor(ra / 90.0) * 90.0
        ra = (ra + (l_quad - ra_quad)) / 15.0
        sin_dec = 0.39782 * math.sin(math.radians(l))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (
            math.cos(math.radians(_ZENITH_DEG)) - (sin_dec * math.sin(math.radians(latitude)))
        ) / (cos_dec * math.cos(math.radians(latitude)))
        if cos_h > 1.0:
            # Sun never rises (polar night) — treat as always night.
            return datetime.combine(on_date, time(12, 0), tzinfo=tz)
        if cos_h < -1.0:
            # Sun never sets (midnight sun) — treat as always day.
            return datetime.combine(on_date, time(0 if rising else 23, 59), tzinfo=tz)
        h = (
            (360.0 - math.degrees(math.acos(cos_h))) / 15.0
            if rising
            else math.degrees(math.acos(cos_h)) / 15.0
        )
        local_mean = h + ra - (0.06571 * t) - 6.622
        utc_hour = (local_mean - lng_hour) % 24.0
        hour = int(utc_hour)
        minute = int((utc_hour - hour) * 60.0)
        second = int(round((((utc_hour - hour) * 60.0) - minute) * 60.0))
        if second >= 60:
            second -= 60
            minute += 1
        if minute >= 60:
            minute -= 60
            hour += 1
        hour %= 24
        utc_dt = datetime(
            on_date.year, on_date.month, on_date.day, hour, minute, second, tzinfo=timezone.utc
        )
        local = utc_dt.astimezone(tz)
        # Crossing midnight near poles / date line: keep on the requested civil day.
        if local.date() < on_date:
            local += timedelta(days=1)
        elif local.date() > on_date:
            local -= timedelta(days=1)
        return local

    sunrise = _event(True)
    sunset = _event(False)
    if sunset <= sunrise:
        sunset += timedelta(days=1)
    return sunrise, sunset


def is_daylight(
    when: datetime | None = None,
    *,
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
    tz_name: str = DEFAULT_TIMEZONE,
) -> bool:
    """True when ``when`` (default now) is between local sunrise and sunset."""
    tz = ZoneInfo(tz_name)
    now = when.astimezone(tz) if when is not None else datetime.now(tz)
    sunrise, sunset = solar_events(
        now.date(), latitude=latitude, longitude=longitude, tz_name=tz_name
    )
    return sunrise <= now < sunset


def clamp_latitude(value: float) -> float:
    return _clamp(float(value), -90.0, 90.0)


def clamp_longitude(value: float) -> float:
    return _clamp(float(value), -180.0, 180.0)
