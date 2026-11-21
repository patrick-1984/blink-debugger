---
name: blink-debugger
description: Summarize an operator-selected local device observation export, identify local host prerequisites, or check whether host listeners are limited to SSH.
---

Use the Blink Debugger MCP tools:

- `debugger_doctor` reports registered Bluetooth adapters and executable names found on the server's PATH. Report which peer commands were found. When asked to check Web Blink compatibility, set `probeWebBlink` to true and report the resulting descriptor edition and status.
- `debugger_inspect_observations` reads the local JSON Lines export the operator selected. Provide the absolute path and, when supplied, the absolute path of its Web Link descriptor. Report counts, sources, time bounds and undeclared types. A parse error invalidates the summary. Counts describe the supplied export.
- `debugger_check_listeners` checks the current Linux network namespace and classifies the result as pass, fail or indeterminate. Report unexpected bindings and missing attribution. Firewall reachability requires a separate check.

An Android or Home Assistant workflow needs its documented integration and device evidence. Use an Android export that the operator explicitly generated or the data returned by the device's supported Home Assistant integration. The plugin's tools run on the machine that hosts the MCP server.

When the user asks you to learn from a software workflow or improve how the debugger works, follow
the packaged `blink-debugger-learning` workflow. Record generalized software lessons only. Surface
relevant suggested plugin fixes or features after a major change and when work finishes.
