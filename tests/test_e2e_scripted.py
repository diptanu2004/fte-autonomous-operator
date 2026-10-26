"""End-to-end loop test with a deterministic stand-in for the LLM.

The fake model reads the same prompts the real one gets and picks element ids from the live page
observation, so this exercises the real portal, Playwright, policy gate, transient-503 recovery,
human approval, verification and reporting — everything except model judgement.
"""
import json
import re
import shutil
from pathlib import Path

import pytest

from demo_portal import portal
from fte.agent import Agent
from fte.human import ScriptedHuman

ROOT = Path(__file__).resolve().parents[1]
PORT = 5055


def el(obs: str, kind: str, label: str) -> int:
    m = re.search(rf"\[(\d+)\] {kind}\S* \"{re.escape(label)}\"", obs)
    assert m, f"{kind} '{label}' not in observation:\n{obs[-1500:]}"
    return int(m.group(1))


class FakeLLM:
    """Records one invoice (INV-2042, > $5k so it hits the approval gate) and verifies it."""

    def __init__(self):
        from fte.llm import Usage

        self.usage = Usage()
        self.submits = 0
        self.po_seen = False
        self.criteria_seen: list[str] = []

    def chat_json(self, messages, required=(), retries=2):
        self.usage.calls += 1
        system, user = messages[0]["content"], messages[-1]["content"]
        if "intake analyst" in system:
            return {"intended_outcome": "INV-2042 recorded", "procedures": ["ap_invoice_processing"],
                    "success_criteria": ["INV-2042 is listed on the Invoices page as Approved for payment"],
                    "missing_information": []}
        if "planner" in system:
            return {"plan": ["Sign in", "Record INV-2042", "Finish"]}
        if "completion report" in system:
            return {"summary": "Recorded INV-2042.", "lessons": ["Invoice service can 503 once; resubmitting works."]}
        if "VERIFIER" in system:
            evidence = user.split("EVIDENCE COLLECTED", 1)[1]
            rows = [l for l in evidence.splitlines() if "INV-2042" in l and "Contoso" in l]
            if not rows:
                return {"thought": "check list", "action": {"tool": "browser_open", "args": {"url": "http://127.0.0.1:5055/invoices"}}}
            row = rows[0]
            crit = user.split("SUCCESS CRITERIA TO VERIFY:", 1)[1].split("PROCEDURE", 1)[0]
            crit = [c.split(". ", 1)[1] for c in crit.strip().splitlines() if ". " in c]
            self.criteria_seen = crit
            return {"thought": "found", "action": {"tool": "report_verification", "args": {"results": [
                {"criterion": c, "passed": "Approved for payment" in row,
                 "evidence": "Invoices row 'INV-2042' with status 'Approved for payment'"} for c in crit]}}}
        obs = user.split("LAST OBSERVATION", 1)[1]
        if "Signed in" not in user and "signed in" not in user:
            return {"thought": "login", "action": {"tool": "browser_login", "args": {"system": "acme_portal"}}}
        if "NEXT ACTION for INV-2042: read_file" in user:
            return {"thought": "read input", "action": {"tool": "read_file", "args": {"path": "inbox/contoso_INV-2042.md"}}}
        if "Wrote" in obs:
            return {"thought": "done", "done_steps": [1, 2], "action": {"tool": "finish", "args": {"summary": "Recorded"}}}
        if "INV-2042 saved" in obs:
            return {"thought": "report", "action": {"tool": "write_file", "args": {
                "path": "reports/ap_summary.md", "content": "INV-2042 Contoso PO-1007 7850.00 Approved for payment"}}}
        if "Record Invoice" in obs and 'value=""' in obs.split("Invoice number", 2)[-1][:40]:
            # first attempt: fill then click separately; after the 503, fill-and-submit in one action
            submit = {"submit_id": el(obs, "button", "Save invoice")} if self.submits else {}
            if submit:
                self.submits += 1
            return {"thought": "fill", "action": {"tool": "browser_fill", "args": {**submit, "fields": {
                str(el(obs, "input", "Invoice number")): "INV-2042",
                str(el(obs, "select", "Vendor")): "Contoso Supplies (V-200)",
                str(el(obs, "input", "PO number")): "PO-1007",
                str(el(obs, "input", "Invoice date")): "2026-10-21",
                str(el(obs, "input", "Amount (USD)")): "7850.00",
                str(el(obs, "select", "Status")): "Approved for payment",
                str(el(obs, "textarea", "Notes")): "Matched PO PO-1007"}}}}
        if "Record Invoice" in obs:
            self.submits += 1
            return {"thought": "submit", "action": {"tool": "browser_click", "args": {"id": el(obs, "button", "Save invoice")}}}
        if not self.po_seen:  # SOP precondition (enforced by policy): review the PO before recording
            self.po_seen = True
            return {"thought": "check PO", "action": {"tool": "browser_open", "args": {"url": "http://127.0.0.1:5055/purchase-orders/PO-1007"}}}
        return {"thought": "open form (again after 503)", "action": {"tool": "browser_open", "args": {"url": "http://127.0.0.1:5055/invoices/new"}}}


