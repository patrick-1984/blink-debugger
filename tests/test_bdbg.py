import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from bdbg import capabilities, check_listeners, host_capabilities, main, read_observations, read_weblink_transports, web_blink_peer


class CapabilityTests(unittest.TestCase):
    def test_counts_only_adapter_entries_and_checks_path_without_running_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            bluetooth = Path(temporary)
            for name in ("hci0", "hci2", "hci0:42", "rfkill0"):
                (bluetooth / name).mkdir()
            with patch("bdbg.shutil.which", side_effect={"minion": "/test/minion", "blink": "/test/blink"}.get):
                capabilities = host_capabilities(bluetooth)
        self.assertEqual(capabilities["bluetooth"], {"status": "detected", "registeredAdapters": 2})
        self.assertEqual(capabilities["commandsOnPath"], {
            "aiMinion": True, "webBlink": True, "radioReceiver": False, "listenerCheck": False,
        })

    def test_distinguishes_missing_adapter_class_and_unchecked_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = host_capabilities(Path(temporary) / "absent")
        self.assertEqual(missing["bluetooth"], {"status": "not_detected", "registeredAdapters": 0})
        self.assertEqual(host_capabilities(None)["bluetooth"], {"status": "unknown", "registeredAdapters": None})


class PeerTests(unittest.TestCase):
    def test_debugger_capability_response(self):
        self.assertEqual(capabilities(), {
            "schemaVersion": 1, "product": "blink-debugger", "version": "0.1.1",
            "webLink": {"descriptor": "urn:web-blink:schema:weblink:1", "transportExtensionVersion": 1},
        })

    def test_missing_peer_is_supported(self):
        with patch("bdbg.shutil.which", return_value=None):
            self.assertEqual(web_blink_peer(), {"status": "missing"})

    def test_checked_index_edition_and_response_shape(self):
        entry = {"id": "urn:web-blink:schema:weblink:1", "version": "1.3.0"}
        index = {"status": "ok", "data": {"schemaVersion": 1, "schemas": [entry]}}
        with patch("bdbg.shutil.which", return_value="/test/blink"), patch("bdbg.read_peer_index", return_value=index):
            self.assertEqual(web_blink_peer(), {
                "status": "compatible", "descriptor": entry["id"], "descriptorSchema": "1.3.0",
            })
            entry["version"] = "1.2.0"
            self.assertEqual(web_blink_peer()["status"], "incompatible")
            index["data"]["schemas"].append(entry)
            self.assertEqual(web_blink_peer(), {"status": "unavailable"})

    def test_executable_is_called_with_fixed_arguments_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "blink"
            executable.touch(mode=0o755)
            with patch("bdbg.shutil.which", return_value=str(executable)):
                for code in (
                    'print("x" * 65537)',
                    'import time; time.sleep(10)',
                    'raise SystemExit(2)',
                ):
                    with self.subTest(code=code):
                        executable.write_text(f"#!{sys.executable}\n{code}\n")
                        with patch("bdbg.PEER_TIMEOUT", 0.5):
                            self.assertEqual(web_blink_peer(), {"status": "unavailable"})
                body = {"status": "ok", "data": {"schemaVersion": 1, "schemas": [
                    {"id": "urn:web-blink:schema:weblink:1", "version": "1.3.0"},
                ]}}
                executable.write_text(
                    f"#!{sys.executable}\nimport sys\n"
                    "assert sys.argv[1:] == ['schemas', '--json']\n"
                    f"print({json.dumps(body)!r})\n"
                )
                self.assertEqual(web_blink_peer()["status"], "compatible")


class ListenerTests(unittest.TestCase):
    def test_extant_ssh_and_loopback_services_pass(self):
        result = check_listeners([
            'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=4))',
            'tcp LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=1,fd=5),("systemd",pid=2,fd=9))',
            'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*',
            'tcp LISTEN 0 128 [::ffff:127.0.0.1]:80 [::]:*',
        ], 22)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["ssh"]), 2)
        self.assertEqual(result["loopbackExcluded"], 2)

    def test_non_ssh_service_or_udp_fail(self):
        result = check_listeners([
            'tcp LISTEN 0 128 *:22 *:* users:(("other-daemon",pid=1,fd=4))',
            'tcp LISTEN 0 128 *:3389 *:* users:(("xrdp",pid=123,fd=4))',
            'udp UNCONN 0 0 [fe80::a]%eth0:22 [::]:*',
        ], 22)
        self.assertEqual(result["status"], "fail")
        self.assertEqual([entry["port"] for entry in result["unexpected"]], [22, 3389, 22])

    def test_missing_ssh_owner_or_empty_snapshot_is_indeterminate(self):
        result = check_listeners(['tcp LISTEN 0 128 *:2222 *:* users:(("systemd",pid=1,fd=4))'], 2222)
        self.assertEqual(result["status"], "indeterminate")
        self.assertEqual(result["unverifiable"][0]["port"], 2222)
        self.assertEqual(check_listeners([], 22)["status"], "indeterminate")

    def test_partial_owner_list_is_indeterminate_even_with_ssh_name(self):
        malformed = (
            'users:(("sshd",pid=1,fd=4),("other-daemon",pid=?,fd=9))',
            'users:(("sshd",pid=1,fd=4))(?unknown-tail)',
            'other:(("sshd",pid=1,fd=4))',
        )
        for trailer in malformed:
            with self.subTest(trailer=trailer):
                result = check_listeners([f'tcp LISTEN 0 128 *:22 *:* {trailer}'], 22)
                self.assertEqual(result["status"], "indeterminate")
                self.assertEqual(result["ssh"], [])
                self.assertEqual(result["unverifiable"][0]["owners"], [])

    def test_corrupt_snapshot_is_an_error(self):
        with self.assertRaises(ValueError):
            check_listeners(["unexpected output"], 22)


