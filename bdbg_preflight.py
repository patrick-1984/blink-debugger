"""Report local prerequisites and operator runbooks without starting a workflow."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import sys

from bdbg import host_capabilities, listener_executable, require_local_input, web_blink_executable


DEVICE_TREE = Path("/proc/device-tree")
SYS = Path("/sys")
DEV = Path("/dev")
SYSTEMD_RUNTIME = Path("/run/systemd/system")
PROFILE = "orange-pi-3b-v2.1"
BOARD_COMPATIBLE = {"rockchip,rk3566-orangepi-3b", "xunlong,orangepi-3b"}
BENCH = {"bench-network", "bench-receive", "kvm"}
BENCH_TOOLS = (
    "ip", "iw", "hostapd", "dnsmasq", "iptables", "ip6tables", "systemctl",
    "systemd-run", "flock", "nmcli", "rfkill", "awk", "logger", "grep",
)
HELPER_SHA256 = "5f6fcc155836edfaae97cec0995db8834f838d9a6af63f499bf2e3f75381e1e7"
SCHEMATIC = "https://raw.githubusercontent.com/defencedog/orangepi3b_v2.1/main/hardware/manuals/OPi%203B%20V2.1_schematics.pdf#page=21"

RUNBOOKS = {
    "doctor": (
        "Run bdbg doctor to report Bluetooth registration and executable discovery.",
        "Choose a flow and check its required components before using it.",
    ),
    "inspect-observations": (
        "Supply the saved UTF-8 JSON Lines observation file. Add a saved Web Link descriptor if needed.",
        "Run bdbg inspect-observations with the same inputs. It validates their contents and reports the observed coverage.",
    ),
    "check-listeners": (
        "Run bdbg check-listeners in the intended Linux namespace with visibility of socket owners.",
        "Inspect every unexpected or unattributed listener before changing firewall policy.",
    ),
    "peer-discovery": (
        "Run bdbg doctor --probe-web-blink to check the separately installed Web Blink schema index.",
        "Use the reported version compatibility to decide whether to compare descriptors.",
    ),
    "capabilities": ("Run bdbg capabilities to read the installed debugger's Web Link capabilities.",),
    "lesson-record": ("Record a generalized software observation and proposed fix or feature with bdbg lessons record.",),
    "lesson-suggest": ("Retrieve local software suggestions with bdbg lessons suggest.",),
    "bench-network": (
        "Use a dedicated, authorized bench fixture and verify that the separate host helper matches the tested version.",
        "After the operator verifies the fixture and guard prerequisites, start the dedicated lab network with sudo hl-wifi-prov lab-ap --confirm.",
        "Check status using sudo hl-wifi-prov status. Verify wireless association and DHCP with the owned fixture.",
        "Stop the network with sudo hl-wifi-prov down. The separate helper also enforces a 30-minute maximum.",
    ),
    "bench-receive": (
        "Verify the selected USB receiver and choose a known sensor that belongs to the bench.",
        "Check that the receiver is available to the authorized operator, then perform a time-bounded receive-only check for that sensor.",
        "Verify the decoded reading against the owned fixture and release the receiver when the check ends.",
    ),
    "kvm": (
        "Route the target's HDMI output to the capture adapter on an approved USB host port. Check its matching V4L2 node, supported format and a known test image.",
        "Use the existing authenticated SSH connection for manual video viewing. Check ffmpeg or ffplay on the operator's machine.",
        "Keep the target disconnected from the candidate peripheral cable until the exact socket's USB device role, registered UDC and VBUS behavior are independently confirmed.",
        "Confirm safe VBUS behavior throughout reset and boot with the target detached. Require authorized manual access and verify video and input separately.",
    ),
}

PI_RUNBOOKS = {
    "bench-network": (
        "Use the dedicated provisioning adapter. The separate helper configures client isolation, DHCP-only local input and denies IPv4 and IPv6 forwarding.",
        "Prior bench checks verified the helper guards and timer cleanup. Wireless association and a DHCP lease are separate checks for each fixture.",
    ),
    "bench-receive": (
        "A previous Orange Pi 3B bench trial received owned temperature and humidity sensor messages with RTL-SDR and rtl_433.",
        "Confirm the receiver is attached and available for the current trial before starting a bounded listen.",
    ),
    "kvm": (
        "The v2.1 Type-C power socket has unconnected data pins. Keep normal power and use the separate USB2 Type-A OTG socket A1 for the peripheral path only after independent checks.",
        "Use the separate USB host path for the HDMI capture bridge. A detected USB descriptor or unrelated V4L2 node does not prove a usable video path.",
        "A1 VBUS has a separate source and sense path. A software role setting or registered UDC alone cannot prove source-off behavior through reset and boot.",
        "Prior work verified board routing and a synthetic video transport. Live video, safe A1 power behavior and keyboard or mouse functionality require separate proof.",
        "The checked KVMD-Armbian installer required Python 3.10 to 3.12 and a working OTG role. The tested Focal and Python 3.8 host uses the manual V4L2 and authenticated SSH route.",
    ),
}


def read_attribute(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(4097)
        return data if len(data) <= 4096 else b""
    except OSError:
        return b""


def attribute_text(path):
    data = read_attribute(path).decode("utf-8", errors="replace")
    return " ".join("".join(char if char.isprintable() else " " for char in data).split())[:150]


def check(name, status, evidence, *, required=True, stage="dependency"):
    return {"name": name, "status": status, "required": required, "stage": stage, "evidence": evidence}


def command_check(name, *, required=True, path=None):
    found = path or shutil.which(name)
    return check(name, "available" if found else "missing",
                 found or f"{name} was not found on PATH.", required=required)


def board_identity(selected):
    linux = sys.platform.startswith("linux")
    model = attribute_text(DEVICE_TREE / "model") if linux else ""
    compatible = read_attribute(DEVICE_TREE / "compatible").decode("ascii", errors="replace").split("\x00") if linux else []
    family = bool(BOARD_COMPATIBLE.intersection(compatible))
    explicit = ""
    if family:
        explicit = attribute_text(DEVICE_TREE / "board-revision") or attribute_text(DEVICE_TREE / "revision")
    model_revision = re.search(r"\b(?:v(?:ersion\s*)?|rev(?:ision)?\s+)(\d+\.\d+)\b", model, re.IGNORECASE)
    revision = explicit or (model_revision[1] if model_revision else "")
    exact = family and revision.lower() in {"2.1", "v2.1"}
    if explicit and model_revision and explicit.lower().lstrip("v") != model_revision[1]:
        exact = False
    if exact:
        status, evidence = "available", "Device-tree family and v2.1 revision match."
    elif selected == PROFILE and family and not revision:
        status, evidence = "operator-confirmed", "Device-tree family matches. The operator confirmed the PCB revision."
    elif family:
        status, evidence = "unverified", "The board family matches. The v2.1 revision needs independent confirmation."
    else:
        status, evidence = "unverified", "No supported Orange Pi 3B identity was found in the local device tree."
    identity = {"platform": sys.platform, "model": model or None, "revision": revision or None,
                "profile": PROFILE if status in {"available", "operator-confirmed"} else None,
                "evidence": evidence}
    return identity, check("board-profile", status, evidence)


def input_check(path, name, allow_stdin=False):
    if path is None:
        return check(name, "missing", "A local input was not supplied.")
    try:
        require_local_input(path, name, allow_stdin=allow_stdin)
    except (OSError, ValueError):
        return check(name, "missing", "The selected input is not a readable regular file.")
    return check(name, "available", "Standard input was selected." if path == "-" else "The chosen local file is readable.")


def lesson_check(flow):
    try:
        from bdbg_lessons import check_store_access
        check_store_access(write=flow == "lesson-record")
    except (ImportError, OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "The local SQLite store is unavailable."
        return check("local-lesson-store", "missing", detail)
    detail = ("The store path permits a write." if flow == "lesson-record"
              else "The store can be read locally. A missing store yields an empty list.")
    return check("local-lesson-store", "available", detail)


def entries(path, pattern="*"):
    try:
        return sorted(path.glob(pattern))
    except OSError:
        return []


def usb_matches(vendor, product):
    return [item for item in entries(SYS / "bus/usb/devices")
            if attribute_text(item / "idVendor") == vendor and attribute_text(item / "idProduct") == product]


def usb_check(name, vendor, product):
    matches = usb_matches(vendor, product)
    authorized = [item for item in matches if attribute_text(item / "authorized") == "1"]
    if not matches:
        return check(name, "missing", f"USB device {vendor}:{product} is absent from sysfs."), authorized
    if not authorized:
        return check(name, "unverified", f"USB device {vendor}:{product} is visible without verified authorization."), authorized
    return check(name, "available", f"USB device {vendor}:{product} is present and authorized in sysfs."), authorized


def linked_devices(path, pattern, authorized):
    parents = {item.resolve() for item in authorized}
    return [item for item in entries(path, pattern)
            if parents.intersection((item / "device").resolve().parents)]


def video_check(authorized):
    nodes = [DEV / item.name for item in linked_devices(SYS / "class/video4linux", "video*", authorized)]
    count = sum(node.is_char_device() and os.access(node, os.R_OK) for node in nodes)
    if count:
        return check("capture-v4l2", "available", f"{count} readable V4L2 node(s) match the authorized USB bridge.")
    return check("capture-v4l2", "missing", "No readable V4L2 node matched the authorized USB bridge.")


def helper_check():
    command = shutil.which("hl-wifi-prov")
    installed = Path("/usr/local/sbin/hl-wifi-prov")
    path = Path(command) if command else installed
    if not path.is_file() or not os.access(path, os.X_OK):
        return check("hl-wifi-prov", "missing", "The separate provisioning helper executable is missing.")
    try:
        with path.open("rb") as stream:
            data = stream.read(65537)
    except OSError:
        return check("hl-wifi-prov", "unverified", "The helper exists but its tested version could not be checked.")
    if len(data) > 65536 or hashlib.sha256(data).hexdigest() != HELPER_SHA256:
        return check("hl-wifi-prov", "unverified", "The helper exists but its bytes differ from the tested version.")
    return check("hl-wifi-prov", "available", "The separate helper matches the exact guard-verified build.")


def bench_checks(flow, board):
    linux = sys.platform.startswith("linux")
    os_check = check("linux", "available" if linux else "missing", "The workflow requires local Linux hardware.")
    if not linux:
        return [os_check, board]
    if flow == "bench-network":
        adapter, authorized = usb_check("bench-wifi", "0bda", "8176")
        interfaces = linked_devices(SYS / "class/net", "*", authorized)
        interface = check("bench-interface", "available" if interfaces else "missing",
                          f"{len(interfaces)} local network interface(s) match the authorized bench adapter.")
        serial = [item for pattern in ("ttyUSB*", "ttyACM*") for item in entries(DEV, pattern)
                  if item.is_char_device() and os.access(item, os.R_OK)]
        systemd = check("systemd-runtime", "available" if SYSTEMD_RUNTIME.is_dir() else "missing",
                        "The local systemd runtime directory is required by the helper.")
        return [os_check, board, helper_check(), systemd, *[command_check(name) for name in BENCH_TOOLS], adapter, interface,
                check("serial-console", "available" if serial else "missing",
                      f"{len(serial)} readable USB serial endpoint(s). Wi-Fi DHCP proof uses its own link.", required=False),
                check("owned-client", "unverified", "Verify association and a DHCP lease with a known bench client.",
                      stage="verification")]
    if flow == "bench-receive":
        receiver, _ = usb_check("rtl-sdr", "0bda", "2838")
        return [os_check, board, command_check("rtl_433"), receiver,
                check("owned-sensor", "unverified", "Verify the intended owned sensor and receiver access in a bounded trial.",
                      stage="verification")]
    bridge, authorized = usb_check("hdmi-capture-bridge", "534d", "2109")
    udcs = entries(SYS / "class/udc")
    modes = [value for node in entries(DEVICE_TREE, "usb@*")
             for value in [attribute_text(node / "dr_mode")] if value in {"host", "peripheral", "otg"}]
    always_on = (DEVICE_TREE / "vcc5v0-otg-regulator/regulator-always-on").exists()
    return [
        os_check, board, command_check("v4l2-ctl"), bridge, video_check(authorized),
        command_check("ffmpeg", required=False), command_check("ffplay", required=False),
        check("registered-udc", "available" if udcs else "missing", f"{len(udcs)} local USB device controller(s) registered."),
        check("usb-controller-modes", "observed" if modes else "unverified",
              ", ".join(modes) if modes else "No controller role settings were read.", required=False),
        check("a1-vbus", "unverified",
              ("The device tree declares the OTG supply always-on. " if always_on else "")
              + "Independently verify source-off behavior through reset and boot before a target connection.",
              stage="verification"),
        check("live-kvm", "unverified",
              "Verify authenticated SSH and the operator viewer, then verify live video and manual input separately.",
              stage="verification"),
    ]


def describe_flow(flow=None, *, file=None, weblink=None, board_profile=None, include_runbook=False):
    if flow not in RUNBOOKS and flow is not None:
        raise ValueError("Choose a supported debugger flow.")
    if ((file is not None or weblink is not None) and flow != "inspect-observations"
            or board_profile is not None and (board_profile != PROFILE or flow not in BENCH)):
        raise ValueError("Options apply only to the matching flow and supported board profile.")
    if flow is None:
        return {"schemaVersion": 1, "status": "available", "flows": list(RUNBOOKS)}

    identity, board = board_identity(board_profile)
    checks = [check("debugger-runtime", "available", "The installed debugger Python entry point is running.")]
    if flow == "doctor":
        current = host_capabilities(SYS / "class/bluetooth" if sys.platform.startswith("linux") else None)
        bluetooth = current["bluetooth"]
        checks.append(check("bluetooth-registration", "observed" if bluetooth["registeredAdapters"] is not None else "unverified",
                            f"Host adapter registration reports {bluetooth['status']}.", required=False))
        checks.extend(check(name, "available" if available else "missing", "Executable discovery on PATH.", required=False)
                      for name, available in current["commandsOnPath"].items())
    elif flow == "inspect-observations":
        checks.append(input_check(file, "observation-file", allow_stdin=True))
        if weblink is not None:
            checks.append(input_check(weblink, "Web-Link-descriptor"))
    elif flow == "check-listeners":
        try:
            path = listener_executable()
        except ValueError as exc:
            checks.append(check("ss", "missing", str(exc)))
        else:
            checks.append(command_check("ss", path=path))
    elif flow == "peer-discovery":
        executable = web_blink_executable()
        checks.append(check("Web-Blink", "available" if executable else "missing",
                            executable or "The Web Blink executable was not found on PATH."))
        checks.append(check("descriptor-compatibility", "unverified",
                            "Run the explicit peer check to verify compatibility.", required=False))
    elif flow in {"lesson-record", "lesson-suggest"}:
        checks.append(lesson_check(flow))
    elif flow in BENCH:
        checks.extend(bench_checks(flow, board))

    blockers = [item for item in checks if item["required"] and item["status"] not in {"available", "operator-confirmed"}]
    report = {"schemaVersion": 1, "flow": flow, "status": "blocked" if blockers else "ready",
              "dependencyStatus": "blocked" if any(item["stage"] == "dependency" for item in blockers) else "ready",
              "scope": "Local dependency checks. Live results require verification in the selected flow.",
              "hardware": identity, "checks": checks}
    if include_runbook:
        report["steps"] = list(RUNBOOKS[flow])
        if identity["profile"] and flow in BENCH:
            report["boardNotes"] = list(PI_RUNBOOKS[flow])
            if flow == "kvm":
                report["references"] = [SCHEMATIC]
    return report
