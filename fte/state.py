"""Run state: the agent's working memory and the audit trail of everything it did."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PlanStep:
    id: int
    text: str
    status: str = "todo"  # todo | done | skipped


@dataclass
class StepRecord:
    n: int
    phase: str
    thought: str
    tool: str
    args: dict[str, Any]
    ok: bool
    observation: str  # full observation (persisted for audit, truncated in prompts)
    summary: str  # one-line outcome used in the rolling history
    ts: float = field(default_factory=time.time)


@dataclass
class Evidence:
    kind: str  # screenshot | file | email | page
    path: str
    caption: str
    step: int


@dataclass
class RunState:
    request: str
    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    phase: str = "understand"  # understand | clarify | plan | execute | verify | report | done
    status: str = "running"  # running | waiting | completed | failed | stopped
    goal: dict[str, Any] = field(default_factory=dict)
    clarifications: list[str] = field(default_factory=list)
    procedures: dict[str, str] = field(default_factory=dict)
    plan: list[PlanStep] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)  # source files read this run (pinned in context)
    item_status: dict[str, str] = field(default_factory=dict)  # work item -> system confirmation seen
    lookups: dict[str, dict[str, str]] = field(default_factory=dict)  # work item -> values read from system pages
    steps: list[StepRecord] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    verify_rounds: int = 0
    report: str = ""
    lessons: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    # ------------------------------------------------------------------ prompt views
    def plan_text(self) -> str:
        mark = {"todo": "[ ]", "done": "[x]", "skipped": "[-]"}
        return "\n".join(f"{mark.get(s.status, '[ ]')} {s.id}. {s.text}" for s in self.plan) or "(no plan)"

    def notes_text(self) -> str:
        if not self.notes:
            return "(none yet)"
        return "\n".join(f"- {k}: {json.dumps(v) if not isinstance(v, str) else v}" for k, v in self.notes.items())

    def history_text(self, last: int = 12) -> str:
        if not self.steps:
            return "(no actions yet)"
        older = len(self.steps) - last
        lines = [f"({older} earlier actions omitted; see NOTES)"] if older > 0 else []
        for s in self.steps[-last:]:
            args = json.dumps(s.args, ensure_ascii=False)
            if len(args) > 160:
                args = args[:157] + "..."
            lines.append(f"#{s.n} [{s.phase}] {s.tool} {args} -> {'OK' if s.ok else 'FAILED'}: {s.summary}")
        return "\n".join(lines)

    def mark_done(self, ids: list[Any]) -> None:
        for i in ids or []:
            for s in self.plan:
                if str(s.id) == str(i):
                    s.status = "done"

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
