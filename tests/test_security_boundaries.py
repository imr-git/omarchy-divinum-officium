from email.message import Message
import fcntl
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request


MODULE_PATH = Path(__file__).resolve().parents[1] / "omadivoff.py"
SPEC = importlib.util.spec_from_file_location("omadivoff_security", MODULE_PATH)
assert SPEC and SPEC.loader
omadivoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadivoff)


def report_payload(day: date) -> dict[str, object]:
    return {
        "date": day.isoformat(),
        "title": "Test feast",
        "rank": "Duplex",
        "season": "Time after Pentecost",
        "color": "white",
        "commemorations": [],
        "sourceUrl": "https://www.divinumofficium.com/",
        "fetchedAt": "2026-09-01T12:00:00+00:00",
        "stale": False,
        "error": "",
    }


def process_state(process_id: int) -> str | None:
    """Return a Linux process state, treating any /proc disappearance as exit."""
    try:
        fields = Path(f"/proc/{process_id}/stat").read_text().split()
    except OSError:
        return None
    return fields[2] if len(fields) > 2 else None


def wait_for_pid_file(path: Path, timeout: float = 2) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value.isascii() and value.isdigit():
            return int(value)
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for child PID in {path.name}.")


class LocalProducerSecurityTests(unittest.TestCase):
    def test_weather_rejects_symlink_hardlink_fifo_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.json"
            valid.write_text(
                json.dumps({"name": "Jackson", "latitude": 42.2, "longitude": -84.4}),
                encoding="utf-8",
            )

            symlink = root / "symlink.json"
            symlink.symlink_to(valid)
            self.assertIsNone(omadivoff.load_weather_location(symlink))

            hardlink = root / "hardlink.json"
            os.link(valid, hardlink)
            self.assertIsNone(omadivoff.load_weather_location(hardlink))

            fifo = root / "weather.fifo"
            os.mkfifo(fifo, 0o600)
            started = time.monotonic()
            self.assertIsNone(omadivoff.load_weather_location(fifo))
            self.assertLess(time.monotonic() - started, 0.5)

            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (omadivoff.MAX_WEATHER_STATE_BYTES + 1))
            self.assertIsNone(omadivoff.load_weather_location(oversized))

    def test_weather_rejects_invalid_schema_and_pathological_json(self):
        invalid_payloads = (
            {"name": "Boolean", "latitude": True, "longitude": -84.4},
            {"name": "NaN", "latitude": float("nan"), "longitude": -84.4},
            {"name": {"nested": "object"}, "latitude": 42.2, "longitude": -84.4},
            {"name": "x" * 97, "latitude": 42.2, "longitude": -84.4},
            {"name": "control\u0001name", "latitude": 42.2, "longitude": -84.4},
            ["not", "an", "object"],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, payload in enumerate(invalid_payloads):
                path = root / f"invalid-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(omadivoff.load_weather_location(path))

            deep = root / "deep.json"
            deep.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            self.assertIsNone(omadivoff.load_weather_location(deep))

            invalid_utf8 = root / "invalid-utf8.json"
            invalid_utf8.write_bytes(b"{\xff}")
            self.assertIsNone(omadivoff.load_weather_location(invalid_utf8))

    def test_cache_directory_and_legacy_lock_are_tightened_by_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            cache_directory.mkdir(mode=0o755)
            cache_directory.chmod(0o755)
            lock = cache_directory / "request.lock"
            lock.write_bytes(b"")
            lock.chmod(0o644)

            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with omadivoff.secure_cache_directory() as descriptor:
                    with omadivoff.request_lock(descriptor):
                        pass

            self.assertEqual(0o700, stat.S_IMODE(cache_directory.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(lock.stat().st_mode))

    def test_cache_directory_symlink_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            linked = root / "omadivoff"
            linked.symlink_to(real, target_is_directory=True)
            with patch.object(omadivoff, "cache_dir", return_value=linked):
                with patch.object(omadivoff, "open_office_request") as request:
                    result = omadivoff.fetch_report(
                        date(2026, 9, 1),
                        "Tridentine - 1570",
                        "Latin",
                        "English",
                        0.1,
                    )
            request.assert_not_called()
            self.assertIn("safety checks", result["error"])

    def test_unsafe_report_entries_fail_closed_without_touching_victims(self):
        constructors = ("symlink", "hardlink", "fifo", "oversized", "permissive")
        for constructor in constructors:
            with self.subTest(constructor=constructor):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    cache_directory = root / "omadivoff"
                    cache_directory.mkdir(mode=0o700)
                    victim = root / "victim"
                    victim.write_text("do not touch", encoding="utf-8")
                    with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                        report = omadivoff.cache_path(
                            date(2026, 9, 1),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                        )
                        if constructor == "symlink":
                            report.symlink_to(victim)
                        elif constructor == "hardlink":
                            os.link(victim, report)
                        elif constructor == "fifo":
                            os.mkfifo(report, 0o600)
                        elif constructor == "oversized":
                            report.write_bytes(b"x" * (omadivoff.MAX_CACHE_FILE_BYTES + 1))
                            report.chmod(0o600)
                        else:
                            report.write_text("{}", encoding="utf-8")
                            report.chmod(0o644)

                        started = time.monotonic()
                        with patch.object(omadivoff, "open_office_request") as request:
                            result = omadivoff.fetch_report(
                                date(2026, 9, 1),
                                "Tridentine - 1570",
                                "Latin",
                                "English",
                                0.1,
                            )

                    request.assert_not_called()
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertIn("safety checks", result["error"])
                    self.assertEqual("do not touch", victim.read_text(encoding="utf-8"))

    def test_stalled_lock_has_an_internal_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            cache_directory.mkdir(mode=0o700)
            lock_path = cache_directory / "request.lock"
            held = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                    with patch.object(omadivoff, "LOCK_WAIT_SECONDS", 0.05):
                        with patch.object(omadivoff, "open_office_request") as request:
                            result = omadivoff.fetch_report(
                                date(2026, 9, 1),
                                "Tridentine - 1570",
                                "Latin",
                                "English",
                                0.1,
                            )
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)

            request.assert_not_called()
            self.assertIn("Timed out waiting", result["error"])

    def test_unsafe_lock_entries_fail_fast_without_network(self):
        for constructor in ("symlink", "hardlink", "fifo"):
            with self.subTest(constructor=constructor):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    cache_directory = root / "omadivoff"
                    cache_directory.mkdir(mode=0o700)
                    victim = root / "victim"
                    victim.write_text("unchanged", encoding="utf-8")
                    lock = cache_directory / "request.lock"
                    if constructor == "symlink":
                        lock.symlink_to(victim)
                    elif constructor == "hardlink":
                        os.link(victim, lock)
                    else:
                        os.mkfifo(lock, 0o600)

                    with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                        started = time.monotonic()
                        with patch.object(omadivoff, "open_office_request") as request:
                            result = omadivoff.fetch_report(
                                date(2026, 9, 1),
                                "Tridentine - 1570",
                                "Latin",
                                "English",
                                0.1,
                            )

                    request.assert_not_called()
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertIn("safety checks", result["error"])
                    self.assertEqual("unchanged", victim.read_text(encoding="utf-8"))

    def test_unsafe_cooldown_entry_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_directory = root / "omadivoff"
            cache_directory.mkdir(mode=0o700)
            victim = root / "victim"
            victim.write_text("unchanged", encoding="utf-8")
            (cache_directory / "rate-limit.json").symlink_to(victim)
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with patch.object(omadivoff, "open_office_request") as request:
                    result = omadivoff.fetch_report(
                        date(2026, 9, 1),
                        "Tridentine - 1570",
                        "Latin",
                        "English",
                        0.1,
                    )
            request.assert_not_called()
            self.assertIn("safety checks", result["error"])
            self.assertEqual("unchanged", victim.read_text(encoding="utf-8"))

    def test_report_read_security_race_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with patch.object(
                    omadivoff,
                    "_read_cache_json",
                    side_effect=omadivoff.LocalDataSecurityError("simulated race"),
                ):
                    with patch.object(omadivoff, "open_office_request") as request:
                        result = omadivoff.fetch_report(
                            date(2026, 9, 1),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )

            request.assert_not_called()
            self.assertIn("safety checks", result["error"])

    def test_cooldown_read_security_race_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            original_read_cache_json = omadivoff._read_cache_json

            def fail_cooldown_read(descriptor: int, name: str):
                if name == "rate-limit.json":
                    raise omadivoff.LocalDataSecurityError("simulated race")
                return original_read_cache_json(descriptor, name)

            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with patch.object(
                    omadivoff,
                    "_read_cache_json",
                    side_effect=fail_cooldown_read,
                ):
                    with patch.object(omadivoff, "open_office_request") as request:
                        result = omadivoff.fetch_report(
                            date(2026, 9, 1),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )

            request.assert_not_called()
            self.assertIn("safety checks", result["error"])

    def test_cache_metadata_race_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            original_cache_metadata = omadivoff._cache_metadata
            calls = 0

            def fail_second_report_lookup(descriptor: int, name: str):
                nonlocal calls
                if name.startswith("report-"):
                    calls += 1
                    if calls == 2:
                        raise omadivoff.LocalDataSecurityError("simulated race")
                return original_cache_metadata(descriptor, name)

            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with patch.object(
                    omadivoff,
                    "_cache_metadata",
                    side_effect=fail_second_report_lookup,
                ):
                    with patch.object(omadivoff, "open_office_request") as request:
                        result = omadivoff.fetch_report(
                            date(2026, 9, 1),
                            "Tridentine - 1570",
                            "Latin",
                            "English",
                            0.1,
                        )

            request.assert_not_called()
            self.assertIn("safety checks", result["error"])

    def test_atomic_cache_write_is_private_durable_shaped_and_cleans_temp(self):
        day = date(2026, 9, 1)
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                path = omadivoff.cache_path(
                    day, "Tridentine - 1570", "Latin", "English"
                )
                omadivoff.write_cache(path, report_payload(day))
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE(cache_directory.stat().st_mode))
                self.assertEqual([], list(cache_directory.glob(".tmp-*")))

                with patch.object(omadivoff.os, "replace", side_effect=OSError("fault")):
                    with self.assertRaises(OSError):
                        omadivoff.write_cache(path, report_payload(day))
                self.assertEqual([], list(cache_directory.glob(".tmp-*")))

    def test_atomic_cache_write_refuses_symlink_destination(self):
        day = date(2026, 9, 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_directory = root / "omadivoff"
            cache_directory.mkdir(mode=0o700)
            victim = root / "victim"
            victim.write_text("unchanged", encoding="utf-8")
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                path = omadivoff.cache_path(
                    day, "Tridentine - 1570", "Latin", "English"
                )
                path.symlink_to(victim)
                with self.assertRaises(OSError):
                    omadivoff.write_cache(path, report_payload(day))
            self.assertEqual("unchanged", victim.read_text(encoding="utf-8"))

    def test_cache_transaction_remains_bound_across_directory_swap(self):
        day = date(2026, 9, 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_directory = root / "omadivoff"
            replacement = root / "replacement"
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                with omadivoff.secure_cache_directory() as descriptor:
                    moved = root / "moved"
                    cache_directory.rename(moved)
                    replacement.mkdir(mode=0o700)
                    replacement.rename(cache_directory)
                    path = omadivoff.cache_path(
                        day, "Tridentine - 1570", "Latin", "English"
                    )
                    omadivoff.write_cache(path, report_payload(day), descriptor)

            self.assertTrue((moved / path.name).is_file())
            self.assertFalse((cache_directory / path.name).exists())

    def test_cache_schema_is_narrow_and_bound_to_expected_date(self):
        day = date(2026, 9, 1)
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "omadivoff"
            with patch.object(omadivoff, "cache_dir", return_value=cache_directory):
                path = omadivoff.cache_path(
                    day, "Tridentine - 1570", "Latin", "English"
                )
                malformed = report_payload(day)
                malformed["commemorations"] = {"not": "a list"}
                omadivoff.write_cache(path, malformed)
                self.assertIsNone(omadivoff.read_cache(path, expected_date=day))

                mismatched = report_payload(date(2026, 8, 31))
                omadivoff.write_cache(path, mismatched)
                self.assertIsNone(omadivoff.read_cache(path, expected_date=day))

    def test_retry_after_is_clamped_to_one_day(self):
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        error = HTTPError(
            "https://example.test",
            429,
            "Too Many Requests",
            {"Retry-After": str(10**30)},
            None,
        )
        self.addCleanup(error.close)
        self.assertEqual(
            omadivoff.MAX_RATE_LIMIT_COOLDOWN_SECONDS,
            omadivoff.retry_after_seconds(error, now),
        )

    def test_cross_origin_metadata_redirect_is_refused(self):
        handler = omadivoff.SameOriginRedirectHandler()
        with self.assertRaises(URLError):
            handler.redirect_request(
                Request("https://www.divinumofficium.com/start"),
                None,
                302,
                "Found",
                Message(),
                "https://attacker.example/large",
            )

    def test_cli_configuration_is_allowlisted(self):
        with self.assertRaises(ValueError):
            omadivoff.calendar_url(
                date(2026, 9, 1),
                "x" * 10000,
                "Latin",
                "English",
            )

    def test_json_emitter_and_text_stream_enforce_output_ceilings(self):
        destination = io.StringIO()
        with patch.object(omadivoff.sys, "stdout", destination):
            omadivoff.emit_json({"error": "x" * omadivoff.MAX_HELPER_OUTPUT_BYTES})
        output = destination.getvalue().encode("utf-8")
        self.assertLessEqual(len(output), omadivoff.MAX_HELPER_OUTPUT_BYTES)
        self.assertIn("safety limit", json.loads(output)["error"])

        destination = io.StringIO()
        bounded = omadivoff.BoundedTextStream(destination, 4)
        bounded.write("abcdef")
        self.assertEqual("abcd", destination.getvalue())

    def test_qml_uses_bounded_streaming_and_group_timeout(self):
        panel = (MODULE_PATH.parent / "Panel.qml").read_text(encoding="utf-8")
        self.assertNotIn("StdioCollector", panel)
        self.assertGreaterEqual(panel.count("SplitParser"), 4)
        self.assertIn('"/usr/bin/timeout"', panel)
        self.assertIn('"--kill-after=2s"', panel)
        self.assertIn('"/usr/bin/python3", "-I"', panel)
        self.assertIn('"/usr/bin/kill", "-" + signalName', panel)
        self.assertGreaterEqual(panel.count("property int processGroupId: 0"), 2)
        self.assertGreaterEqual(panel.count("onStarted: processGroupId = Number(processId)"), 2)
        self.assertGreaterEqual(panel.count("processGroupId = 0"), 5)
        self.assertIn("Number(process.processGroupId)", panel)
        destruction = panel.split("Component.onDestruction:", 1)[1].split("KeyboardPanel", 1)[0]
        self.assertIn("reportProc.running && reportProc.processGroupId > 1", destruction)
        self.assertIn("solarProc.running && solarProc.processGroupId > 1", destruction)
        self.assertIn('root.signalProcessTree(reportProc, "KILL")', destruction)
        self.assertIn('root.signalProcessTree(solarProc, "KILL")', destruction)
        self.assertGreaterEqual(panel.count("if (exitStatus !== 0)"), 2)
        self.assertIn("maxHelperOutputCharacters", panel)
        self.assertIn("onRunningChanged", panel)

    def test_timeout_kills_an_ignoring_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            script = root / "forker.py"
            script.write_text(
                "import os, signal, sys, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                "    while True: time.sleep(1)\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "while True: time.sleep(1)\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "/usr/bin/timeout",
                    "--signal=TERM",
                    "--kill-after=0.2s",
                    "0.2s",
                    "/usr/bin/python3",
                    "-I",
                    os.fspath(script),
                    os.fspath(child_pid_path),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            self.assertNotEqual(0, result.returncode)
            child_pid = wait_for_pid_file(child_pid_path)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = process_state(child_pid)
                if state is None or state == "Z":
                    break
                time.sleep(0.05)
            state = process_state(child_pid)
            if state is not None:
                self.assertEqual("Z", state)

    def test_supervisor_teardown_kills_the_complete_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            script = root / "forker.py"
            script.write_text(
                "import os, signal, sys, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                "    while True: time.sleep(1)\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "while True: time.sleep(1)\n",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [
                    "/usr/bin/timeout",
                    "--signal=TERM",
                    "--kill-after=2s",
                    "100s",
                    "/usr/bin/python3",
                    "-I",
                    os.fspath(script),
                    os.fspath(child_pid_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            process_group_id = process.pid
            try:
                child_pid = wait_for_pid_file(child_pid_path)
                self.assertEqual(process_group_id, os.getpgid(process.pid))
                os.kill(process.pid, 9)
                process.wait(timeout=2)

                # Killing only Quickshell's direct timeout process leaves the
                # ignoring descendant alive in the recorded process group.
                child_state = process_state(child_pid)
                self.assertIsNotNone(child_state)
                self.assertNotEqual("Z", child_state)

                subprocess.run(
                    ["/usr/bin/kill", "-KILL", "--", f"-{process_group_id}"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                try:
                    os.killpg(process_group_id, 9)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.wait(timeout=2)

            assert child_pid is not None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = process_state(child_pid)
                if state is None or state == "Z":
                    break
                time.sleep(0.05)
            state = process_state(child_pid)
            if state is not None:
                self.assertEqual("Z", state)


if __name__ == "__main__":
    unittest.main()
