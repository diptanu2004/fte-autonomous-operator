"""Command-line operator.

    python -m fte.cli "Process the vendor invoices in the inbox"
    python -m fte.cli --provider groq --model qwen/qwen3-32b "..."      (GROQ_API_KEY in env)
    python -m fte.cli --auto-approve --answer "personal email=priya@gmail.com" "Onboard ..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import Agent
from .human import ConsoleHuman, ScriptedHuman
from .llm import LLM, LLMConfig

ICONS = {"phase": "==", "thought": "  >", "observation": "  <", "policy": " !!", "human_request": " ??",
         "human_reply": " ->", "adapt": " ~~", "verified": " ✓?", "error": " XX"}


def printer(kind: str, d: dict) -> None:
    if kind == "phase":
        print(f"\n== {d['phase'].upper()} ==")
    elif kind == "understood":
        print("Outcome:", d["goal"].get("intended_outcome"))
        print("Procedures:", d["procedures"])
        for c in d["goal"].get("success_criteria", []):
            print("  criterion:", c)
    elif kind == "planned":
        print("Plan" + (" (revised)" if d.get("replanned") else "") + ":")
        for i, s in enumerate(d["plan"], 1):
            print(f"  {i}. {s}")
    elif kind == "thought":
        print(f"  > {d['thought']}\n    {d['tool']} {json.dumps(d['args'], ensure_ascii=False)[:200]}")
    elif kind == "observation":
        print(f"  < #{d['n']} {'OK ' if d['ok'] else 'ERR'} {d['summary'][:200]}")
    elif kind == "done":
        print("\n" + d["report"])
    elif kind in ICONS:
        print(ICONS[kind], {k: v for k, v in d.items() if k not in ("observation",)})


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="FTE autonomous operator")
    ap.add_argument("request")
    ap.add_argument("--provider", default=None, help="ollama | lmstudio | groq | custom")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--thinking", action="store_true", help="enable Qwen3 thinking mode")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--max-steps", type=int, default=70)
    ap.add_argument("--auto-approve", action="store_true", help="non-interactive: approve all gates")
    ap.add_argument("--answer", action="append", default=[], help='keyword=answer for ask_human (non-interactive)')
    ap.add_argument("--reset", action="store_true", help="reset the demo portal + workspace first")
    a = ap.parse_args(argv)

    if a.reset:
        from scripts.reset_demo import reset

        reset()
    cfg = LLMConfig.from_env()
    for attr, val in (("provider", a.provider), ("model", a.model), ("base_url", a.base_url), ("api_key", a.api_key)):
        if val:
            setattr(cfg, attr, val)
    cfg.thinking = cfg.thinking or a.thinking

    from demo_portal.portal import serve_in_background

    serve_in_background()
    human = (ScriptedHuman(dict(x.split("=", 1) for x in a.answer)) if a.auto_approve else ConsoleHuman())
    agent = Agent(LLM(cfg), human, headless=not a.headed, max_steps=a.max_steps, emit=printer)
    state = agent.run(a.request)
    print(f"\nRun artifacts: {agent.run_dir}")
    return 0 if state.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
