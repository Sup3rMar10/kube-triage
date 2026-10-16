"""The Finding record every rule produces, and severity ordering."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SEVERITIES = ["critical", "high", "medium", "low", "info"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    rule: str                 # stable id, e.g. "KT001"
    severity: str
    namespace: str
    workload: str             # "Deployment/web", "Pod/debug", "Node/worker-1"
    subject: str              # pod name, or node name for node rules
    title: str
    cause: str                # plain-English explanation
    evidence: list = field(default_factory=list)   # "path = value" lines quoted from the input JSON
    fix: list = field(default_factory=list)        # suggested actions and commands
    variant: Optional[str] = None                  # sub-type, e.g. "tag-not-found"
    container: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "variant": self.variant, "severity": self.severity,
            "namespace": self.namespace, "workload": self.workload, "subject": self.subject,
            "container": self.container, "title": self.title, "cause": self.cause,
            "evidence": list(self.evidence), "fix": list(self.fix),
        }