class ObservationTests(unittest.TestCase):
    def observation(self, timestamp):
        return json.dumps({
            "schemaVersion": 1, "observedAt": timestamp, "source": "fixture",
            "kind": "radio", "subtype": "zigbee", "event": "sample",
        }).encode("utf-8")

    def test_groups_across_device_categories_and_compares_utc_bounds(self):
        rows = [
            ("2026-11-21T13:10:01-02:00", "web", "http", "message"),
            ("2026-11-21T13:10:01Z", "radio", "subghz-ook", "sample"),
            ("2026-11-21T13:10:02Z", "bluetooth", "ble", "notification"),
            ("2026-11-21T13:10:03Z", "radio", "subghz-ook", "sample"),
            ("2026-11-21T13:10:04Z", "network", "mqtt", "connection"),
        ]
        stream = io.BytesIO("\n".join(json.dumps({
            "schemaVersion": 1, "observedAt": time, "source": "fixture",
            "kind": kind, "subtype": subtype, "event": event,
        }) for time, kind, subtype, event in rows).encode("utf-8"))
        result = read_observations(stream)
        self.assertEqual(result["observations"], 5)
        self.assertEqual(result["firstObservedAt"], "2026-11-21T13:10:01+00:00")
        self.assertEqual(result["lastObservedAt"], "2026-11-21T15:10:01+00:00")
        self.assertEqual({group["kind"]: group["count"] for group in result["groups"]}, {
            "bluetooth": 1, "network": 1, "radio": 2, "web": 1,
        })

    def test_rejects_unrecognized_fields_and_does_not_echo_them(self):
        raw = '{"schemaVersion":1,"observedAt":"2026-11-21T13:10:00Z","source":"fixture",'
        raw += '"kind":"radio","subtype":"zigbee","event":"sample","payload":"private-data"}'
        with self.assertRaisesRegex(ValueError, "line 1: invalid version-1 observation"):
            read_observations(io.BytesIO(raw.encode("utf-8")))

    def test_accepts_one_through_six_fractional_digits(self):
        for count in range(1, 7):
            fraction = "123456"[:count]
            with self.subTest(fraction=fraction):
                stream = io.BytesIO(self.observation(f"2026-11-21T13:10:00.{fraction}+01:30"))
                result = read_observations(stream)
                self.assertEqual(result["firstObservedAt"], f"2026-11-21T11:40:00.{fraction.ljust(6, '0')}+00:00")

    def test_invalid_utf8_reports_its_line_without_echoing_input(self):
        first = self.observation("2026-11-21T13:10:00Z")
        stream = io.BytesIO(first + b"\ninvalid-private-\xff\n")
        with self.assertRaisesRegex(ValueError, r"^line 2: expected UTF-8$"):
            read_observations(stream)

    def test_cli_stdin_preserves_binary_line_boundaries(self):
        first = self.observation("2026-11-21T13:10:00.12Z")
        stream = io.TextIOWrapper(io.BytesIO(first + b"\ninvalid-private-\xff\n"))
        with patch("sys.argv", ["bdbg.py", "inspect-observations", "-"]):
            with patch("sys.stdin", stream), patch("sys.stderr", io.StringIO()) as error:
                self.assertEqual(main(), 2)
        self.assertEqual(error.getvalue(), "line 2: expected UTF-8\n")
        self.assertFalse(stream.closed)

    def test_duplicate_members_are_rejected(self):
        with self.assertRaises(ValueError):
            read_observations(io.BytesIO(b'{"schemaVersion":1,"schemaVersion":2}'))

    def test_empty_export_does_not_invent_an_observation(self):
        result = read_observations(io.BytesIO())
        self.assertEqual(result["observations"], 0)
        self.assertIsNone(result["firstObservedAt"])


class DescriptorTests(unittest.TestCase):
    def descriptor(self, transports):
        return io.BytesIO(json.dumps({
            "$schema": "urn:web-blink:schema:weblink:1",
            "x-blink-ext": {"blink-debugger": {"version": 1, "transports": transports}},
        }).encode("utf-8"))

    def test_reads_only_declared_transport_metadata(self):
        stream = self.descriptor({"bluetooth": ["ble"], "radio": ["lora", "zigbee"]})
        self.assertEqual(read_weblink_transports(stream), [("bluetooth", "ble"), ("radio", "lora"), ("radio", "zigbee")])

    def test_rejects_mismatched_and_duplicate_subtypes(self):
        for transports in ({"web": ["ble"]}, {"radio": ["lora", "lora"]}):
            with self.subTest(transports=transports), self.assertRaises(ValueError):
                read_weblink_transports(self.descriptor(transports))
