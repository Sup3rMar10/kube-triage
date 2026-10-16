"""Optional `--explain`: a language model rephrases the findings. Off by default; nothing here is needed to diagnose.

What the model sees is only the finding fields (rule, title, cause, evidence, fix) after redaction. It never
receives pod specs, environment variables, ConfigMap or Secret data, or the raw kubectl output. It is told to
rephrase, not to add causes. The Anthropic adapter has NOT been run against the live API in this repo; only the
prompt building, redaction and wiring are tested, with a fake model function.
"""
from __future__ import annotations

import os
import re

_REDACTIONS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization)(\"?\s*[:=]\s*)\"?[^\s\",;]+"),
     r"\1\2<redacted>"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://<redacted>@"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "<redacted-blob>"),
]


def redact(text: str) -> str:
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text


SYSTEM = ("You help an engineer read Kubernetes diagnostics. Rephrase the findings below in plain, short language for "
          "someone who is not a Kubernetes expert. Do not add causes, commands or facts that are not in the findings. "
          "If something is uncertain in the findings, keep it uncertain.")


def build_prompt(report_dict: dict) -> str:
    lines = []
    for w in report_dict["workloads"]:
        lines.append(f"Workload {w['workload']} (namespace {w['namespace'] or 'cluster'}), {w['pods_affected']} pods affected")
        for f in w["findings"]:
            lines.append(f"- {f['rule']} [{f['severity']}] {redact(f['title'])}")
            lines.append(f"  cause: {redact(f['cause'])}")
            lines.extend(f"  evidence: {redact(e)}" for e in f["evidence"])
            lines.extend(f"  fix: {redact(x)}" for x in f["fix"])
    return "\n".join(lines)


def explain(report_dict: dict, llm) -> str:
    """`llm(system, prompt) -> str` is any callable; tests pass a fake."""
    if not report_dict["workloads"]:
        return ""
    return llm(SYSTEM, build_prompt(report_dict)).strip()


class AnthropicExplainer:
    """Adapter for the Anthropic Messages API. Needs `pip install anthropic` and ANTHROPIC_API_KEY."""

    def __init__(self, model=None):
        self.model = model or os.environ.get("KUBETRIAGE_MODEL", "claude-sonnet-5-5")

    def __call__(self, system: str, prompt: str) -> str:
        try:
            import anthropic
        except ImportError:
            raise SystemExit("--explain anthropic needs: pip install anthropic") from None
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("--explain anthropic needs ANTHROPIC_API_KEY in the environment")
        msg = anthropic.Anthropic().messages.create(model=self.model, max_tokens=1200, system=system,
                                                    messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
