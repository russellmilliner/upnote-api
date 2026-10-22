#!/usr/bin/env python3
"""
Export OpenAPI 3.1.0 specification (docs/openapi.json) and Markdown Swagger/API
reference documentation (docs/SWAGGER.md) directly from the FastAPI app.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import upnote_api  # noqa: E402


def main() -> None:
    docs_dir = REPO_ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    spec = upnote_api.app.openapi()
    openapi_path = docs_dir / "openapi.json"
    openapi_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[OK] Exported {openapi_path}")

    # Generate Markdown Swagger reference (docs/SWAGGER.md)
    lines: list[str] = [
        f"# {spec['info']['title']} — OpenAPI / Swagger Reference",
        "",
        f"- **Version:** `v{spec['info']['version']}`",
        "- **OpenAPI Version:** `3.1.0`",
        "- **Interactive Swagger UI:** `http://127.0.0.1:8765/docs`",
        "- **Interactive ReDoc UI:** `http://127.0.0.1:8765/redoc`",
        "- **Raw OpenAPI Spec:** [`docs/openapi.json`](./openapi.json) (`http://127.0.0.1:8765/openapi.json`)",
        "",
        spec["info"].get("description", ""),
        "",
        "---",
        "",
        "## Endpoints Summary",
        "",
        "| Tag | Method | Path | Summary |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            tags = ", ".join(op.get("tags", ["General"]))
            summary = op.get("summary", "")
            lines.append(f"| **{tags}** | `{method.upper()}` | `{path}` | {summary} |")

    lines.extend(["", "---", "", "## Detailed Endpoint Reference", ""])

    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            m_up = method.upper()
            summary = op.get("summary", f"{m_up} {path}")
            desc = (op.get("description") or "").strip()
            tags = ", ".join(op.get("tags", []))
            lines.append(f"### `{m_up} {path}` — {summary}")
            lines.append("")
            if tags:
                lines.append(f"- **Category:** `{tags}`")
            if desc:
                lines.append("")
                lines.append(desc)
            params = op.get("parameters", [])
            if params:
                lines.append("")
                lines.append("#### Parameters")
                lines.append("")
                lines.append("| Name | In | Type | Required | Description |")
                lines.append("| :--- | :--- | :--- | :---: | :--- |")
                for p in params:
                    p_name = p["name"]
                    p_in = p["in"]
                    p_req = "Yes" if p.get("required") else "No"
                    p_schema = p.get("schema", {})
                    p_type = p_schema.get("type") or " / ".join(
                        x.get("type", "") for x in p_schema.get("anyOf", []) if "type" in x
                    ) or "string"
                    p_desc = (p.get("description") or p_schema.get("description") or "").replace("\n", " ")
                    lines.append(f"| `{p_name}` | `{p_in}` | `{p_type}` | {p_req} | {p_desc} |")

            req_body = op.get("requestBody", {})
            if req_body:
                content = req_body.get("content", {}).get("application/json", {})
                schema_ref = content.get("schema", {}).get("$ref", "")
                if schema_ref:
                    model_name = schema_ref.split("/")[-1]
                    lines.append("")
                    lines.append(f"- **Request Body (`application/json`):** [`{model_name}`](#schema-{model_name.lower()})")

            lines.append("")

    lines.extend(["---", "", "## Data Schemas (Component Models)", ""])
    schemas = spec.get("components", {}).get("schemas", {})
    for model_name, model_def in sorted(schemas.items()):
        lines.append(f"### Schema: `{model_name}`")
        lines.append("")
        props = model_def.get("properties", {})
        req_fields = set(model_def.get("required", []))
        if props:
            lines.append("| Field | Type | Required | Default | Description |")
            lines.append("| :--- | :--- | :---: | :--- | :--- |")
            for f_name, f_def in props.items():
                f_type = f_def.get("type") or " / ".join(
                    x.get("type", x.get("$ref", "").split("/")[-1])
                    for x in f_def.get("anyOf", [])
                ) or f_def.get("$ref", "").split("/")[-1] or "any"
                is_req = "Yes" if f_name in req_fields else "No"
                default_val = f"`{json.dumps(f_def['default'])}`" if "default" in f_def else "—"
                f_desc = (f_def.get("description") or "").replace("\n", " ")
                lines.append(f"| `{f_name}` | `{f_type}` | {is_req} | {default_val} | {f_desc} |")
        lines.append("")

    swagger_md = docs_dir / "SWAGGER.md"
    swagger_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] Exported {swagger_md}")


if __name__ == "__main__":
    main()
