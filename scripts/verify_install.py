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
assert "webBlinkPeer" not in json.loads(doctor.stdout)
assert json.loads(run_cli("doctor", "--probe-web-blink").stdout)["webBlinkPeer"] == {"status": "missing"}
assert json.loads(run_cli("capabilities").stdout) == {
    "schemaVersion": 1, "product": "blink-debugger", "version": version,
    "webLink": {"descriptor": "urn:web-blink:schema:weblink:1", "transportExtensionVersion": 1},
}
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
    request(7, "tools/call", name="debugger_doctor", arguments={"probeWebBlink": True}),
    request(8, "tools/call", name="debugger_doctor", arguments={"probeWebBlink": "true"}),
])
assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
assert responses[0]["result"]["serverInfo"]["version"] == version
assert run_cli("--version").stdout.strip() == f"bdbg {version}"
tool_specs = {tool["name"]: tool for tool in responses[1]["result"]["tools"]}
assert len(tool_specs) == 7
assert tool_specs["debugger_record_lesson"]["annotations"]["readOnlyHint"] is False
assert tool_specs["debugger_suggest_lessons"]["annotations"]["readOnlyHint"] is True
assert tool_mcp(responses[2]) == json.loads(doctor.stdout)
assert tool_mcp(responses[3]) == summary
assert responses[4]["result"]["isError"] is True
assert responses[5]["result"]["isError"] is True
assert tool_mcp(responses[6])["webBlinkPeer"] == {"status": "missing"}
assert responses[7]["result"]["isError"] is True

known_flows = json.loads(run_cli("preflight").stdout)["flows"]
assert len(known_flows) == 10
assert "kvm" in known_flows and "lesson-record" in known_flows
assert json.loads(run_cli("preflight", "doctor").stdout)["status"] == "ready"
missing = run_cli("preflight", "check-listeners")
assert missing.returncode == 1
assert any(item["name"] == "ss" and item["status"] == "missing" for item in json.loads(missing.stdout)["checks"])
assert run_cli("check-listeners").returncode == 2
kvm = run_cli("runbook", "kvm")
assert kvm.returncode == 1 and json.loads(kvm.stdout)["steps"]
flow_results = invoke_mcp([
    request(9, "tools/call", name="debugger_preflight",
            arguments={"flow": "inspect-observations", "file": observations, "weblink": descriptor}),
    request(16, "tools/call", name="debugger_preflight",
            arguments={"flow": "inspect-observations", "file": str(plugin / "missing.jsonl")}),
    request(17, "tools/call", name="debugger_runbook", arguments={"flow": "kvm"}),
])
assert tool_mcp(flow_results[0])["status"] == "ready"
assert tool_mcp(flow_results[1])["status"] == "blocked"
assert tool_mcp(flow_results[2]) == json.loads(kvm.stdout)

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

assert (plugin / "AGENTS.md").is_file()
assert (plugin / "skills/blink-debugger-learning/SKILL.md").is_file()

with tempfile.TemporaryDirectory() as temporary:
    state = Path(temporary) / "state"
    env = dict(os.environ, BLINK_DEBUGGER_STATE_DIR=str(state))
    first_read = invoke_mcp([request(11, "tools/call", name="debugger_suggest_lessons")], env=env)[0]
    assert tool_mcp(first_read) == {"schemaVersion": 1, "suggestions": []}
    assert not state.exists()
    note = {
        "kind": "fix",
        "observation": "Incomplete listener ownership needs a clearer explanation",
        "suggestion": "Show why listener ownership was indeterminate",
    }
    first_run = invoke_mcp([
        request(12, "tools/call", name="debugger_record_lesson", arguments=note),
        request(13, "tools/call", name="debugger_record_lesson",
                arguments={**note, "observation": "Device address 10.0.0.1"}),
    ], env=env)
    assert tool_mcp(first_run[0])["recorded"] is True
    assert first_run[1]["result"]["isError"] is True
    identifier = tool_mcp(first_run[0])["id"]

    next_run = invoke_mcp([
        request(14, "tools/call", name="debugger_record_lesson", arguments=note),
        request(15, "tools/call", name="debugger_suggest_lessons"),
    ], env=env)
    assert tool_mcp(next_run[0]) == {"schemaVersion": 1, "id": identifier, "recorded": False}
    expected = {"schemaVersion": 1, "suggestions": [{"id": identifier, **note}]}
    assert tool_mcp(next_run[1]) == expected
    later_cli = run_cli("lessons", "suggest", env=env)
    assert later_cli.returncode == 0, later_cli.stderr
    assert json.loads(later_cli.stdout) == expected
    if os.name == "posix":
        assert state.stat().st_mode & 0o777 == 0o700
        assert (state / "software-lessons.sqlite3").stat().st_mode & 0o777 == 0o600

print("Installed MCP preserved a generalized lesson across sessions and surfaced its suggested fix via MCP and CLI.")
print("Fresh wheel CLI and ZIP MCP passed with both peers absent. All three listener outcomes passed.")
