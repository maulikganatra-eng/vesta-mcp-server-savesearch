"""Put `scripts/` on sys.path so the tooling tests can import it.

scripts/ is deliberately not a package: its modules are entry points run as
`uv run --frozen python scripts/<name>.py`, and making them importable-only would
mean either a console-script indirection or a src/ move, both of which would put
this repo's own tooling inside the shipped wheel.
"""

from __future__ import annotations

from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
