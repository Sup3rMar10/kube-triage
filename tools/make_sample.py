"""Builds sample/shop.json, the namespace shown by `python -m kubetriage demo` and in the README.

Run: python tools/make_sample.py
"""
import json

from fixturelib import (ROOT, container, cstatus, event, k8s_list, node, pod, replicaset_owner, running, term,
                        unscheduled_conditions, waiting)

NS = "shop"
IMG = "registry.example.com/shop/{}:1.8.0"


def main():
    items = []
    # web: 3 replicas, two of them OOM-killed over and over
    for sfx, bad in (("a1b2c", True), ("d3e4f", True), ("g5h6i", False)):
        c = container("web", IMG.format("web"), limits=("128Mi",), requests={"memory": "96Mi", "cpu": "100m"})
        last = term(137, reason="OOMKilled") if bad else {}
        items.append(pod(f"web-6b7c8d9e0-{sfx}", NS, owner=replicaset_owner("web", "6b7c8d9e0"), containers=[c],
                         statuses=[cstatus("web", IMG.format("web"), ready=not bad or False, last=last, restarts=14 if bad else 0)]))
    # api: reads a key that is not in the ConfigMap
    c = container("api", IMG.format("api"), env=[{"name": "PAYMENTS_URL", "valueFrom": {"configMapKeyRef": {"name": "api-config", "key": "PAYMENTS_URL"}}}])
    items.append(pod("api-5a4b3c2d1-j7k8l", NS, owner=replicaset_owner("api", "5a4b3c2d1"), phase="Pending", containers=[c],
                     statuses=[cstatus("api", IMG.format("api"), state=waiting("CreateContainerConfigError", "couldn't find key PAYMENTS_URL in ConfigMap shop/api-config"))]))
    # worker: tag that was never pushed
    wimg = "registry.example.com/shop/worker:1.9.0"
    w = pod("worker-4c3d2e1f0-m9n0o", NS, owner=replicaset_owner("worker", "4c3d2e1f0"), phase="Pending", containers=[container("worker", wimg)],
            statuses=[cstatus("worker", wimg, state=waiting("ImagePullBackOff", f'Back-off pulling image "{wimg}"'))])
    items += [w, event(w["metadata"]["name"], NS, "Failed",
                       f'Failed to pull image "{wimg}": rpc error: code = NotFound desc = failed to pull and unpack image "{wimg}": failed to resolve reference "{wimg}": {wimg}: not found',
                       count=6, field_path="spec.containers{worker}")]
    # orders-db: claim does not exist
    msg = '0/2 nodes are available: persistentvolumeclaim "orders-data" not found. preemption: 0/2 nodes are available: 2 Preemption is not helpful for scheduling.'
    items.append(pod("orders-db-0", NS, owner={"kind": "StatefulSet", "name": "orders-db"}, phase="Pending", node=None, statuses=[],
                     volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "orders-data"}}],
                     containers=[container("db", "registry.example.com/shop/postgres:16.4")], conditions=unscheduled_conditions(msg)))
    # cache: healthy, but unpinned and unbounded
    items.append(pod("cache-3d2e1f0a9-p1q2r", NS, owner=replicaset_owner("cache", "3d2e1f0a9"),
                     containers=[container("cache", "redis:latest", limits=None, requests={"memory": "64Mi"})],
                     statuses=[cstatus("cache", "redis:latest", ready=True)]))
    items += [node("worker-1", cpu="4", memory="16Gi"), node("worker-2", cpu="4", memory="16Gi")]
    out = ROOT / "sample" / "shop.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(k8s_list(items), indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
