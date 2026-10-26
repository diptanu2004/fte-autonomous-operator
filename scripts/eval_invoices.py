"""Ground-truth eval for the AP invoice task.

Runs the agent N times from a clean world and scores each run against the TRUE expected end state, so we
measure not just "did it say completed" but whether it was right — especially false successes.

    python scripts/eval_invoices.py --runs 3 --provider custom --base-url https://.../v1 --model qwen3-30b-a3b
    (API key via FTE_API_KEY / GROQ_API_KEY)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_portal import portal  # noqa: E402
from fte.agent import Agent  # noqa: E402
from fte.human import ScriptedHuman  # noqa: E402
from fte.llm import LLM, LLMConfig  # noqa: E402
from scripts.reset_demo import WORKSPACE, reset  # noqa: E402

EXPECTED = {"INV-2041": "Approved for payment", "INV-2042": "Approved for payment", "INV-3307": "On hold"}
REQUEST = "Process this week's vendor invoices in the inbox."


def ground_truth() -> dict:
    conn = sqlite3.connect(portal.DB_PATH)
    rows = dict(conn.execute("SELECT invoice_number, status FROM invoices").fetchall())
    conn.close()
    emails = [p.read_text(encoding="utf-8") for p in (WORKSPACE / "outbox").glob("*.eml")]
    report = WORKSPACE / "reports" / "ap_summary.md"
    checks = {f"{k} = {v}": rows.get(k) == v for k, v in EXPECTED.items()}
    checks["hold email to Fabrikam"] = any("accounts@fabrikam.example" in e and "INV-3307" in e for e in emails)
    checks["no wrong emails"] = all("fabrikam" in e for e in emails)
    checks["summary report covers all"] = report.exists() and all(k in report.read_text(encoding="utf-8") for k in EXPECTED)
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--provider", default="custom")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--out", default=str(ROOT / "runs" / "eval_invoices.jsonl"))
    a = ap.parse_args()

    portal.serve_in_background()
    cfg = LLMConfig.from_env()
    cfg.provider, cfg.base_url, cfg.model = a.provider, a.base_url or cfg.base_url, a.model or cfg.model
    rows = []
    for i in range(1, a.runs + 1):
        reset()
        t0 = time.time()
        agent = Agent(LLM(cfg), ScriptedHuman(approve=True), max_steps=a.max_steps)
        st = agent.run(REQUEST)
        truth = ground_truth()
        correct = all(truth.values())
        row = {"run": i, "run_id": st.run_id, "agent_status": st.status, "actually_correct": correct,
               "false_success": st.status == "completed" and not correct, "truth": truth,
               "steps": len(st.steps), "llm_calls": agent.llm.usage.calls, "minutes": round((time.time() - t0) / 60, 1)}
        rows.append(row)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"run {i}: agent={st.status:9} correct={correct!s:5} steps={row['steps']:3} {row['minutes']}min "
              f"failed_truth={[k for k, v in truth.items() if not v]}", flush=True)

    n = len(rows)
    print(f"\nSUMMARY over {n} runs: correct {sum(r['actually_correct'] for r in rows)}/{n} | "
          f"agent-reported completed {sum(r['agent_status'] == 'completed' for r in rows)}/{n} | "
          f"needs review {sum(r['agent_status'] == 'needs_review' for r in rows)}/{n} | "
          f"FALSE SUCCESSES {sum(r['false_success'] for r in rows)}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
