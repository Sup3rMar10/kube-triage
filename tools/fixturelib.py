"""Builders for realistic `kubectl get pods,events,nodes -o json` documents.

Deliberately independent of the kubetriage package: the rules never see the builders, and the expected labels
in a scenario are written by hand from what the scenario is meant to show, not from rule output.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
T0 = "2026-10-01T10:00:00Z"
T1 = "2026-10-01T10:05:00Z"


def container(name="app", image="registry.example.com/shop/app:1.4.2", *, limits=("256Mi",), requests=None,
              ports=(8080,), liveness=None, readiness=None, startup=None, env=None, env_from=None, command=None):
    c = {"name": name, "image": image, "imagePullPolicy": "IfNotPresent"}
    if command:
        c["command"] = command
    if ports:
        c["ports"] = [{"containerPort": p, "protocol": "TCP"} for p in ports]
    if env:
        c["env"] = env
    if env_from:
        c["envFrom"] = env_from
    res = {}
    if limits:
        res["limits"] = {"memory": limits[0], **({"cpu": limits[1]} if len(limits) > 1 else {})}
    if requests:
        res["requests"] = dict(requests)
    c["resources"] = res
    for key, probe in (("livenessProbe", liveness), ("readinessProbe", readiness), ("startupProbe", startup)):
        if probe:
            c[key] = probe
    return c


def http_probe(path="/healthz", port=8080, **kw):
    return {"httpGet": {"path": path, "port": port, "scheme": "HTTP"},
            "periodSeconds": kw.get("period", 10), "timeoutSeconds": kw.get("timeout", 1),
            "failureThreshold": kw.get("failures", 3), "successThreshold": 1,
            **({"initialDelaySeconds": kw["delay"]} if "delay" in kw else {})}


def term(code, reason="Error", started=T0, finished="2026-10-01T10:00:02Z", message=None):
    t = {"exitCode": code, "reason": reason, "startedAt": started, "finishedAt": finished,
         "containerID": "containerd://" + "ab" * 32}
    if message:
        t["message"] = message
    return {"terminated": t}


def running(since=T0):
    return {"running": {"startedAt": since}}


def waiting(reason, message=""):
    return {"waiting": {"reason": reason, **({"message": message} if message else {})}}


def cstatus(name="app", image="registry.example.com/shop/app:1.4.2", *, state=None, last=None, ready=False,
            restarts=0, started=None):
    s = {"name": name, "image": image, "imageID": "", "ready": ready, "restartCount": restarts,
         "state": state if state is not None else running(), "lastState": last or {}}
    if started is not None:
        s["started"] = started
    return s


def pod(name, ns="default", *, containers=None, statuses=None, phase="Running", node="worker-1", owner=None,
        reason=None, message=None, conditions=None, volumes=None, node_selector=None, tolerations=None,
        qos="Burstable", labels=None, image_pull_secrets=None, restart_policy="Always", init_containers=None,
        init_statuses=None):
    labels = dict(labels or {"app": (owner or {}).get("app", name)})
    meta = {"name": name, "namespace": ns, "uid": f"uid-{name}", "creationTimestamp": T0, "labels": labels,
            "resourceVersion": "1000"}
    if owner:
        meta["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": owner["kind"], "name": owner["name"],
                                    "uid": f"uid-{owner['name']}", "controller": True, "blockOwnerDeletion": True}]
        if owner["kind"] == "ReplicaSet":
            labels["pod-template-hash"] = owner["name"].rsplit("-", 1)[1]
    spec = {"containers": containers or [container()], "restartPolicy": restart_policy, "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler", "terminationGracePeriodSeconds": 30}
    if init_containers:
        spec["initContainers"] = init_containers
    if node:
        spec["nodeName"] = node
    if volumes:
        spec["volumes"] = volumes
    if node_selector:
        spec["nodeSelector"] = node_selector
    if tolerations is not None:
        spec["tolerations"] = tolerations
    if image_pull_secrets:
        spec["imagePullSecrets"] = [{"name": n} for n in image_pull_secrets]
    st = {"phase": phase, "qosClass": qos}
    if reason:
        st["reason"] = reason
    if message:
        st["message"] = message
    if conditions is None:
        ok = phase == "Running" and statuses and all(s.get("ready") for s in statuses)
        conditions = [{"type": "PodScheduled", "status": "True" if node else "False", "lastTransitionTime": T0},
                      {"type": "Ready", "status": "True" if ok else "False", "lastTransitionTime": T0}]
    st["conditions"] = conditions
    if statuses is not None:
        st["containerStatuses"] = statuses
    if init_statuses is not None:
        st["initContainerStatuses"] = init_statuses
    if node:
        st.update({"hostIP": "10.0.0.11", "podIP": "10.244.1.7", "startTime": T0})
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": spec, "status": st}


def replicaset_owner(deploy, hash_="7d9f8c6b5"):
    return {"kind": "ReplicaSet", "name": f"{deploy}-{hash_}", "app": deploy}


def unscheduled_conditions(message):
    return [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": message,
             "lastProbeTime": None, "lastTransitionTime": T0}]


def event(pod_name, ns, reason, message, *, type_="Warning", count=1, field_path=None, kind="Pod", time=T1):
    e = {"apiVersion": "v1", "kind": "Event",
         "metadata": {"name": f"{pod_name}.{hashlib.md5((reason + message).encode()).hexdigest()[:8]}", "namespace": ns},
         "involvedObject": {"kind": kind, "namespace": ns, "name": pod_name, "uid": f"uid-{pod_name}"},
         "reason": reason, "message": message, "type": type_, "count": count,
         "firstTimestamp": T0, "lastTimestamp": time, "source": {"component": "kubelet"}}
    if field_path:
        e["involvedObject"]["fieldPath"] = field_path
    return e


def node(name="worker-1", *, cpu="4", memory="16Gi", ready=True, pressure=(), taints=None, unschedulable=False):
    conds = [{"type": "MemoryPressure", "status": "True" if "memory" in pressure else "False",
              "message": "kubelet has insufficient memory available" if "memory" in pressure else "kubelet has sufficient memory available"},
             {"type": "DiskPressure", "status": "True" if "disk" in pressure else "False",
              "message": "kubelet has disk pressure" if "disk" in pressure else "kubelet has no disk pressure"},
             {"type": "PIDPressure", "status": "True" if "pid" in pressure else "False",
              "message": "kubelet has sufficient PID available"},
             {"type": "Ready", "status": "True" if ready else "Unknown",
              "reason": "KubeletReady" if ready else "NodeStatusUnknown",
              "message": "kubelet is posting ready status" if ready else "Kubelet stopped posting node status."}]
    spec = {}
    if taints:
        spec["taints"] = taints
    if unschedulable:
        spec["unschedulable"] = True
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}},
            "spec": spec,
            "status": {"capacity": {"cpu": cpu, "memory": memory, "pods": "110"},
                       "allocatable": {"cpu": cpu, "memory": memory, "pods": "110"}, "conditions": conds}}


def k8s_list(*items):
    flat = []
    for i in items:
        flat.extend(i if isinstance(i, list) else [i])
    return {"apiVersion": "v1", "kind": "List", "metadata": {"resourceVersion": ""}, "items": flat}


# ---- writing fixtures + manifest ---------------------------------------------------------------

def write_fixture(split, name, doc, expected, *, source="hand", note=""):
    d = FIXTURES / split
    d.mkdir(parents=True, exist_ok=True)
    rel = f"{split}/{name}.json"
    (FIXTURES / rel).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    mpath = FIXTURES / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"fixtures": []}
    entry = {"name": f"{split}-{name}", "file": rel, "split": split, "source": source, "expected": expected, "note": note}
    manifest["fixtures"] = [e for e in manifest["fixtures"] if e["name"] != entry["name"]] + [entry]
    manifest["fixtures"].sort(key=lambda e: (e["split"], e["name"]))
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def exp(subject, rule, variant=None):
    return {"subject": subject, "rule": rule, **({"variant": variant} if variant else {})}
