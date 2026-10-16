import json

from fixturelib import container, cstatus, k8s_list, node, pod, replicaset_owner, term, waiting
from kubetriage.engine import diagnose

NS = "t"
IMG = "registry.example.com/app:1.0"


def crashing(name, deploy="web", code=1, ns=NS):
    return pod(name, ns, owner=replicaset_owner(deploy, "abc123def"),
               statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"), last=term(code), restarts=3)])


def test_identical_findings_across_replicas_merge_into_one_entry():
    r = diagnose(k8s_list(crashing("web-abc123def-1"), crashing("web-abc123def-2"), crashing("web-abc123def-3")))
    (g,) = r.groups
    assert g.workload == "Deployment/web" and g.pods_affected == 3 and g.pods_total == 3
    (m,) = g.findings
    assert m.pods == ["web-abc123def-1", "web-abc123def-2", "web-abc123def-3"]
    assert len(r.findings) == 3                          # per-pod findings are still all there for scoring


def test_different_exit_codes_stay_separate():
    r = diagnose(k8s_list(crashing("web-abc123def-1", code=1), crashing("web-abc123def-2", code=139)))
    assert sorted(m.first.variant for m in r.groups[0].findings) == ["exit-1", "exit-139"]


def test_workloads_are_ranked_by_severity_then_pods_affected():
    healthy_but_floating = pod("old", NS, containers=[container("app", "x:latest")], statuses=[cstatus("app", "x:latest", ready=True)])
    r = diagnose(k8s_list(healthy_but_floating, crashing("a-abc123def-1", "a"), crashing("b-abc123def-1", "b"),
                          crashing("b-abc123def-2", "b"), node("n", ready=False)))
    assert [g.workload for g in r.groups] == ["Node/n", "Deployment/b", "Deployment/a", "Pod/old"]
    assert [g.severity for g in r.groups] == ["critical", "high", "high", "low"]


def test_min_severity_filters_findings():
    doc = k8s_list(crashing("web-abc123def-1"),
                   pod("old", NS, containers=[container("app", "x:latest")], statuses=[cstatus("app", "x:latest", ready=True)]))
    assert {f.rule for f in diagnose(doc, "info").findings} == {"KT001", "KT008"}
    assert {f.rule for f in diagnose(doc, "high").findings} == {"KT001"}


def test_same_workload_name_in_two_namespaces_is_two_groups():
    r = diagnose(k8s_list(crashing("web-abc123def-1", ns="a"), crashing("web-abc123def-1", ns="b")))
    assert sorted(g.namespace for g in r.groups) == ["a", "b"]


def test_json_shape():
    d = diagnose(k8s_list(crashing("web-abc123def-1"), node("n", ready=False))).to_dict()
    assert set(d) == {"summary", "workloads"}
    assert d["summary"]["by_severity"] == {"critical": 1, "high": 1}
    w = d["workloads"][0]
    assert set(w) == {"namespace", "workload", "severity", "pods_affected", "pods_total", "findings"}
    assert set(w["findings"][0]) >= {"rule", "variant", "severity", "title", "cause", "evidence", "fix", "pods", "container"}
    json.dumps(d)                                         # serialisable


def test_input_forms():
    p = crashing("web-abc123def-1")
    assert diagnose(p).findings and diagnose({"kind": "List", "items": [p]}).findings   # a bare object, or a List
    assert diagnose({"kind": "List", "items": [{"kind": "List", "items": [p]}]}).findings  # nested lists
    assert diagnose({"kind": "List", "items": []}).findings == []
    assert diagnose({"kind": "ConfigMap", "items": []}).findings == []                   # unknown kinds are ignored


def test_events_in_the_events_k8s_io_shape_are_understood():
    p = pod("p", NS, phase="Pending", node=None, statuses=[], conditions=[{"type": "PodScheduled", "status": "False"}])
    e = {"kind": "Event", "metadata": {"namespace": NS, "name": "e1"}, "regarding": {"kind": "Pod", "name": "p", "namespace": NS},
         "reason": "FailedScheduling", "note": "0/1 nodes are available: 1 Insufficient cpu.", "type": "Warning",
         "eventTime": "2026-10-01T10:00:00Z"}
    assert [f.variant for f in diagnose(k8s_list(p, e)).findings] == ["insufficient-cpu"]
