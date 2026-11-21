import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bdbg_preflight import PROFILE, board_identity, describe_flow, helper_check, video_check


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.devicetree = self.root / "device-tree"
        self.sys = self.root / "sys"
        self.dev = self.root / "dev"
        for name, path in (("DEVICE_TREE", self.devicetree), ("SYS", self.sys), ("DEV", self.dev)):
            patcher = patch(f"bdbg_preflight.{name}", path)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("bdbg_preflight.sys.platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    def board(self, compatible, model="Rockchip RK3566 OPi 3B", revision=None):
        self.devicetree.mkdir(exist_ok=True)
        (self.devicetree / "compatible").write_bytes(compatible.encode() + b"\x00rockchip,rk3566\x00")
        (self.devicetree / "model").write_bytes(model.encode() + b"\x00")
        if revision:
            (self.devicetree / "board-revision").write_bytes(revision.encode() + b"\x00")

    def usb(self, name, vendor, product, authorization):
        device = self.sys / "bus/usb/devices" / name
        device.mkdir(parents=True)
        for attribute, value in (("idVendor", vendor), ("idProduct", product), ("authorized", authorization)):
            (device / attribute).write_text(value + "\n")
        return device

    def test_inputs_and_optional_components_report_independently(self):
        with patch("bdbg.shutil.which", return_value=None):
            doctor = describe_flow("doctor")
            self.assertEqual(doctor["status"], "ready")
            self.assertTrue(all(not item["required"] for item in doctor["checks"][1:]))
            self.assertEqual(describe_flow("check-listeners")["status"], "blocked")
            self.assertEqual(describe_flow("peer-discovery")["status"], "blocked")
        self.assertEqual(describe_flow("inspect-observations", file="-")["status"], "ready")
        self.assertEqual(describe_flow("inspect-observations", file=str(self.root/"missing"))["status"], "blocked")

    def test_profile_requires_matching_family_and_confirms_unknown_revision_explicitly(self):
        self.board("example,other")
        self.assertIsNone(board_identity(PROFILE)[0]["profile"])
        self.assertNotIn("boardNotes", describe_flow("kvm", board_profile=PROFILE, include_runbook=True))
        self.board("rockchip,rk3566-orangepi-3b")
        self.assertIsNone(board_identity(None)[0]["profile"])
        self.assertEqual(board_identity(PROFILE)[1]["status"], "operator-confirmed")
        self.board("rockchip,rk3566-orangepi-3b", revision="1.1")
        self.assertIsNone(board_identity(PROFILE)[0]["profile"])
        self.board("rockchip,rk3566-orangepi-3b", revision="2.1")
        self.assertEqual(board_identity(None)[1]["status"], "available")

    def test_kvm_reports_authorization_roles_and_vbus_without_guesses(self):
        self.board("rockchip,rk3566-orangepi-3b")
        self.usb("4-1", "534d", "2109", "0")
        for address in ("fcc00000", "fd000000"):
            controller = self.devicetree / f"usb@{address}"
            controller.mkdir()
            (controller / "dr_mode").write_bytes(b"host\x00")
        regulator = self.devicetree / "vcc5v0-otg-regulator"
        regulator.mkdir()
        (regulator / "regulator-always-on").touch()
        with patch("bdbg_preflight.shutil.which", return_value="/usr/bin/test"):
            result = describe_flow("kvm", board_profile=PROFILE, include_runbook=True)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(checks["hdmi-capture-bridge"]["status"], "unverified")
        self.assertEqual(checks["capture-v4l2"]["status"], "missing")
        self.assertEqual(checks["registered-udc"]["status"], "missing")
        self.assertEqual(checks["usb-controller-modes"]["evidence"], "host, host")
        self.assertIn("always-on", checks["a1-vbus"]["evidence"])
        self.assertTrue(any("Type-C" in note for note in result["boardNotes"]))

    def test_video_node_must_match_the_usb_bridge(self):
        bridge = self.usb("4-1", "534d", "2109", "1")
        interface = bridge / "4-1:1.0"
        interface.mkdir()
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        for index, location in enumerate((interface, unrelated)):
            video = self.sys / "class/video4linux" / f"video{index}"
            video.mkdir(parents=True)
            (video / "device").symlink_to(location)
        with patch("bdbg_preflight.Path.is_char_device", return_value=True), patch("bdbg_preflight.os.access", return_value=True):
            self.assertIn("1 readable", video_check([bridge])["evidence"])
            self.assertEqual(video_check([])["status"], "missing")

    def test_verified_helper_matches_the_absolute_operator_command(self):
        payload = b"supported helper"
        other = self.root / "from-path"
        other.write_bytes(payload)
        other.chmod(0o700)
        canonical = self.root / "installed-helper"
        with patch("bdbg_preflight.HELPER", canonical), patch(
                "bdbg_preflight.HELPER_SHA256", hashlib.sha256(payload).hexdigest()
        ), patch("bdbg_preflight.shutil.which", return_value=str(other)):
            self.assertEqual(helper_check()["status"], "missing")
            canonical.write_bytes(payload)
            canonical.chmod(0o700)
            self.assertEqual(helper_check()["status"], "available")
            canonical.write_bytes(b"different helper")
            self.assertEqual(helper_check()["status"], "unverified")

    def test_satisfied_receive_dependencies_do_not_certify_the_owned_fixture(self):
        self.board("rockchip,rk3566-orangepi-3b", revision="2.1")
        self.usb("1-1", "0bda", "2838", "1")
        with patch("bdbg_preflight.shutil.which", return_value="/usr/bin/rtl_433"):
            result = describe_flow("bench-receive")
        self.assertEqual(result["dependencyStatus"], "ready")
        self.assertEqual(result["status"], "blocked")

    def test_missing_sqlite_blocks_lessons_but_keeps_read_only_diagnostics_available(self):
        with patch("bdbg_lessons.sqlite3", None):
            self.assertEqual(describe_flow("lesson-record")["status"], "blocked")
            self.assertEqual(describe_flow("lesson-suggest")["status"], "blocked")
            self.assertEqual(describe_flow("doctor")["status"], "ready")

    def test_lesson_preflight_is_read_only_and_flags_unsafe_store(self):
        state = self.root / "local-notes"
        with patch.dict(os.environ, BLINK_DEBUGGER_STATE_DIR=str(state)):
            self.assertEqual(describe_flow("lesson-record")["status"], "ready")
            self.assertFalse(state.exists())
            if os.name == "posix":
                state.mkdir(mode=0o755)
                self.assertEqual(describe_flow("lesson-record")["status"], "blocked")
