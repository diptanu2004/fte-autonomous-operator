"""Tool registry: what the agent can do, how each tool is described to the model, and dispatch."""
from __future__ import annotations

import ast
import inspect
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..knowledge import CompanyContext
from .browser import Browser, BrowserError
from .files import FileError, Workspace

# name -> (args signature, description). Order is the order shown to the model.
TOOL_SPECS: dict[str, tuple[str, str]] = {
    "search_knowledge": ('{"query": str}', "Search company handbook/SOPs/policies. Returns matching excerpts."),
    "read_document": ('{"name": str}', "Read a full handbook document by name."),
    "list_files": ('{"path": str}', 'List files in the shared workspace (path "" for all, or e.g. "inbox").'),
    "read_file": ('{"path": str}', "Read a workspace file (txt, md, csv, json, xlsx, pdf)."),
    "write_file": ('{"path": str, "content": str}', "Create/overwrite a workspace file (reports/, outbox/, scratch/)."),
    "send_email": ('{"to": str, "subject": str, "body": str}', "Send an email from the company ops mailbox."),
    "browser_login": ('{"system": str}', "Sign in to a company web system using stored credentials."),
    "browser_open": ('{"url": str}', "Navigate to a URL in a permitted company system."),
    "browser_read": ("{}", "Re-read the current page."),
    "browser_fill": ('{"fields": {"<id or label>": "<value>", ...}, "submit_id": int (optional)}', "Fill inputs / choose select options (by visible text) on the current page. Fill EVERY field the form needs in one call. With submit_id it then clicks that submit button (preferred for forms)."),
    "browser_click": ('{"id": int}', "Click a link or button by its [id] from the latest page observation."),
    "browser_screenshot": ('{"caption": str}', "Save a screenshot of the current page as evidence."),
    "calculate": ('{"expression": str}', "Exact arithmetic, e.g. \"abs(3560-3200)/3200*100\". Use it for every amount comparison or total."),
    "update_plan": ('{"steps": [str]}', "Replace ALL unfinished plan steps with this complete list of remaining steps (done steps are kept)."),
    "ask_human": ('{"question": str}', "Ask the requester for information you cannot find in company systems or documents."),
    "request_approval": ('{"action": str, "reason": str}', "Ask a human to sign off before a sensitive step the procedure says needs approval."),
    "finish": ('{"summary": str}', "Declare the task complete; triggers independent verification."),
    "report_verification": ('{"results": [{"criterion": str, "passed": bool, "evidence": str}]}', "VERIFY PHASE ONLY: report the check result for every success criterion."),
}

EXECUTE_TOOLS = [t for t in TOOL_SPECS if t != "report_verification"]
VERIFY_TOOLS = ["list_files", "read_file", "calculate", "browser_login", "browser_open", "browser_read", "browser_fill",
                "browser_click", "browser_screenshot", "report_verification"]


def tools_prompt(names: list[str]) -> str:
    return "\n".join(f"- {n} {TOOL_SPECS[n][0]}: {TOOL_SPECS[n][1]}" for n in names)


@dataclass
class ToolResult:
    ok: bool
    observation: str
    summary: str
    evidence: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, path, caption)
    control: str = ""  # finish | verified | plan
    payload: Any = None


class ToolError(RuntimeError):
    pass


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
        ast.Gt: operator.gt, ast.Lt: operator.lt, ast.GtE: operator.ge, ast.LtE: operator.le}
_FUNCS = {"abs": abs, "round": round, "min": min, "max": max}


