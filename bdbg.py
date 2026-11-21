#!/usr/bin/env python3
"""Read listener state and summarize exported device observations."""

import argparse
import collections
import contextlib
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time


__version__ = "0.3.0"


SSH_PROCESSES = {"sshd", "sshd-session", "dropbear"}
PROCESS_PATTERN = re.compile(r'\("([^"\n]+)",pid=\d+,fd=\d+\)')
OWNER_LIST_PATTERN = re.compile(rf"users:\({PROCESS_PATTERN.pattern}(?:,{PROCESS_PATTERN.pattern})*\)")
TIMESTAMP_PATTERN = re.compile(
    r"(?P<date_time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?(?P<offset>Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
TRANSPORTS = {
    "web": {"http", "https", "websocket", "other"},
    "network": {"tcp", "udp", "mqtt", "other"},
    "bluetooth": {"ble", "classic", "other"},
    "radio": {"subghz-ook", "subghz-fsk", "lora", "zigbee", "other"},
}
SOURCES = {"endpoint", "adapter", "device", "fixture"}
EVENTS = {"advertisement", "connection", "disconnect", "message", "notification", "sample"}
TRACE_FIELDS = {"schemaVersion", "observedAt", "source", "kind", "subtype", "event"}
MAX_LINE_BYTES = 1024
MAX_OBSERVATIONS = 100_000
MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
WEB_LINK_SCHEMA = "urn:web-blink:schema:weblink:1"
WEB_LINK_EDITION = "1.3.0"
TRANSPORT_EXTENSION_VERSION = 1
PEER_OUTPUT_LIMIT = 64 * 1024
PEER_TIMEOUT = 5


def web_blink_executable():
    return shutil.which("web-blink") or shutil.which("blink")


def listener_executable():
    path = shutil.which("ss")
    if path is None:
        raise ValueError("The ss executable is required for the listener check.")
    return path


def require_local_input(path, label, *, allow_stdin=False):
    if allow_stdin and path == "-":
        return
    if not Path(path).is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"Supply a readable regular file for {label}.")


def host_capabilities(bluetooth_devices):
    bluetooth = {"status": "unknown", "registeredAdapters": None}
    if bluetooth_devices is not None:
        try:
            count = sum(1 for device in bluetooth_devices.iterdir() if re.fullmatch(r"hci[0-9]+", device.name))
        except FileNotFoundError:
            count = 0
        except OSError:
            count = None
        if count is not None:
            bluetooth = {"status": "detected" if count else "not_detected", "registeredAdapters": count}
    return {
        "bluetooth": bluetooth,
        "commandsOnPath": {
            "aiMinion": shutil.which("minion") is not None,
            "webBlink": web_blink_executable() is not None,
            "radioReceiver": shutil.which("rtl_433") is not None,
            "listenerCheck": shutil.which("ss") is not None,
        },
    }


def capabilities():
    return {
        "schemaVersion": 1,
        "product": "blink-debugger",
        "version": __version__,
        "webLink": {
            "descriptor": WEB_LINK_SCHEMA,
            "transportExtensionVersion": TRANSPORT_EXTENSION_VERSION,
        },
    }


def read_peer_index(executable):
    # Web Blink loads settings even for a schema query. Give the child an
    # absent settings directory so the probe never touches operator settings.
    with tempfile.TemporaryDirectory(prefix="bdbg-peer-") as scratch:
        environment = dict(os.environ, BLINK_CONFIG_DIR=str(Path(scratch) / "settings"), BLINK_OUTPUT="json")
        deadline = time.monotonic() + PEER_TIMEOUT
        output = bytearray()
        failed = threading.Event()
        process = subprocess.Popen(
            [executable, "schemas", "--json"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, env=environment,
        )

        def drain():
            try:
                while len(output) <= PEER_OUTPUT_LIMIT:
                    chunk = process.stdout.read(min(4096, PEER_OUTPUT_LIMIT + 1 - len(output)))
                    if not chunk:
                        return
                    output.extend(chunk)
            except OSError:
                failed.set()

        reader = threading.Thread(target=drain, daemon=True)
        try:
            reader.start()
            reader.join(timeout=max(0, deadline - time.monotonic()))
            if reader.is_alive() or failed.is_set() or len(output) > PEER_OUTPUT_LIMIT:
                raise ValueError("Peer output unavailable or exceeds limits")
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ValueError("Peer command failed")
            return json.loads(output, object_pairs_hook=unique_fields)
        finally:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            process.stdout.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.25)