@pytest.fixture()
def world(tmp_path):
    portal.reset_db()
    portal.serve_in_background(PORT)
    ws = tmp_path / "workspace"
    shutil.copytree(ROOT / "demo_data", ws)
    for f in (ws / "inbox").iterdir():  # one work item keeps the scripted flow short
        if "INV-2042" not in f.name:
            f.unlink()
    return tmp_path, ws


def test_invoice_flow_with_approval_and_503_recovery(world):
    tmp, ws = world
    human = ScriptedHuman(approve=True)
    agent = Agent(FakeLLM(), human, workspace_dir=ws, runs_dir=tmp / "runs", memory_path=tmp / "lessons.jsonl")
    st = agent.run("Book Contoso invoice INV-2042")

    assert st.status == "completed", st.report
    assert agent.llm.submits == 2, "first submit hits the transient 503, second succeeds"
    assert len(st.goal["success_criteria"]) > 1, "SOP 'Done means' bullets are backfilled into the criteria"
    assert any(r.kind == "approval" for r in human.log), "> $5k approval gate must fire"
    assert sum(r.kind == "approval" for r in human.log) == 1, "the retry after the 503 is the identical action: not re-asked"
    import urllib.request

    data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/__state"))
    inv = {i["invoice_number"]: i for i in data["invoices"]}
    assert inv["INV-2042"]["status"] == "Approved for payment"
    assert (tmp / "runs" / st.run_id / "report.md").exists()
    assert any(e.kind == "screenshot" for e in st.evidence)
    assert "503" in (tmp / "lessons.jsonl").read_text()
    assert "contoso_INV-2042.md" in str(st.item_status), "item confirmed from the portal's 'saved' alert"
    auto = [v for v in st.verification if v["criterion"].startswith("[auto]")]
    assert len(auto) == 4 and all(v["passed"] for v in auto), auto


def test_auto_checks_catch_unprocessed_items(world, tmp_path):
    """Completeness is counted in code: an unread inbox item fails verification whatever the model claims."""
    from fte.checks import CompletionChecks
    from fte.knowledge import CompanyContext
    from fte.state import RunState
    from fte.tools import Toolbox
    from fte.tools.browser import Browser
    from fte.tools.files import Workspace

    _, ws = world
    shutil.copy(ROOT / "demo_data" / "inbox" / "northwind_invoice_2041.txt", ws / "inbox")
    ctx = CompanyContext(ROOT / "company")
    st = RunState("x", procedures={"ap_invoice_processing": ""}, documents={"inbox/contoso_INV-2042.md": "..."})
    browser = Browser(tmp_path / "shots")
    try:
        tools = Toolbox(ctx, Workspace(ws), browser)
        checks = CompletionChecks(ROOT / "company" / "completion_checks.yaml")
        work = checks.worklist_text(st, tools)
        assert "[ ] read_file the invoice file inbox/northwind_invoice_2041.txt" in work, work
        assert "[ ] browser_open http://127.0.0.1:5055/purchase-orders/PO-1007" in work, work
        assert checks.next_action(st, tools).startswith("NEXT ACTION for INV-2042: browser_open")
        res = {r["criterion"]: r for r in checks.run(st, tools)}
    finally:
        browser.close()
    assert all(not r["passed"] for r in res.values()), res
    assert any("INV-2041" in r["evidence"] for r in res.values())


def test_denied_approval_is_not_executed(world):
    tmp, ws = world
    human = ScriptedHuman(approve=False)
    agent = Agent(FakeLLM(), human, workspace_dir=ws, runs_dir=tmp / "runs", memory_path=tmp / "l.jsonl", max_steps=12)
    st = agent.run("Book Contoso invoice INV-2042")
    import urllib.request

    data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/__state"))
    assert "INV-2042" not in {i["invoice_number"] for i in data["invoices"]}
    assert any("DENIED" in s.observation for s in st.steps)


