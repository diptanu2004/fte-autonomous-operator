"""Ground-truth eval for the onboarding task (personal email missing -> the agent must ask).

    python scripts/eval_onboarding.py --runs 3 --provider custom --base-url https://.../v1 --model qwen3-30b-a3b
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_portal import portal  # noqa: E402
from fte.agent import Agent  # noqa: E402
from fte.human import ScriptedHuman  # noqa: E402
from fte.llm import LLM, LLMConfig  # noqa: E402
from scripts.reset_demo import WORKSPACE, reset  # noqa: E402

PERSONAL = "priya.sharma.dev@gmail.com"


def ground_truth(start: str, asked: bool) -> dict:
    conn = sqlite3.connect(portal.DB_PATH)
    conn.row_factory = sqlite3.Row
    emps = conn.execute("SELECT * FROM employees WHERE name = 'Priya Sharma'").fetchall()
    e = dict(emps[0]) if emps else {}
    tickets = {(r["category"], r["item"], r["priority"])
               for r in conn.execute("SELECT * FROM tickets WHERE employee_id = ?", (e.get("id", "-"),))}
    conn.close()
    emails = [p.read_text(encoding="utf-8") for p in (WORKSPACE / "outbox").glob("*.eml")]
    want_tickets = {("Hardware", "MacBook Pro 14 (16GB)", "High"), ("Software access", "Tableau", "High"),
                    ("Software access", "Snowflake read access", "High")}
    return {
        "asked for personal email": asked,
        "exactly one employee record": len(emps) == 1,
        "employee fields correct": bool(e) and e["email"] == "priya.sharma@acme-corp.com" and e["personal_email"] == PERSONAL
        and e["department"] == "Analytics" and e["manager_id"] == "E-002" and e["start_date"] == start
        and e["location"] == "Bangalore" and "analyst" in e["title"].lower(),
        "exactly the 3 matrix tickets (High)": tickets == want_tickets,
        "manager emailed": any("To: dana.lee@acme-corp.com" in m and "Priya" in m for m in emails),
        "checklist saved": any((WORKSPACE / "reports").glob("onboarding_*priya*")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--provider", default="custom")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--out", default=str(ROOT / "runs" / "eval_onboarding.jsonl"))
    a = ap.parse_args()

    portal.serve_in_background()
    cfg = LLMConfig.from_env()
    cfg.provider, cfg.base_url, cfg.model = a.provider, a.base_url or cfg.base_url, a.model or cfg.model
    start = (date.today() + timedelta(days=7)).isoformat()  # within 7 days -> High priority per SOP
    request = f"Please onboard Priya Sharma, our new Data Analyst in Dana Lee's team. She starts on {start}."
    rows = []
    for i in range(1, a.runs + 1):
        reset()
        t0 = time.time()
        human = ScriptedHuman({"personal email": PERSONAL, "email": PERSONAL})
        agent = Agent(LLM(cfg), human, max_steps=a.max_steps)
        st = agent.run(request)
        asked = any("email" in r.message.lower() and r.kind == "question" for r in human.log)
        truth = ground_truth(start, asked)
        correct = all(truth.values())
        row = {"run": i, "run_id": st.run_id, "agent_status": st.status, "actually_correct": correct,
               "false_success": st.status == "completed" and not correct, "truth": truth,
               "steps": len(st.steps), "minutes": round((time.time() - t0) / 60, 1)}
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
