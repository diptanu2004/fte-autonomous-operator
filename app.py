"""FTE — Streamlit console (bring-your-own-key).

    streamlit run app.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import streamlit as st

from demo_portal import portal
from fte.knowledge import CompanyContext, LessonMemory
from fte.llm import PROVIDERS, LLMConfig
from fte.runner import RunHandle, start_run

ROOT = Path(__file__).resolve().parent
MEMORY = ROOT / "memory" / "lessons.jsonl"
EXAMPLES = {
    "Process invoices": "Process this week's vendor invoices in the inbox.",
    "Onboard (info missing)": "Please onboard Priya Sharma, our new Data Analyst in Dana Lee's team. She starts on 2026-11-02.",
    "Onboard (complete)": "Onboard Arjun Mehta as a Backend Engineer reporting to Raj Patel, starting 2026-11-16, "
                          "London office. Personal email arjun.mehta@gmail.com.",
}

st.set_page_config(page_title="FTE · Autonomous Operator", page_icon="🧑‍💼", layout="wide")


# ---------------------------------------------------------------------- one-time setup
@st.cache_resource
def boot() -> str:
    portal.ensure_db()
    portal.serve_in_background(5055)
    try:  # make sure a Chromium build exists (first boot on a fresh host)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception:  # noqa: BLE001
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    return "ok"


def session_workspace() -> Path:
    """Each browser session gets its own copy of the shared drive so parallel demos don't collide."""
    if "workspace" not in st.session_state:
        ws = Path(tempfile.mkdtemp(prefix="fte-ws-"))
        shutil.copytree(ROOT / "demo_data", ws, dirs_exist_ok=True)
        st.session_state.workspace = ws
    return st.session_state.workspace


def reset_world() -> None:
    portal.reset_db()
    ws = session_workspace()
    shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(ROOT / "demo_data", ws, dirs_exist_ok=True)


boot()

# ---------------------------------------------------------------------- sidebar: BYOK
with st.sidebar:
    st.header("Model (bring your own key)")
    provider = st.selectbox("Provider", list(PROVIDERS), index=list(PROVIDERS).index("groq"),
                            help="ollama / lmstudio = local Qwen3-30B-A3B (Q4). groq = hosted Qwen3-32B.")
    preset = PROVIDERS[provider]
    base_url = st.text_input("Base URL", preset["base_url"], key=f"url-{provider}")
    model = st.text_input("Model", preset["model"], key=f"model-{provider}")
    api_key = st.text_input("API key", type="password", key=f"key-{provider}",
                            help="Kept only in this browser session's memory; never written to disk or logs.",
                            placeholder="not needed for local servers" if provider in ("ollama", "lmstudio") else "gsk_...")
    thinking = st.toggle("Qwen3 thinking mode", value=False, help="Slower; can help on tricky steps.")
    max_steps = st.slider("Step budget", 20, 120, 70, 5)
    headless = st.toggle("Headless browser", value=True, help="Turn off locally to watch the browser work.")
    st.divider()
    st.caption("Demo world: Acme portal at http://127.0.0.1:5055 (ops.agent / Acme!2026) + a shared drive.")
    if st.button("Reset demo world", use_container_width=True):
        reset_world()
        st.toast("Portal data and workspace reset.")

tab_run, tab_ctx, tab_mem = st.tabs(["Operator", "Company context", "Memory & past runs"])


# ---------------------------------------------------------------------- rendering helpers
PHASES = ["understand", "clarify", "plan", "execute", "verify", "report", "done"]


def render_pending(h: RunHandle) -> None:
    req = h.pending
    if not req:
        return
    key = f"hr-{id(req)}"
    with st.container(border=True):
        if req.kind == "approval":
            st.warning(f"**Approval required:** {req.message}")
            ctx = dict(req.context)
            shot = ctx.pop("screenshot", None)
            c1, c2 = st.columns([3, 2])
            c1.json(ctx, expanded=True)
            if shot and Path(shot).exists():
                c2.image(shot, caption="Page awaiting approval")
            comment = st.text_input("Comment (optional)", key=key + "-c")
            a, d = st.columns(2)
            if a.button("✅ Approve", key=key + "-a", type="primary", use_container_width=True):
                h.respond(True, comment)
                st.rerun()
            if d.button("⛔ Deny", key=key + "-d", use_container_width=True):
                h.respond(False, comment)
                st.rerun()
        else:
            st.info(f"**The operator needs your input:**\n\n{req.message}")
            ans = st.text_area("Your answer", key=key + "-t")
            if st.button("Send answer", key=key + "-s", type="primary") and ans.strip():
                h.respond(True, ans.strip())
                st.rerun()


