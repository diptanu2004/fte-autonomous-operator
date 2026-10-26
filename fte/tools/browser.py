"""Browser operation via Playwright, rendered as text so a non-vision LLM can act on it.

Each observation lists the page text plus numbered interactive elements. The agent acts on those numbers;
they are re-assigned on every snapshot, which keeps the handles short and unambiguous.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Callable

_SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-fte-id]').forEach(e => e.removeAttribute('data-fte-id'));
  const els = Array.from(document.querySelectorAll('a[href], button, input, select, textarea, [role=button]'));
  const out = []; let n = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (el.type === 'hidden' || st.display === 'none' || st.visibility === 'hidden' || (r.width === 0 && r.height === 0)) continue;
    n++; el.setAttribute('data-fte-id', String(n));
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = l.innerText.trim(); }
    if (!label) label = el.getAttribute('aria-label') || el.placeholder || (el.innerText || '').trim() || el.value || el.name || '';
    const item = {id: n, tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '', label: label.slice(0, 80)};
    if (el.tagName === 'SELECT') {
      item.options = Array.from(el.options).map(o => o.text).slice(0, 40);
      item.value = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : '';
    } else if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.type !== 'password') {
      item.value = el.value;
    } else if (el.type === 'password') { item.value = el.value ? '••••' : ''; }
    if (el.tagName === 'A') item.href = el.getAttribute('href');
    const f = el.closest('form'); if (f) item.form = (f.getAttribute('method') || 'get').toLowerCase();
    out.push(item);
  }
  const main = document.querySelector('main') || document.body;
  const text = main ? main.innerText : '';
  // What the system itself displays, with every editable field hidden: evidence that cannot be agent-typed.
  const hide = document.createElement('style');
  hide.textContent = 'input,textarea,select{display:none !important}';
  document.head.appendChild(hide);
  const displayed = main ? main.innerText : '';
  hide.remove();
  return {
    url: location.href, title: document.title,
    alerts: Array.from(document.querySelectorAll('[role=alert]')).map(a => a.innerText.trim()),
    text: text, displayed: displayed, elements: out,
  };
}
"""

_DESCRIBE_JS = r"""
(id) => {
  const el = document.querySelector(`[data-fte-id="${id}"]`);
  if (!el) return null;
  const f = el.closest('form'); const values = {};
  if (f) for (const e of f.elements) {
    if (!e.name || e.type === 'password') continue;
    values[e.name] = e.tagName === 'SELECT' ? [e.value, e.selectedIndex >= 0 ? e.options[e.selectedIndex].text : ''] : e.value;
  }
  return {label: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(), tag: el.tagName.toLowerCase(),
          form_method: f ? (f.getAttribute('method') || 'get').toLowerCase() : null, form_values: values};
}
"""


class BrowserError(RuntimeError):
    pass


