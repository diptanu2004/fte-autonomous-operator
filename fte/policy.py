"""Permission gate. Every action passes through here *before* execution.

The model is told the rules, but enforcement lives in code: an approval-gated action pauses the run until a
human decides, and a blocked action never executes regardless of what the model says.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

READ_ONLY_TOOLS = {
    "search_knowledge", "read_document", "list_files", "read_file", "browser_open", "browser_read",
    "browser_screenshot", "browser_login", "ask_human", "report_verification", "calculate",
}


@dataclass
class Decision:
    verdict: str  # allow | approve | block
    reason: str = ""
    rule: str = ""


ALLOW = Decision("allow")


def _num(v: Any) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)))
    except ValueError:
        return None


class Policy:
    def __init__(self, permissions: dict[str, Any]):
        self.p = permissions or {}
        email = self.p.get("email", {})
        self.internal_domain = email.get("internal_domain", "")
        self.external_requires_approval = email.get("external_requires_approval", True)
        self.writable_prefixes = tuple(self.p.get("files", {}).get("writable_prefixes", []))
        self.allowed_origins = [o.rstrip("/") for o in self.p.get("browser", {}).get("allowed_origins", [])]
        self.click_rules = self.p.get("browser", {}).get("click_rules", [])
        self.max_consecutive_failures = int(self.p.get("max_consecutive_failures", 4))

    def origin_allowed(self, url: str) -> bool:
        if not self.allowed_origins:
            return True
        m = re.match(r"^(https?://[^/]+)", url)
        return bool(m) and m.group(1).rstrip("/") in self.allowed_origins

    def check(self, tool: str, args: dict[str, Any], phase: str, describe_click: Callable[[Any], dict] | None,
              visited: list[str] | None = None) -> Decision:
        # Verification must observe, never change the world.
        if phase == "verify" and tool not in READ_ONLY_TOOLS | {"browser_click", "browser_fill"}:
            return Decision("block", "Verification is read-only: re-checking, not doing.", "verify-read-only")

        if tool == "browser_open" and not self.origin_allowed(str(args.get("url", ""))):
            return Decision("block", f"URL is outside the permitted company systems: {self.allowed_origins}", "allowed-origins")

        if tool == "write_file":
            path = str(args.get("path", "")).replace("\\", "/").lstrip("./")
            if self.writable_prefixes and not path.startswith(self.writable_prefixes):
                return Decision("block", f"Writes are only allowed under {list(self.writable_prefixes)}.", "writable-prefixes")

        if tool == "send_email":
            to = [t.strip() for t in re.split(r"[,;]", str(args.get("to", ""))) if t.strip()]
            external = [t for t in to if not t.lower().endswith("@" + self.internal_domain.lower())]
            if external and self.external_requires_approval:
                return Decision("approve", f"Email to external recipient(s) {external} requires approval.", "external-email")

        if tool == "browser_click" and describe_click is not None:
            info = describe_click(args.get("id")) or {}
            if phase == "verify" and info.get("form_method") == "post":
                return Decision("block", "Submitting forms is not allowed while verifying.", "verify-read-only")
            label = info.get("label", "")
            values = info.get("form_values", {})
            for rule in self.click_rules:
                if not re.search(rule.get("label_regex", "$^"), label):
                    continue
                if "requires_visited" in rule:
                    if self._visited_ok(rule["requires_visited"], values, visited or []):
                        continue
                    return Decision(rule.get("decision", "block"), " ".join(rule.get("reason", "").split()), rule.get("name", ""))
                if all(self._field_ok(c, values) for c in rule.get("field_conditions", [])):
                    return Decision(rule.get("decision", "approve"), " ".join(rule.get("reason", "").split()), rule.get("name", ""))
        return ALLOW

    @staticmethod
    def _visited_ok(template: str, values: dict[str, Any], visited: list[str]) -> bool:
        flat = {k: str(v[0] if isinstance(v, list) else v).strip().upper() for k, v in values.items()}
        try:
            needle = template.format(**flat).lower()
        except KeyError:
            return True  # form doesn't carry the referenced field: rule not applicable
        if needle.rstrip("/").endswith("/"):  # empty value -> let the form's own validation complain
            return True
        return any(u.lower().split("?")[0].rstrip("/").endswith(needle.rstrip("/")) for u in visited)

    @staticmethod
    def _field_ok(cond: dict[str, Any], values: dict[str, Any]) -> bool:
        v = values.get(cond["field"])
        if v is None:
            return False
        texts = v if isinstance(v, list) else [v]
        if "equals" in cond:
            return any(str(t).strip().lower() == str(cond["equals"]).lower() for t in texts)
        if "greater_than" in cond:
            n = _num(texts[0])
            return n is not None and n > float(cond["greater_than"])
        return False
