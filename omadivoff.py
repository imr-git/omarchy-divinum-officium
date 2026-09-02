#!/usr/bin/env python3
"""Fetch and cache the day's liturgical metadata from Divinum Officium."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import html
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


OFFICE_ENDPOINT = "https://www.divinumofficium.com/cgi-bin/horas/officium.pl"
CACHE_SCHEMA_VERSION = "6"
CACHE_RETENTION_DAYS = 3
MAX_CACHE_DIRECTORY_ENTRIES = 256
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CACHE_FILE_BYTES = 64 * 1024
MAX_WEATHER_STATE_BYTES = 16 * 1024
MAX_WEATHER_NAME_CHARS = 96
MAX_WEATHER_NAME_BYTES = 256
MAX_HELPER_OUTPUT_BYTES = 64 * 1024
MAX_HELPER_STDERR_BYTES = 8 * 1024
LOCK_WAIT_SECONDS = 10
MAX_RATE_LIMIT_COOLDOWN_SECONDS = 24 * 60 * 60
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60 * 60
DEFAULT_ACCESS_DENIED_COOLDOWN_SECONDS = 6 * 60 * 60
REQUEST_USER_AGENT = (
    "omarchy-divinum-officium "
    "(+https://github.com/imr-git/omarchy-divinum-officium)"
)
COOLDOWN_RATE_LIMIT = "rate-limit"
COOLDOWN_ACCESS_DENIED = "access-denied"
HEADING_COLOR_MAP = {
    "black": "black",
    "blue": "white",
    "green": "green",
    "purple": "violet",
    "red": "red",
    "rose": "rose",
}
RANK_WORDS = (
    "duplex",
    "semiduplex",
    "simplex",
    "classis",
    "feria",
    "dominica",
    "vigilia",
    "infra octavam",
)
OFFICE_VERSIONS = (
    "Tridentine - 1570",
    "Tridentine - 1888",
    "Tridentine - 1906",
    "Divino Afflatu - 1939",
    "Divino Afflatu - 1954",
    "Reduced - 1955",
    "Rubrics 1960 - 1960",
    "Rubrics 1960 - 2020 USA",
)
OFFICE_LANGUAGES = (
    "Latin",
    "English",
    "Deutsch",
    "French",
    "Italiano",
    "Magyar",
    "Polski",
    "Portuguese",
    "Espanol",
    "Cesky",
    "Nederlands",
)


class ResponseTooLargeError(RuntimeError):
    """Raised when an upstream response exceeds the configured safety limit."""


class MetadataParseError(RuntimeError):
    """Raised when an upstream response is not recognizable calendar metadata."""


class LocalDataSecurityError(OSError):
    """Raised when a local producer does not satisfy the safe-file contract."""


class HelperDeadlineError(TimeoutError):
    """Raised when the helper exceeds its total execution deadline."""


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow only HTTPS redirects that remain on Divinum Officium."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        try:
            destination = urlsplit(new_url)
            allowed = (
                destination.scheme == "https"
                and destination.hostname == "www.divinumofficium.com"
                and destination.port in {None, 443}
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise URLError("Refused a cross-origin metadata redirect.")
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


OFFICE_OPENER = build_opener(SameOriginRedirectHandler())


class BoundedTextStream:
    """Forward at most a fixed number of encoded bytes to a text stream."""

    def __init__(self, wrapped: TextIO, limit: int) -> None:
        self.wrapped = wrapped
        self.remaining = limit
        self.encoding = getattr(wrapped, "encoding", None) or "utf-8"

    def write(self, value: str) -> int:
        text = str(value)
        if self.remaining <= 0:
            return len(text)
        encoded = text.encode(self.encoding, errors="replace")
        chunk = encoded[: self.remaining]
        self.remaining -= len(chunk)
        buffer = getattr(self.wrapped, "buffer", None)
        if buffer is not None:
            buffer.write(chunk)
        else:
            self.wrapped.write(chunk.decode(self.encoding, errors="ignore"))
        return len(text)

    def flush(self) -> None:
        self.wrapped.flush()

    def fileno(self) -> int:
        return self.wrapped.fileno()

    def isatty(self) -> bool:
        return self.wrapped.isatty()


def _validate_owned_regular_file(
    descriptor: int,
    *,
    require_private: bool,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise LocalDataSecurityError("Local state is not a regular file.")
    if metadata.st_uid != os.geteuid():
        raise LocalDataSecurityError("Local state is not owned by the current user.")
    if metadata.st_nlink != 1:
        raise LocalDataSecurityError("Local state must have exactly one hard link.")
    if require_private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise LocalDataSecurityError("Local state permissions are not private.")
    return metadata


def _read_bounded_descriptor(descriptor: int, limit: int) -> bytes:
    metadata = os.fstat(descriptor)
    if metadata.st_size > limit:
        raise LocalDataSecurityError(f"Local state exceeds the {limit}-byte limit.")
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(16 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > limit:
        raise LocalDataSecurityError(f"Local state exceeds the {limit}-byte limit.")
    _validate_owned_regular_file(descriptor, require_private=False)
    return content


def read_owned_json_file(path: Path, limit: int) -> object:
    """Read a bounded, owned, regular, single-link JSON file without following it."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        _validate_owned_regular_file(descriptor, require_private=False)
        content = _read_bounded_descriptor(descriptor, limit)
    finally:
        os.close(descriptor)
    return json.loads(content.decode("utf-8"))


