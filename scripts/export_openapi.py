"""Generate `contract/openapi.json` from the running FastAPI app (ADR-014).

    python scripts/export_openapi.py

CI runs this and fails the build if the committed copy is stale (ADR-014's "Drift is a
broken build at both ends"); a developer runs it by hand after any route change, before
committing.
"""

from __future__ import annotations

import json
from pathlib import Path

from cineatelie.main import app

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "contract" / "openapi.json"


def main() -> None:
    schema = app.openapi()
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
