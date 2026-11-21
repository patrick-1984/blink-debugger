"""Expose the local diagnostic CLI through an MCP stdio server."""

import json
from pathlib import Path
import subprocess
import sys


CLI = Path(__file__).resolve().parents[1] / "bdbg.py"
sys.path.insert(0, str(CLI.parent))
from bdbg import __version__


PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_REQUEST = 1024 * 1024
TOOLS = (
    ("debugger_doctor", "Check registered Bluetooth adapters and executable discovery on this host.", {}, ()),
    ("debugger_check_listeners", "Read TCP and UDP listener state in this Linux network namespace.",
     {"sshPort": {"type": "integer", "minimum": 1, "maximum": 65535}}, ()),
    ("debugger_inspect_observations", "Summarize an operator-selected local JSON Lines observation export.",
     {"file": {"type": "string", "description": "Absolute path to the observation file."},
      "weblink": {"type": "string", "description": "Optional absolute path to a Web Link descriptor."}},
     ("file",)),
)
TOOL_SPECS = {tool[0]: (tool[2], tool[3]) for tool in TOOLS}


def descriptors():
    return [{
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required,
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    } for name, description, properties, required in TOOLS]


def local_file(value):
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        raise ValueError("Supply an absolute path to a local file.")
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Supply an absolute path to an existing local file.")
    return value


def arguments_for(name, parameters):
    if name not in TOOL_SPECS:
        raise ValueError("Unknown Blink Debugger tool.")
    properties, required = TOOL_SPECS[name]
    if (not isinstance(parameters, dict) or parameters.keys() - properties.keys()
            or any(key not in parameters for key in required)):
        raise ValueError("Invalid tool arguments.")

    if name == "debugger_doctor":
        return ["doctor"]
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