def test_business_rule_lookup_and_submit_guard(world, tmp_path):
    """After the PO page is opened, the 2% rule is computed in code and a wrong status is blocked pre-submit."""
    from fte.checks import CompletionChecks
    from fte.knowledge import CompanyContext
    from fte.state import RunState
    from fte.tools import Toolbox
    from fte.tools.browser import Browser
    from fte.tools.files import Workspace

    _, ws = world
    shutil.copy(ROOT / "demo_data" / "inbox" / "fabrikam-3307.csv", ws / "inbox")
    ctx = CompanyContext(ROOT / "company")
    st = RunState("x", procedures={"ap_invoice_processing": ""}, documents={"inbox/fabrikam-3307.csv": "..."})
    browser = Browser(tmp_path / "shots")
    try:
        tools = Toolbox(ctx, Workspace(ws), browser)
        checks = CompletionChecks(ROOT / "company" / "completion_checks.yaml")
        wrong = {"invoice_number": "INV-3307", "amount": "3560.00", "status": ["Approved for payment"] * 2}
        assert checks.submit_guard(wrong, st, tools) == "", "no expectation before the PO was reviewed"
        tools.t_browser_login("acme_portal")
        tools.t_browser_open(f"http://127.0.0.1:{PORT}/purchase-orders/PO-1011")
        assert "status must be 'On hold'" in checks.submit_guard(wrong, st, tools)
        right = {**wrong, "amount": "3,560", "status": ["On hold", "On hold"]}
        assert checks.submit_guard(right, st, tools) == ""
        # the hold email's recipient is a looked-up fact: unknown until the Vendors page is opened, then enforced
        assert "not established yet" in checks.email_guard("billing@fabrikam.example", "INV-3307 on hold", "", st, tools)
        tools.t_browser_open(f"http://127.0.0.1:{PORT}/vendors")
        assert "accounts@fabrikam.example" in checks.email_guard("billing@fabrikam.example", "INV-3307 on hold", "", st, tools)
        assert checks.email_guard("accounts@fabrikam.example", "INV-3307 on hold", "", st, tools) == ""
        assert checks.email_guard("someone@else.example", "unrelated", "", st, tools) == ""
        assert "status 'On hold'" in checks.next_action(st, tools) or "On hold" in checks.worklist_text(st, tools)
        # text the agent types is visible to it, but never counts as system-displayed evidence
        tools.t_browser_open(f"http://127.0.0.1:{PORT}/tickets/new")
        obs = browser.fill({"Notes": "Snowflake read access"}, lambda v: v)
        assert "Snowflake read access" in obs
        assert "Snowflake read access" not in browser.displayed_log[-1]
    finally:
        browser.close()


def test_onboarding_checklist_from_sop_table(world, tmp_path):
    """Items come from the SOP's equipment matrix; steps tick only on rows the portal displays — merged tickets don't count."""
    from datetime import date, timedelta

    from fte.checks import CompletionChecks
    from fte.knowledge import CompanyContext
    from fte.state import RunState
    from fte.tools import Toolbox
    from fte.tools.browser import Browser
    from fte.tools.files import Workspace

    _, ws = world
    ctx = CompanyContext(ROOT / "company")
    start = (date.today() + timedelta(days=5)).isoformat()
    st = RunState(f"Please onboard Priya Sharma, our new Data Analyst in Dana Lee's team. She starts on {start}.",
                  procedures={"employee_onboarding": ""}, clarifications=["[Q] personal email? -> priya@gmail.com"])
    browser = Browser(tmp_path / "shots")
    try:
        tools = Toolbox(ctx, Workspace(ws), browser)
        checks = CompletionChecks(ROOT / "company" / "completion_checks.yaml", ctx.docs)
        groups = checks.item_progress(st, tools)
        assert [g["item"] for g in groups] == ["setup", "Hardware: MacBook Pro 14 (16GB)", "Software access: Tableau",
                                              "Software access: Snowflake read access", "close-out"]
        assert "Priority 'High'" in groups[1]["steps"][0]["do"], "start within 7 days -> High (configured rule)"
        assert "work email 'priya.sharma@acme-corp.com'" in groups[0]["steps"][0]["do"], "SOP format derived, not guessed"
        assert checks.next_action(st, tools).startswith("NEXT ACTION for setup: Create the employee Priya Sharma")

        tools.t_browser_login("acme_portal")
        tools.t_browser_open(f"http://127.0.0.1:{PORT}/employees/new")
        browser.fill({"Full name": "Priya Sharma", "Work email": "priya.sharma@acme-corp.com", "Personal email": "priya@gmail.com",
                      "Job title": "Data Analyst", "Department": "Analytics", "Manager": "Dana Lee", "Start date": start,
                      "Location": "Bangalore"}, lambda v: v)
        browser.click("Create employee")
        tools.t_browser_open(f"http://127.0.0.1:{PORT}/tickets/new")
        browser.fill({"Category": "Software access", "For employee": "Priya Sharma", "Item": "Tableau; Snowflake read access",
                      "Priority": "High"}, lambda v: v)
        browser.click("Open ticket")
        done = {g["item"]: g["complete"] for g in checks.item_progress(st, tools)}
        assert done["setup"] is True
        assert done["Software access: Tableau"] is False and done["Software access: Snowflake read access"] is False
        assert checks.next_action(st, tools).startswith("NEXT ACTION for Hardware: MacBook Pro 14 (16GB)")
    finally:
        browser.close()


def test_fill_accepts_labels_and_explains_misses(world, tmp_path):
    from fte.tools.browser import Browser, BrowserError

    b = Browser(tmp_path / "shots")
    try:
        b.open(f"http://127.0.0.1:{PORT}/login")
        out = b.fill({"Username": "ops.agent", "password": "Acme!2026"}, lambda v: v)
        assert "Filled 2 field(s)" in out
        with pytest.raises(BrowserError, match="Fields/buttons here: .*Username"):
            b.fill({"Invoice number": "x"}, lambda v: v)
        assert "Dashboard" in b.click("Sign in")
    finally:
        b.close()
