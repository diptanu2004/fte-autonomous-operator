"""Human-in-the-loop channels. The agent blocks on these; each UI supplies its own implementation."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HumanRequest:
    kind: str  # approval | question | guidance
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class HumanReply:
    approved: bool = False
    text: str = ""


class HumanChannel:
    def ask(self, req: HumanRequest) -> HumanReply:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleHuman(HumanChannel):
    def ask(self, req: HumanRequest) -> HumanReply:
        print("\n" + "=" * 70)
        if req.kind == "approval":
            print(f"APPROVAL NEEDED: {req.message}")
            for k, v in req.context.items():
                print(f"  {k}: {v}")
            ans = input("Approve? [y/N] (optionally add a comment after a space): ").strip()
            approved = ans[:1].lower() == "y"
            return HumanReply(approved=approved, text=ans[1:].strip())
        print(f"{'QUESTION' if req.kind == 'question' else 'AGENT IS STUCK'}: {req.message}")
        return HumanReply(approved=True, text=input("Your answer: ").strip())


class ScriptedHuman(HumanChannel):
    """Non-interactive: approves everything and answers from a dict (keyword -> answer). For tests/CI."""

    def __init__(self, answers: dict[str, str] | None = None, approve: bool = True):
        self.answers, self.approve, self.log = answers or {}, approve, []

    def ask(self, req: HumanRequest) -> HumanReply:
        self.log.append(req)
        if req.kind == "approval":
            return HumanReply(approved=self.approve, text="auto")
        for key, ans in self.answers.items():
            if key.lower() in req.message.lower():
                return HumanReply(True, ans)
        return HumanReply(True, "No further information is available; use your best judgement within policy.")


class QueueHuman(HumanChannel):
    """For UIs running the agent on a worker thread: the request is published, the worker waits for a reply."""

    def __init__(self):
        self.pending: HumanRequest | None = None
        self._replies: queue.Queue[HumanReply] = queue.Queue()
        self._lock = threading.Lock()
        self.cancelled = threading.Event()

    def ask(self, req: HumanRequest) -> HumanReply:
        with self._lock:
            self.pending = req
        while not self.cancelled.is_set():
            try:
                reply = self._replies.get(timeout=0.5)
                break
            except queue.Empty:
                continue
        else:
            reply = HumanReply(False, "Run stopped by user.")
        with self._lock:
            self.pending = None
        return reply

    def respond(self, reply: HumanReply) -> None:
        self._replies.put(reply)
