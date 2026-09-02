from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "omadivoff.py"
SPEC = importlib.util.spec_from_file_location("omadivoff", MODULE_PATH)
assert SPEC and SPEC.loader
omadivoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadivoff)


class FakeResponse:
    def __init__(
        self,
        document: str,
        charset: str = "utf-8",
        content_length: int | None = None,
        max_chunk: int | None = None,
    ):
        self.document = document.encode("utf-8")
        self.offset = 0
        self.read_sizes = []
        self.max_chunk = max_chunk
        self.headers = Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}"
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            size = len(self.document) - self.offset
        if self.max_chunk is not None:
            size = min(size, self.max_chunk)
        start = self.offset
        self.offset = min(len(self.document), self.offset + size)
        return self.document[start:self.offset]


CALENDAR_DOCUMENT = """
<html><body>
  <P ALIGN=CENTER><FONT COLOR="red">In Decollatione S. Joannis Baptistæ ~ Duplex majus</FONT><br/>
  <I><SPAN><SPAN>Commemoratio:</SPAN> <FONT COLOR="red">S. Sabinæ Martyris</FONT></SPAN></I></P>
</body></html>
"""


class MetadataTests(unittest.TestCase):
    def test_calendar_url_requests_only_the_lightweight_heading(self):
        result = omadivoff.calendar_url(
            date(2026, 8, 29), "Tridentine - 1570", "Latin", "English"
        )
        self.assertIn("command=kalendar", result)
        self.assertNotIn("prayLaudes", result)
        self.assertIn("date1=08-29-2026", result)
        self.assertIn("version=Tridentine+-+1570", result)
        self.assertIn("lang1=Latin", result)
        self.assertIn("lang2=English", result)

    def test_parse_metadata_finds_title_rank_and_commemoration(self):
        document = """
        <html><body>
          <div>Die 29 Augusti 2026</div>
          <P ALIGN=CENTER><FONT COLOR="red">In Decollatione S. Joannis Baptistæ ~ Duplex majus</FONT><br/>
          <I><SPAN><SPAN>Commemoratio:</SPAN> <FONT COLOR="red">S. Sabinæ Martyris</FONT></SPAN></I></P>
        </body></html>
        """
        result = omadivoff.parse_metadata(
            document, date(2026, 8, 29), "https://example.test/office"
        )
        self.assertEqual("In Decollatione S. Joannis Baptistæ", result["title"])
        self.assertEqual("Duplex majus", result["rank"])
        self.assertEqual(["S. Sabinæ Martyris"], result["commemorations"])
        self.assertEqual("Time after Pentecost", result["season"])
        self.assertEqual("red", result["color"])

    def test_parse_metadata_accepts_a_green_sunday_heading(self):
        document = """
        <html><head><TITLE>Divinum Officium Laudes</TITLE></head><body>
          <P ALIGN=CENTER><FONT COLOR="green">Dominica XIV Post Pentecosten I. Septembris ~ Semiduplex Dominica minor</FONT><br/>
          <I><SPAN><SPAN>Commemoratio:</SPAN> <FONT COLOR="red">Ss. Felicis et Adaucti Martyrum</FONT></SPAN></I></P>
        </body></html>
        """
        result = omadivoff.parse_metadata(
            document, date(2026, 8, 30), "https://example.test/office"
        )
        self.assertEqual("Dominica XIV Post Pentecosten I. Septembris", result["title"])
        self.assertEqual("Semiduplex Dominica minor", result["rank"])
        self.assertEqual("green", result["color"])
        self.assertEqual(["Ss. Felicis et Adaucti Martyrum"], result["commemorations"])

    def test_parse_metadata_accepts_a_heading_without_a_commemoration(self):
        document = """
        <P ALIGN=CENTER><FONT COLOR="blue">In Assumptione Beatæ Mariæ Virginis ~ Duplex I. classis</FONT></P>
        """
        result = omadivoff.parse_metadata(
            document, date(2026, 8, 15), "https://example.test/office"
        )
        self.assertEqual("In Assumptione Beatæ Mariæ Virginis", result["title"])
        self.assertEqual("Duplex I. classis", result["rank"])
        self.assertEqual("white", result["color"])

    def test_parse_metadata_falls_back_to_visible_heading_when_markup_changes(self):
        document = """
        <html><body><div class="calendar-result">
          Feria Secunda infra Hebdomadam XIV post Octavam Pentecostes I. Septembris ~ Feria
        </div></body></html>
        """

        result = omadivoff.parse_metadata(
            document, date(2026, 8, 31), "https://example.test/office"
        )

        self.assertEqual(
            "Feria Secunda infra Hebdomadam XIV post Octavam Pentecostes I. Septembris",
            result["title"],
        )
        self.assertEqual("Feria", result["rank"])
        self.assertEqual("green", result["color"])

    def test_unrecognized_success_response_is_not_cached(self):
        response = FakeResponse(
            "<html><head><title>Attention Required</title></head>"
            "<body>Cloudflare challenge</body></html>"
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with patch.object(omadivoff, "urlopen", return_value=response):
                    report = omadivoff.fetch_report(
                        date(2026, 8, 29),
                        "Tridentine - 1570",
                        "Latin",
                        "English",
                        0.1,
                    )

            cached_reports = list(cache_directory.glob("report-*.json"))

        self.assertIn("recognizable liturgical heading", report["error"])
        self.assertEqual([], cached_reports)

    def test_liturgical_text_does_not_get_mistaken_for_the_season(self):
        document = """
        <P ALIGN=CENTER><FONT COLOR="red">S. Augustini Episcopi ~ Duplex</FONT><br/></P>
        <p>This lesson mentions Lent and Advent without identifying today's season.</p>
        """
        result = omadivoff.parse_metadata(document, date(2026, 8, 28), "https://example.test")
        self.assertEqual("Time after Pentecost", result["season"])

    def test_liturgical_seasons_follow_the_traditional_cycle(self):
        self.assertEqual("Advent", omadivoff.liturgical_season(date(2026, 12, 10)))
        self.assertEqual("Septuagesima", omadivoff.liturgical_season(date(2026, 2, 8)))
        self.assertEqual("Lent", omadivoff.liturgical_season(date(2026, 3, 1)))
        self.assertEqual("Eastertide", omadivoff.liturgical_season(date(2026, 4, 12)))

    def test_liturgical_color_uses_the_calendar_title_convention(self):
        self.assertEqual("red", omadivoff.liturgical_color("In Decollatione S. Joannis Baptistæ"))
        self.assertEqual("white", omadivoff.liturgical_color("Assumptio Beatæ Mariæ Virginis"))
        self.assertEqual("green", omadivoff.liturgical_color("Feria post Octavam Pentecostes"))
        self.assertEqual("violet", omadivoff.liturgical_color("Dominica I Adventus"))

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            payload = {"title": "Test feast", "stale": False}
            omadivoff.write_cache(path, payload)
            self.assertEqual(payload, omadivoff.read_cache(path))

    def test_successful_report_is_requested_only_once_per_civil_day(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                with patch.object(
                    omadivoff,
                    "urlopen",
                    side_effect=lambda *args, **kwargs: FakeResponse(CALENDAR_DOCUMENT),
                ) as mocked_open:
                    first = omadivoff.fetch_report(
                        date(2026, 8, 29), "Tridentine - 1570", "Latin", "English", 0.1
                    )
                    second = omadivoff.fetch_report(
                        date(2026, 8, 29), "Tridentine - 1570", "Latin", "English", 0.1
                    )
                    third = omadivoff.fetch_report(
                        date(2026, 8, 30), "Tridentine - 1570", "Latin", "English", 0.1
                    )

        self.assertEqual(first["title"], second["title"])
        self.assertEqual("2026-08-30", third["date"])
        self.assertEqual(2, mocked_open.call_count)

    def test_response_reader_uses_only_bounded_reads(self):
        response = FakeResponse(CALENDAR_DOCUMENT, max_chunk=17)

        document = omadivoff.read_response_text(response)

        self.assertIn("In Decollatione", document)
        self.assertTrue(response.read_sizes)
        self.assertTrue(all(0 < size <= 64 * 1024 for size in response.read_sizes))

    def test_oversized_response_is_rejected_without_writing_a_success_cache(self):
        response = FakeResponse("x" * (omadivoff.MAX_RESPONSE_BYTES + 1))
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with patch.object(omadivoff, "urlopen", return_value=response):
                    report = omadivoff.fetch_report(
                        date(2026, 8, 29),
                        "Tridentine - 1570",
                        "Latin",
                        "English",
                        0.1,
                    )

            cached_reports = list(cache_directory.glob("report-*.json"))

        self.assertIn("response limit", report["error"])
        self.assertEqual([], cached_reports)
        self.assertTrue(all(size <= 64 * 1024 for size in response.read_sizes))

    def test_declared_oversized_response_is_rejected_before_reading(self):
        response = FakeResponse(
            "small body",
            content_length=omadivoff.MAX_RESPONSE_BYTES + 1,
        )

        with self.assertRaises(omadivoff.ResponseTooLargeError):
            omadivoff.read_response_text(response)

        self.assertEqual([], response.read_sizes)

    def test_unknown_response_charset_falls_back_to_utf8(self):
        response = FakeResponse("Sanctæ Mariæ", charset="not-a-real-charset")

        self.assertEqual("Sanctæ Mariæ", omadivoff.read_response_text(response))

    def test_report_uses_previous_success_during_an_outage(self):
        current_day = date(2026, 8, 30)
        previous_day = current_day - timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                omadivoff.write_cache(
                    omadivoff.cache_path(
                        previous_day, "Tridentine - 1570", "Latin", "English"
                    ),
                    {
                        "date": previous_day.isoformat(),
                        "title": "Cached feast",
                        "rank": "Duplex",
                        "error": "",
                        "stale": False,
                    },
                )
                with patch.object(omadivoff, "urlopen", side_effect=OSError("offline")):
                    report = omadivoff.fetch_report(
                        current_day, "Tridentine - 1570", "Latin", "English", 0.1
                    )

        self.assertEqual("Cached feast", report["title"])
        self.assertEqual(previous_day.isoformat(), report["date"])
        self.assertEqual(current_day.isoformat(), report["requestedDate"])
        self.assertTrue(report["stale"])
        self.assertIn("Using cached metadata", report["error"])

    def test_429_persists_retry_after_cooldown_and_blocks_manual_refresh(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        error = HTTPError(
            "https://example.test",
            429,
            "Too Many Requests",
            {"Retry-After": "120"},
            None,
        )
        self.addCleanup(error.close)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                with patch.object(omadivoff, "current_time", return_value=now):
                    with patch.object(omadivoff, "urlopen", side_effect=error) as mocked_open:
                        first = omadivoff.fetch_report(
                            date(2026, 8, 31),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )
                        second = omadivoff.fetch_report(
                            date(2026, 8, 31),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                            force=True,
                        )

                cooldown = json.loads((Path(directory) / "rate-limit.json").read_text())

        self.assertEqual(1, mocked_open.call_count)
        self.assertEqual("2026-08-31T12:02:00+00:00", cooldown["until"])
        self.assertEqual(first["cooldownUntil"], second["cooldownUntil"])
        self.assertIn("requests are paused", second["error"])
        self.assertNotIn("is rate-limiting", second["error"])

    def test_403_persists_access_denied_cooldown_and_blocks_automatic_retry(self):
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        error = HTTPError("https://example.test", 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                with patch.object(omadivoff, "current_time", return_value=now):
                    with patch.object(omadivoff, "urlopen", side_effect=error) as mocked_open:
                        first = omadivoff.fetch_report(
                            date(2026, 9, 1),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )
                        second = omadivoff.fetch_report(
                            date(2026, 9, 1),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                            force=True,
                        )

                cooldown = json.loads((Path(directory) / "rate-limit.json").read_text())

        self.assertEqual(1, mocked_open.call_count)
        self.assertEqual("access-denied", cooldown["kind"])
        self.assertEqual("2026-09-01T18:00:00+00:00", cooldown["until"])
        self.assertEqual("access-denied", first["cooldownKind"])
        self.assertEqual(first["cooldownUntil"], second["cooldownUntil"])
        self.assertIn("denied the metadata request", second["error"])

    def test_429_without_retry_after_defaults_to_one_hour(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        error = HTTPError("https://example.test", 429, "Too Many Requests", {}, None)
        self.addCleanup(error.close)
        self.assertEqual(
            60 * 60,
            omadivoff.retry_after_seconds(error, now),
        )

    def test_retry_after_accepts_an_http_date(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        error = HTTPError(
            "https://example.test",
            429,
            "Too Many Requests",
            {"Retry-After": "Mon, 31 Aug 2026 12:05:00 GMT"},
            None,
        )
        self.addCleanup(error.close)
        self.assertEqual(5 * 60, omadivoff.retry_after_seconds(error, now))

    def test_manual_refresh_succeeds_after_cooldown_expires(self):
        clock = [datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)]
        error = HTTPError("https://example.test", 429, "Too Many Requests", {}, None)
        self.addCleanup(error.close)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                with patch.object(omadivoff, "current_time", side_effect=lambda: clock[0]):
                    with patch.object(
                        omadivoff,
                        "urlopen",
                        side_effect=[error, FakeResponse(CALENDAR_DOCUMENT)],
                    ) as mocked_open:
                        omadivoff.fetch_report(
                            date(2026, 8, 31),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )
                        clock[0] += timedelta(hours=1, seconds=1)
                        report = omadivoff.fetch_report(
                            date(2026, 8, 31),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                            force=True,
                        )

        self.assertEqual(2, mocked_open.call_count)
        self.assertEqual("In Decollatione S. Joannis Baptistæ", report["title"])
        self.assertEqual("", report["error"])

    def test_cache_pruning_retains_only_today_and_previous_three_days(self):
        current_day = date(2026, 8, 31)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                retained = omadivoff.cache_path(
                    current_day - timedelta(days=3),
                    "Tridentine - 1570",
                    "Latin",
                    "English",
                )
                expired = omadivoff.cache_path(
                    current_day - timedelta(days=4),
                    "Tridentine - 1570",
                    "Latin",
                    "English",
                )
                for path, cached_day in (
                    (retained, current_day - timedelta(days=3)),
                    (expired, current_day - timedelta(days=4)),
                ):
                    omadivoff.write_cache(
                        path,
                        {
                            "date": cached_day.isoformat(),
                            "title": "Cached feast",
                            "error": "",
                        },
                    )
                omadivoff.prune_cache(current_day)

                self.assertTrue(retained.exists())
                self.assertFalse(expired.exists())

    def test_simultaneous_requests_are_deduplicated(self):
        entered_request = threading.Event()
        release_request = threading.Event()

        def slow_response(*args, **kwargs):
            entered_request.set()
            release_request.wait(timeout=2)
            return FakeResponse(CALENDAR_DOCUMENT)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                with patch.object(omadivoff, "urlopen", side_effect=slow_response) as mocked_open:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        first = executor.submit(
                            omadivoff.fetch_report,
                            date(2026, 8, 31),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )
                        self.assertTrue(entered_request.wait(timeout=1))
                        second = executor.submit(
                            omadivoff.fetch_report,
                            date(2026, 8, 31),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )
                        time.sleep(0.05)
                        release_request.set()
                        reports = (first.result(timeout=2), second.result(timeout=2))

        self.assertEqual(1, mocked_open.call_count)
        self.assertEqual(reports[0]["title"], reports[1]["title"])

    def test_report_falls_back_cleanly_when_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(omadivoff, "cache_dir", return_value=Path(directory)):
                with patch.object(omadivoff, "urlopen", side_effect=OSError("offline")):
                    report = omadivoff.fetch_report(
                        date(2026, 8, 29), "Tridentine - 1570", "Latin", "English", 0.1
                    )
        self.assertEqual("2026-08-29", report["date"])
        self.assertIn("unavailable", report["error"])
        json.dumps(report)

    def test_solar_schedule_tracks_the_daylight_divisions(self):
        result = omadivoff.build_solar_schedule(
            date(2026, 3, 20), 0.0, 0.0, utc_offset_minutes=0
        )
        schedule = result["schedule"]
        self.assertEqual("06:05", result["sunrise"])
        self.assertEqual("18:11", result["sunset"])
        self.assertEqual(result["sunrise"], schedule["primeTime"])
        self.assertEqual("12:08", schedule["sextTime"])
        self.assertEqual(result["sunset"], schedule["vespersTime"])
        self.assertEqual("19:11", schedule["complineTime"])

    def test_solar_report_uses_omarchy_weather_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weather.json"
            path.write_text(
                json.dumps({"name": "Jackson", "latitude": 42.24587, "longitude": -84.40135}),
                encoding="utf-8",
            )
            report = omadivoff.solar_report(date(2026, 8, 29), path)
        self.assertEqual("Jackson", report["location"]["name"])
        self.assertEqual("", report["error"])
        self.assertIn("matinsTime", report["schedule"])

    def test_solar_report_explains_missing_weather_location(self):
        with tempfile.TemporaryDirectory() as directory:
            report = omadivoff.solar_report(date(2026, 8, 29), Path(directory) / "missing.json")
        self.assertIn("Omarchy Weather", report["error"])


if __name__ == "__main__":
    unittest.main()
