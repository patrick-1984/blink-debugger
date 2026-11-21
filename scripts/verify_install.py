"""Exercise a fresh plugin archive and installed wheel through their public entry points."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

plugin = Path(sys.argv[1])
cli = sys.argv[2]
claude_manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
codex_manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
assert claude_manifest["name"] == codex_manifest["name"] == "blink-debugger"
version = claude_manifest["version"]
assert codex_manifest["version"] == version
claude_entry = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["blink-debugger"]
codex_entry = json.loads((plugin / codex_manifest["mcpServers"]).read_text())["mcpServers"]["blink-debugger"]
server = Path(claude_entry["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)))
assert server == (plugin / codex_entry["cwd"] / codex_entry["args"][0]).resolve()



def run_cli(*args, env=None):
    return subprocess.run([cli, *args], capture_output=True, text=True, check=False, env=env)


def invoke_mcp(messages, env=None):
    result = subprocess.run(
        [sys.executable, str(server)], check=True, capture_output=True, text=True,
        input="".join(json.dumps(request) + "\n" for request in messages), env=env,
    )
    return [json.loads(line) for line in result.stdout.splitlines()]


def request(request_id, method, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def tool_mcp(response):
    return json.loads(response["result"]["content"][0]["text"])


doctor = run_cli("doctor")
assert doctor.returncode == 0, doctor.stderr
assert json.loads(doctor.stdout)["commandsOnPath"] == {
    "aiMinion": False, "webBlink": False, "radioReceiver": False, "listenerCheck": False,
}

observations = str(plugin / "examples/observations.jsonl")
descriptor = str(plugin / "examples/weblink.json")
trace = run_cli("inspect-observations", observations, "--weblink", descriptor)
assert trace.returncode == 0, trace.stderr
summary = json.loads(trace.stdout)
assert summary["observations"] == 7
assert len(summary["declaredTransports"]) == 5
assert len(summary["undeclaredTransports"]) == 2

responses = invoke_mcp([
    request(1, "initialize", protocolVersion="2025-06-18"),
    request(2, "tools/list"),
    request(3, "tools/call", name="debugger_doctor"),
    request(4, "tools/call", name="debugger_inspect_observations",
            arguments={"file": observations, "weblink": descriptor}),
    request(5, "tools/call", name="debugger_inspect_observations", arguments={"file": "-"}),
    request(6, "tools/call", name="debugger_doctor", arguments={"command": "other"}),
])
assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
assert responses[0]["result"]["serverInfo"]["version"] == version
assert run_cli("--version").stdout.strip() == f"bdbg {version}"
assert len(responses[1]["result"]["tools"]) == 3
assert tool_mcp(responses[2]) == json.loads(doctor.stdout)
assert tool_mcp(responses[3]) == summary
assert responses[4]["result"]["isError"] is True
assert responses[5]["result"]["isError"] is True

ss_input = 'tcp LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:(("sshd",pid=1,fd=4))'
unexpected = 'udp UNCONN 0 0 0.0.0.0:100 0.0.0.0:* users:(("other",pid=2,fd=4))'
missing_attribution = "tcp LISTEN 0 128 0.0.0.0:2222 0.0.0.0:*"
with tempfile.TemporaryDirectory() as temporary:
    mock_ss = Path(temporary) / "ss"
    env = dict(os.environ, PATH=temporary)
    for snapshot, expected_status, expected_exit in (
        (ss_input, "pass", 0),
        (ss_input + "\n" + unexpected, "fail", 1),
        (missing_attribution, "indeterminate", 2),
    ):
        mock_ss.write_text(f"#!{sys.executable}\nprint({snapshot!r})\n")
        mock_ss.chmod(0o755)
        result = run_cli("check-listeners", "--ssh-port", "2222", env=env)
        assert result.returncode == expected_exit, result.stderr
        assert json.loads(result.stdout)["status"] == expected_status
        response = invoke_mcp([
            request(10, "tools/call", name="debugger_check_listeners", arguments={"sshPort": 2222}),
        ], env=env)[0]
        assert tool_mcp(response) == json.loads(result.stdout)

print("Fresh wheel CLI and ZIP MCP passed with both peers absent. All three listener outcomes passed.")
