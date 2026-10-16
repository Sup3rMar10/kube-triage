"""Every evidence line that names a JSON path must be true of the input it came from.

This is the check behind the claim "evidence is quoted from the JSON": for each finding on each fixture, the
path-style lines (`status.containerStatuses[app].restartCount = 6`, `... is not set`, `events[Failed] = "..."`)
are looked up in the original document and compared.
"""
import json
import re
from pathlib import Path

from kubetriage.engine import diagnose
from kubetriage.evaluate import load_manifest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MISSING = object()
SKIP = object()
SEG = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(.*)\])?$")


def resolve(obj, path):
    cur = obj
    for seg in path.split("."):
        m = SEG.match(seg)
        if not m:
            return SKIP
        key, sel = m.group(1), m.group(2)
        if not isinstance(cur, dict) or key not in cur:
            return MISSING
        cur = cur[key]
        if sel is not None:
            if sel == "":
                return SKIP
            if not isinstance(cur, list):
                return MISSING
            cur = next((x for x in cur if sel in (x.get("name"), x.get("type"))), MISSING)
            if cur is MISSING:
                return MISSING
    return cur


def norm(v):
    return " ".join(v.split()) if isinstance(v, str) else v


def objects(doc):
    pods, nodes, events = {}, {}, []
    for it in doc["items"]:
        if it["kind"] == "Pod":
            pods[(it["metadata"]["namespace"], it["metadata"]["name"])] = it
        elif it["kind"] == "Node":
            nodes[it["metadata"]["name"]] = it
        elif it["kind"] == "Event":
            events.append(it)
    return pods, nodes, events


def check_line(line, obj, events):
    """Return True/False if the line is checkable, None if it is derived text that is not a JSON quote."""
    m = re.match(r"^(\S+) is not set$", line)
    if m:
        return resolve(obj, m.group(1)) is MISSING
    m = re.match(r"^((?:spec|status)\.\S+) = (.*)$", line)
    if m:
        got = resolve(obj, m.group(1))
        if got is SKIP:
            return None
        want = json.loads(m.group(2))
        if isinstance(want, str) and want.endswith("…"):
            return isinstance(got, str) and norm(got).startswith(want[:-1])
        return got is not MISSING and norm(got) == norm(want)
    m = re.match(r"^events\[(\w+)\](?: x\d+)? = (.*)$", line)
    if m:
        want = json.loads(m.group(2))
        pool = [norm(e.get("message") or e.get("note", "")) for e in events if e.get("reason") == m.group(1)]
        if want.endswith("…"):
            return any(p.startswith(want[:-1]) for p in pool)
        return want in pool
    return None


def test_evidence_lines_match_the_input_documents():
    checked, failures = 0, []
    for entry in load_manifest(FIXTURES):
        doc = json.loads((FIXTURES / entry["file"]).read_text(encoding="utf-8"))
        pods, nodes, events = objects(doc)
        pod_events = events
        for f in diagnose(doc, "info").findings:
            obj = nodes.get(f.subject) if f.workload.startswith("Node/") else pods.get((f.namespace, f.subject))
            assert obj is not None, f"{entry['name']}: finding for unknown object {f.subject}"
            for line in f.evidence:
                ok = check_line(line, obj, pod_events)
                if ok is None:
                    continue
                checked += 1
                if not ok:
                    failures.append(f"{entry['name']} {f.rule} {f.subject}: {line}")
    assert not failures, "\n".join(failures)
    assert checked >= 50, f"only {checked} evidence lines were checkable; the test has become vacuous"
