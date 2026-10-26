# FTE — an autonomous company operator (prototype)

FTE takes a short business request ("process this week's vendor invoices") and turns it into **completed,
verified work** inside company systems. It works out the procedure from company context, operates a real browser
and the shared drive, pauses for humans only where policy or missing information requires it, independently
verifies the result, and returns an evidence-backed report. It also learns from the problems it recovers from.

This is a deliberately **narrow but real** system. There's no simulated autonomy: the agent drives Chromium against
a live web app, its writes land in a database, and a separate verification pass checks them.

## The loop

```
Request ─► UNDERSTAND ─► CLARIFY? ─► PLAN ─► EXECUTE ⇄ OBSERVE ⇄ ADAPT ─► VERIFY ─► REPORT & LEARN
             │              │          │          │                         │   │
   SOP retrieval +   asks only for   ordered    one action/turn,         read-only   lessons → long-term
   success criteria  un-lookup-able  steps per  policy gate before       re-check    memory, report.md,
   from "Done means" facts           item       every action              of systems  screenshots, trace
                                                                  fail ◄──────┘ (back to EXECUTE with gaps)
```

| Capability asked for | How FTE does it |
|---|---|
| Understand intended outcome | Intake pass grounds the request in retrieved SOPs (BM25 over `company/handbook`) and produces `intended_outcome` plus **checkable success criteria** taken from the SOP's "Done means". |
| Determine what to do and plan | The planner reads the full governing procedure and writes per-item steps. `update_plan` lets the executor re-plan when reality differs. |
| Select tools and execute | One JSON action per turn from the toolset: browser (DOM snapshot with numbered handles), workspace files (txt/md/csv/xlsx/pdf), email outbox, knowledge search, human. |
| Observe, adapt, recover | Every action returns an observation (page text, red validation alerts, HTTP status). The portal *really* throws a transient 503, expires sessions and rejects bad formats. Guards catch repeated identical actions, invalid JSON (repair loop) and consecutive failures (escalate to a human). |
| State and memory | **Working memory**: plan statuses plus a `notes` scratchpad the model writes each turn. Old observations are dropped, so context stays bounded for a 30B model. **Episodic**: the full trace in `runs/<id>/state.json`. **Long-term**: lessons distilled after each run (`memory/lessons.jsonl`) and recalled by relevance on future runs. |
| Human input and approval | `ask_human` for missing facts (e.g. a personal email the SOP says never to guess). **Policy approvals are enforced in code** (`company/permissions.yaml`): invoices over $5k set to *Approved for payment*, and any external email, pause for a human with the live form values and a screenshot. Deletions and off-domain navigation are blocked outright. A denied action is never executed. |
| Verify | Two layers. **(1) Executable completion checks** (`company/completion_checks.yaml`) run in code: every work item was read, every invoice ID is on the portal, the report covers every ID. They also **gate `finish`**, so the agent can't declare done while one fails. **(2) An independent LLM verifier** with **read-only** tools (form submission is blocked by policy) gets the SOP and the source documents, re-derives the *expected* outcome per item, and must quote what it saw. Failures send the agent back to execution with the specific gaps (up to 3 rounds). |
| Evidence | A screenshot after every form submission, the files and emails it produced, a per-criterion verification table, `report.md`, and the downloadable full trace. |

## Demo world

- **Acme Portal** (`demo_portal/`, Flask + SQLite at `http://127.0.0.1:5055`) covers purchase orders, invoices,
  vendors, employees and IT tickets. It has strict validation (dates `YYYY-MM-DD`, amounts without `$`/commas),
  pagination, a one-time 503 on invoice submission, and 8-minute sessions.
- **Shared drive** (`workspace/`): `inbox/` holds three vendor invoices in three formats (txt, markdown table, csv
  line items).
- **Company context** (`company/`): SOPs, a systems registry (secrets referenced by name; the model never sees
  values) and the permission policy. Edit these to teach new work. No code changes needed.

Example requests:

1. *"Process this week's vendor invoices in the inbox."* The agent reads 3 invoices, matches each against its PO
   in the portal, and books them. INV-2042 ($7,850) **requires approval**. INV-3307 has a hidden rush surcharge
   (+11%), so it goes **on hold** and the agent drafts an **external email** to the vendor (approval). It writes
   `reports/ap_summary.md`, then verifies.
2. *"Please onboard Priya Sharma, our new Data Analyst in Dana Lee's team. She starts on 2026-11-02."* The
   personal email is missing and the SOP forbids guessing, so the agent **asks**. Location defaults to the
   manager's (looked up in the portal). It creates the employee, 3 IT tickets from the equipment matrix, emails
   the manager and saves a checklist.

## Run it

```bash
python -m venv .venv && .venv\Scripts\activate         # (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
python -m playwright install chromium
python scripts/reset_demo.py                            # fresh portal DB + workspace
```