def render_timeline(events: list[dict]) -> None:
    for ev in events:
        k = ev["event"]
        if k == "worklist":
            continue  # shown in its own panel
        if k == "phase":
            st.markdown(f"##### ▸ {ev['phase'].upper()}")
        elif k == "understood":
            g = ev["goal"]
            st.markdown(f"**Intended outcome:** {g.get('intended_outcome', '')}  \n"
                        f"**Procedures:** {', '.join(ev['procedures'])}")
            if ev.get("lessons"):
                st.caption("Recalled lessons: " + " · ".join(ev["lessons"]))
        elif k == "planned":
            st.markdown(("**Re-planned:** " if ev.get("replanned") else "**Plan:** ") + " → ".join(ev["plan"]))
        elif k == "thought":
            st.markdown(f"💭 {ev['thought']}  \n`{ev['tool']}` `{json.dumps(ev['args'], ensure_ascii=False)[:220]}`")
        elif k == "observation":
            icon = "✅" if ev["ok"] else "⚠️"
            with st.expander(f"{icon} #{ev['n']} {ev['summary'][:140]}"):
                st.code(ev["observation"], language=None)
        elif k == "policy":
            st.markdown(f"🛡️ **Policy {ev['verdict']}** ({ev['rule']}): {ev['reason']}")
        elif k == "human_request":
            st.markdown(f"🙋 **Asked human ({ev['kind']}):** {ev['message']}")
        elif k == "human_reply":
            verdict = ("approved" if ev["approved"] else "denied") if ev["kind"] == "approval" else "answered"
            st.markdown(f"👤 Human {verdict}: {ev.get('text') or ''}")
        elif k == "adapt":
            st.markdown(f"🔁 **{ev['message']}**")
        elif k == "auto_checks":
            for r in ev["results"]:
                st.markdown(f"{'✅' if r['passed'] else '❌'} {r['criterion']}  \n<small>{r['evidence'][:200]}</small>",
                            unsafe_allow_html=True)
        elif k == "verified":
            verdict = ev.get("verdict", "pass" if ev["passed"] else "fail")
            label = {"pass": "✅ **Verification passed**", "review": "🟡 **Verified in code — some claims need a human glance**",
                     "fail": "🔎 **Verification FAILED**"}[verdict]
            st.markdown(label)
        elif k == "error":
            st.error(ev["message"][:2000])


def render_state(h: RunHandle) -> None:
    agent = h.agent
    stt = agent.state if agent else None
    phase = stt.phase if stt else "understand"
    status = stt.status if stt else "starting"
    cols = st.columns(len(PHASES))
    for c, p in zip(cols, PHASES):
        cur = p == phase
        c.markdown(f"{'🟦' if cur else ('✅' if PHASES.index(p) < PHASES.index(phase) else '⬜')} "
                   f"{'**' + p + '**' if cur else p}")
    top = st.columns([1, 1, 1, 1])
    top[0].metric("Status", status)
    top[1].metric("Actions", len(stt.steps) if stt else 0)
    top[2].metric("LLM calls", agent.llm.usage.calls if agent else 0)
    if h.running and top[3].button("Stop run", use_container_width=True):
        h.stop()

    render_pending(h)

    left, right = st.columns([2, 3])
    with left:
        if stt and stt.goal:
            st.subheader("Success criteria")
            ver = {v["criterion"]: v for v in stt.verification}
            for c in stt.goal.get("success_criteria", []):
                v = ver.get(c)
                st.markdown(f"{'✅' if v and v['passed'] else ('❌' if v else '◻️')} {c}")
        worklist = next((e["text"] for e in reversed(list(h.events)) if e["event"] == "worklist"), "")
        if worklist and not worklist.startswith("(no fixed"):
            st.subheader("Work items")
            st.caption("Procedure checklist per item — ticked only by evidence observed in the systems.")
            st.code(worklist, language=None)
        if stt and stt.plan:
            with st.expander("Plan", expanded=not worklist or worklist.startswith("(no fixed")):
                st.code(stt.plan_text(), language=None)
        if stt and stt.notes:
            with st.expander("Working memory (notes)", expanded=False):
                st.json(stt.notes)
        shots = [e for e in (stt.evidence if stt else []) if e.kind == "screenshot" and Path(e.path).exists()]
        if shots:
            st.subheader("Latest screenshot")
            st.image(shots[-1].path, caption=shots[-1].caption)
    with right:
        st.subheader("Activity")
        with st.container(height=640):
            render_timeline(list(h.events))

    if stt and stt.phase == "done":
        st.divider()
        st.subheader("Completion report")
        st.markdown(stt.report)
        ev_files = [e for e in stt.evidence if e.kind in ("file", "email") and Path(e.path).exists()]
        for e in ev_files:
            with st.expander(f"{e.kind}: {e.caption}"):
                st.code(Path(e.path).read_text(encoding="utf-8")[:5000], language=None)
        all_shots = [e for e in stt.evidence if e.kind == "screenshot" and Path(e.path).exists()]
        if all_shots:
            with st.expander(f"All screenshots ({len(all_shots)})"):
                for e in all_shots:
                    st.image(e.path, caption=f"step {e.step}: {e.caption}")
        d1, d2 = st.columns(2)
        d1.download_button("Download report.md", stt.report, f"{stt.run_id}-report.md")
        d2.download_button("Download full trace (state.json)", json.dumps(stt.to_dict(), indent=2, default=str),
                           f"{stt.run_id}-state.json")