def weather_state_path() -> Path:
    """Return the location state owned by Omarchy's Weather panel."""
    return Path.home() / ".local/state/omarchy/settings/weather.json"


def load_weather_location(path: Path | None = None) -> dict[str, object] | None:
    try:
        payload = read_owned_json_file(
            path or weather_state_path(),
            MAX_WEATHER_STATE_BYTES,
        )
        if not isinstance(payload, dict):
            return None
        latitude_value = payload.get("latitude")
        longitude_value = payload.get("longitude")
        if (
            isinstance(latitude_value, bool)
            or not isinstance(latitude_value, (int, float))
            or isinstance(longitude_value, bool)
            or not isinstance(longitude_value, (int, float))
        ):
            return None
        latitude = float(latitude_value)
        longitude = float(longitude_value)
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
        ):
            return None
        raw_name = payload.get("name")
        if raw_name is None or raw_name == "":
            name = "Local weather location"
        elif not isinstance(raw_name, str):
            return None
        else:
            name = re.sub(r"\s+", " ", raw_name).strip()
            if (
                not name
                or not name.isprintable()
                or len(name) > MAX_WEATHER_NAME_CHARS
                or len(name.encode("utf-8")) > MAX_WEATHER_NAME_BYTES
            ):
                return None
        if not name:
            return None
        return {
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
        }
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        json.JSONDecodeError,
    ):
        return None