def safe_eval(expr: str, names: dict[str, float] | None = None) -> Any:
    """Arithmetic only — no attributes or calls beyond a few math helpers; optional numeric variables."""
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr.replace("$", ""))  # tolerate 1,240.00 and $ signs
    names = names or {}

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id in names:
            return names[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _OPS:
            return _OPS[type(n.ops[0])](ev(n.left), ev(n.comparators[0]))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ToolError(f"Unsupported expression element: {ast.dump(n)[:60]}")

    try:
        return ev(ast.parse(expr, mode="eval"))
    except SyntaxError as e:
        raise ToolError(f"Not a valid arithmetic expression: {expr}") from e
    except ZeroDivisionError as e:
        raise ToolError("Division by zero.") from e


class Toolbox:
    def __init__(self, ctx: CompanyContext, workspace: Workspace, browser: Browser):
        self.ctx, self.ws, self.browser = ctx, workspace, browser
        self.sender = f"ops@{ctx.permissions.get('email', {}).get('internal_domain', 'example.com')}"

    def _secret_sub(self, value: str) -> str:
        def sub(m: re.Match) -> str:
            v = self.ctx.secret(m.group(1))
            if v is None:
                raise ToolError(f"Unknown secret '{m.group(1)}'.")
            return v

        return re.sub(r"\{\{secret:([a-zA-Z0-9_]+)\}\}", sub, value)

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        fn = getattr(self, f"t_{tool}", None)
        if fn is None:
            return ToolResult(False, f"Unknown tool '{tool}'.", "unknown tool")
        params = inspect.signature(fn).parameters
        dropped = [k for k in (args or {}) if k not in params]
        args = {k: v for k, v in (args or {}).items() if k in params}
        try:
            res = fn(**args)
            if dropped:  # tolerate extra keys from small models, but tell them
                res.observation = f"(Ignored unsupported argument(s) {dropped} for {tool}.)\n" + res.observation
            return res
        except TypeError as e:
            return ToolResult(False, f"Bad arguments for {tool}: {e}. Expected {TOOL_SPECS.get(tool, ('?',))[0]}", "bad arguments")
        except (BrowserError, FileError, ToolError) as e:
            msg = str(e)
            return ToolResult(False, msg, msg.splitlines()[0][:150])
        except Exception as e:  # noqa: BLE001 - never let a tool crash the loop; the agent adapts
            return ToolResult(False, f"{type(e).__name__}: {e}", f"{type(e).__name__}: {str(e)[:120]}")

    # ------------------------------------------------------------------ knowledge
    def t_search_knowledge(self, query: str) -> ToolResult:
        hits = self.ctx.search(query, k=4)
        if not hits:
            return ToolResult(True, "No matching handbook content.", "no matches")
        obs = "\n\n".join(f"### [{c.doc}] {c.heading}\n{c.text}" for c in hits)
        return ToolResult(True, obs, f"{len(hits)} excerpts from {sorted({c.doc for c in hits})}")

    def t_read_document(self, name: str) -> ToolResult:
        key = Path(name).stem
        if key not in self.ctx.docs:
            return ToolResult(False, f"No document '{name}'. Available:\n{self.ctx.doc_titles()}", "not found")
        return ToolResult(True, self.ctx.docs[key], f"read {key}")

    def t_calculate(self, expression: str) -> ToolResult:
        value = safe_eval(str(expression))
        out = f"{expression} = {round(value, 6) if isinstance(value, float) else value}"
        return ToolResult(True, out, out[:150])

    # ------------------------------------------------------------------ files / email
    def t_list_files(self, path: str = "") -> ToolResult:
        out = self.ws.list(path)
        return ToolResult(True, out, f"{out.count(chr(10)) + 1} entries" if not out.startswith("(") else out)

    def t_read_file(self, path: str) -> ToolResult:
        return ToolResult(True, f"--- {path} ---\n{self.ws.read(path)}", f"read {path}")

    def t_write_file(self, path: str, content: str) -> ToolResult:
        p = self.ws.write(path, content)
        rel = self.ws.rel(p)
        return ToolResult(True, f"Wrote {len(content)} chars to {rel}.", f"wrote {rel}", [("file", str(p), rel)])

    def t_send_email(self, to: str, subject: str, body: str) -> ToolResult:
        outbox = self.ws.root / "outbox"
        for prev in sorted(outbox.glob("*.eml")) if outbox.exists() else []:  # email is irreversible: never resend
            head = prev.read_text(encoding="utf-8", errors="replace").split("\n\n", 1)[0]
            if f"To: {to}\n" in head + "\n" and f"Subject: {subject}" in head:
                return ToolResult(True, f"Not sent again: an identical email (same recipient and subject) was already sent "
                                        f"({self.ws.rel(prev)}). Move on to the next step.", f"already sent to {to}")
        p = self.ws.send_email(to, subject, body, self.sender)
        return ToolResult(True, f"Email sent to {to} (copy: {self.ws.rel(p)}).", f"emailed {to}", [("email", str(p), subject)])

    # ------------------------------------------------------------------ browser
    def _login_system(self, system: str) -> dict[str, Any]:
        """Accept a system name, or any URL on that system; default to the only login-capable system."""
        candidates = [s for s in self.ctx.systems.get("systems", []) if s.get("login")]
        sysdef = self.ctx.system(system)
        if sysdef and sysdef.get("login"):
            return sysdef
        m = re.match(r"^(?:https?://)?([^/]+)", str(system).strip())
        host = m.group(1).lower() if m else ""
        for s in candidates:
            if host and host in s.get("url", "").lower():
                return s
        if len(candidates) == 1 and not host.count("."):  # e.g. "Acme Portal", "portal"
            return candidates[0]
        names = ", ".join(s["name"] for s in candidates)
        raise ToolError(f"No login recipe for '{system}'. Use one of these system names: {names}. "
                        "Credentials are stored — never guess or ask for passwords.")

    def t_browser_login(self, system: str = "") -> ToolResult:
        sysdef = self._login_system(system)
        system = sysdef["name"]
        login = sysdef["login"]
        user, pwd = self.ctx.secret(login["username_secret"]), self.ctx.secret(login["password_secret"])
        b = self.browser
        b.open(login["url"])
        page = b._page
        page.fill("input[name=username], input[type=email]", user or "")
        page.fill("input[type=password]", pwd or "")
        page.click("button[type=submit], input[type=submit]")
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        snap = b.snapshot()
        if "/login" in page.url:
            return ToolResult(False, "Login failed.\n" + snap, "login failed")
        return ToolResult(True, f"Signed in to {system}.\n" + snap, f"signed in to {system}")

    def t_browser_open(self, url: str) -> ToolResult:
        snap = self.browser.open(url)
        return self._page_result(snap, f"opened {url}")

    def t_browser_read(self) -> ToolResult:
        return self._page_result(self.browser.snapshot(), "read page")

    def t_browser_fill(self, fields: dict[str, Any]) -> ToolResult:
        snap = self.browser.fill(fields, self._secret_sub)
        return ToolResult(True, snap, snap.splitlines()[0])

    def t_browser_click(self, id: Any) -> ToolResult:  # noqa: A002 - matches the protocol
        info = self.browser.describe(id) or {}
        snap = self.browser.click(id)
        res = self._page_result(snap, f"clicked \"{info.get('label', id)}\"")
        if info.get("form_method") == "post":  # a write happened (or was attempted): capture evidence
            shot = self.browser.screenshot(f"after {info.get('label', 'submit')}")
            res.evidence.append(("screenshot", str(shot), f"After clicking \"{info.get('label', '')}\""))
        return res

    def t_browser_screenshot(self, caption: str = "") -> ToolResult:
        p = self.browser.screenshot(caption or "evidence")
        return ToolResult(True, f"Screenshot saved: {p.name}", f"screenshot {p.name}", [("screenshot", str(p), caption)])

    def _page_result(self, snap: str, summary: str) -> ToolResult:
        status = self.browser.last_status or 200
        alerts = next((l for l in snap.splitlines() if l.startswith("ALERTS:")), "")
        ok = status < 400
        extra = f" — HTTP {status}" if not ok else ""
        if alerts:
            extra += " — " + alerts[:140]
        if "/login" in snap.splitlines()[0] and "Sign in" in snap:
            extra += " — on sign-in page"
        return ToolResult(ok, snap, summary + extra)
