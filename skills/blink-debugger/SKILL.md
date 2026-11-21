---
name: blink-debugger
description: Summarize an operator-selected local device observation export, identify local host prerequisites, or check whether host listeners are limited to SSH.
---

Start every supported flow with `debugger_preflight`, even when the user names it only in general
terms. Omit the flow to see the available choices. Include the selected local inputs when inspecting
observations. Report missing required components and unresolved verification steps before running any
diagnostic. Request `debugger_runbook` to read the matching workflow. Use a board profile only after the
operator has independently confirmed the PCB revision and the reported local board family matches.
Treat a blocked manual workflow as evidence for the operator to review. Keep device, network and role
changes in the separate operator workflow.

Use the Blink Debugger MCP tools:

- `debugger_doctor` reports registered Bluetooth adapters and executable names found on the server's PATH. Report which peer commands were found. When asked to check Web Blink compatibility, set `probeWebBlink` to true and report the resulting descriptor edition and status.
- `debugger_inspect_observations` reads the local JSON Lines export the operator selected. Provide the absolute path and, when supplied, the absolute path of its Web Link descriptor. Report counts, sources, time bounds and undeclared types. A parse error invalidates the summary. Counts describe the supplied export.
- `debugger_check_listeners` checks the current Linux network namespace and classifies the result as pass, fail or indeterminate. Report unexpected bindings and missing attribution. Firewall reachability requires a separate check.

An Android or Home Assistant workflow needs its documented integration and device evidence. Use an Android export that the operator explicitly generated or the data returned by the device's supported Home Assistant integration. The plugin's tools run on the machine that hosts the MCP server.

When the user asks you to learn from a software workflow or improve how the debugger works, follow
the packaged `blink-debugger-learning` workflow. Record generalized software lessons only. Surface
relevant suggested plugin fixes or features after a major change and when work finishes.

For an authorized Orange Pi bench with the separate `hl-wifi-prov` helper, the operator starts the
dedicated open `web-debug` network using `sudo /usr/local/sbin/hl-wifi-prov lab-ap --confirm` from the host shell.
It is off by default and the helper expires it after 30 minutes. The operator can stop it with
`sudo /usr/local/sbin/hl-wifi-prov down`. Use the debugger's read-only diagnostics and saved observation inspector
independently of these operator commands. Treat live wireless association as a separate fixture check.