def local_utc_offset_minutes(day: date) -> int:
    """Resolve the machine's local UTC offset, including DST, for this date."""
    local_noon = datetime(day.year, day.month, day.day, 12).astimezone()
    offset = local_noon.utcoffset()
    return int(offset.total_seconds() // 60) if offset else 0


def solar_event_minutes(
    day: date,
    latitude: float,
    longitude: float,
    zenith: float = 90.833,
    utc_offset_minutes: int | None = None,
) -> tuple[float, float] | None:
    """Calculate local sunrise/sunset minutes with NOAA's fractional-year equations."""
    days_in_year = 366 if day.replace(month=12, day=31).timetuple().tm_yday == 366 else 365
    gamma = 2 * math.pi / days_in_year * (day.timetuple().tm_yday - 1)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    latitude_radians = math.radians(latitude)
    cosine_hour_angle = (
        math.cos(math.radians(zenith)) / (math.cos(latitude_radians) * math.cos(declination))
        - math.tan(latitude_radians) * math.tan(declination)
    )
    if cosine_hour_angle < -1 or cosine_hour_angle > 1:
        return None

    hour_angle = math.degrees(math.acos(cosine_hour_angle))
    solar_noon_utc = 720 - 4 * longitude - equation_of_time
    offset = local_utc_offset_minutes(day) if utc_offset_minutes is None else utc_offset_minutes
    sunrise = (solar_noon_utc - 4 * hour_angle + offset) % 1440
    sunset = (solar_noon_utc + 4 * hour_angle + offset) % 1440
    return sunrise, sunset


def format_minutes(value: float) -> str:
    minute = int(round(value)) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def build_solar_schedule(
    day: date,
    latitude: float,
    longitude: float,
    utc_offset_minutes: int | None = None,
) -> dict[str, object]:
    """Build a Benedictine-inspired schedule around the local solar day."""
    today = solar_event_minutes(day, latitude, longitude, utc_offset_minutes=utc_offset_minutes)
    previous_day = day - timedelta(days=1)
    previous = solar_event_minutes(
        previous_day,
        latitude,
        longitude,
        utc_offset_minutes=utc_offset_minutes,
    )
    dawn = solar_event_minutes(
        day,
        latitude,
        longitude,
        zenith=96.0,
        utc_offset_minutes=utc_offset_minutes,
    )
    if not today or not previous:
        raise ValueError("Sunrise or sunset does not occur at this location on this date.")

    sunrise, sunset = today
    previous_sunset = previous[1]
    civil_dawn = dawn[0] if dawn else (sunrise - 30) % 1440
    daylight = (sunset - sunrise) % 1440
    night = (sunrise - previous_sunset) % 1440
    if daylight <= 0 or night <= 0:
        raise ValueError("Could not derive the local solar day.")

    # RB 8 places the winter Night Office at the eighth hour of night.
    matins = (previous_sunset + night * 8 / 12) % 1440
    schedule = {
        "matinsTime": format_minutes(matins),
        "laudsTime": format_minutes(civil_dawn),
        "primeTime": format_minutes(sunrise),
        "terceTime": format_minutes(sunrise + daylight * 3 / 12),
        "sextTime": format_minutes(sunrise + daylight * 6 / 12),
        "noneTime": format_minutes(sunrise + daylight * 9 / 12),
        "vespersTime": format_minutes(sunset),
        "complineTime": format_minutes(sunset + 60),
    }
    return {
        "sunrise": format_minutes(sunrise),
        "sunset": format_minutes(sunset),
        "civilDawn": format_minutes(civil_dawn),
        "schedule": schedule,
    }


def solar_report(day: date, path: Path | None = None) -> dict[str, object]:
    location = load_weather_location(path)
    if not location:
        return {
            "date": day.isoformat(),
            "error": "Set a location in Omarchy Weather before enabling the solar schedule.",
        }
    try:
        result = build_solar_schedule(
            day,
            float(location["latitude"]),
            float(location["longitude"]),
        )
        result.update({"date": day.isoformat(), "location": location, "error": ""})
        return result
    except ValueError as error:
        return {"date": day.isoformat(), "location": location, "error": str(error)}


class VisibleTextParser(HTMLParser):
    """Collect human-visible text chunks without third-party dependencies."""

    BLOCKS = {"address", "article", "br", "div", "h1", "h2", "h3", "hr", "li", "p", "table", "td", "th", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in self.BLOCKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in self.BLOCKS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.chunks.append(data)

    def lines(self) -> list[str]:
        text = html.unescape("".join(self.chunks)).replace("\xa0", " ")
        return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "omadivoff"


def cache_path(day: date, version: str, primary: str, secondary: str) -> Path:
    identity = "\0".join((CACHE_SCHEMA_VERSION, day.isoformat(), version, primary, secondary)).encode()
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return cache_dir() / f"report-{digest}.json"


def cooldown_path() -> Path:
    return cache_dir() / "rate-limit.json"


def request_lock_path() -> Path:
    return cache_dir() / "request.lock"


def _cache_leaf(path: Path) -> str:
    configured_directory = os.path.abspath(os.fspath(cache_dir()))
    requested_directory = os.path.abspath(os.fspath(path.parent))
    if requested_directory != configured_directory:
        raise LocalDataSecurityError("Cache path escaped the configured cache directory.")
    name = path.name
    if not re.fullmatch(
        r"(?:report-[0-9a-f]{16}\.json|rate-limit\.json|request\.lock)",
        name,
    ):
        raise LocalDataSecurityError("Cache filename is not recognized.")
    return name


@contextmanager
def secure_cache_directory(*, create: bool = True):
    """Open and hold the owned private cache directory without following it."""
    path = cache_dir()
    parent = path.parent
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_descriptor = os.open(parent, flags)
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
        ):
            raise LocalDataSecurityError(
                "Cache parent is not an owned directory."
            )
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise LocalDataSecurityError("Cache path is not a directory.")
        if metadata.st_uid != os.geteuid():
            raise LocalDataSecurityError("Cache directory is not owned by the current user.")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            os.fchmod(descriptor, 0o700)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise LocalDataSecurityError("Cache directory permissions are not private.")
        yield descriptor
    finally:
        os.close(descriptor)


def _open_cache_file(
    cache_descriptor: int,
    name: str,
    *,
    flags: int = os.O_RDONLY,
    mode: int = 0o600,
) -> int:
    descriptor = os.open(
        name,
        flags | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW,
        mode,
        dir_fd=cache_descriptor,
    )
    try:
        _validate_owned_regular_file(descriptor, require_private=True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_cache_json(cache_descriptor: int, name: str) -> object:
    descriptor = _open_cache_file(cache_descriptor, name)
    try:
        content = _read_bounded_descriptor(descriptor, MAX_CACHE_FILE_BYTES)
        _validate_owned_regular_file(descriptor, require_private=True)
    finally:
        os.close(descriptor)
    return json.loads(content.decode("utf-8"))


def _cache_metadata(cache_descriptor: int, name: str) -> os.stat_result | None:
    try:
        descriptor = _open_cache_file(cache_descriptor, name)
    except FileNotFoundError:
        return None
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _unlink_cache_file(cache_descriptor: int, name: str) -> None:
    try:
        descriptor = _open_cache_file(cache_descriptor, name)
    except FileNotFoundError:
        return
    else:
        os.close(descriptor)
    os.unlink(name, dir_fd=cache_descriptor)


def current_time() -> datetime:
    return datetime.now().astimezone()


@contextmanager
def request_lock(cache_descriptor: int):
    """Serialize metadata requests across helper processes."""
    name = _cache_leaf(request_lock_path())
    flags = os.O_RDWR | os.O_CREAT
    try:
        descriptor = _open_cache_file(cache_descriptor, name, flags=flags)
    except LocalDataSecurityError:
        # v0.1.4 created the lock as 0644. Bind it safely first, then tighten
        # that legacy file through its descriptor before validating again.
        descriptor = os.open(
            name,
            flags | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW,
            0o600,
            dir_fd=cache_descriptor,
        )
        try:
            metadata = _validate_owned_regular_file(descriptor, require_private=False)
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                os.fchmod(descriptor, 0o600)
            _validate_owned_regular_file(descriptor, require_private=True)
        except BaseException:
            os.close(descriptor)
            raise
    try:
        os.fchmod(descriptor, 0o600)
        _validate_owned_regular_file(descriptor, require_private=True)
        lock_deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= lock_deadline:
                    raise HelperDeadlineError("Timed out waiting for the request lock.")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def site_date(day: date) -> str:
    return day.strftime("%m-%d-%Y")


def gregorian_easter(year: int) -> date:
    """Return Gregorian Easter using the algorithm used by Divinum Officium."""
    golden_number = year % 19
    century = year // 100
    correction = (
        century
        - century // 4
        - (8 * century + 13) // 25
        + 19 * golden_number
        + 15
    ) % 30
    adjusted = correction - (correction // 28) * (
        1
        - (correction // 28)
        * (29 // (correction + 1))
        * ((21 - golden_number) // 11)
    )
    weekday = (year + year // 4 + adjusted + 2 - century + century // 4) % 7
    offset = adjusted - weekday
    month = 3 + (offset + 40) // 44
    day_of_month = offset + 28 - 31 * (month // 4)
    return date(year, month, day_of_month)


def first_sunday_of_advent(year: int) -> date:
    christmas = date(year, 12, 25)
    days_since_sunday = (christmas.weekday() + 1) % 7 or 7
    return christmas - timedelta(days=days_since_sunday + 21)


def liturgical_season(day: date) -> str:
    """Return the broad traditional Roman season for a civil date."""
    advent = first_sunday_of_advent(day.year)
    christmas = date(day.year, 12, 25)
    if advent <= day < christmas:
        return "Advent"
    if day >= christmas or (day.month == 1 and day.day <= 13):
        return "Christmastide"

    easter = gregorian_easter(day.year)
    if day < easter - timedelta(days=63):
        return "Epiphanytide"
    if day < easter - timedelta(days=46):
        return "Septuagesima"
    if day < easter - timedelta(days=14):
        return "Lent"
    if day < easter:
        return "Passiontide"
    if day < easter + timedelta(days=39):
        return "Eastertide"
    if day < easter + timedelta(days=49):
        return "Ascensiontide"
    if day < easter + timedelta(days=56):
        return "Pentecost"
    return "Time after Pentecost"


def liturgical_color(title: str) -> str:
    """Map Divinum Officium's calendar-title convention to vestment colors."""
    if re.search(r"(?:Beat|Sanct)(?:ae|æ) Mari|Blessed Virgin|Our Lady|Virgin Mary", title, re.I) \
            and not re.search(r"Vigil", title, re.I):
        return "white"
    if re.search(
        r"Vigilia Pentecostes|Quattuor Temporum Pentecostes|Decollatione|Beheading|Martyr|Reliqui",
        title,
        re.I,
    ):
        return "red"
    if re.search(r"Defunctorum|Parasceve|Good Friday|Morte|Dead", title, re.I):
        return "black"
    if re.search(r"^In Vigilia Ascensionis|^In Vigilia Epiphani", title, re.I):
        return "white"
    if re.search(
        r"Vigilia|Quattuor|Rogatio|Passion|Palmis|gesim|Holy Week|Hebdomad(?:ae|æ)(?: Sanct(?:ae|æ))?|Sabbato Sancto|Dolorum|Ciner|Advent",
        title,
        re.I,
    ) and not re.search(r"commemoratione|votivum", title, re.I):
        return "violet"
    if re.search(r"Conversione|Dedicatione|Cathedra|oann|Pasch|Confessor|Ascensio|Cena", title, re.I):
        return "white"
    if re.search(r"Pentecosten(?!.*infra octavam)|Epiphaniam|post octavam", title, re.I):
        return "green"
    if re.search(r"Pentecostes|Evangel|Innocentium|Sanguinis|Cruc|Apostol", title, re.I):
        return "red"
    return "white"


def calendar_url(day: date, version: str, primary: str, secondary: str) -> str:
    if version not in OFFICE_VERSIONS:
        raise ValueError("Office version is not supported.")
    if primary not in OFFICE_LANGUAGES or secondary not in OFFICE_LANGUAGES:
        raise ValueError("Office language is not supported.")
    return OFFICE_ENDPOINT + "?" + urlencode(
        {
            # This returns only the calendar heading after precedence has
            # been calculated, instead of rendering an entire canonical hour.
            "command": "kalendar",
            "date1": site_date(day),
            "version": version,
            "lang1": primary,
            "lang2": secondary,
        }
    )


def clean_candidate(value: str) -> str:
    value = re.sub(r"^[✠*+~\-–—\s]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" |·:-")
    return value


def is_navigation(line: str) -> bool:
    lowered = line.casefold()
    words = (
        "divinum officium",
        "sancta missa",
        "compare",
        "ordo",
        "setup",
        "help",
        "previous",
        "next",
        "today",
        "matutinum",
        "laudes",
        "prima",
        "tertia",
        "sexta",
        "nona",
        "vesperae",
        "completorium",
    )
    return len(line) < 3 or any(line.casefold() == word for word in words) or lowered.startswith("pray ")


def find_rank(lines: list[str]) -> tuple[str, int]:
    for index, line in enumerate(lines[:160]):
        lowered = line.casefold()
        if any(word in lowered for word in RANK_WORDS) and len(line) <= 160:
            return clean_candidate(line), index
    return "", -1


def find_title(lines: list[str], rank_index: int, day: date) -> str:
    date_tokens = {str(day.year), day.strftime("%B").casefold(), day.strftime("%b").casefold()}
    upper = rank_index if rank_index >= 0 else min(len(lines), 100)
    for line in reversed(lines[max(0, upper - 12) : upper]):
        candidate = clean_candidate(line)
        lowered = candidate.casefold()
        if is_navigation(candidate) or len(candidate) > 180:
            continue
        if sum(token in lowered for token in date_tokens) >= 2:
            continue
        return candidate
    return ""


def find_commemorations(lines: Iterable[str]) -> list[str]:
    result: list[str] = []
    pattern = re.compile(r"(?:commemoratio|commemoration(?:s)?)(?:\s*[:—-]\s*)(.+)", re.IGNORECASE)
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        candidate = clean_candidate(match.group(1))
        if candidate and candidate not in result:
            result.append(candidate)
    return result[:4]


def find_season(lines: Iterable[str]) -> str:
    pattern = re.compile(r"(?:liturgical\s+season|season|tempus)\s*[:—-]\s*(.+)", re.IGNORECASE)
    for line in lines:
        match = pattern.fullmatch(line)
        if match:
            return clean_candidate(match.group(1))
    return ""


def find_heading(document: str) -> tuple[str, str, str]:
    """Read the generated `Feast ~ Rank` heading and its calendar color."""
    match = re.search(
        r'<P\s+ALIGN=["\']?CENTER["\']?[^>]*>\s*<FONT\b([^>]*)>(.*?)</FONT>(?:\s*<br\s*/?>|\s*</P>)',
        document,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return "", "", ""
    color_match = re.search(r'\bCOLOR\s*=\s*["\']?([A-Za-z]+)', match.group(1), re.IGNORECASE)
    color = HEADING_COLOR_MAP.get(color_match.group(1).casefold(), "") if color_match else ""
    heading_parser = VisibleTextParser()
    heading_parser.feed(match.group(2))
    heading = clean_candidate(" ".join(heading_parser.lines()))
    if "~" not in heading:
        return heading, "", color
    title, rank = heading.rsplit("~", 1)
    return clean_candidate(title), clean_candidate(rank), color


def find_visible_heading(lines: Iterable[str]) -> tuple[str, str]:
    """Find a `Feast ~ Rank` heading when the site's HTML wrapper changes."""
    for line in lines:
        if "~" not in line:
            continue
        title, rank = (clean_candidate(part) for part in line.rsplit("~", 1))
        lowered_rank = rank.casefold()
        if title and rank and any(word in lowered_rank for word in RANK_WORDS):
            return title, rank
    return "", ""


def parse_metadata(document: str, day: date, source_url: str) -> dict[str, object]:
    parser = VisibleTextParser()
    parser.feed(document)
    lines = parser.lines()
    title, rank, heading_color = find_heading(document)
    visible_title, visible_rank = find_visible_heading(lines[:160])
    if not title:
        title = visible_title
    if not rank:
        rank = visible_rank
    fallback_rank, rank_index = find_rank(lines)
    if not rank:
        rank = fallback_rank
    if not title:
        title = find_title(lines, rank_index, day)
    if not title or not rank:
        raise MetadataParseError(
            "Daily feast metadata response did not contain a recognizable liturgical heading."
        )
    season = find_season(lines) or liturgical_season(day)
    commemorations = find_commemorations(lines)
    if (
        len(title) > 240
        or len(rank) > 200
        or len(season) > 120
        or any(len(commemoration) > 240 for commemoration in commemorations)
    ):
        raise MetadataParseError("Daily feast metadata contained an oversized display field.")
    return {
        "date": day.isoformat(),
        "title": title,
        "rank": rank,
        "season": season,
        # Only trust the color on the centered calendar heading; the rest of
        # the Office page uses red extensively for rubrics.
        "color": heading_color or liturgical_color(title),
        "commemorations": commemorations,
        "sourceUrl": source_url,
        "fetchedAt": current_time().isoformat(timespec="seconds"),
        "stale": False,
        "error": "",
    }


@contextmanager
def _cache_scope(cache_descriptor: int | None, *, create: bool = True):
    if cache_descriptor is not None:
        yield cache_descriptor
        return
    with secure_cache_directory(create=create) as opened_descriptor:
        yield opened_descriptor


def _bounded_text(value: object, limit: int, *, allow_empty: bool = True) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    if not allow_empty and not value:
        return None
    if any(ord(character) < 0x20 and character not in "\t\n\r" for character in value):
        return None
    return value


def validate_cached_report(payload: object) -> dict[str, object] | None:
    """Return a narrow, bounded cache schema suitable for JSON/QML output."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    cached_date = _bounded_text(payload.get("date"), 10, allow_empty=False)
    title = _bounded_text(payload.get("title"), 240, allow_empty=False)
    if cached_date is None or title is None:
        return None
    try:
        date.fromisoformat(cached_date)
    except ValueError:
        return None

    result: dict[str, object] = {"date": cached_date, "title": title}
    for key, limit in (
        ("rank", 200),
        ("season", 120),
        ("sourceUrl", 2048),
        ("fetchedAt", 64),
    ):
        value = _bounded_text(payload.get(key), limit)
        if value is None:
            return None
        result[key] = value

    color = payload.get("color", "")
    if color not in {"", "black", "green", "red", "rose", "violet", "white"}:
        return None
    result["color"] = color

    commemorations = payload.get("commemorations", [])
    if not isinstance(commemorations, list) or len(commemorations) > 4:
        return None
    bounded_commemorations: list[str] = []
    for commemoration in commemorations:
        value = _bounded_text(commemoration, 240, allow_empty=False)
        if value is None:
            return None
        bounded_commemorations.append(value)
    result["commemorations"] = bounded_commemorations

    stale = payload.get("stale", False)
    if not isinstance(stale, bool):
        return None
    result["stale"] = stale
    result["error"] = ""
    return result


def read_cache(
    path: Path,
    cache_descriptor: int | None = None,
    expected_date: date | None = None,
) -> dict[str, object] | None:
    try:
        name = _cache_leaf(path)
        with _cache_scope(cache_descriptor, create=False) as descriptor:
            payload = validate_cached_report(_read_cache_json(descriptor, name))
            if payload is not None and expected_date is not None:
                if payload["date"] != expected_date.isoformat():
                    return None
            return payload
    except FileNotFoundError:
        return None
    except (
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        json.JSONDecodeError,
    ):
        return None


def _encoded_cache_payload(payload: dict[str, object]) -> bytes:
    try:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise LocalDataSecurityError("Cache payload is not bounded JSON.") from error
    if len(content) > MAX_CACHE_FILE_BYTES:
        raise LocalDataSecurityError(
            f"Cache payload exceeds the {MAX_CACHE_FILE_BYTES}-byte limit."
        )
    return content


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("Could not finish writing local state.")
        offset += written


def write_cache(
    path: Path,
    payload: dict[str, object],
    cache_descriptor: int | None = None,
) -> None:
    content = _encoded_cache_payload(payload)
    name = _cache_leaf(path)
    with _cache_scope(cache_descriptor) as descriptor:
        # Validate any existing destination before atomically replacing it.
        _cache_metadata(descriptor, name)
        temporary_name = f".tmp-{os.getpid()}-{secrets.token_hex(12)}"
        temporary_descriptor: int | None = None
        try:
            temporary_descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            os.fchmod(temporary_descriptor, 0o600)
            _validate_owned_regular_file(temporary_descriptor, require_private=True)
            _write_all(temporary_descriptor, content)
            os.fsync(temporary_descriptor)
            _validate_owned_regular_file(temporary_descriptor, require_private=True)
            os.close(temporary_descriptor)
            temporary_descriptor = None
            os.replace(
                temporary_name,
                name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            os.fsync(descriptor)
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            try:
                os.unlink(temporary_name, dir_fd=descriptor)
            except FileNotFoundError:
                pass


def prune_cache(reference_day: date, cache_descriptor: int | None = None) -> None:
    """Keep today and the previous three civil days of successful metadata."""
    cutoff = reference_day - timedelta(days=CACHE_RETENTION_DAYS)
    with _cache_scope(cache_descriptor, create=False) as descriptor:
        with os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_CACHE_DIRECTORY_ENTRIES:
                    break
                name = entry.name
                if not re.fullmatch(r"report-[0-9a-f]{16}\.json", name):
                    continue
                payload = read_cache(cache_dir() / name, descriptor)
                if payload is None:
                    continue
                try:
                    cached_day = date.fromisoformat(str(payload["date"]))
                    if cached_day < cutoff:
                        _unlink_cache_file(descriptor, name)
                except (OSError, ValueError, TypeError, KeyError):
                    continue


def latest_cached_report(
    day: date,
    version: str,
    primary: str,
    secondary: str,
    cache_descriptor: int | None = None,
) -> dict[str, object] | None:
    for days_ago in range(CACHE_RETENTION_DAYS + 1):
        candidate_day = day - timedelta(days=days_ago)
        cached = read_cache(
            cache_path(candidate_day, version, primary, secondary),
            cache_descriptor,
            candidate_day,
        )
        if cached:
            return cached
    return None


def validate_cooldown(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    until = _bounded_text(payload.get("until"), 64, allow_empty=False)
    kind = payload.get("kind")
    reason = _bounded_text(payload.get("reason"), 160, allow_empty=False)
    if until is None or reason is None or kind not in {
        COOLDOWN_RATE_LIMIT,
        COOLDOWN_ACCESS_DENIED,
    }:
        return None
    return {"until": until, "kind": kind, "reason": reason}


def read_cooldown(
    now: datetime | None = None,
    cache_descriptor: int | None = None,
) -> tuple[dict[str, object], datetime] | None:
    try:
        name = _cache_leaf(cooldown_path())
        with _cache_scope(cache_descriptor, create=False) as descriptor:
            payload = validate_cooldown(_read_cache_json(descriptor, name))
            if payload is None:
                return None
            until = datetime.fromisoformat(str(payload["until"]))
            current = now or current_time()
            if until.tzinfo is None:
                until = until.replace(tzinfo=current.tzinfo)
            if until > current:
                return payload, until
            _unlink_cache_file(descriptor, name)
    except FileNotFoundError:
        return None
    except (
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        json.JSONDecodeError,
    ):
        pass
    return None


def write_cooldown(
    until: datetime,
    kind: str = COOLDOWN_RATE_LIMIT,
    cache_descriptor: int | None = None,
) -> None:
    reason = (
        "Divinum Officium access denial"
        if kind == COOLDOWN_ACCESS_DENIED
        else "Divinum Officium rate limit"
    )
    write_cache(
        cooldown_path(),
        {
            "until": until.isoformat(timespec="seconds"),
            "kind": kind,
            "reason": reason,
        },
        cache_descriptor,
    )


def clear_cooldown(cache_descriptor: int | None = None) -> None:
    try:
        name = _cache_leaf(cooldown_path())
        with _cache_scope(cache_descriptor, create=False) as descriptor:
            _unlink_cache_file(descriptor, name)
    except OSError:
        pass


def retry_after_seconds(error: HTTPError, now: datetime | None = None) -> int:
    value = error.headers.get("Retry-After") if error.headers else None
    if value:
        try:
            seconds = int(value)
            if seconds > 0:
                return min(seconds, MAX_RATE_LIMIT_COOLDOWN_SECONDS)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                current = now or current_time()
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=current.tzinfo)
                seconds = int((retry_at - current).total_seconds())
                if seconds > 0:
                    return min(seconds, MAX_RATE_LIMIT_COOLDOWN_SECONDS)
            except (TypeError, ValueError, OverflowError):
                pass
    return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS


def cooldown_message(kind: str, retry_time: str) -> str:
    if kind == COOLDOWN_ACCESS_DENIED:
        return (
            "Divinum Officium denied the metadata request; automatic retries "
            f"are paused until {retry_time}."
        )
    return f"Divinum Officium requests are paused until {retry_time}."


def read_response_text(response: object, limit: int = MAX_RESPONSE_BYTES) -> str:
    """Read and decode an HTTP response without allowing unbounded memory use."""
    headers = response.headers
    content_length = headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise ResponseTooLargeError(
                    f"Daily feast metadata exceeded the {limit}-byte response limit."
                )
        except ValueError:
            # A malformed Content-Length is untrusted metadata; the bounded
            # reads below remain the source of truth.
            pass

    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = response.read(min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)

    body = b"".join(chunks)
    if len(body) > limit:
        raise ResponseTooLargeError(
            f"Daily feast metadata exceeded the {limit}-byte response limit."
        )

    charset = headers.get_content_charset() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def open_office_request(request: Request, timeout: float):
    return OFFICE_OPENER.open(request, timeout=timeout)


def fallback_report(
    day: date,
    version: str,
    primary: str,
    secondary: str,
    source_url: str,
    message: str,
    cooldown_until: datetime | None = None,
    cooldown_kind: str | None = None,
    cache_descriptor: int | None = None,
    use_cache: bool = True,
) -> dict[str, object]:
    message = message[:1024]
    stale = (
        latest_cached_report(day, version, primary, secondary, cache_descriptor)
        if use_cache
        else None
    )
    if stale:
        payload = dict(stale)
        payload["stale"] = True
        payload["requestedDate"] = day.isoformat()
        cached_day = str(payload.get("date") or "an earlier day")
        payload["error"] = f"Using cached metadata from {cached_day}. {message}"
    else:
        payload = {
            "date": day.isoformat(),
            "title": day.strftime("%A, %d %B %Y"),
            "rank": "",
            "season": liturgical_season(day),
            "color": "",
            "commemorations": [],
            "sourceUrl": source_url,
            "fetchedAt": "",
            "stale": False,
            "error": message,
        }
    if cooldown_until:
        payload["cooldownUntil"] = cooldown_until.isoformat(timespec="seconds")
        payload["cooldownKind"] = cooldown_kind or COOLDOWN_RATE_LIMIT
    return payload


def cache_mtime(path: Path, cache_descriptor: int | None = None) -> int | None:
    try:
        name = _cache_leaf(path)
        with _cache_scope(cache_descriptor, create=False) as descriptor:
            metadata = _cache_metadata(descriptor, name)
            return metadata.st_mtime_ns if metadata else None
    except FileNotFoundError:
        return None


def fetch_report(
    day: date,
    version: str,
    primary: str,
    secondary: str,
    timeout: float,
    force: bool = False,
) -> dict[str, object]:
    path = cache_path(day, version, primary, secondary)
    url = calendar_url(day, version, primary, secondary)
    try:
        with secure_cache_directory() as cache_descriptor:
            return _fetch_report_with_cache(
                day,
                version,
                primary,
                secondary,
                min(max(0.1, timeout), 8.0),
                force,
                path,
                url,
                cache_descriptor,
            )
    except HelperDeadlineError as error:
        return fallback_report(
            day,
            version,
            primary,
            secondary,
            url,
            str(error),
            use_cache=False,
        )
    except OSError:
        return fallback_report(
            day,
            version,
            primary,
            secondary,
            url,
            "Local metadata state failed its safety checks; no request was made.",
            use_cache=False,
        )


def _fetch_report_with_cache(
    day: date,
    version: str,
    primary: str,
    secondary: str,
    timeout: float,
    force: bool,
    path: Path,
    url: str,
    cache_descriptor: int,
) -> dict[str, object]:
    report_metadata = _cache_metadata(cache_descriptor, _cache_leaf(path))
    if report_metadata is not None and report_metadata.st_size > MAX_CACHE_FILE_BYTES:
        raise LocalDataSecurityError("Cached report exceeds its byte limit.")
    initial_mtime = cache_mtime(path, cache_descriptor)
    if not force:
        cached = read_cache(path, cache_descriptor, day)
        if cached:
            return cached

    with request_lock(cache_descriptor):
        cached = read_cache(path, cache_descriptor, day)
        current_mtime = cache_mtime(path, cache_descriptor)
        if not force and cached:
            return cached
        # A concurrent forced or initial request populated this cache while
        # this process waited for the lock, so do not make the same request.
        if cached and current_mtime != initial_mtime:
            return cached

        now = current_time()
        cooldown_metadata = _cache_metadata(
            cache_descriptor,
            _cache_leaf(cooldown_path()),
        )
        if cooldown_metadata is not None and cooldown_metadata.st_size > MAX_CACHE_FILE_BYTES:
            raise LocalDataSecurityError("Cooldown state exceeds its byte limit.")
        cooldown = read_cooldown(now, cache_descriptor)
        if cooldown:
            cooldown_payload, until = cooldown
            cooldown_kind = str(
                cooldown_payload.get("kind") or COOLDOWN_RATE_LIMIT
            )
            retry_time = until.astimezone().strftime("%H:%M")
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                cooldown_message(cooldown_kind, retry_time),
                until,
                cooldown_kind,
                cache_descriptor,
            )

        request = Request(
            url,
            headers={
                "User-Agent": REQUEST_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with open_office_request(request, timeout) as response:
                document = read_response_text(response)
            payload = parse_metadata(document, day, url)
        except HTTPError as error:
            if error.code in {403, 429}:
                cooldown_kind = (
                    COOLDOWN_ACCESS_DENIED
                    if error.code == 403
                    else COOLDOWN_RATE_LIMIT
                )
                cooldown_seconds = (
                    DEFAULT_ACCESS_DENIED_COOLDOWN_SECONDS
                    if error.code == 403
                    else retry_after_seconds(error, now)
                )
                until = now + timedelta(seconds=cooldown_seconds)
                try:
                    write_cooldown(until, cooldown_kind, cache_descriptor)
                except OSError:
                    pass
                retry_time = until.astimezone().strftime("%H:%M")
                return fallback_report(
                    day,
                    version,
                    primary,
                    secondary,
                    url,
                    cooldown_message(cooldown_kind, retry_time),
                    until,
                    cooldown_kind,
                    cache_descriptor,
                )
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                f"Daily feast metadata unavailable: {error}",
                cache_descriptor=cache_descriptor,
            )
        except (ResponseTooLargeError, MetadataParseError) as error:
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                str(error),
                cache_descriptor=cache_descriptor,
            )
        except (URLError, TimeoutError, OSError) as error:
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                f"Daily feast metadata unavailable: {error}",
                cache_descriptor=cache_descriptor,
            )

        try:
            write_cache(path, payload, cache_descriptor)
            clear_cooldown(cache_descriptor)
            prune_cache(day, cache_descriptor)
        except OSError:
            # A read-only or full cache should not hide otherwise valid data.
            pass
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report", help="Print today's liturgical metadata as JSON")
    report.add_argument("--date", default=date.today().isoformat(), help="Civil date in YYYY-MM-DD form")
    report.add_argument(
        "--version",
        choices=OFFICE_VERSIONS,
        default="Tridentine - 1570",
    )
    report.add_argument(
        "--primary-language",
        choices=OFFICE_LANGUAGES,
        default="Latin",
    )
    report.add_argument(
        "--secondary-language",
        choices=OFFICE_LANGUAGES,
        default="English",
    )
    report.add_argument("--timeout", type=float, default=8.0)
    report.add_argument(
        "--force",
        action="store_true",
        help="Bypass the daily success cache; an active cooldown is still enforced",
    )
    solar = subparsers.add_parser("solar", help="Print a local solar canonical-hour schedule as JSON")
    solar.add_argument("--date", default=date.today().isoformat(), help="Civil date in YYYY-MM-DD form")
    solar.add_argument("--weather-state", type=Path, default=None, help="Override Omarchy's Weather state file")
    return parser


def emit_json(payload: dict[str, object]) -> None:
    """Write exactly one JSON result while enforcing the QML producer ceiling."""
    try:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        content = b'{"error":"Helper produced invalid JSON."}'
    if len(content) > MAX_HELPER_OUTPUT_BYTES:
        content = b'{"error":"Helper output exceeded its safety limit."}'
    sys.stdout.write(content.decode("utf-8"))
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"report", "solar"}:
        try:
            requested_day = date.fromisoformat(args.date)
        except ValueError:
            emit_json({"error": "Date must use YYYY-MM-DD."})
            return 2
    if args.command == "report":
        payload = fetch_report(
            requested_day,
            args.version,
            args.primary_language,
            args.secondary_language,
            max(0.1, args.timeout),
            force=args.force,
        )
        emit_json(payload)
        return 0
    if args.command == "solar":
        payload = solar_report(requested_day, args.weather_state)
        emit_json(payload)
        return 0
    return 2


def run_cli() -> int:
    """Install output ceilings before parsing or touching local/external data."""
    sys.stdout = BoundedTextStream(sys.stdout, MAX_HELPER_OUTPUT_BYTES)  # type: ignore[assignment]
    sys.stderr = BoundedTextStream(sys.stderr, MAX_HELPER_STDERR_BYTES)  # type: ignore[assignment]
    try:
        return main()
    except BrokenPipeError:
        return 1
    except Exception:
        emit_json({"error": "Metadata helper failed safely."})
        return 1


if __name__ == "__main__":
    sys.exit(run_cli())