def web_blink_peer():
    executable = web_blink_executable()
    if executable is None:
        return {"status": "missing"}
    try:
        response = read_peer_index(executable)
        data = response["data"]
        if (response["status"] != "ok" or type(data["schemaVersion"]) is not int
                or not isinstance(data["schemas"], list)
                or not all(isinstance(schema, dict) for schema in data["schemas"])):
            raise ValueError("Invalid peer schema index")
        if data["schemaVersion"] != 1:
            return {"status": "incompatible"}
        entries = [schema for schema in data["schemas"] if schema.get("id") == WEB_LINK_SCHEMA]
        if not entries:
            return {"status": "incompatible"}
        if len(entries) != 1:
            raise ValueError("Duplicate peer descriptor")
        edition = entries[0]["version"]
        if not isinstance(edition, str) or re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4}){2}", edition) is None:
            raise ValueError("Invalid peer descriptor edition")
        return {
            "status": "compatible" if edition == WEB_LINK_EDITION else "incompatible",
            "descriptor": WEB_LINK_SCHEMA,
            "descriptorSchema": edition,
        }
    except (AttributeError, KeyError, OSError, RecursionError, subprocess.SubprocessError, TypeError, ValueError):
        return {"status": "unavailable"}


def check_listeners(lines, ssh_port):
    allowed = []
    unexpected = []
    unverifiable = []
    loopback = 0
    for line in lines:
        columns = line.split(None, 6)
        if len(columns) < 6 or columns[0] not in {"tcp", "udp"}:
            raise ValueError("unexpected ss output")
        protocol, _, _, _, endpoint, _ = columns[:6]
        host, port_text = endpoint.rsplit(":", 1)
        host = host.split("%", 1)[0].strip("[]")
        address = ipaddress.ip_address(host) if host != "*" else None
        local = getattr(address, "ipv4_mapped", None) or address
        port = int(port_text)
        if local and local.is_loopback:
            loopback += 1
            continue
        owner_text = columns[6].strip() if len(columns) == 7 else ""
        owners = []
        if OWNER_LIST_PATTERN.fullmatch(owner_text):
            owners = sorted(set(PROCESS_PATTERN.findall(owner_text)))
        entry = {
            "protocol": protocol,
            "address": str(address) if address else "*",
            "port": port,
            "owners": owners,
        }
        unexpected_owners = set(owners) - SSH_PROCESSES - {"systemd"}
        if protocol != "tcp" or port != ssh_port or unexpected_owners:
            unexpected.append(entry)
        elif SSH_PROCESSES.intersection(owners):
            allowed.append(entry)
        else:
            unverifiable.append(entry)

    status = "pass"
    if unexpected:
        status = "fail"
    elif unverifiable or not allowed:
        status = "indeterminate"
    return {
        "status": status,
        "ssh": allowed,
        "unexpected": unexpected,
        "unverifiable": unverifiable,
        "loopbackExcluded": loopback,
    }


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate observation field")
    return fields


def read_observations(stream):
    groups = collections.Counter()
    first = None
    last = None
    for line_number in range(1, MAX_OBSERVATIONS + 2):
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_LINE_BYTES or line_number > MAX_OBSERVATIONS:
            raise ValueError(f"line {line_number}: trace exceeds the input limit")
        try:
            row = json.loads(line.decode("utf-8"), object_pairs_hook=unique_fields)
            if not isinstance(row, dict) or set(row) != TRACE_FIELDS:
                raise ValueError("expected observation fields")
            if type(row["schemaVersion"]) is not int or row["schemaVersion"] != 1:
                raise ValueError("unsupported observation version")
            kind = row["kind"]
            subtype = row["subtype"]
            source = row["source"]
            event = row["event"]
            if subtype not in TRANSPORTS.get(kind, ()) or source not in SOURCES or event not in EVENTS:
                raise ValueError("invalid transport, source, or event")
            timestamp_match = TIMESTAMP_PATTERN.fullmatch(row["observedAt"])
            if timestamp_match is None:
                raise ValueError("expected timestamp with time zone")
            date_time, fraction, offset = timestamp_match.groups()
            fraction = (fraction or "").ljust(6, "0")
            offset = "+00:00" if offset == "Z" else offset
            timestamp = datetime.datetime.fromisoformat(f"{date_time}.{fraction}{offset}")
            timestamp = timestamp.astimezone(datetime.timezone.utc)
        except UnicodeDecodeError:
            raise ValueError(f"line {line_number}: expected UTF-8") from None
        except (KeyError, OverflowError, RecursionError, TypeError, ValueError):
            raise ValueError(f"line {line_number}: invalid version-1 observation") from None
        first = min(first, timestamp) if first else timestamp
        last = max(last, timestamp) if last else timestamp
        groups[(kind, subtype, source, event)] += 1

    return {
        "schemaVersion": 1,
        "firstObservedAt": first.isoformat() if first else None,
        "lastObservedAt": last.isoformat() if last else None,
        "observations": sum(groups.values()),
        "groups": [
            {"kind": kind, "subtype": subtype, "source": source, "event": event, "count": count}
            for (kind, subtype, source, event), count in sorted(groups.items())
        ],
    }