**Local model (Qwen3-30B-A3B, 4-bit) via Ollama**

```bash
ollama pull qwen3:30b-a3b                                # Q4_K_M, ~18 GB
set OLLAMA_CONTEXT_LENGTH=16384                          # restart ollama after setting; default context is too small
python -m fte.cli "Process this week's vendor invoices in the inbox" --headed
```

LM Studio works too: `--provider lmstudio --model qwen/qwen3-30b-a3b` (set the context length to 16k or more in the UI).

**Groq**

```bash
set GROQ_API_KEY=gsk_...
python -m fte.cli --provider groq "Process this week's vendor invoices in the inbox"   # model qwen/qwen3-32b
```

**Streamlit console (BYOK)**

```bash
streamlit run app.py
```

Pick a provider in the sidebar and paste a key. The key stays in that browser session's memory only. You get a
live phase tracker, the plan, criteria ticking off, the activity timeline with every observation, approval and
question cards, the latest screenshot, and the final report with downloads. The app starts the demo portal
itself, so it deploys as a single process. On Streamlit Community Cloud, `packages.txt` provides Chromium's
system libraries and the app installs Chromium on first boot.

**Evals against ground truth** (needs a model):
`python scripts/eval_invoices.py --runs 3 --base-url <url>/v1 --model qwen3-30b-a3b` and `scripts/eval_onboarding.py`
(key in `FTE_API_KEY`). Don't run `pytest` during an eval: both reset the shared portal DB.

**Tests** (no model needed): `pytest -q`. `tests/test_e2e_scripted.py` drives the real portal and browser with
a deterministic stand-in LLM, covering login, the 503 recovery, the approval gate, a denied approval that must not
execute, verification, the report and lesson memory.

## Design choices

- **JSON action protocol instead of native tool-calling.** Quantized local models break tool-call formatting far
  more often than JSON, and JSON can be repaired. Think tags are stripped, fences and trailing commas are
  tolerated, and invalid replies are re-prompted with the error. The same code runs on Ollama, LM Studio, Groq or
  any OpenAI-compatible endpoint. Unsupported request options (`response_format`, Groq `reasoning_effort`) are
  detected and dropped automatically.
- **DOM-as-text computer use.** Qwen3-30B is text-only, so the browser is rendered as page text plus numbered
  interactive elements with labels, values and select options. That's cheaper and more reliable than pixel
  grounding for web apps. Screenshots are taken for humans as evidence, not for the model.
- **Stateless turns over explicit state.** Each turn rebuilds the prompt from goal, SOP, plan, notes, the last 12
  action summaries and only the latest full observation. Context stays roughly constant over 70+ steps, which
  matters at 16k–32k context.
- **Policy in code, not in the prompt.** The model is told the rules but can't bypass them. Gates read *live form
  values* at click time, so "save as Approved for payment with amount > 5000" is caught however the model got there.
- **Verification is a separate role** with read-only permissions, so "done" means observed in the system of
  record, not claimed.

## What live testing on Qwen3-30B-A3B (4-bit) taught, and what changed

Early versions looked fine on paper but failed in instructive ways on the real model. Each failure became a
structural fix, not a longer prompt:

