#!/usr/bin/env python3
from pathlib import Path
import yaml
from happy_path.main import app

ROOT = Path(__file__).resolve().parent.parent

def main() -> None:
    schema = app.openapi()
    schema["openapi"] = "3.1.0"
    out = ROOT / "openapi.yaml"
    out.write_text(yaml.dump(schema, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"[OK] Exported OpenAPI 3.1.0 specification to {out}")

if __name__ == "__main__":
    main()
