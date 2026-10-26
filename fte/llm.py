"""Provider-agnostic LLM access over the OpenAI-compatible chat API.

Local Ollama / LM Studio (Qwen3-30B-A3B Q4) for development, Groq for deployment, any compatible
endpoint for BYOK. The agent speaks a small JSON protocol rather than native tool-calling: quantized
local models emit malformed tool calls far more often than malformed JSON, and JSON we can repair.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

PROVIDERS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen3:30b-a3b", "key_env": ""},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "model": "qwen/qwen3-30b-a3b", "key_env": ""},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "qwen/qwen3-32b", "key_env": "GROQ_API_KEY"},
    "custom": {"base_url": "", "model": "", "key_env": "OPENAI_API_KEY"},
}


@dataclass
class LLMConfig:
    provider: str = "ollama"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048
    thinking: bool = False  # Qwen3 hybrid thinking; off = faster, usually sufficient with explicit plans
    json_mode: bool = True
    timeout: float = 180.0

    def resolved(self) -> "LLMConfig":
        preset = PROVIDERS.get(self.provider, PROVIDERS["custom"])
        key = (self.api_key or (os.environ.get(preset["key_env"], "") if preset["key_env"] else "")
               or os.environ.get("FTE_API_KEY", ""))
        return LLMConfig(
            provider=self.provider,
            model=self.model or os.environ.get("FTE_MODEL") or preset["model"],
            base_url=self.base_url or os.environ.get("FTE_BASE_URL") or preset["base_url"],
            api_key=key or "not-needed",
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            thinking=self.thinking,
            json_mode=self.json_mode,
            timeout=self.timeout,
        )

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            provider=os.environ.get("FTE_PROVIDER", "ollama"),
            model=os.environ.get("FTE_MODEL", ""),
            base_url=os.environ.get("FTE_BASE_URL", ""),
            api_key=os.environ.get("FTE_API_KEY", ""),
            thinking=os.environ.get("FTE_THINKING", "0") == "1",
        )


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


@dataclass
class LLM:
    config: LLMConfig
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self):
        from openai import OpenAI

        self.config = self.config.resolved()
        if not self.config.base_url or not self.config.model:
            raise LLMError("LLM base URL and model must be set.")
        self._client = OpenAI(base_url=self.config.base_url, api_key=self.config.api_key, timeout=self.config.timeout,
                              max_retries=0)  # retries are handled (and classified) in chat()
        # Optional request features get dropped (and stay dropped) if a backend rejects them.
        self._optional = {"response_format": self.config.json_mode, "reasoning": self.config.provider == "groq"}

    # ------------------------------------------------------------------ raw call
    def chat(self, messages: list[dict[str, str]], *, json_mode: bool | None = None) -> str:
        cfg = self.config
        msgs = [dict(m) for m in messages]
        if not cfg.thinking and "qwen3" in cfg.model.lower() and cfg.provider != "groq":
            # Qwen3 soft switch; Groq uses reasoning_effort instead.
            msgs[-1]["content"] = msgs[-1]["content"] + "\n/no_think"
        want_json = cfg.json_mode if json_mode is None else json_mode

        for attempt in range(7):
            kwargs: dict[str, Any] = {
                "model": cfg.model,
                "messages": msgs,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
            }
            extra: dict[str, Any] = {}
            if want_json and self._optional["response_format"]:
                kwargs["response_format"] = {"type": "json_object"}
            if self._optional["reasoning"]:
                if cfg.thinking:
                    extra["reasoning_format"] = "hidden"
                else:
                    extra["reasoning_effort"] = "none"
            if extra:
                kwargs["extra_body"] = extra
            t0 = time.time()
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001 - classify by message across SDK/backends
                msg = str(e).lower()
                status = getattr(e, "status_code", None)
                if status == 400 and "response_format" in kwargs and ("response_format" in msg or "json" in msg):
                    self._optional["response_format"] = False
                    continue
                if status == 400 and extra and any(k in msg for k in ("reasoning", "extra", "unsupported", "property")):
                    self._optional["reasoning"] = False
                    continue
                unreachable = status is None and "connection" in msg and "timeout" not in msg
                retryable = status == 429 or (status is not None and 500 <= status <= 530)  # incl. Cloudflare 52x/530
                if retryable or "timeout" in msg or "connection" in msg:
                    if attempt < (1 if unreachable else 6):
                        wait = min(60.0, 2 ** attempt * 2)
                        headers = getattr(getattr(e, "response", None), "headers", None) or {}
                        try:  # Groq/OpenAI send Retry-After on 429s (free-tier TPM limits are tight)
                            wait = min(90.0, max(wait if status != 429 else 1.0, float(headers.get("retry-after", 0))))
                        except (TypeError, ValueError):
                            pass
                        time.sleep(wait)
                        continue
                raise LLMError(f"{cfg.provider} call failed: {e}") from e
            self.usage.calls += 1
            self.usage.seconds += time.time() - t0
            if resp.usage:
                self.usage.prompt_tokens += resp.usage.prompt_tokens or 0
                self.usage.completion_tokens += resp.usage.completion_tokens or 0
            text = resp.choices[0].message.content or ""
            return strip_thinking(text)
        raise LLMError("LLM call failed after retries.")

    # ------------------------------------------------------------------ JSON call with repair loop
    def chat_json(self, messages: list[dict[str, str]], *, required: tuple[str, ...] = (), retries: int = 2) -> dict:
        msgs = list(messages)
        last_err = ""
        for _ in range(retries + 1):
            text = self.chat(msgs)
            try:
                data = parse_json(text)
                missing = [k for k in required if k not in data]
                if missing:
                    raise ValueError(f"missing keys: {missing}")
                return data
            except ValueError as e:
                last_err = str(e)
                msgs = msgs + [
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content": f"That was not valid for the protocol ({last_err}). "
                                                 "Reply again with ONE JSON object only, no prose, no code fences."},
                ]
        raise LLMError(f"Model did not return valid JSON: {last_err}")


# ---------------------------------------------------------------------- parsing helpers

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_thinking(text: str) -> str:
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # opening tag consumed by the chat template
        text = text.split("</think>", 1)[1]
    return text.strip()


def parse_json(text: str) -> dict:
    """Extract the first JSON object from model text, tolerating fences, prose and trailing commas."""
    text = strip_thinking(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                for candidate in (blob, re.sub(r",\s*([}\]])", r"\1", blob)):
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                raise ValueError("malformed JSON object")
    raise ValueError("unterminated JSON object")
