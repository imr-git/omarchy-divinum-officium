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
import sys
import tempfile
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OFFICE_ENDPOINT = "https://www.divinumofficium.com/cgi-bin/horas/officium.pl"
CACHE_SCHEMA_VERSION = "5"
CACHE_RETENTION_DAYS = 3
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60 * 60
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


def weather_state_path() -> Path:
    """Return the location state owned by Omarchy's Weather panel."""
    return Path.home() / ".local/state/omarchy/settings/weather.json"


def load_weather_location(path: Path | None = None) -> dict[str, object] | None:
    try:
        payload = json.loads((path or weather_state_path()).read_text(encoding="utf-8"))
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return {
            "name": str(payload.get("name") or "Local weather location"),
            "latitude": latitude,
            "longitude": longitude,
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
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


def current_time() -> datetime:
    return datetime.now().astimezone()


@contextmanager
def request_lock():
    """Serialize metadata requests across helper processes."""
    path = request_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    return day.strftime("%A, %d %B %Y")


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


def parse_metadata(document: str, day: date, source_url: str) -> dict[str, object]:
    parser = VisibleTextParser()
    parser.feed(document)
    lines = parser.lines()
    title, rank, heading_color = find_heading(document)
    fallback_rank, rank_index = find_rank(lines)
    if not rank:
        rank = fallback_rank
    if not title:
        title = find_title(lines, rank_index, day)
    return {
        "date": day.isoformat(),
        "title": title,
        "rank": rank,
        "season": find_season(lines) or liturgical_season(day),
        # Only trust the color on the centered calendar heading; the rest of
        # the Office page uses red extensively for rubrics.
        "color": heading_color or liturgical_color(title),
        "commemorations": find_commemorations(lines),
        "sourceUrl": source_url,
        "fetchedAt": current_time().isoformat(timespec="seconds"),
        "stale": False,
        "error": "",
    }


def read_cache(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and not payload.get("error"):
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_cache(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(path)


def prune_cache(reference_day: date) -> None:
    """Keep today and the previous three civil days of successful metadata."""
    cutoff = reference_day - timedelta(days=CACHE_RETENTION_DAYS)
    for path in cache_dir().glob("report-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_day = date.fromisoformat(str(payload["date"]))
            if cached_day < cutoff:
                path.unlink()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue


def latest_cached_report(
    day: date, version: str, primary: str, secondary: str
) -> dict[str, object] | None:
    for days_ago in range(CACHE_RETENTION_DAYS + 1):
        cached = read_cache(cache_path(day - timedelta(days=days_ago), version, primary, secondary))
        if cached:
            return cached
    return None


def read_cooldown(now: datetime | None = None) -> tuple[dict[str, object], datetime] | None:
    try:
        payload = json.loads(cooldown_path().read_text(encoding="utf-8"))
        until = datetime.fromisoformat(str(payload["until"]))
        current = now or current_time()
        if until.tzinfo is None:
            until = until.replace(tzinfo=current.tzinfo)
        if until > current:
            return payload, until
        cooldown_path().unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return None


def write_cooldown(until: datetime) -> None:
    write_cache(
        cooldown_path(),
        {
            "until": until.isoformat(timespec="seconds"),
            "reason": "Divinum Officium rate limit",
        },
    )


def clear_cooldown() -> None:
    try:
        cooldown_path().unlink(missing_ok=True)
    except OSError:
        pass


def retry_after_seconds(error: HTTPError, now: datetime | None = None) -> int:
    value = error.headers.get("Retry-After") if error.headers else None
    if value:
        try:
            seconds = int(value)
            if seconds > 0:
                return seconds
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                current = now or current_time()
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=current.tzinfo)
                seconds = int((retry_at - current).total_seconds())
                if seconds > 0:
                    return seconds
            except (TypeError, ValueError, OverflowError):
                pass
    return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS


def fallback_report(
    day: date,
    version: str,
    primary: str,
    secondary: str,
    source_url: str,
    message: str,
    cooldown_until: datetime | None = None,
) -> dict[str, object]:
    stale = latest_cached_report(day, version, primary, secondary)
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
    return payload


def cache_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
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
    initial_mtime = cache_mtime(path)
    if not force:
        cached = read_cache(path)
        if cached:
            return cached

    with request_lock():
        cached = read_cache(path)
        current_mtime = cache_mtime(path)
        if not force and cached:
            return cached
        # A concurrent forced or initial request populated this cache while
        # this process waited for the lock, so do not make the same request.
        if cached and current_mtime != initial_mtime:
            return cached

        url = calendar_url(day, version, primary, secondary)
        now = current_time()
        cooldown = read_cooldown(now)
        if cooldown:
            _, until = cooldown
            retry_time = until.astimezone().strftime("%H:%M")
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                f"Divinum Officium requests are paused until {retry_time}.",
                until,
            )

        # The public CGI currently rejects bot-style user agents with HTTP
        # 403, while serving the same request to normal desktop browsers.
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                document = response.read().decode(
                    response.headers.get_content_charset() or "utf-8", errors="replace"
                )
        except HTTPError as error:
            if error.code == 429:
                until = now + timedelta(seconds=retry_after_seconds(error, now))
                try:
                    write_cooldown(until)
                except OSError:
                    pass
                retry_time = until.astimezone().strftime("%H:%M")
                return fallback_report(
                    day,
                    version,
                    primary,
                    secondary,
                    url,
                    f"Divinum Officium requests are paused until {retry_time}.",
                    until,
                )
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                f"Daily feast metadata unavailable: {error}",
            )
        except (URLError, TimeoutError, OSError) as error:
            return fallback_report(
                day,
                version,
                primary,
                secondary,
                url,
                f"Daily feast metadata unavailable: {error}",
            )

        payload = parse_metadata(document, day, url)
        try:
            write_cache(path, payload)
            clear_cooldown()
            prune_cache(day)
        except OSError:
            # A read-only or full cache should not hide otherwise valid data.
            pass
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report", help="Print today's liturgical metadata as JSON")
    report.add_argument("--date", default=date.today().isoformat(), help="Civil date in YYYY-MM-DD form")
    report.add_argument("--version", default="Tridentine - 1570")
    report.add_argument("--primary-language", default="Latin")
    report.add_argument("--secondary-language", default="English")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"report", "solar"}:
        try:
            requested_day = date.fromisoformat(args.date)
        except ValueError:
            print(json.dumps({"error": "Date must use YYYY-MM-DD."}))
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
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    if args.command == "solar":
        payload = solar_report(requested_day, args.weather_state)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
