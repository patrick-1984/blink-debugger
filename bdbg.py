#!/usr/bin/env python3
"""Read listener state and summarize exported device observations."""

import argparse
import collections
import contextlib
import datetime
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


__version__ = "0.1.0"


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
            "webBlink": bool(shutil.which("web-blink") or shutil.which("blink")),
            "radioReceiver": shutil.which("rtl_433") is not None,
            "listenerCheck": shutil.which("ss") is not None,
        },
    }


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
        if descriptor["$schema"] != "urn:web-blink:schema:weblink:1":
            raise ValueError("unsupported Web Link schema")
        metadata = descriptor["x-blink-ext"]["blink-debugger"]
        if set(metadata) != {"version", "transports"} or metadata["version"] != 1 or metadata["version"] is True:
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
    subcommands.add_parser("doctor", help="inspect host Bluetooth adapters and commands on PATH")
    trace = subcommands.add_parser("inspect-observations", help="summarize a local JSON Lines export")
    trace.add_argument("file", help="file path or - for stdin")
    trace.add_argument("--weblink", help="compare observed transport types with a local Web Link descriptor")
    args = parser.parse_args()

    try:
        if args.command == "check-listeners":
            if not 0 < args.ssh_port < 65536:
                parser.error("--ssh-port must be between 1 and 65535")
            output = subprocess.run(
                ["ss", "-H", "-O", "-lntup"],
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
            exit_code = 0
        else:
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