# ---------------------------------------------------------------------- operator tab
with tab_run:
    st.title("FTE — autonomous company operator")
    st.caption("Give it a business request. It works out the procedure, operates the company systems, asks when it "
               "must, verifies the outcome independently, and hands back evidence.")
    h: RunHandle | None = st.session_state.get("handle")
    busy = bool(h and h.running)

    ex_cols = st.columns(len(EXAMPLES))
    for c, (label, text) in zip(ex_cols, EXAMPLES.items()):
        if c.button(label, use_container_width=True, disabled=busy):
            st.session_state.request = text
    request = st.text_area("Request", key="request", height=90, disabled=busy,
                           placeholder="e.g. Process this week's vendor invoices in the inbox.")
    if st.button("▶ Run", type="primary", disabled=busy or not (request or "").strip()):
        if provider not in ("ollama", "lmstudio") and not api_key:
            st.error("Enter your API key in the sidebar.")
        else:
            cfg = LLMConfig(provider=provider, model=model, base_url=base_url, api_key=api_key, thinking=thinking)
            st.session_state.handle = start_run(request, cfg, workspace_dir=session_workspace(),
                                                headless=headless, max_steps=max_steps)
            st.rerun()

    if h:
        @st.fragment(run_every=1.0 if h.running else None)
        def live() -> None:
            render_state(h)
            if not h.running and st.session_state.get("_was_running"):
                st.session_state._was_running = False
                st.rerun()
            st.session_state._was_running = h.running

        live()

# ---------------------------------------------------------------------- context tab
with tab_ctx:
    ctx = CompanyContext(ROOT / "company")
    st.markdown("The operator grounds every request in this company context. Edit the files under `company/` "
                "to teach it new procedures, systems or rules — no code changes.")
    for name, text in ctx.docs.items():
        with st.expander(f"📘 {name}"):
            st.markdown(text)
    with st.expander("🛡️ permissions.yaml (enforced in code)"):
        st.code((ROOT / "company" / "permissions.yaml").read_text(encoding="utf-8"), language="yaml")
    with st.expander("🔌 systems.yaml"):
        st.code(ctx.systems_text())
    ws = session_workspace()
    with st.expander("🗂️ Shared drive (this session)"):
        files = [p for p in sorted(ws.rglob("*")) if p.is_file()]
        for p in files:
            st.markdown(f"`{p.relative_to(ws).as_posix()}`")

# ---------------------------------------------------------------------- memory tab
with tab_mem:
    st.subheader("Long-term lessons")
    lessons = LessonMemory(MEMORY).all()
    if lessons:
        for l in reversed(lessons):
            st.markdown(f"- {l['lesson']}  \n  <small>from: {l['task'][:90]} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(l['ts']))}</small>",
                        unsafe_allow_html=True)
        if st.button("Clear lessons"):
            MEMORY.unlink(missing_ok=True)
            st.rerun()
    else:
        st.caption("No lessons yet — they are distilled from problems the operator hits and recovers from.")
    st.subheader("Past runs")
    runs = sorted((ROOT / "runs").glob("*/report.md"), reverse=True)[:15]
    for r in runs:
        with st.expander(r.parent.name):
            st.markdown(r.read_text(encoding="utf-8"))
