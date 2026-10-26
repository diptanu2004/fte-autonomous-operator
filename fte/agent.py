"""The operator: Goal → Understand → (Clarify) → Plan → Execute/Observe/Adapt → Verify → Report & Learn."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import prompts as P
from .checks import CompletionChecks
from .human import HumanChannel, HumanReply, HumanRequest
from .knowledge import CompanyContext, LessonMemory, tokenize
from .llm import LLM, LLMError
from .policy import Policy
from .state import Evidence, PlanStep, RunState, StepRecord
from .tools import EXECUTE_TOOLS, VERIFY_TOOLS, Toolbox, ToolResult, tools_prompt
from .tools.browser import Browser
from .tools.files import Workspace

ROOT = Path(__file__).resolve().parents[1]

Emit = Callable[[str, dict[str, Any]], None]


class StopRun(Exception):
    pass


def _clean_step(step: Any) -> str:
    """Models often echo checkboxes/numbering ("[x] 8. ..."); keep just the step text."""
    return re.sub(r"^\s*(\[[ x\-]\]\s*)?(\d+[.)]\s*)*", "", str(step)).strip()


class Agent:
    def __init__(
        self,
        llm: LLM,
        human: HumanChannel,
        *,
        company_dir: Path = ROOT / "company",
        workspace_dir: Path = ROOT / "workspace",
        runs_dir: Path = ROOT / "runs",
        memory_path: Path = ROOT / "memory" / "lessons.jsonl",
        headless: bool = True,
        max_steps: int = 80,
        max_verify_steps: int = 25,
        emit: Emit | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.llm, self.human = llm, human
        self.ctx = CompanyContext(company_dir)
        self.policy = Policy(self.ctx.permissions)
        self.checks = CompletionChecks(Path(company_dir) / "completion_checks.yaml", self.ctx.docs)
        self.memory = LessonMemory(memory_path)
        self.workspace = Workspace(workspace_dir)
        self.runs_dir = Path(runs_dir)
        self.headless = headless
        self.max_steps, self.max_verify_steps = max_steps, max_verify_steps
        self._emit = emit or (lambda kind, data: None)
        self.stop_event = stop_event or threading.Event()
        self.state: RunState | None = None
        self.lessons: list[str] = []

    # ================================================================== public
    def run(self, request: str) -> RunState:
        self.state = st = RunState(request=request.strip())
        self._approved: set[str] = set()
        self.run_dir = self.runs_dir / st.run_id
        self.browser = Browser(self.run_dir / "shots", headless=self.headless, origin_allowed=self.policy.origin_allowed)
        self.tools = Toolbox(self.ctx, self.workspace, self.browser)
        self.emit("run_started", request=st.request, run_id=st.run_id)
        try:
            self._understand()
            self._plan()
            while True:
                outcome = self._loop("execute", EXECUTE_TOOLS, self.max_steps)
                if outcome != "finish":
                    st.status = "failed"
                    st.notes["failure"] = outcome
                    break
                verdict = self._verify()
                if verdict == "pass":
                    st.status = "completed"
                    break
                if verdict == "review":  # work checks out in code; some claims just couldn't be substantiated
                    st.status = "needs_review"
                    break
                st.verify_rounds += 1
                if st.verify_rounds >= 3:
                    st.status = "failed"
                    break
                failed = [v for v in st.verification if not v.get("passed")]
                st.notes["VERIFICATION_FAILED"] = [f"{v.get('criterion')}: {v.get('evidence')}" for v in failed]
                self.emit("adapt", message="Verification found gaps; returning to execution.", failed=failed)
        except StopRun:
            st.status = "stopped"
        except LLMError as e:
            st.status = "failed"
            st.notes["failure"] = f"LLM error: {e}"
            self.emit("error", message=str(e))
        finally:
            self.browser.close()
        self._report()
        return st

    # ================================================================== helpers
    def emit(self, event: str, /, **data: Any) -> None:
        self._emit(event, data)

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRun()

    def _set_phase(self, phase: str) -> None:
        self.state.phase = phase
        self.emit("phase", phase=phase)
        self._save()

    def _save(self) -> None:
        self.state.save(self.run_dir)

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d (%A)")

    def _ask(self, kind: str, message: str, **context: Any) -> HumanReply:
        st = self.state
        prev = st.status
        st.status = "waiting"
        self.emit("human_request", kind=kind, message=message, context=context)
        self._save()
        reply = self.human.ask(HumanRequest(kind, message, context))
        st.status = prev
        self._check_stop()
        self.emit("human_reply", kind=kind, approved=reply.approved, text=reply.text)
        tag = {"approval": "APPROVAL", "question": "Q", "guidance": "GUIDANCE"}[kind]
        verdict = ("approved" if reply.approved else "DENIED") if kind == "approval" else ""
        st.clarifications.append(f"[{tag}] {message} -> {verdict} {reply.text}".strip())
        return reply

    def _procedures_text(self, limit: int = 6000) -> str:
        text = "\n\n".join(self.state.procedures.values()) or "(no specific procedure found; use good judgement)"
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]"

    def _criteria_text(self) -> str:
        return "\n".join(f"{i}. {c}" for i, c in enumerate(self.state.goal.get("success_criteria", []), 1))

    # ================================================================== understand / clarify / plan
    def _understand(self) -> None:
        st = self.state
        self._set_phase("understand")
        self.lessons = self.memory.relevant(st.request)
        hits = self.ctx.search(st.request, k=6)
        excerpts = "\n\n".join(f"[{c.doc}] {c.heading}\n{c.text[:900]}" for c in hits) or "(none)"
        goal = self.llm.chat_json(
            [
                {"role": "system", "content": P.UNDERSTAND_SYSTEM.format(company=self.ctx.company_name)},
                {"role": "user", "content": P.UNDERSTAND_USER.format(
                    request=st.request, today=self._today(), doc_titles=self.ctx.doc_titles(), excerpts=excerpts,
                    systems=self.ctx.systems_text(), lessons="\n".join(f"- {l}" for l in self.lessons) or "(none)")},
            ],
            required=("intended_outcome", "success_criteria"),
        )
        goal["success_criteria"] = [str(c) for c in goal.get("success_criteria") or []][:10]
        st.goal = goal
        names = [n for n in (Path(str(x)).stem for x in goal.get("procedures") or []) if n in self.ctx.docs]
        if not names:  # fall back to retrieval when the model names nothing usable
            names = list(dict.fromkeys(c.doc for c in hits))[:2]
        # The portal guide and communication rules are cheap and almost always relevant.
        for extra in ("acme_portal_guide", "communication_guidelines"):
            if extra in self.ctx.docs and extra not in names:
                names.append(extra)
        st.procedures = {n: self.ctx.docs[n] for n in names}
        goal["success_criteria"] = self._complete_criteria(goal["success_criteria"])
        self.emit("understood", goal=goal, procedures=names, lessons=self.lessons)

        missing = [str(m) for m in goal.get("missing_information") or [] if str(m).strip()]
        if missing:
            self._set_phase("clarify")
            q = "Before I start I need:\n" + "\n".join(f"- {m}" for m in missing)
            reply = self._ask("question", q)
            st.notes["requester_clarification"] = reply.text

    def _complete_criteria(self, criteria: list[str]) -> list[str]:
        """The SOP's "Done means" is the contract: any bullet the model dropped is added back verbatim."""
        out = list(criteria)
        for text in self.state.procedures.values():
            m = re.search(r"^## Done means\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
            if not m:
                continue
            for bullet in re.findall(r"^\s*[-*]\s+(.+)$", m.group(1), re.M):
                want = set(tokenize(bullet))
                covered = any(len(want & set(tokenize(c))) >= max(2, len(want) // 2) for c in out)
                if not covered:
                    out.append(bullet.strip())
        return out

    def _plan(self) -> None:
        st = self.state
        self._set_phase("plan")
        data = self.llm.chat_json(
            [
                {"role": "system", "content": P.PLAN_SYSTEM.format(company=self.ctx.company_name)},
                {"role": "user", "content": P.PLAN_USER.format(
                    request=st.request, goal=st.goal.get("intended_outcome", ""), criteria=self._criteria_text(),
                    clarifications="\n".join(st.clarifications) or "(none)", procedures=self._procedures_text(),
                    systems=self.ctx.systems_text())},
            ],
            required=("plan",),
        )
        st.plan = [PlanStep(i, _clean_step(s)) for i, s in enumerate(data["plan"][:20], 1)]
        self.emit("planned", plan=[s.text for s in st.plan])
        self._save()

    # ================================================================== the act/observe loop
    def _loop(self, phase: str, tool_names: list[str], budget: int) -> str:
        st = self.state
        if st.phase != phase:
            self._set_phase(phase)
        last_obs, last_label = "(nothing yet — start with the first plan step)" if phase == "execute" else "(none yet)", ""
        consecutive_fail = 0
        used = 0
        warning = ""
        if phase == "execute" and st.notes.get("VERIFICATION_FAILED"):
            warning = "IMPORTANT: independent verification FAILED for: " + "; ".join(st.notes["VERIFICATION_FAILED"]) + \
                      "\nFix these, then call finish again."
        while used < budget:
            self._check_stop()
            used += 1
            messages = self._step_messages(phase, tool_names, last_obs, last_label, warning)
            warning = ""
            try:
                decision = self.llm.chat_json(messages, required=("action",))
            except LLMError as e:
                consecutive_fail += 1
                last_obs, last_label = f"Your previous reply could not be parsed: {e}", ""
                if consecutive_fail >= 3:
                    raise
                continue
            thought = str(decision.get("thought", ""))[:600]
            tool, args = self._normalize_action(decision)
            if isinstance(decision.get("notes"), dict):
                st.notes.update({str(k): v for k, v in decision["notes"].items()})
            st.mark_done(decision.get("done_steps") or [])
            self.emit("thought", phase=phase, thought=thought, tool=tool, args=args)

            if self._is_repeat_failure(phase, tool, args):
                result = ToolResult(False, "NOT EXECUTED: this exact action already failed twice in a row. Repeating it "
                                           "will not work. Re-read the procedure/plan and choose a DIFFERENT action "
                                           "(other tool, other URL, other values) or ask_human.", "refused repeat of failing action")
            elif phase == "execute" and tool == "read_file" and self._pinned(args.get("path", "")):
                result = ToolResult(True, f"Already read — its full content is under SOURCE DOCUMENTS. Do not re-read it. "
                                          f"{self._next_hint()}", "already read (pinned)")
            elif phase == "verify" and self._already_observed(tool, args):
                result = ToolResult(True, "Already observed — its result is in EVIDENCE COLLECTED above. Check a criterion "
                                          "you have not checked yet, or call report_verification now.", "already observed")
            else:
                result = self._dispatch(phase, tool, args, tool_names)
            if tool == "read_file" and result.ok:  # source documents stay pinned in context for the whole run
                st.documents[self.workspace.rel(self.workspace.resolve(str(args.get("path", ""))))] = result.observation
            n = len(st.steps) + 1
            self._track_items(n, tool, result)
            st.steps.append(StepRecord(n, phase, thought, tool, args, result.ok, result.observation, result.summary))
            for kind, path, caption in result.evidence:
                st.evidence.append(Evidence(kind, path, caption, n))
            self.emit("observation", n=n, tool=tool, ok=result.ok, summary=result.summary,
                      observation=result.observation[:4000], evidence=result.evidence)
            if phase == "execute":  # computed here: the checklist may touch the browser, which is bound to this thread
                self.emit("worklist", text=self.checks.worklist_text(st, self.tools))
            self._save()

            if result.control == "finish" and phase == "execute":
                st.notes["operator_claim"] = args.get("summary", "")
                return "finish"
            if result.control == "verified" and phase == "verify":
                return "verified"

            last_obs, last_label = result.observation, f" (from #{n} {tool})"
            consecutive_fail = 0 if result.ok else consecutive_fail + 1
            warning = self._loop_warning(phase)
            if consecutive_fail >= self.policy.max_consecutive_failures and phase == "execute":
                recent = "\n".join(f"- {s.tool}: {s.summary}" for s in st.steps[-consecutive_fail:])
                reply = self._ask("guidance", f"I've hit {consecutive_fail} failures in a row and may be stuck:\n{recent}\n"
                                              "How should I proceed? (Reply 'abort' to stop the run.)")
                if reply.text.strip().lower() == "abort":
                    return "aborted by human after repeated failures"
                warning = f"HUMAN GUIDANCE: {reply.text}"
                consecutive_fail = 0
        return f"step budget exhausted in {phase} ({budget} steps)"

    def _step_messages(self, phase: str, tool_names: list[str], obs: str, label: str, warning: str) -> list[dict]:
        st = self.state
        cap = 7000
        obs = obs if len(obs) <= cap else obs[:cap] + "\n...[observation truncated]"
        common = dict(company=self.ctx.company_name, tools=tools_prompt(tool_names))
        if phase == "verify":
            files = sorted({e.caption for e in st.evidence if e.kind in ("file", "email")}) or ["(none)"]
            verify_steps = [s for s in st.steps if s.phase == "verify"]
            history = "\n".join(f"#{s.n} {s.tool} {json.dumps(s.args)[:120]} -> {'OK' if s.ok else 'FAILED'}: {s.summary}"
                                for s in verify_steps[-10:]) or "(none yet)"
            return [
                {"role": "system", "content": P.VERIFY_SYSTEM.format(**common)},
                {"role": "user", "content": P.VERIFY_USER.format(
                    request=st.request, criteria=self._criteria_text(), claim=st.notes.get("operator_claim", ""),
                    files=", ".join(files), systems=self.ctx.systems_text(), history=history,
                    procedures=self._procedures_text(4000), documents=self._documents_text(per_doc=1500, total=4500),
                    evidence=self._evidence_pack(), observation=obs[:1500], last_label=label, warning=warning)},
            ]
        return [
            {"role": "system", "content": P.EXECUTE_SYSTEM.format(**common)},
            {"role": "user", "content": P.EXECUTE_USER.format(
                request=st.request, goal=st.goal.get("intended_outcome", ""), criteria=self._criteria_text(),
                clarifications="\n".join(st.clarifications) or "(none)", today=self._today(),
                procedures=self._procedures_text(), systems=self.ctx.systems_text(),
                lessons="\n".join(f"- {l}" for l in self.lessons) or "(none)", plan=st.plan_text(),
                documents=self._documents_text(), worklist=self.checks.worklist_text(st, self.tools),
                notes=st.notes_text(), history=st.history_text(), observation=obs, last_label=label,
                warning=("\n" + warning + "\n") if warning else "",
                next_action=self.checks.next_action(st, self.tools) or
                "All work items are complete: do the close-out steps of the procedure, then finish.")},
        ]

    @staticmethod
    def _normalize_action(decision: dict) -> tuple[str, dict]:
        action = decision.get("action")
        if isinstance(action, str):  # {"action": "browser_click", "args": {...}}
            return action, decision.get("args") or decision.get("arguments") or {}
        action = action or {}
        tool = action.get("tool") or action.get("name") or decision.get("tool") or ""
        args = action.get("args") or action.get("arguments") or action.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return str(tool), args if isinstance(args, dict) else {}

    def _already_observed(self, tool: str, args: dict) -> bool:
        if tool == "browser_read":  # nothing changes while verifying: re-reading the same page adds nothing
            prev = [s for s in self.state.steps if s.phase == "verify"][-1:]
            return bool(prev) and prev[0].ok and prev[0].tool in ("browser_read", "browser_open", "browser_login")
        if tool not in ("list_files", "read_file", "browser_open"):
            return False
        sig = (tool, json.dumps(args, sort_keys=True))
        return any(s.ok and s.phase == "verify" and (s.tool, json.dumps(s.args, sort_keys=True)) == sig
                   for s in self.state.steps)

    def _evidence_pack(self, per_item: int = 2500, max_items: int = 5) -> str:
        """Distinct successful verification observations, newest last — the verifier's memory."""
        seen: dict[str, StepRecord] = {}
        for s in self.state.steps:
            if s.phase == "verify" and s.ok and s.tool not in ("report_verification",) and \
                    not s.observation.startswith("Already observed"):
                key = s.observation.splitlines()[0] if s.tool.startswith("browser") else f"{s.tool}{s.args}"
                seen.pop(key, None)
                seen[key] = s
        items = list(seen.values())[-max_items:]
        if not items:
            return "(nothing collected yet)"
        out = []
        for s in items:
            obs = s.observation if len(s.observation) <= per_item else s.observation[:per_item] + "\n...[truncated]"
            out.append(f"--- #{s.n} {s.tool} {json.dumps(s.args)[:100]} ---\n{obs}")
        return "\n\n".join(out)

    def _documents_text(self, per_doc: int = 2500, total: int = 7000) -> str:
        docs, used = [], 0
        for path, text in self.state.documents.items():
            t = text if len(text) <= per_doc else text[:per_doc] + "\n...[truncated]"
            if used + len(t) > total:
                docs.append(f"(+ {path} omitted for space — re-read it if needed)")
                continue
            docs.append(t)
            used += len(t)
        return "\n\n".join(docs) or "(none yet)"

    def _is_repeat_failure(self, phase: str, tool: str, args: dict) -> bool:
        sig = (tool, json.dumps(args, sort_keys=True))
        recent = [s for s in self.state.steps if s.phase == phase][-2:]
        return len(recent) == 2 and all(not s.ok and (s.tool, json.dumps(s.args, sort_keys=True)) == sig for s in recent)

    def _track_items(self, n: int, tool: str, result: ToolResult) -> None:
        """An item is DONE when a successful submit's confirmation message names it — observed, not claimed."""
        if not result.ok or tool not in ("browser_fill", "browser_click"):
            return
        alert = next((l for l in result.observation.splitlines() if l.startswith("ALERTS:")), "")
        if not alert:
            return
        for item, iid in self.checks.item_ids(self.state, self.tools).items():
            if iid and iid in alert:
                self.state.item_status[item] = f"#{n}: {alert[len('ALERTS:'):].strip()[:120]}"

    def _stalled(self, window: int = 8) -> bool:
        steps = [s for s in self.state.steps if s.phase == "execute"][-window:]
        progress = lambda s: s.ok and (s.tool in ("write_file", "send_email", "update_plan") or  # noqa: E731
                                       (s.tool in ("browser_fill", "browser_click") and "ALERTS" in s.observation))
        return len(steps) == window and not any(progress(s) for s in steps)

    def _pinned(self, path: str) -> bool:
        try:
            return self.workspace.rel(self.workspace.resolve(str(path))) in self.state.documents
        except Exception:  # noqa: BLE001 - invalid path: let the tool report it
            return False

    def _next_hint(self) -> str:
        st = self.state
        todo = next((s for s in st.plan if s.status != "done"), None)
        nxt = self.checks.next_action(st, self.tools)
        if nxt:  # concrete beats abstract: the exact next procedure step for the next unfinished item
            return nxt
        return f"Next unfinished plan step: {todo.id}. {todo.text}" if todo else "All plan steps are marked done."

    def _loop_warning(self, phase: str) -> str:
        steps = [s for s in self.state.steps if s.phase == phase]
        sigs = [(s.tool, json.dumps(s.args, sort_keys=True)) for s in steps]
        if len(sigs) >= 3 and len(set(sigs[-3:])) == 1:
            return ("WARNING: you have repeated the same action 3 times. It is not making progress. "
                    "Choose a different action or approach (or ask_human). " + (self._next_hint() if phase == "execute" else ""))
        if phase == "execute" and self._stalled():
            return "WARNING: no progress in the last 8 actions (only reading/navigating). " + self._next_hint()
        if len(sigs) >= 6 and len(set(sigs[-6:])) <= 2:  # A,B,A,B,... cycles
            return ("WARNING: you are going in circles (the last 6 actions alternate between the same 2 actions). "
                    "Stop re-checking; act on what you already know. " + (self._next_hint() if phase == "execute" else
                    "If the evidence answers every criterion, call report_verification now."))
        return ""

    # ================================================================== dispatch with policy gate
    def _dispatch(self, phase: str, tool: str, args: dict, allowed: list[str]) -> ToolResult:
        st = self.state
        if tool not in allowed:
            return ToolResult(False, f"'{tool}' is not an available tool in this phase. Available: {', '.join(allowed)}",
                              f"invalid tool '{tool}'")

        # ---- control tools (handled by the agent itself)
        if tool == "finish":
            unread = [i for i in self.checks.work_items(st, self.tools) if i not in st.documents]
            if unread:
                return ToolResult(False, f"NOT FINISHED: {len(unread)} work item(s) have not even been read: {unread}. "
                                         "Process every item per the procedure, then finish.", "finish refused: items left")
            failing = [r for r in self.checks.run(st, self.tools) if not r["passed"]]
            if failing:  # countable completion is a gate on finishing, not just a verdict afterwards
                lines = "\n".join(f"- {r['criterion']}: {r['evidence']}" for r in failing)
                return ToolResult(False, f"NOT FINISHED — completion checks fail:\n{lines}\nDo the missing work for these "
                                         f"items (each submit must show a 'saved' confirmation), then finish.\n"
                                         f"{self._next_hint()}", f"finish refused: {len(failing)} check(s) failing")
            return ToolResult(True, "Task declared complete; handing over to verification.", "finish", control="finish")
        if tool == "report_verification":
            results = args.get("results")
            if not isinstance(results, list) or not results:
                return ToolResult(False, 'report_verification needs "results": [{"criterion","passed","evidence"}...]', "bad report")
            n_criteria = len(st.goal.get("success_criteria", []))
            if len([r for r in results if isinstance(r, dict)]) < n_criteria:
                return ToolResult(False, f"Incomplete: {n_criteria} criteria must each get a result, you gave {len(results)}. "
                                         f"Criteria:\n{self._criteria_text()}", "incomplete verification report")
            grounded = [self._ground(
                {"criterion": str(r.get("criterion", "")), "passed": bool(r.get("passed")), "evidence": str(r.get("evidence", ""))})
                for r in results if isinstance(r, dict)]
            unquoted = [i for i, v in enumerate(grounded, 1) if "a pass must quote" in v["evidence"]]
            if unquoted and not st.notes.get("_requoted"):  # a formatting slip shouldn't fail correct work: one retry
                st.notes["_requoted"] = True
                return ToolResult(False, f"Results {unquoted} claim a pass without quoting what you saw. Quote the exact "
                                         "observed text in 'single quotes' (from EVIDENCE COLLECTED) and call "
                                         "report_verification again with ALL criteria.", "report needs quotes")
            st.verification = grounded
            return ToolResult(True, "Verification recorded.", f"{sum(v['passed'] for v in st.verification)}/{len(st.verification)} passed",
                              control="verified")
        if tool == "update_plan":
            steps = [_clean_step(s) for s in args.get("steps") or [] if str(s).strip()]
            if not steps:
                return ToolResult(False, 'update_plan needs "steps": [..]', "empty plan")
            kept = [s.text for s in st.plan if s.status == "done"]
            st.plan = [PlanStep(i, t, "done") for i, t in enumerate(kept, 1)] + \
                      [PlanStep(len(kept) + i, s) for i, s in enumerate(steps, 1)]
            self.emit("planned", plan=[s.text for s in st.plan], replanned=True)
            return ToolResult(True, "Plan updated:\n" + st.plan_text(), "plan updated", control="plan")
        if tool == "ask_human":
            q = str(args.get("question", "")).strip()
            if not q:
                return ToolResult(False, "ask_human needs a question.", "empty question")
            reply = self._ask("question", q)
            return ToolResult(True, f"Human answered: {reply.text}", f"human: {reply.text[:100]}")
        if tool == "request_approval":
            reply = self._ask("approval", str(args.get("action", "")), reason=args.get("reason", ""))
            if reply.approved:
                return ToolResult(True, f"APPROVED by human. {reply.text}", "approved")
            return ToolResult(True, f"DENIED by human. {reply.text} Do not perform that action.", "denied")

        # ---- fill-and-submit as one step (forms re-filled from scratch every time = no stale-form submits).
        # The gate runs between fill and click, so it sees the values actually typed.
        if tool == "browser_fill" and args.get("submit_id") not in (None, "", 0, "0"):
            filled = self._dispatch(phase, "browser_fill", {"fields": args.get("fields") or {}}, allowed)
            if not filled.ok:
                return filled
            res = self._dispatch(phase, "browser_click", {"id": args["submit_id"]}, allowed)
            res.observation = filled.summary + (" Then submitted.\n" if res.ok else " Submit FAILED:\n") + res.observation
            res.summary = f"{filled.summary.rstrip('.')} → {res.summary}"
            return res

        # ---- irreversible submits about a work item must match the procedure's known expectations
        if tool == "browser_click" and phase == "execute":
            info = self.browser.describe(args.get("id")) or {}
            if info.get("form_method") == "post":
                problem = self.checks.submit_guard(info.get("form_values") or {}, st, self.tools)
                if problem:
                    self.emit("policy", verdict="block", rule="submit-guard", reason=problem, tool=tool, args=args)
                    return ToolResult(False, f"BLOCKED before submit — nothing was saved. The form does not match the "
                                             f"procedure: {problem}. Fix those fields and submit again.",
                                      "blocked: submit-guard (nothing saved)")

        if tool == "send_email" and phase == "execute":
            problem = self.checks.email_guard(str(args.get("to", "")), str(args.get("subject", "")),
                                              str(args.get("body", "")), st, self.tools)
            if problem:
                self.emit("policy", verdict="block", rule="email-guard", reason=problem, tool=tool, args=args)
                return ToolResult(False, f"BLOCKED before sending — nothing was sent. {problem}.", "blocked: email-guard")

        if tool == "browser_open" and "url" in args:
            args = {**args, "url": self.browser.resolve_url(args["url"])}

        # ---- world-changing / observing tools go through the policy gate
        decision = self.policy.check(tool, args, phase, self.browser.describe, self.browser.visited)
        if decision.verdict == "block":
            self.emit("policy", verdict="block", rule=decision.rule, reason=decision.reason, tool=tool, args=args)
            return ToolResult(False, f"BLOCKED by company policy ({decision.rule}) — the action was NOT performed; nothing "
                                     f"was submitted or saved. {decision.reason}", f"blocked: {decision.rule} (nothing saved)")
        if decision.verdict == "approve":
            ctx: dict[str, Any] = {"tool": tool, "rule": decision.rule}
            if tool == "browser_click":
                info = self.browser.describe(args.get("id")) or {}
                ctx["button"] = info.get("label")
                ctx["form"] = {k: (v[1] if isinstance(v, list) else v) for k, v in (info.get("form_values") or {}).items()}
                ctx["screenshot"] = str(self.browser.screenshot("pending approval"))
            else:
                ctx["args"] = args
            # An identical action (same rule, same form values / arguments) approved earlier this run is not re-asked,
            # e.g. a resubmit after a transient error. Any change in values asks again.
            key = json.dumps([decision.rule, ctx.get("form") or ctx.get("args")], sort_keys=True, default=str)
            if key in self._approved:
                reply = HumanReply(True, "previously approved (identical action)")
                self.emit("policy", verdict="approve", rule=decision.rule, reason="already approved for this exact action",
                          tool=tool, args=args)
            else:
                self.emit("policy", verdict="approve", rule=decision.rule, reason=decision.reason, tool=tool, args=args)
                reply = self._ask("approval", decision.reason, **ctx)
                if reply.approved:
                    self._approved.add(key)
            if not reply.approved:
                return ToolResult(False, f"DENIED by human approver ({decision.rule}). Comment: {reply.text or '-'}. "
                                         "Do not retry this action; follow the procedure for unapproved items or ask how to proceed.",
                                  "denied by approver")
        result = self.tools.run(tool, args)
        if decision.verdict == "approve":
            result.observation = f"(Human approved this action.)\n{result.observation}"
        return result

    # ================================================================== verify / report
    def _verify(self) -> str:
        """'pass' | 'review' (code checks pass; some model claims unsubstantiated, none contradicted) | 'fail'."""
        st = self.state
        st.verification = []
        self._set_phase("verify")
        auto = self.checks.run(st, self.tools)  # countable completeness and rules, checked in code first
        self.emit("auto_checks", results=auto)
        if any(not r["passed"] for r in auto):  # no point asking the model to judge an incomplete job
            st.verification = auto
            self.emit("verified", passed=False, results=st.verification)
            return "fail"
        outcome = self._loop("verify", VERIFY_TOOLS, self.max_verify_steps)
        if outcome != "verified" or not st.verification:
            st.verification = self._forced_judgment(outcome)
        st.verification = auto + st.verification
        failed = [v for v in st.verification if not v["passed"]]
        verdict = "pass" if not failed else (
            "review" if auto and all(v.get("unsupported") for v in failed) else "fail")
        self.emit("verified", passed=verdict == "pass", verdict=verdict, results=st.verification)
        return verdict

    def _ground(self, result: dict[str, Any]) -> dict[str, Any]:
        """A 'pass' must rest on observed facts: every quoted value in its evidence has to appear in something the
        system actually observed this run (pages, files, tool outputs). Otherwise it is downgraded to a fail."""
        if not result["passed"]:
            return result
        quotes = [q for pair in re.findall(r"(?<![A-Za-z])'([^']{4,120})'(?![A-Za-z])|\"([^\"]{4,120})\"",
                                           result["evidence"]) for q in pair if q]
        if not quotes:  # silence proves nothing
            return {**result, "passed": False, "unsupported": True,
                    "evidence": f"{result['evidence']} [UNSUPPORTED: a pass must quote the observed text in 'single quotes']"}
        norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()  # noqa: E731
        # System claims are grounded in what systems DISPLAYED (page text, not form inputs the agent typed) and in
        # the input documents — never in the agent's own writing, so a claim cannot cite itself.
        pages = list(getattr(getattr(self, "browser", None), "displayed_log", []))
        # file names the system itself listed or confirmed writing are observed facts too
        listings = [s.observation for s in self.state.steps if s.ok and s.tool in ("list_files", "write_file", "send_email")]
        corpus = norm("\n".join(pages + listings + list(self.state.documents.values())))
        if re.search(r"(?i)\b(file|report|checklist|summary|outbox|emailed|sent|notified)\b", result["criterion"]):
            for e in self.state.evidence:  # criteria about produced files/emails may cite them as they are on disk
                if e.kind in ("file", "email"):
                    try:
                        corpus += "\n" + norm(Path(e.path).read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
        missing = [q for q in quotes if norm(q) not in corpus]
        if missing:
            return {**result, "passed": False, "unsupported": True,
                    "evidence": f"{result['evidence']} [UNSUPPORTED: {missing} not found in any observation this run]"}
        return result

    def _forced_judgment(self, outcome: str) -> list[dict[str, Any]]:
        """The verifier ran out of steps: judge each criterion strictly from the evidence it did collect."""
        criteria = self.state.goal.get("success_criteria", [])
        try:
            data = self.llm.chat_json(
                [
                    {"role": "system", "content": P.VERIFY_FINAL_SYSTEM},
                    {"role": "user", "content": P.VERIFY_FINAL_USER.format(
                        criteria=self._criteria_text(), procedures=self._procedures_text(4000),
                        evidence=self._evidence_pack(per_item=3000, max_items=6))},
                ],
                required=("results",),
            )
            results = [r for r in data["results"] if isinstance(r, dict)]
            if results:
                return [self._ground({"criterion": str(r.get("criterion", "")), "passed": bool(r.get("passed")),
                                      "evidence": str(r.get("evidence", "")) + " (judged from collected evidence)"})
                        for r in results]
        except LLMError:
            pass
        return [{"criterion": c, "passed": False, "evidence": f"Not verified ({outcome})."} for c in criteria]

    def _report(self) -> None:
        st = self.state
        self._set_phase("report")
        human = [c for c in st.clarifications] or ["(none)"]
        try:
            data = self.llm.chat_json(
                [
                    {"role": "system", "content": P.REPORT_SYSTEM.format(company=self.ctx.company_name)},
                    {"role": "user", "content": P.REPORT_USER.format(
                        request=st.request, goal=st.goal.get("intended_outcome", ""), status=st.status,
                        verification=json.dumps(st.verification, indent=1)[:3000], notes=st.notes_text()[:3000],
                        history=st.history_text(last=40)[:6000], human="\n".join(human))},
                ],
                required=("summary",),
            )
            summary = str(data.get("summary", ""))
            st.lessons = [str(l) for l in data.get("lessons") or [] if str(l).strip()][:3]
        except LLMError as e:
            summary = f"(Report generation failed: {e})"
        st.report = self._render_report(summary)
        (self.run_dir / "report.md").write_text(st.report, encoding="utf-8")
        if st.lessons and st.status == "completed":  # only learn from verified outcomes
            self.memory.add(st.lessons, st.request, st.run_id)
        st.finished = time.time()
        st.phase = "done"
        self._save()
        self.emit("done", status=st.status, report=st.report, lessons=st.lessons)

    def _render_report(self, summary: str) -> str:
        st = self.state
        u = self.llm.usage
        lines = [
            f"# Run {st.run_id} — {st.status.upper()}",
            f"**Request:** {st.request}",
            f"**Intended outcome:** {st.goal.get('intended_outcome', '-')}",
            "", "## Summary", summary, "", "## Verification",
            "| # | Criterion | Result | Evidence |", "|---|---|---|---|",
        ]
        for i, v in enumerate(st.verification, 1):
            ev = str(v.get("evidence", "")).replace("|", "/").replace("\n", " ")[:300]
            lines.append(f"| {i} | {v['criterion']} | {'PASS' if v['passed'] else 'FAIL'} | {ev} |")
        if not st.verification:
            lines.append("| - | (not reached) | - | - |")
        lines += ["", "## Human interactions"] + [f"- {c}" for c in st.clarifications or ["(none)"]]
        lines += ["", "## Evidence"] + ([f"- {e.kind}: {e.caption} — `{e.path}` (step {e.step})" for e in st.evidence] or ["(none)"])
        lines += ["", "## Plan"] + st.plan_text().splitlines()
        lines += ["", f"_Steps: {len(st.steps)} · LLM calls: {u.calls} · tokens in/out: {u.prompt_tokens}/{u.completion_tokens} "
                      f"· LLM time: {u.seconds:.0f}s · wall time: {time.time() - st.started:.0f}s_"]
        if st.notes.get("failure"):
            lines.insert(3, f"**Failure:** {st.notes['failure']}")
        return "\n".join(lines)