| Observed failure | Fix |
|---|---|
| Booked a mismatched invoice as *Approved*: it never opened the PO and invented the PO amount | **Procedural precondition gate** (`requires_visited` in `permissions.yaml`): *Save invoice* is refused until the PO named in the form has actually been opened this run. |
| Verifier "passed" a wrong status (it checked that the row existed, not that it was correct) | The verifier gets the SOP and the source documents and must re-derive the expected outcome per item. |
| Skipped one of three invoices; the verifier still said 3/3 | **Countable completeness in code** (`completion_checks.yaml`), plus a live worklist in the prompt and a `finish` refusal while checks fail. |
| Intake dropped a success criterion | The SOP's "Done means" bullets are backfilled into the criteria. |
| Mixed up facts between invoices; mental arithmetic errors | Source documents stay pinned in context; notes are keyed per item; a safe `calculate` tool. |
| Submitted an empty form after a 503; "blocked" was misread as "saved" | Atomic fill-and-submit (the policy gate still runs between the fill and the click); blocked results say plainly that nothing was saved. |
| Passed a URL as the login system; keyed fields by label; cryptic tool errors led to 50-step loops | Forgiving tool interfaces (system name or URL; id or label or field name), and every error names the valid options. |
| Re-read loops (A,B,A,B…); verifier never concluded | Cycle detection that points to the next unfinished step; dedup of re-reads; a verifier evidence pack plus a forced final judgment. |
| Vague "lessons" learned from failed runs | Lessons are kept only from verified-successful runs and must cite a concrete event. |
| Lost track of which items were done; re-read files for 70 steps | The SOP compiles into a **per-item checklist ticked only by observed evidence** (file read, page visited, a "saved" confirmation naming the item, an outbox email naming it). The single next action goes at the very end of the prompt. |
| Took one CSV line as the invoice total (missed a $360 surcharge) | `read_file` appends computed column totals for amount-like CSV columns. |
| Opened the PO, lost it from context, assumed it matched | **Deterministic business rules in config** (`lookups` + `rules`): the PO amount is read from the page the agent opened and the 2% rule is computed in code. The result is given to the agent as a fact and checked afterwards. |
| An irreversible submit with a wrong status (the portal has no undo) | **Pre-submit guard:** a form about a work item must match the known expectations, or it is blocked with the reason. |
| Verifier fabricated evidence ("…and Snowflake read access") | **Evidence grounding:** every quoted value in a "pass" must appear in text the *system displayed* (editable fields excluded, so the agent can't cite its own typing). A pass with no quotes is rejected. |
| Learned "ensure automated approval is enabled" | Lesson memory rejects anything about approvals, permissions or policy (governance only comes from company config) and drops near-duplicates. |
| Re-sent the manager email each fix round | `send_email` is idempotent (same recipient and subject is never sent twice). |
| Emailed an invented vendor address (`billing@fabrikam…`, copied from another vendor) | The recipient is a **looked-up fact** (PO page → vendor ID → Vendors page → billing email), and an **email guard** blocks mail about an item to any other address. |
| Intake invented a wrong work-email format, which the executor then copied | SOP formatting rules are `derived` templates (`{first}.{last}@acme-corp.com`), and intake may not invent concrete values. |
| Onboarding merged two software tickets into one | Work items come from the SOP's equipment-matrix table, and a ticket counts only if a displayed row has a cell **exactly** equal to the item. |
| Correct work reported as failed because the verifier didn't quote | A three-way outcome: **completed** / **needs_review** (code checks pass; some model claims couldn't be substantiated, none contradicted) / **failed**. |
| Same approval asked again after a 503 retry | Approvals are remembered for the *identical* action within a run; any change in values asks again. |

### Final scorecard (Qwen3-30B-A3B, 4-bit, via OpenAI-compatible endpoint)

Ground truth comes from the portal DB and the outbox, not from the agent. Approvals were auto-approved by the eval harness.

| Task | Runs | Actually correct | Agent said *completed* | Agent said *needs review* | **False successes** | Time / run |
|---|---|---|---|---|---|---|
| AP invoices (3 invoices, 503, $5k approval, mismatch → hold + vendor email) | 3 | **3/3** | 2 | 1 | **0** | ~2.5 min |
| Onboarding (missing personal email → ask; SOP equipment matrix → 3 tickets) | 3 | **3/3** | 1 | 2 | **0** | ~3 min |

Early versions of the same agent on the same model scored 0/3 correct, with up to 3/3 false successes. The difference
comes entirely from the structural changes in the table above.

The Streamlit console was also driven end to end by a headless browser against the real model: BYOK setup,
live timeline, approval cards clicked as they appeared, and the final report with verification and evidence.

`scripts/eval_invoices.py` measures this against **ground truth** (the real portal DB and outbox), not the
agent's own verdict. It reports correct runs, and **false successes**: runs where the agent said "completed" but
the real state was wrong. For an autonomous operator, that's the number that matters most.

## Layout

```
fte/agent.py        loop: understand → clarify → plan → execute/observe/adapt → verify → report/learn
fte/llm.py          OpenAI-compatible client, provider presets, JSON repair, retries / rate-limit backoff
fte/prompts.py      all prompts
fte/policy.py       permission gate (allow / approve / block, procedural preconditions)
fte/checks.py       procedure-as-code: per-item checklists ticked by evidence, lookups, business rules,
                    pre-submit / email guards, completion checks
fte/knowledge.py    SOP retrieval (BM25), systems + secrets, long-term lesson memory
fte/state.py        run state / working memory / trace persistence
fte/tools/          browser (Playwright), workspace files + email outbox, registry
fte/human.py        human channels: console, queue (UI), scripted (tests)
fte/runner.py       background-thread runner for the UI
fte/cli.py          command line
app.py              Streamlit BYOK console
demo_portal/        the mock company web system
company/            handbook SOPs, systems.yaml, permissions.yaml, completion_checks.yaml
scripts/            reset_demo.py, eval_invoices.py, eval_onboarding.py (ground-truth evals)
```

## Limits and next steps

- Desktop apps: the tool interface is ready for a UI Automation (pywinauto) backend using the same
  numbered-element approach. Not built in this prototype.
- The shared portal DB is global in a multi-user deployment (each Streamlit session gets its own workspace copy).
- Groq's free tier has a low tokens-per-minute cap and prompts are about 5–8k tokens, so expect 429 back-offs
  (handled) or use a paid tier.
- Long runs could checkpoint and resume from `state.json`. The state is already persisted every step.
