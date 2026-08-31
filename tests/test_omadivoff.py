import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "omadivoff.py"
SPEC = importlib.util.spec_from_file_location("omadivoff", MODULE_PATH)
assert SPEC and SPEC.loader
omadivoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadivoff)


class MetadataTests(unittest.TestCase):
    def test_office_url_contains_selected_parameters(self):
        result = omadivoff.office_url(
            date(2026, 8, 29), "Tridentine - 1570", "Latin", "English"
        )
        self.assertIn("command=prayLaudes", result)
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
