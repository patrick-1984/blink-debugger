"""Validate the packaged descriptor against the exact released Web Blink schema."""

import json
from pathlib import Path
import sys

import jsonschema

plugin = Path(sys.argv[1])
export = Path(sys.argv[2])
compat = json.loads((plugin / "compat.json").read_text())["webBlink"]
descriptor = json.loads((plugin / "examples/weblink.json").read_text())
for path in export.rglob("*.json"):
    schema = json.loads(path.read_text())
    if schema.get("$id") == descriptor["$schema"]:
        version = schema["x-blink-schema"]["version"]
        if version != compat["descriptorSchema"]:
            raise SystemExit(f"Unexpected schema version: {version}")
        jsonschema.Draft202012Validator(schema).validate(descriptor)
        print(f"Validated example with Web Blink {compat['version']} descriptor schema {version}.")
        break
else:
    raise SystemExit("Web Blink export did not contain the pinned descriptor schema.")