class Browser:
    def __init__(self, shots_dir: Path, headless: bool = True, origin_allowed: Callable[[str], bool] | None = None,
                 max_text: int = 3500):
        self.shots_dir = Path(shots_dir)
        self.headless = headless
        self.origin_allowed = origin_allowed or (lambda _u: True)
        self.max_text = max_text
        self._pw = self._browser = self._page = None
        self.last_status: int | None = None
        self.visited: list[str] = []  # every successfully loaded page URL (for procedural preconditions)
        self.displayed_log: list[str] = []  # system-displayed page text (no editable fields), for evidence grounding
        self._shot_n = 0

    # ------------------------------------------------------------------ lifecycle
    def _ensure(self):
        if self._page is not None:
            return self._page
        if sys.platform == "win32":  # Playwright needs a subprocess-capable loop, also off the main thread.
            asyncio.set_event_loop(asyncio.ProactorEventLoop())
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.route("**/*", self._guard)
        self._page = ctx.new_page()
        self._page.set_default_timeout(10_000)
        self._page.on("response", self._on_response)
        return self._page

    def _guard(self, route):
        req = route.request
        if req.resource_type == "document" and not self.origin_allowed(req.url):
            return route.abort("blockedbyclient")
        return route.continue_()

    def _on_response(self, resp):
        try:
            if resp.request.resource_type == "document" and resp.frame == self._page.main_frame:
                self.last_status = resp.status
                if resp.status < 400:
                    self.visited.append(resp.url)
        except Exception:  # noqa: BLE001 - best effort bookkeeping
            pass

    def close(self):
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._page = None

    # ------------------------------------------------------------------ observation
    def snapshot(self) -> str:
        page = self._ensure()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        data = page.evaluate(_SNAPSHOT_JS)
        self._elements = data["elements"]
        if self.last_status is None or self.last_status < 400:
            self.displayed_log.append(f"{data['url']}\n{data['displayed']}")
        text = re.sub(r"\n{3,}", "\n\n", data["text"] or "").strip()
        if len(text) > self.max_text:
            text = text[: self.max_text] + f"\n...[page text truncated, {len(text) - self.max_text} more chars]"
        lines = [f"URL: {data['url']}  |  TITLE: {data['title']}  |  HTTP: {self.last_status}"]
        if data["alerts"]:
            lines.append("ALERTS: " + " | ".join(data["alerts"]))
        lines += ["PAGE TEXT:", text, "INTERACTIVE ELEMENTS:"]
        for e in data["elements"]:
            lines.append(self._fmt(e))
        return "\n".join(lines)

    @staticmethod
    def _fmt(e: dict[str, Any]) -> str:
        tag, label = e["tag"], e["label"].replace("\n", " ")
        if tag == "a":
            return f"[{e['id']}] link \"{label}\" -> {e.get('href', '')}"
        if tag == "select":
            opts = ", ".join(e.get("options", []))
            return f"[{e['id']}] select \"{label}\" selected=\"{e.get('value', '')}\" options=[{opts}]"
        if tag in ("input", "textarea"):
            if e["type"] in ("submit", "button"):
                return f"[{e['id']}] button \"{label}\""
            return f"[{e['id']}] {tag}{'(' + e['type'] + ')' if e['type'] not in ('text', 'textarea', '') else ''} \"{label}\" value=\"{e.get('value', '')}\""
        suffix = " (submits form)" if e.get("form") == "post" else ""
        return f"[{e['id']}] button \"{label}\"{suffix}"

    def describe(self, el_id: Any) -> dict[str, Any] | None:
        if self._page is None:
            return None
        try:
            return self._page.evaluate(_DESCRIBE_JS, str(self.resolve_id(el_id)))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ actions
    def resolve_url(self, url: str) -> str:
        """Relative paths ("/employees") resolve against the current site."""
        url = str(url).strip()
        if url.startswith("/"):
            m = re.match(r"^(https?://[^/]+)", self._page.url if self._page is not None else "")
            if m:
                return m.group(1) + url
        return url

    def open(self, url: str) -> str:
        url = self.resolve_url(url)
        if not self.origin_allowed(url):
            raise BrowserError(f"Navigation to {url} is not permitted.")
        page = self._ensure()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Could not open {url}: {str(e).splitlines()[0]}") from e
        return self.snapshot()

    def resolve_id(self, key: Any) -> int:
        """Accept an element id, or a label / field name (models often use those). Exact match first, then substring."""
        s = str(key).strip().strip("[]")
        if s.isdigit():
            return int(s)
        norm = lambda t: re.sub(r"[\s:*]+", " ", str(t)).strip().lower()  # noqa: E731
        want = norm(s)
        els = getattr(self, "_elements", [])
        for test in (lambda e: norm(e["label"]) == want or norm(e.get("name", "")) == want,
                     lambda e: want and (want in norm(e["label"]) or want in norm(e.get("name", "")))):
            hits = [e for e in els if test(e)]
            if len(hits) >= 1:
                return int(hits[0]["id"])
        fields = ", ".join(f"[{e['id']}] {e['label']}" for e in els if e["tag"] in ("input", "select", "textarea", "button"))
        raise BrowserError(f"No element matching '{key}' on the current page. Use an id or label from the LATEST "
                           f"observation. Fields/buttons here: {fields or 'none'}")

    def _locator(self, el_id: Any):
        page = self._ensure()
        el_id = self.resolve_id(el_id)
        loc = page.locator(f'[data-fte-id="{el_id}"]')
        if loc.count() == 0:
            raise BrowserError(f"No element [{el_id}] on the current page. Use ids from the LATEST observation.")
        return loc

    def click(self, el_id: Any) -> str:
        loc = self._locator(el_id)
        try:
            loc.click(timeout=5000)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Click on [{el_id}] failed: {str(e).splitlines()[0]}") from e
        try:
            self._page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        return self.snapshot()

    def fill(self, fields: dict[str, Any], resolve: Callable[[str], str]) -> str:
        if not isinstance(fields, dict) or not fields:
            raise BrowserError('browser_fill needs "fields": {"<element id>": "<value>", ...}')
        done, errors = [], []
        for el_id, value in fields.items():
            try:
                loc = self._locator(el_id)
                value = resolve(str(value))
                tag = loc.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    self._select(loc, value)
                else:
                    loc.fill(value, timeout=5000)
                done.append(str(el_id))
            except Exception as e:  # noqa: BLE001
                errors.append(f"[{el_id}]: {str(e).splitlines()[0]}")
        snap = self.snapshot()
        head = f"Filled {len(done)} field(s): {', '.join(done) or 'none'}."
        if errors:
            head += " ERRORS: " + "; ".join(errors)
            raise BrowserError(head + "\n" + snap)
        return head + "\n" + snap

    @staticmethod
    def _select(loc, value: str) -> None:
        options = loc.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
        v = value.strip().lower()
        for val, text in options:  # exact, then substring match on value or visible text
            if v in (val.lower(), text.strip().lower()):
                loc.select_option(value=val)
                return
        for val, text in options:
            if v and (v in text.lower() or v in val.lower()):
                loc.select_option(value=val)
                return
        raise BrowserError(f"no option matching '{value}'. Options: {[t for _, t in options]}")

    def fetch_text(self, url: str) -> str:
        """GET a page with the browser's session cookies without navigating the visible page."""
        if not self.origin_allowed(url):
            raise BrowserError(f"Fetching {url} is not permitted.")
        page = self._ensure()
        return page.context.request.get(url).text()

    def screenshot(self, caption: str = "") -> Path:
        page = self._ensure()
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._shot_n += 1
        slug = re.sub(r"[^a-z0-9]+", "-", caption.lower()).strip("-")[:40] or "page"
        path = self.shots_dir / f"{self._shot_n:03d}-{slug}.png"
        page.screenshot(path=str(path), full_page=True)
        return path
