"""Render docs/API.md from the OpenAPI document the app generates from its own code.

python -m parse_app.apidoc           # rewrite docs/API.md
python -m parse_app.apidoc --check   # exit 1 if docs/API.md is out of date (CI)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DOC = Path(__file__).resolve().parents[2] / "docs" / "API.md"


def _type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(_type(s) for s in schema[key])
    if schema.get("type") == "array":
        return f"{_type(schema.get('items', {}))}[]"
    if "enum" in schema:
        return " | ".join(f'"{v}"' for v in schema["enum"])
    if "const" in schema:
        return f'"{schema["const"]}"'
    return str(schema.get("type", "any"))


def _constraints(schema: dict[str, Any]) -> str:
    keys = ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern", "default")
    return ", ".join(f"{k}={schema[k]!r}" for k in keys if k in schema)


def render() -> str:
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://docs:docs@127.0.0.1:1/docs")  # never connected to
    from .api import app

    spec = app.openapi()
    out = [
        f"# API reference — {spec['info']['title']} v{spec['info']['version']}",
        "",
        "> Generated from the code by `python -m parse_app.apidoc`. Do not edit by hand; CI fails if it drifts.",
        "> Interactive version: `/api/v1/docs` on a running stack. Machine-readable: `/api/v1/openapi.json`.",
        "",
        spec["info"]["description"],
        "",
    ]
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            out += [f"## `{method.upper()} {path}`", ""]
            if op.get("description") or op.get("summary"):
                out += [(op.get("description") or op["summary"]).strip(), ""]
            params = op.get("parameters", [])
            if params:
                out += ["| Parameter | In | Type | Required | Constraints / notes |", "|---|---|---|---|---|"]
                for p in params:
                    note = "; ".join(x for x in (_constraints(p["schema"]), p.get("description", "")) if x)
                    out.append(
                        f"| `{p['name']}` | {p['in']} | {_type(p['schema'])} | {'yes' if p.get('required') else 'no'} | {note} |"
                    )
                out.append("")
            body = op.get("requestBody", {}).get("content", {})
            for media, content in body.items():
                out += [f"Request body (`{media}`): **{_type(content.get('schema', {}))}**", ""]
            out += ["| Status | Meaning | Body |", "|---|---|---|"]
            for status, resp in op["responses"].items():
                contents: list[dict[str, Any]] = list(resp.get("content", {}).values())
                schema = contents[0].get("schema", {}) if contents else {}
                out.append(f"| {status} | {resp.get('description', '')} | {_type(schema) if schema else '—'} |")
            out.append("")
    out += ["## Schemas", ""]
    for name, schema in sorted(spec["components"]["schemas"].items()):
        if "properties" not in schema:
            continue
        out += [f"### {name}", "", "| Field | Type | Required | Constraints / notes |", "|---|---|---|---|"]
        required = set(schema.get("required", []))
        for field, fs in schema["properties"].items():
            note = "; ".join(x for x in (_constraints(fs), fs.get("description", "")) if x)
            out.append(f"| `{field}` | {_type(fs)} | {'yes' if field in required else 'no'} | {note} |")
        out.append("")
    return "\n".join(out).replace("\r\n", "\n")


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        current = DOC.read_text(encoding="utf-8").replace("\r\n", "\n") if DOC.exists() else ""
        if current != text:
            print("docs/API.md is out of date: run `python -m parse_app.apidoc`", file=sys.stderr)
            sys.exit(1)
        print("docs/API.md is in sync")
    else:
        DOC.parent.mkdir(exist_ok=True)
        DOC.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {DOC}")