def read_weblink_transports(stream):
    raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Web Link descriptor exceeds the byte limit")
    try:
        descriptor = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        if descriptor["$schema"] != WEB_LINK_SCHEMA:
            raise ValueError("unsupported Web Link schema")
        metadata = descriptor["x-blink-ext"]["blink-debugger"]
        if set(metadata) != {"version", "transports"} or metadata["version"] != TRANSPORT_EXTENSION_VERSION or metadata["version"] is True:
            raise ValueError("unsupported transport metadata version")
        transports = metadata["transports"]
        if not isinstance(transports, dict) or not transports or not transports.keys() <= TRANSPORTS.keys():
            raise ValueError("unknown transport group")
        declared = []
        for kind, subtypes in transports.items():
            if not isinstance(subtypes, list) or not subtypes or not set(subtypes) <= TRANSPORTS[kind]:
                raise ValueError("unknown transport subtype")
            if len(set(subtypes)) != len(subtypes):
                raise ValueError("duplicate transport subtype")
            declared.extend((kind, subtype) for subtype in subtypes)
        return declared
    except (KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("invalid Web Link transport metadata") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check-listeners", help="check the host's TCP and UDP listeners")
    check.add_argument("--ssh-port", type=int, metavar="PORT", default=22)
    doctor = subcommands.add_parser("doctor", help="inspect host Bluetooth adapters and commands on PATH")
    doctor.add_argument("--probe-web-blink", action="store_true", help="query the local Web Blink schema index")
    subcommands.add_parser("capabilities", help="describe this executable's supported Web Link descriptor")
    lessons = subcommands.add_parser("lessons", help="save and retrieve reusable software improvement notes")
    actions = lessons.add_subparsers(dest="lesson_action", required=True)
    record = actions.add_parser("record", help="record a generalized software lesson")
    record.add_argument("--kind", choices=("fix", "feature"), required=True)
    record.add_argument("--observation", required=True, help="short generalized software observation")
    record.add_argument("--suggestion", required=True, help="specific suggested plugin improvement")
    actions.add_parser("suggest", help="show recent locally saved software suggestions")
    trace = subcommands.add_parser("inspect-observations", help="summarize a local JSON Lines export")
    trace.add_argument("file", help="file path or - for stdin")
    trace.add_argument("--weblink", help="compare observed transport types with a local Web Link descriptor")
    for action in ("preflight", "runbook"):
        flow = subcommands.add_parser(action, help="check local prerequisites and view the selected workflow")
        flow.add_argument("flow", nargs="?", help="flow name, or omit it to list flows")
        flow.add_argument("--file", help="observation input path")
        flow.add_argument("--weblink", help="optional descriptor path for observation inspection")
        flow.add_argument("--board-profile", help="physically confirmed board profile for a bench workflow")
    args = parser.parse_args()

    try:
        if args.command == "check-listeners":
            if not 0 < args.ssh_port < 65536:
                parser.error("--ssh-port must be between 1 and 65535")
            output = subprocess.run(
                [listener_executable(), "-H", "-O", "-lntup"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            result = check_listeners(output.stdout.splitlines(), args.ssh_port)
            exit_code = {"pass": 0, "fail": 1, "indeterminate": 2}[result["status"]]
        elif args.command == "doctor":
            devices = Path("/sys/class/bluetooth") if sys.platform.startswith("linux") else None
            result = host_capabilities(devices)
            if args.probe_web_blink:
                result["webBlinkPeer"] = web_blink_peer()
            exit_code = 0
        elif args.command == "capabilities":
            result = capabilities()
            exit_code = 0
        elif args.command == "lessons":
            from bdbg_lessons import record_lesson, suggest_lessons
            result = (record_lesson(args.kind, args.observation, args.suggestion)
                      if args.lesson_action == "record" else suggest_lessons())
            exit_code = 0
        elif args.command in {"preflight", "runbook"}:
            from bdbg_preflight import describe_flow
            result = describe_flow(
                args.flow, file=args.file, weblink=args.weblink, board_profile=args.board_profile,
                include_runbook=args.command == "runbook",
            )
            exit_code = 1 if result["status"] == "blocked" else 0
        else:
            require_local_input(args.file, "the observation export", allow_stdin=True)
            if args.weblink is not None:
                require_local_input(args.weblink, "the Web Link descriptor")
            source = contextlib.nullcontext(sys.stdin.buffer) if args.file == "-" else open(args.file, "rb")
            with source as stream:
                result = read_observations(stream)
            if args.weblink:
                with open(args.weblink, "rb") as descriptor:
                    declared = set(read_weblink_transports(descriptor))
                observed = {(group["kind"], group["subtype"]) for group in result["groups"]}
                result["declaredTransports"] = [
                    {"kind": kind, "subtype": subtype} for kind, subtype in sorted(declared)
                ]
                result["undeclaredTransports"] = [
                    {"kind": kind, "subtype": subtype} for kind, subtype in sorted(observed - declared)
                ]
            exit_code = 0
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
