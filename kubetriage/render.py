"""Plain-text rendering of a Report."""
from __future__ import annotations

from .engine import Report


def _n(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


def render_text(report: Report) -> str:
    d = report.to_dict()["summary"]
    if not report.groups:
        return (f"kube-triage: nothing to report ({report.pods_seen} {_n(report.pods_seen, 'pod')}, "
                f"{report.nodes_seen} {_n(report.nodes_seen, 'node')} checked).\n")
    sev = ", ".join(f"{n} {s}" for s, n in d["by_severity"].items())
    w = len(report.groups)
    lines = [f"kube-triage: {w} {_n(w, 'workload')} {'needs' if w == 1 else 'need'} attention "
             f"({d['findings']} {_n(d['findings'], 'finding')}: {sev}) across {report.pods_seen} {_n(report.pods_seen, 'pod')} "
             f"and {report.nodes_seen} {_n(report.nodes_seen, 'node')}", ""]
    for g in report.groups:
        scope = []
        if g.namespace:
            scope.append(f"namespace {g.namespace}")
        if g.pods_total and not g.workload.startswith("Node/"):
            scope.append(f"{g.pods_affected} of {g.pods_total} pods")
        lines.append(f"{g.severity.upper():<9}{g.workload}" + (f"  ({', '.join(scope)})" if scope else ""))
        for m in g.findings:
            f = m.first
            lines.append(f"  {f.rule} {f.title}")
            if not f.workload.startswith("Node/"):
                shown = ", ".join(m.pods[:3]) + (f" (+{len(m.pods) - 3} more)" if len(m.pods) > 3 else "")
                lines.append(f"    pods:     {shown}")
            lines.append(f"    why:      {f.cause}")
            lines.append("    evidence:")
            lines.extend(f"      - {e}" for e in f.evidence)
            lines.append("    fix:")
            lines.extend(f"      - {x}" for x in f.fix)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
