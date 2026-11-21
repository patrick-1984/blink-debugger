"""Expose the local diagnostic CLI through an MCP stdio server."""

import json
from pathlib import Path
import subprocess
import sys


CLI = Path(__file__).resolve().parents[1] / "bdbg.py"
sys.path.insert(0, str(CLI.parent))
from bdbg import __version__
from bdbg_lessons import KINDS, TEXT_LIMIT, record_lesson, suggest_lessons
from bdbg_preflight import PROFILE, RUNBOOKS, describe_flow


PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_REQUEST = 1024 * 1024
FLOW_PARAMETERS = {
    "flow": {"type": "string", "enum": list(RUNBOOKS), "description": "Omit for available flows."},
    "file": {"type": "string", "description": "Absolute observation file path for inspection."},
    "weblink": {"type": "string", "description": "Optional absolute Web Link descriptor path."},
    "boardProfile": {"type": "string", "enum": [PROFILE],
                     "description": "Use after independently confirming the physical board revision."},
}
TOOLS = (
    ("debugger_doctor", "Check registered Bluetooth adapters and executable discovery on this host.",
     {"probeWebBlink": {"type": "boolean", "description": "Query the local Web Blink schema index."}}, ()),
    ("debugger_check_listeners", "Read TCP and UDP listener state in this Linux network namespace.",
     {"sshPort": {"type": "integer", "minimum": 1, "maximum": 65535}}, ()),
    ("debugger_inspect_observations", "Summarize an operator-selected local JSON Lines observation export.",
     {"file": {"type": "string", "description": "Absolute path to the observation file."},
      "weblink": {"type": "string", "description": "Optional absolute path to a Web Link descriptor."}},
     ("file",)),
    ("debugger_record_lesson", "Save a generalized, local software improvement lesson.",
     {"kind": {"type": "string", "enum": KINDS},
      "observation": {"type": "string", "maxLength": TEXT_LIMIT},
      "suggestion": {"type": "string", "maxLength": TEXT_LIMIT}},
     ("kind", "observation", "suggestion")),
    ("debugger_suggest_lessons", "Read recent locally saved software improvement suggestions.", {}, ()),
    ("debugger_preflight", "Check local prerequisites before any supported debugger workflow.", FLOW_PARAMETERS, ()),
    ("debugger_runbook", "Read the workflow and applicable board-specific guidance.", FLOW_PARAMETERS, ()),
)
TOOL_SPECS = {tool[0]: (tool[2], tool[3]) for tool in TOOLS}


def descriptors():
    return [{
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required,
                        "additionalProperties": False},
        "annotations": {
            "readOnlyHint": name != "debugger_record_lesson",
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    } for name, description, properties, required in TOOLS]


def local_path(value):
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        raise ValueError("Supply an absolute path to a local file.")
    if not Path(value).is_absolute():
        raise ValueError("Supply an absolute path to a local file.")
    return value


def local_file(value):
    path = local_path(value)
    if not Path(path).is_file():
        raise ValueError("Supply an absolute path to an existing local file.")
    return path


def require_parameters(name, parameters):
    if name not in TOOL_SPECS:
        raise ValueError("Unknown Blink Debugger tool.")
    properties, required = TOOL_SPECS[name]
    if (not isinstance(parameters, dict) or parameters.keys() - properties.keys()
            or any(key not in parameters for key in required)):
        raise ValueError("Invalid tool arguments.")


def arguments_for(name, parameters):
    if name == "debugger_doctor":
        probe = parameters.get("probeWebBlink", False)
        if type(probe) is not bool:
            raise ValueError("Supply a boolean for probeWebBlink.")
        return ["doctor", "--probe-web-blink"] if probe else ["doctor"]
    if name == "debugger_check_listeners":
        port = parameters.get("sshPort", 22)
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("Supply a valid SSH port number.")
        return ["check-listeners", "--ssh-port", str(port)]

    args = ["inspect-observations"]
    if "weblink" in parameters:
        args += ["--weblink", local_file(parameters["weblink"])]
    return args + ["--", local_file(parameters["file"])]


def run_tool(name, parameters):
    try:
        require_parameters(name, parameters)
        if name == "debugger_record_lesson":
            summary = record_lesson(**parameters)
        elif name == "debugger_suggest_lessons":
            summary = suggest_lessons()
        elif name in {"debugger_preflight", "debugger_runbook"}:
            summary = describe_flow(
                parameters.get("flow"),
                file=local_path(parameters["file"]) if "file" in parameters else None,
                weblink=local_path(parameters["weblink"]) if "weblink" in parameters else None,
                board_profile=parameters.get("boardProfile"),
                include_runbook=name == "debugger_runbook",
            )
        else:
            arguments = arguments_for(name, parameters)
            result = subprocess.run(
                [sys.executable, str(CLI), *arguments],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            if not result.stdout:
                raise ValueError(result.stderr.strip()[:500] or "Blink Debugger did not return a result.")
            # The listener command uses nonzero exit codes for valid diagnostics.
            summary = json.loads(result.stdout)
        return {"content": [{"type": "text", "text": json.dumps(summary)}]}
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return {"isError": True, "content": [{"type": "text", "text": str(exc)[:500]}]}


def respond(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"id": None, "error": {"code": -32600, "message": "Invalid request."}}
    if "id" not in request:
        return None
    request_id = request["id"]
    method = request.get("method")
    parameters = request.get("params", {})
    if not isinstance(parameters, dict):
        return {"id": request_id, "error": {"code": -32602, "message": "Invalid parameters."}}
    if method == "initialize":
        protocol = parameters.get("protocolVersion")
        result = {
            "protocolVersion": protocol if protocol in PROTOCOLS else PROTOCOLS[-1],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "blink-debugger", "version": __version__},
        }
    elif method == "tools/list":
        result = {"tools": descriptors()}
    elif method == "tools/call":
        result = run_tool(parameters.get("name"), parameters.get("arguments", {}))
    elif method == "ping":
        result = {}
    else:
        return {"id": request_id, "error": {"code": -32601, "message": "Method not found."}}
    return {"id": request_id, "result": result}


def main():
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST:
            return
        try:
            response = respond(json.loads(line))
        except (ValueError, RecursionError):
            response = {"id": None, "error": {"code": -32700, "message": "Invalid JSON."}}
        if response is not None:
            print(json.dumps({"jsonrpc": "2.0", **response}), flush=True)


if __name__ == "__main__":
    main()
