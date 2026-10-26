"""Procedure structure in code: per-item checklists ticked by observed evidence, business rules, and
completion checks.

The countable and rule-based parts of "done" — every item, every applicable step, values a rule dictates —
are tracked and verified here rather than judged by the model. The model does the unstructured work: reading
varied documents, navigating, filling forms, writing messages, recovering from errors.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import yaml

from .state import RunState
from .tools import Toolbox, ToolError, safe_eval


def _same(a: str, b: str) -> bool:
    """Equal as numbers when both are numeric (1,240.00 == 1240), else case/space-insensitive text."""
    na, nb = (re.sub(r"[,$\s]", "", str(x)) for x in (a, b))
    try:
        return abs(float(na) - float(nb)) < 0.005
    except ValueError:
        return na.lower() == nb.lower()


class _Unknown(dict):
    def __missing__(self, key: str) -> str:
        return "<known after earlier steps>"


def _fmt(template: str, fields: dict[str, Any]) -> str:
    return template.format_map(_Unknown(fields))


class CompletionChecks:
    def __init__(self, path: Path, docs: dict[str, str] | None = None):
        self.spec: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.docs = docs or {}

    def for_run(self, state: RunState) -> list[tuple[str, dict[str, Any]]]:
        return [(name, self.spec[name]) for name in state.procedures if name in self.spec]

    # ================================================================== fields
    @staticmethod
    def _apply_rules(spec: dict, out: dict[str, Any]) -> dict[str, Any]:
        nums: dict[str, float] = {}
        for k, v in list(out.items()):
            s = str(v)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
                try:
                    out[f"{k}_days"] = (date.fromisoformat(s) - date.today()).days
                except ValueError:
                    pass
        for k, v in out.items():
            try:
                nums[k] = float(str(v).replace(",", "").replace("$", ""))
            except ValueError:
                pass
        for rule in spec.get("rules") or []:
            try:
                ok = safe_eval(rule["when"], nums)
            except (ToolError, ZeroDivisionError):
                continue  # inputs not known yet
            out[rule["field"]] = _fmt(rule["then"] if ok else rule["else"], out)
        return out

    @staticmethod
    def _fields(spec: dict, item: str, tools: Toolbox, state: RunState | None = None) -> dict[str, Any]:
        """Fields of a file work item: regexes over the source document, then system lookups, then rules."""
        regexes = dict(spec.get("fields") or {})
        if spec.get("id_regex"):
            regexes.setdefault("id", spec["id_regex"])
        text = tools.ws.read(item)
        out: dict[str, Any] = {"item": item}
        for name, rx in regexes.items():
            m = re.search(rx, text)
            out[name] = (m.group(1) if m.groups() else m.group(0)) if m else ""
        if state is None:
            return out
        cache = state.lookups.setdefault(item, {})
        out.update(cache)
        browser = getattr(tools, "browser", None)
        visited = [u.lower().split("?")[0].rstrip("/") for u in getattr(browser, "visited", [])]
        for lk in spec.get("lookups") or []:
            if lk["field"] not in cache:
                try:
                    url = lk["url"].format(**out)
                except KeyError:
                    continue
                if url.lower().rstrip("/") in visited:  # only once the operator has actually opened it
                    rx = lk["regex"]
                    for k, v in out.items():  # earlier fields as literal {name} placeholders
                        rx = rx.replace("{" + k + "}", re.escape(str(v)))
                    if re.search(r"\{[a-z_]+\}", rx):
                        continue  # depends on a field not known yet
                    m = re.search(rx, browser.fetch_text(url))
                    if m:  # available to later lookups in this same pass
                        cache[lk["field"]] = out[lk["field"]] = m.group(1) if m.groups() else m.group(0)
        return CompletionChecks._apply_rules(spec, out)

    @staticmethod
    def _subject(spec: dict, state: RunState) -> dict[str, Any]:
        text = "\n".join([state.request] + list(state.clarifications))
        out: dict[str, Any] = {}
        for name, rx in (spec.get("subject") or {}).items():
            m = re.search(rx, text)
            if m:
                out[name] = m.group(1) if m.groups() else m.group(0)
        for name, template in (spec.get("derived") or {}).items():
            value = _fmt(template, out)
            if "<" not in value:
                out[name] = value.lower()
        return out

    def _table_rows(self, doc: str, section: str) -> list[dict[str, str]]:
        text = self.docs.get(doc, "")
        m = re.search(rf"^##+\s*{re.escape(section)}\s*\n(.*?)(?=^##|\Z)", text, re.S | re.M)
        if not m:
            return []
        lines = [l.strip() for l in m.group(1).splitlines() if l.strip().startswith("|")]
        rows = [[c.strip() for c in l.strip("|").split("|")] for l in lines]
        rows = [r for r in rows if not all(re.fullmatch(r":?-+:?", c) for c in r)]
        if len(rows) < 2:
            return []
        return [dict(zip(rows[0], r)) for r in rows[1:]]

    def _table_items(self, spec: dict, state: RunState) -> list[dict[str, Any]]:
        t = spec["items_from_table"]
        text = "\n".join([state.request] + list(state.clarifications))
        rows = self._table_rows(t["doc"], t["section"])
        chosen = fallback = None
        for row in rows:  # first row whose key appears in the request; "(anything else)" is the fallback
            key = row.get(t["match_column"], "")
            if "anything" in key.lower():
                fallback = row
            elif key and re.search(rf"\b{re.escape(key)}", text, re.I):
                chosen = row
                break
        chosen = chosen or fallback
        if not chosen:
            return []
        items = []
        for it in t["items"]:
            raw = chosen.get(it["column"], "")
            parts = [p.strip() for p in raw.split(it["split"])] if it.get("split") else [raw.strip()]
            items += [{"item": p, "category": it.get("category", "")} for p in parts if p]
        return items

    # ================================================================== work items (one interface for both kinds)
    def _spec_items(self, state: RunState, tools: Toolbox) -> Iterator[tuple[dict, str, dict[str, Any]]]:
        """(spec, key, fields) for every work item of the procedures in this run."""
        for _, spec in self.for_run(state):
            if spec.get("work_items"):
                for p in sorted(tools.ws.root.glob(spec["work_items"])):
                    if p.is_file():
                        key = tools.ws.rel(p)
                        yield spec, key, self._fields(spec, key, tools, state)
            elif spec.get("items_from_table"):
                subject = self._subject(spec, state)
                for it in self._table_items(spec, state):
                    f = self._apply_rules(spec, {**subject, **it})
                    yield spec, f"{it['category']}: {it['item']}", f

    def work_items(self, state: RunState, tools: Toolbox) -> list[str]:
        """File-backed work items (each must be read before finishing)."""
        items: list[str] = []
        for _, spec in self.for_run(state):
            if spec.get("work_items"):
                items += sorted(tools.ws.rel(p) for p in tools.ws.root.glob(spec["work_items"]) if p.is_file())
        return items

    def item_ids(self, state: RunState, tools: Toolbox) -> dict[str, str]:
        out = {}
        for _, spec in self.for_run(state):
            if spec.get("work_items"):
                for p in sorted(tools.ws.root.glob(spec["work_items"])):
                    if p.is_file():
                        key = tools.ws.rel(p)
                        out[key] = self._fields(spec, key, tools).get("id", "")
        return out

    # ================================================================== evidence predicates
    def _step_done(self, cond: dict, f: dict[str, Any], state: RunState, tools: Toolbox) -> bool:
        item = f.get("item", "")
        browser = getattr(tools, "browser", None)
        if cond.get("read"):
            return item in state.documents
        if "visited" in cond:
            path = _fmt(cond["visited"], f).lower().rstrip("/")
            return any(u.lower().split("?")[0].rstrip("/").endswith(path) for u in getattr(browser, "visited", []))
        if cond.get("confirmed"):
            return item in state.item_status
        if cond.get("emailed") or "email_mentions" in cond:
            wants = [f.get("id", "")] if cond.get("emailed") else [_fmt(v, f) for v in cond["email_mentions"]]
            if not all(wants) or any(w.startswith("<") for w in wants):
                return False
            outbox = tools.ws.root / "outbox"
            return outbox.exists() and any(
                all(w.lower() in p.read_text(encoding="utf-8", errors="replace").lower() for w in wants)
                for p in outbox.glob("*.eml"))
        if "displayed_row" in cond:  # a table row the system displayed has a cell equal to each value
            wants = [_fmt(v, f).strip().lower() for v in cond["displayed_row"]]
            if any(w.startswith("<") for w in wants):
                return False
            for page in getattr(browser, "displayed_log", []):
                for line in page.splitlines():
                    cells = [c.strip().lower() for c in line.split("\t")]
                    if len(cells) > 1 and all(w in cells for w in wants):
                        return True
            return False
        if "file_glob" in cond:
            return any(p.is_file() for p in tools.ws.root.glob(cond["file_glob"]))
        return False

    def _steps(self, specs: list[dict], f: dict[str, Any], state: RunState, tools: Toolbox) -> list[dict[str, Any]]:
        steps = []
        for s in specs or []:
            cond = s.get("applies_if") or {}
            if "confirmation_contains" in cond:
                conf = state.item_status.get(f.get("item", ""))
                if conf is None or cond["confirmation_contains"].lower() not in conf.lower():
                    continue  # not applicable (or not decidable until the item is confirmed)
            steps.append({"do": _fmt(s["do"], f), "done": self._step_done(s["done_when"], f, state, tools)})
        return steps

    def item_progress(self, state: RunState, tools: Toolbox) -> list[dict[str, Any]]:
        """Groups in procedure order: setup steps, one group per work item, close-out steps."""
        groups: list[dict[str, Any]] = []
        for _, spec in self.for_run(state):
            subject = self._apply_rules(spec, self._subject(spec, state)) if spec.get("subject") else {}
            if spec.get("steps_before_items"):
                steps = self._steps(spec["steps_before_items"], subject, state, tools)
                groups.append({"item": "setup", "id": "", "steps": steps, "complete": all(s["done"] for s in steps)})
            for s_spec, key, f in self._spec_items(state, tools):
                if s_spec is not spec:
                    continue
                steps = self._steps(spec.get("item_steps"), f, state, tools)
                groups.append({"item": key, "id": f.get("id", ""), "steps": steps, "complete": all(s["done"] for s in steps)})
            if spec.get("steps_after_items"):
                steps = self._steps(spec["steps_after_items"], subject, state, tools)
                groups.append({"item": "close-out", "id": "", "steps": steps, "complete": all(s["done"] for s in steps)})
        return groups

    def next_action(self, state: RunState, tools: Toolbox) -> str:
        for p in self.item_progress(state, tools):
            nxt = next((s for s in p["steps"] if not s["done"]), None)
            if nxt:
                return f"NEXT ACTION for {p['id'] or p['item']}: {nxt['do']}"
        return ""

    def worklist_text(self, state: RunState, tools: Toolbox) -> str:
        progress = self.item_progress(state, tools)
        if not progress:
            return "(no fixed work items for this procedure)"
        lines = []
        for p in progress:
            head = f"- {p['item']} ({p['id']})" if p["id"] else f"- {p['item']}"
            if p["complete"]:
                lines.append(f"{head}: DONE" + (f" — {state.item_status[p['item']]}" if p["item"] in state.item_status else ""))
                continue
            lines.append(f"{head}: in progress")
            lines += [f"    [{'x' if s['done'] else ' '}] {s['do']}" for s in p["steps"]]
        done = sum(p["complete"] for p in progress)
        return f"{done}/{len(progress)} complete — finish every one, in order:\n" + "\n".join(lines)

    def pending_items(self, state: RunState, tools: Toolbox) -> list[str]:
        return [f"{p['item']} ({p['id']})" if p["id"] else p["item"]
                for p in self.item_progress(state, tools) if not p["complete"]]

    # ================================================================== pre-submit guard
    def submit_guard(self, form: dict[str, Any], state: RunState, tools: Toolbox) -> str:
        """Problems with a form about to be submitted for a work item, or "" if it matches expectations."""
        val = lambda v: [str(x) for x in v] if isinstance(v, list) else [str(v)]  # noqa: E731 - select = [value, text]
        for spec, _, f in self._spec_items(state, tools):
            guard = spec.get("submit_guard")
            if not guard or not f.get("id"):
                continue
            if not any(_same(f["id"], v) for v in val(form.get(guard["item_field"], ""))):
                continue
            problems = []
            for form_field, field in guard["expect"].items():
                want = str(f.get(field) or "")
                if not want or want.startswith("<"):
                    continue  # expectation not established yet
                got = val(form.get(form_field, ""))
                if not any(_same(want, g) for g in got):
                    problems.append(f"{form_field} must be '{want}' (form has '{got[-1]}')")
            if problems:
                why = ", ".join(f"{k}={f[k]}" for k in ("total", "po_amount") if f.get(k))
                return f"{f['id']}: " + "; ".join(problems) + (f" — based on {why}" if why else "")
        return ""

    def email_guard(self, to: str, subject: str, body: str, state: RunState, tools: Toolbox) -> str:
        """An email about a work item must go to the address the procedure looked up for it."""
        recipients = [t.strip().lower() for t in re.split(r"[,;]", to or "") if t.strip()]
        for spec, _, f in self._spec_items(state, tools):
            guard = spec.get("email_guard")
            if not guard or not f.get("id") or f["id"] not in f"{subject}\n{body}":
                continue
            want = str(f.get(guard["expect_to"]) or "")
            if not want:
                return (f"{f['id']}: the recipient is not established yet — open the page that lists it "
                        f"(see WORK ITEMS) before emailing; never guess addresses")
            if want.lower() not in recipients:
                return f"{f['id']}: must be sent to '{want}' (looked up in the system), not {recipients}"
        return ""

    # ================================================================== verification
    def run(self, state: RunState, tools: Toolbox) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for _, spec in self.for_run(state):
            for chk in spec.get("checks", []):
                try:
                    results.append(self._check(chk, spec, state, tools))
                except Exception as e:  # noqa: BLE001 - a broken check is a failed check, with the reason
                    results.append(self._res(chk, f"{chk.get('type')}", False, f"check errored: {e}"))
        return results

    @staticmethod
    def _res(chk: dict, default_desc: str, passed: bool, evidence: str) -> dict[str, Any]:
        return {"criterion": "[auto] " + chk.get("description", default_desc), "passed": passed, "evidence": evidence}

    def _check(self, chk: dict, spec: dict, state: RunState, tools: Toolbox) -> dict:
        kind = chk["type"]
        ids = {key: str(f.get("id", "")) for s, key, f in self._spec_items(state, tools) if s is spec}
        if kind == "all_items_read":
            missing = [i for i in ids if i not in state.documents]
            return self._res(chk, f"All {len(ids)} work items were read", not missing,
                             f"not read: {missing}" if missing else f"read all: {sorted(ids)}")
        if kind == "item_steps_done":
            groups = [g for g in self.item_progress(state, tools)]
            missing = [f"{g['item']}: " + "; ".join(s["do"][:70] for s in g["steps"] if not s["done"])
                       for g in groups if not g["complete"]]
            return self._res(chk, "Every work item went through every applicable step", not missing and bool(groups),
                             f"incomplete: {missing}" if missing else f"all complete: {[g['id'] or g['item'] for g in groups]}")
        if kind == "item_ids_on_page":
            text = self._page_text(chk["url"], tools)
            missing = [f"{i} ({iid or 'no id found'})" for i, iid in ids.items() if not iid or iid not in text]
            return self._res(chk, f"Every item id appears on {chk['url']}", not missing,
                             f"missing on page: {missing}" if missing else f"found {sorted(ids.values())} on {chk['url']}")
        if kind == "item_row_contains":  # the system's row for each item must carry the expected values
            lines = self._page_text(chk["url"], tools).splitlines()
            norm = lambda v: re.sub(r"[,$\s]", "", str(v)).lower()  # noqa: E731
            problems = []
            for s, key, f in self._spec_items(state, tools):
                if s is not spec:
                    continue
                row = next((l for l in lines if f.get("id") and f["id"] in l), None)
                if row is None:
                    problems.append(f"{f.get('id') or key}: no row")
                    continue
                for field in chk["fields"]:
                    want = str(f.get(field, ""))
                    if not want or want.startswith("<") or norm(want) not in norm(row):
                        problems.append(f"{f['id']}: expected {field}={want or '?'} but row is: {row.strip()}")
            return self._res(chk, "System rows match the source documents", not problems,
                             "; ".join(problems) if problems else f"all rows carry the expected {chk['fields']}")
        if kind == "file_exists":
            p = tools.ws.resolve(chk["path"])
            if not p.is_file():
                return self._res(chk, f"{chk['path']} exists", False, f"{chk['path']} does not exist")
            if chk.get("must_contain_item_ids"):
                body = p.read_text(encoding="utf-8", errors="replace")
                missing = [iid for iid in ids.values() if iid and iid not in body]
                return self._res(chk, f"{chk['path']} exists and covers every item", not missing,
                                 f"{chk['path']} lacks {missing}" if missing else f"{chk['path']} mentions {sorted(ids.values())}")
            return self._res(chk, f"{chk['path']} exists", True, f"{chk['path']} exists")
        if kind == "files_match":
            found = [tools.ws.rel(p) for p in tools.ws.root.glob(chk["glob"]) if p.is_file()]
            return self._res(chk, f"At least {chk.get('min', 1)} file(s) match {chk['glob']}",
                             len(found) >= chk.get("min", 1), f"found: {found or 'none'}")
        raise ValueError(f"unknown check type {kind}")

    @staticmethod
    def _page_text(url: str, tools: Toolbox) -> str:
        res = tools.t_browser_open(url)
        if "on sign-in page" in res.summary:
            tools.t_browser_login(url)
            tools.t_browser_open(url)
        return tools.browser._page.inner_text("body")  # full text: snapshots are truncated for the model
