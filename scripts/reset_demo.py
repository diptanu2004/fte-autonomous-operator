"""Reset the demo world: fresh portal database and a workspace with only the seeded inbox."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_portal.portal import reset_db  # noqa: E402

WORKSPACE = ROOT / "workspace"


def reset(workspace: Path = WORKSPACE) -> None:
    reset_db()
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(ROOT / "demo_data", workspace)
    for sub in ("reports", "outbox", "scratch"):
        (workspace / sub).mkdir(exist_ok=True)


if __name__ == "__main__":
    reset()
    print(f"Portal DB reset; workspace re-seeded at {WORKSPACE}")
