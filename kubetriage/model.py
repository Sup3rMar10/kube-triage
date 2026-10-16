"""Reading `kubectl get pods,events,nodes -o json` output, plus small accessors over pod and node dicts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .quantity import cpu_millicores, mem_bytes


@dataclass
class Event:
    namespace: str
    kind: str
    name: str
    type: str
    reason: str
    message: str
    count: int
    time: str
    field_path: str = ""

    @property
    def container(self) -> Optional[str]:
        m = re.search(r"\{(.+?)\}", self.field_path or "")
        return m.group(1) if m else None


class Cluster:
    """The objects in one input document: pods, nodes and the events that point at them."""

    def __init__(self, pods, nodes, events):
        self.pods = pods
        self.nodes = nodes
        self.events = events
        self._by_object: dict = {}
        for e in events:
            self._by_object.setdefault((e.namespace, e.kind, e.name), []).append(e)

    def events_for(self, pod) -> list:
        key = (pod_ns(pod), "Pod", pod_name(pod))
        return sorted(self._by_object.get(key, []), key=lambda e: e.time)

    def largest_allocatable(self):
        """(millicores, bytes) of the biggest single node, or None when no node data was given."""
        best = None
        for n in self.nodes:
            alloc = (n.get("status") or {}).get("allocatable") or {}
            try:
                cpu = cpu_millicores(alloc["cpu"])
                mem = mem_bytes(alloc["memory"])
            except (KeyError, ValueError):
                continue
            best = (cpu, mem) if best is None else (max(best[0], cpu), max(best[1], mem))
        return best


def load(doc) -> Cluster:
    """Build a Cluster from a kubectl List (or a single object). Unknown kinds are ignored."""
    pods, nodes, events = [], [], []

    def walk(obj):
        if not isinstance(obj, dict):
            return
        kind = obj.get("kind")
        if kind == "List" or (kind is None and "items" in obj):
            for item in obj.get("items") or []:
                walk(item)
        elif kind == "Pod":
            pods.append(obj)
        elif kind == "Node":
            nodes.append(obj)
        elif kind == "Event":
            events.append(_event(obj))

    walk(doc)
    return Cluster(pods, nodes, events)


def _event(e) -> Event:
    obj = e.get("involvedObject") or e.get("regarding") or {}
    series = e.get("series") or {}
    return Event(
        namespace=obj.get("namespace") or meta(e).get("namespace", ""),
        kind=obj.get("kind", ""),
        name=obj.get("name", ""),
        type=e.get("type", ""),
        reason=e.get("reason", ""),
        message=e.get("message") or e.get("note") or "",
        count=int(e.get("count") or series.get("count") or 1),
        time=e.get("lastTimestamp") or e.get("eventTime") or series.get("lastObservedTime")
        or meta(e).get("creationTimestamp", ""),
        field_path=obj.get("fieldPath", ""),
    )


# ---- accessors ----------------------------------------------------------------------------------

def meta(o) -> dict:
    return o.get("metadata") or {}


def pod_name(p) -> str:
    return meta(p).get("name", "")


def pod_ns(p) -> str:
    return meta(p).get("namespace", "default")


def pod_phase(p) -> str:
    return (p.get("status") or {}).get("phase", "")


def workload_of(pod) -> tuple:
    """(kind, name) of the thing that owns the pod; a Deployment is recovered from its ReplicaSet's name."""
    owners = meta(pod).get("ownerReferences") or []
    owner = next((o for o in owners if o.get("controller")), owners[0] if owners else None)
    if owner is None:
        return "Pod", pod_name(pod)
    kind, name = owner.get("kind", "Pod"), owner.get("name", pod_name(pod))
    if kind == "ReplicaSet":
        h = (meta(pod).get("labels") or {}).get("pod-template-hash")
        if h and name.endswith("-" + h):
            return "Deployment", name[: -(len(h) + 1)]
        m = re.match(r"^(.*)-[a-z0-9]{6,10}$", name)
        return ("Deployment", m.group(1)) if m else ("ReplicaSet", name)
    if kind == "Job":
        m = re.match(r"^(.*)-\d{7,}$", name)
        return ("CronJob", m.group(1)) if m else ("Job", name)
    return kind, name


def containers(pod) -> list:
    """[(spec dict, is_init)] for every container in the pod spec."""
    spec = pod.get("spec") or {}
    return [(c, False) for c in spec.get("containers") or []] + [(c, True) for c in spec.get("initContainers") or []]


def container_spec(pod, name):
    for c, _ in containers(pod):
        if c.get("name") == name:
            return c
    return None


def all_statuses(pod) -> list:
    """Each container status with its spec and a JSON path prefix, init containers included."""
    st = pod.get("status") or {}
    out = []
    for key, init in (("containerStatuses", False), ("initContainerStatuses", True)):
        for s in st.get(key) or []:
            name = s.get("name", "")
            out.append({"status": s, "spec": container_spec(pod, name) or {}, "init": init,
                        "name": name, "path": f"status.{key}[{name}]",
                        "spec_path": f"spec.{'initContainers' if init else 'containers'}[{name}]"})
    return out


def waiting(status) -> dict:
    return ((status or {}).get("state") or {}).get("waiting") or {}


def terminated(state) -> dict:
    return (state or {}).get("terminated") or {}


def condition(pod, ctype) -> dict:
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return {}


def requests(pod) -> tuple:
    """Effective (millicores, bytes) request: sum over containers, or the largest init container if bigger."""
    spec = pod.get("spec") or {}

    def one(c):
        r = ((c.get("resources") or {}).get("requests")) or {}
        try:
            cpu = cpu_millicores(r["cpu"]) if "cpu" in r else 0
            mem = mem_bytes(r["memory"]) if "memory" in r else 0
        except ValueError:
            cpu = mem = 0
        return cpu, mem

    run = [one(c) for c in spec.get("containers") or []]
    init = [one(c) for c in spec.get("initContainers") or []]
    cpu = max([sum(x[0] for x in run)] + [x[0] for x in init])
    mem = max([sum(x[1] for x in run)] + [x[1] for x in init])
    return cpu, mem


def parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
