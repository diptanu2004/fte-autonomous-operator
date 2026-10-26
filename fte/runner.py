"""Runs an Agent on a worker thread so a UI (Streamlit) can poll events and answer human requests."""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from .agent import Agent
from .human import HumanReply, QueueHuman
from .llm import LLM, LLMConfig


@dataclass
class RunHandle:
    request: str
    events: list[dict[str, Any]] = field(default_factory=list)
    human: QueueHuman = field(default_factory=QueueHuman)
    stop_event: threading.Event = field(default_factory=threading.Event)
    agent: Agent | None = None
    thread: threading.Thread | None = None
    error: str = ""

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def pending(self):
        return self.human.pending

    def respond(self, approved: bool, text: str = "") -> None:
        self.human.respond(HumanReply(approved, text))

    def stop(self) -> None:
        self.stop_event.set()
        self.human.cancelled.set()


def start_run(request: str, llm_config: LLMConfig, **agent_kwargs: Any) -> RunHandle:
    handle = RunHandle(request)
    lock = threading.Lock()

    def emit(kind: str, data: dict[str, Any]) -> None:
        with lock:
            handle.events.append({**data, "ts": time.time(), "event": kind})

    def work() -> None:
        try:
            agent = Agent(LLM(llm_config), handle.human, emit=emit, stop_event=handle.stop_event, **agent_kwargs)
            handle.agent = agent
            agent.run(request)
        except Exception as e:  # noqa: BLE001 - surface to the UI
            handle.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            emit("error", {"message": handle.error})

    handle.thread = threading.Thread(target=work, daemon=True, name="fte-run")
    handle.thread.start()
    return handle
