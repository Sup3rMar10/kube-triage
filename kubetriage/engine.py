"""Run the rules over a document, rank, and group the findings by workload."""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from .findings import SEVERITIES, SEVERITY_RANK, Finding
from .model import Cluster, load, meta, pod_name, pod_ns, workload_of
from .rules import NODE_RULES, POD_RULES


@dataclass
class MergedFinding:
    """One finding, shared by every pod of a workload that has it (evidence comes from the first pod)."""
    first: Finding
    pods: list = field(default_factory=list)


@dataclass
class WorkloadGroup:
    namespace: str
    workload: str
    severity: str
    pods_total: int
    findings: list  # [MergedFinding]

    @property
    def pods_affected(self) -> int:
        return len({p for f in self.findings for p in f.pods})


@dataclass
class Report:
    findings: list          # every per-pod Finding, ranked
    groups: list            # [WorkloadGroup], most urgent first
    pods_seen: int
    nodes_seen: int

    def to_dict(self) -> dict:
        counts = Counter(f.severity for f in self.findings)
        return {
            "summary": {
                "pods_seen": self.pods_seen, "nodes_seen": self.nodes_seen,
                "workloads_with_findings": len(self.groups), "findings": len(self.findings),
                "by_severity": {s: counts[s] for s in SEVERITIES if counts[s]},
            },
            "workloads": [{
                "namespace": g.namespace, "workload": g.workload, "severity": g.severity,
                "pods_affected": g.pods_affected, "pods_total": g.pods_total,
                "findings": [dict(m.first.to_dict(), pods=list(m.pods)) for m in g.findings],
            } for g in self.groups],
        }


def run_rules(cluster: Cluster) -> list:
    findings = []
    for pod in cluster.pods:
        for rule in POD_RULES:
            findings.extend(rule(pod, cluster))
    for node in cluster.nodes:
        for rule in NODE_RULES:
            findings.extend(rule(node, cluster))
    return findings


def diagnose(doc, min_severity: str = "low") -> Report:
    cluster = load(doc)
    cutoff = SEVERITY_RANK[min_severity]
    findings = [f for f in run_rules(cluster) if SEVERITY_RANK[f.severity] <= cutoff]

    totals = Counter()
    for pod in cluster.pods:
        kind, name = workload_of(pod)
        totals[(pod_ns(pod), f"{kind}/{name}")] += 1

    by_workload: "OrderedDict[tuple, OrderedDict]" = OrderedDict()
    for f in sorted(findings, key=lambda f: (SEVERITY_RANK[f.severity], f.rule, f.subject)):
        merged = by_workload.setdefault((f.namespace, f.workload), OrderedDict())
        key = (f.rule, f.variant, f.container)
        if key not in merged:
            merged[key] = MergedFinding(first=f)
        merged[key].pods.append(f.subject)

    groups = []
    for (ns, workload), merged in by_workload.items():
        items = list(merged.values())
        groups.append(WorkloadGroup(
            namespace=ns, workload=workload,
            severity=min((m.first.severity for m in items), key=SEVERITY_RANK.get),
            pods_total=totals.get((ns, workload), 1 if workload.startswith("Node/") else 0), findings=items))
    groups.sort(key=lambda g: (SEVERITY_RANK[g.severity], -g.pods_affected, -len(g.findings), g.namespace, g.workload))

    ranked = sorted(findings, key=lambda f: (SEVERITY_RANK[f.severity], f.namespace, f.workload, f.rule, f.subject))
    return Report(findings=ranked, groups=groups, pods_seen=len(cluster.pods), nodes_seen=len(cluster.nodes))
