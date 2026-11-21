# Blink Debugger

Blink Debugger checks Linux host readiness and summarizes local device observation exports. It runs with Python 3.8 or later. Web Blink and AI Minion are optional.

## Install

In Claude Code, add the owner marketplace, then install the plugin and reload the session.

```text
/plugin marketplace add patrick-1984/marketplace
/plugin install blink-debugger@patrick-1984
```

The Codex-compatible package includes the same three MCP tools and the `blink-debugger` skill. Install it from the owner's catalog or use the plugin directly from the [release ZIP](https://github.com/patrick-1984/blink-debugger/releases/tag/v0.1.0).

For shell use, install the wheel in an isolated environment and run `bdbg --help`. The release ZIP also runs directly with `python3 bdbg.py --help`.

```sh
bdbg doctor
bdbg inspect-observations examples/observations.jsonl --weblink examples/weblink.json
bdbg check-listeners
```

The first two commands are supported through the MCP tools `debugger_doctor` and `debugger_inspect_observations`. When invoking MCP, supply absolute paths to your files. `debugger_check_listeners` provides the host listener check.

## Host readiness

`doctor` lists Linux Bluetooth adapters registered by the kernel and finds `minion`, `blink` or `web-blink`, `rtl_433` and `ss` on the current PATH. A command name on PATH is evidence of executable discovery. Use its documented version or capability command if your workflow requires compatibility. Bluetooth registration alone does not establish successful pairing or GATT. The plugin checks the host on which it runs.

`check-listeners` checks TCP and UDP bindings in the current Linux network namespace with `ss`. Run it with sufficient socket owner visibility. It excludes loopback, accepts attributable SSH listeners on TCP 22 by default, and reports unexpected bindings. Use `--ssh-port` if this host uses another port. Exit 0 means pass, 1 means unexpected bindings, and 2 means incomplete attribution or an execution error. Routing and firewall rules determine reachability.

## Device observations

Provide UTF-8 JSON Lines with one object per line. Each record has `schemaVersion` (integer 1), `observedAt` (ISO 8601 with an explicit offset or Z), `source`, `kind`, `subtype` and `event`. Sources are `endpoint`, `adapter`, `device` and `fixture`. Events are `advertisement`, `connection`, `disconnect`, `message`, `notification` and `sample`.

| Kind | Subtypes |
|---|---|
| `web` | `http`, `https`, `websocket`, `other` |
| `network` | `tcp`, `udp`, `mqtt`, `other` |
| `bluetooth` | `ble`, `classic`, `other` |
| `radio` | `subghz-ook`, `subghz-fsk`, `lora`, `zigbee`, `other` |

The summary reports counts by source, transport and event, plus UTC time bounds. It reads the selected local input, rejects fields outside this contract, and fails the whole summary on invalid input or limits. It accepts at most 100,000 observations and 1,024 bytes per line. A summary describes the supplied file's coverage.

An optional v1 Web Link descriptor can declare transports using `x-blink-ext.blink-debugger`. The summary lists the declared types and the observed types absent from the declaration. The release test installs the pinned Web Blink v0.21.0 wheel, runs its schema export entry point, validates the example Web Link against the exported descriptor schema and checks the debugger summary. See [the compatibility pin](compat.json).

On Android, supply app-owned observations through the app's supported export when available. A Minion Android export integration needs a documented export capability. For Home Assistant use the supported integration for the exact device, firmware and bridge. A state read requires a compatible bench device and test instance.
