"""Hand-built DEV fixtures. These were written alongside the rules and are what the rules were adjusted against.

Run: python tools/make_dev_fixtures.py     (rewrites fixtures/dev/*.json and their entries in fixtures/manifest.json)
Expected labels are written by hand from what each scenario is meant to show.
"""
from fixturelib import (T0, cstatus, container, event, exp, http_probe, k8s_list, node, pod, replicaset_owner,
                        running, term, unscheduled_conditions, waiting, write_fixture)

NS = "shop"
IMG = "registry.example.com/shop/app:1.4.2"
HASH = "7d9f8c6b5"


def dep_pod(deploy, suffix, **kw):
    return pod(f"{deploy}-{HASH}-{suffix}", NS, owner=replicaset_owner(deploy, HASH), **kw)


def scenario(name, doc, expected, note):
    write_fixture("dev", name, doc, expected, source="hand", note=note)


def main():
    nodes3 = [node(f"worker-{i}", cpu="4", memory="16Gi") for i in (1, 2, 3)]

    # 1. crash loop, exit 1, two replicas of one Deployment
    pods, evs, want = [], [], []
    for sfx in ("x2k4p", "m8q7z"):
        p = dep_pod("api", sfx, phase="Running", statuses=[cstatus("app", IMG, state=waiting(
            "CrashLoopBackOff", "back-off 2m40s restarting failed container=app pod=api"), last=term(1), restarts=6)])
        pods.append(p)
        evs.append(event(p["metadata"]["name"], NS, "BackOff", "Back-off restarting failed container app in pod " + p["metadata"]["name"],
                         count=14, field_path="spec.containers{app}"))
        want.append(exp(p["metadata"]["name"], "KT001", "exit-1"))
    scenario("crashloop-exit1", k8s_list(pods, evs, node()), want, "Deployment with 2 replicas, both exit 1")

    # 2. exit 127: command not found
    p = pod("worker-0", NS, owner={"kind": "StatefulSet", "name": "worker"}, statuses=[cstatus(
        "app", IMG, state=waiting("CrashLoopBackOff", "back-off 1m20s restarting failed container=app pod=worker-0"),
        last=term(127, message="sh: 1: migrate-db: not found"), restarts=4)])
    scenario("crashloop-exit127", k8s_list(p, node()), [exp("worker-0", "KT001", "exit-127")], "StatefulSet pod, command not found")

    # 3. OOMKilled while crash-looping: only KT003 should fire (KT001 defers to it)
    p = dep_pod("report", "q9w8e", statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff", "back-off 5m0s restarting failed container=app pod=report"),
                                                       last=term(137, reason="OOMKilled"), restarts=9)],
                containers=[container("app", IMG, limits=("128Mi",))])
    scenario("oom-crashloop", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT003", "limit-set")], "OOM then backoff: KT003 only")

    # 4. OOMKilled earlier, running now
    p = dep_pod("search", "t5y6u", statuses=[cstatus("app", IMG, ready=True, state=running(), last=term(137, reason="OOMKilled"), restarts=4)],
                containers=[container("app", IMG, limits=("512Mi",))])
    scenario("oom-running-restarts", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT003", "limit-set")], "Running again after OOM kills")

    # 5-7. image pull variants
    def pull(name, image, wait_msg, ev_msg, variant, secrets=None):
        p = dep_pod(name, "p1l2m", phase="Pending", containers=[container("app", image)], image_pull_secrets=secrets,
                    statuses=[cstatus("app", image, state=waiting("ImagePullBackOff", wait_msg))])
        e = event(p["metadata"]["name"], NS, "Failed", ev_msg, count=3, field_path="spec.containers{app}")
        scenario(f"imagepull-{variant}", k8s_list(p, e, node()), [exp(p["metadata"]["name"], "KT002", variant)], variant)

    pull("checkout", "registry.example.com/shop/checkout:9.9.9",
         'Back-off pulling image "registry.example.com/shop/checkout:9.9.9"',
         'Failed to pull image "registry.example.com/shop/checkout:9.9.9": rpc error: code = Unknown desc = Error response from daemon: '
         "manifest for registry.example.com/shop/checkout:9.9.9 not found: manifest unknown: manifest unknown", "tag-not-found")
    pull("billing", "registry.example.com/private/billing:1.0",
         'Back-off pulling image "registry.example.com/private/billing:1.0"',
         'Failed to pull image "registry.example.com/private/billing:1.0": failed to authorize: failed to fetch anonymous token: '
         "unexpected status from GET request: 401 Unauthorized", "auth")
    pull("mailer", "registry.exmaple.com/shop/mailer:3.1",
         'Back-off pulling image "registry.exmaple.com/shop/mailer:3.1"',
         'Failed to pull image "registry.exmaple.com/shop/mailer:3.1": failed to resolve reference: failed to do request: '
         "Head \"https://registry.exmaple.com/v2/shop/mailer/manifests/3.1\": dial tcp: lookup registry.exmaple.com on 10.96.0.10:53: no such host",
         "registry-unreachable")

    # 8-9. unschedulable on CPU: cluster full vs. bigger than any node
    def pending(name, label, message, requests=None, volumes=None, selector=None, tolerations=None, want_variant=None, nodes=None):
        p = dep_pod(name, "k3j4h", phase="Pending", node=None, statuses=[],
                    containers=[container("app", IMG, requests=requests)], volumes=volumes, node_selector=selector,
                    tolerations=tolerations, conditions=unscheduled_conditions(message))
        e = event(p["metadata"]["name"], NS, "FailedScheduling", message, count=8)
        scenario(label, k8s_list(p, e, nodes if nodes is not None else nodes3), [exp(p["metadata"]["name"], "KT004", want_variant)], label)

    pending("ingest", "pending-cpu-full",
            "0/3 nodes are available: 3 Insufficient cpu. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod.",
            requests={"cpu": "2", "memory": "1Gi"}, want_variant="insufficient-cpu")
    pending("trainer", "pending-cpu-too-big",
            "0/3 nodes are available: 3 Insufficient cpu. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod.",
            requests={"cpu": "16", "memory": "8Gi"}, want_variant="cpu-exceeds-node-capacity")
    pending("gpu-job", "pending-taint",
            "0/3 nodes are available: 3 node(s) had untolerated taint {dedicated: gpu}. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.",
            want_variant="taint", nodes=[node(f"gpu-{i}", taints=[{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]) for i in (1, 2, 3)])
    pending("cache", "pending-nodeselector",
            "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.",
            selector={"disktype": "ssd"}, want_variant="node-selector")
    pending("orders-db", "pending-pvc",
            '0/3 nodes are available: persistentvolumeclaim "orders-data" not found. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.',
            volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "orders-data"}}], want_variant="unbound-pvc")

    # 13-14. missing config
    p = dep_pod("web", "c4v5b", phase="Pending", statuses=[cstatus("app", IMG, state=waiting(
        "CreateContainerConfigError", "couldn't find key DB_HOST in ConfigMap shop/app-config"))],
        containers=[container("app", IMG, env=[{"name": "DB_HOST", "valueFrom": {"configMapKeyRef": {"name": "app-config", "key": "DB_HOST"}}}])])
    scenario("config-missing-key", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT005", "missing-configmap-key")], "key absent from ConfigMap")
    p = dep_pod("auth", "n6m7k", phase="Pending", statuses=[cstatus("app", IMG, state=waiting(
        "CreateContainerConfigError", 'secret "db-credentials" not found'))],
        containers=[container("app", IMG, env_from=[{"secretRef": {"name": "db-credentials"}}])])
    scenario("config-missing-secret", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT005", "missing-secret")], "Secret absent")

    # 15-16. probes
    p = dep_pod("portal", "r8t9y", statuses=[cstatus("app", IMG, ready=False, started=True, state=running())],
                containers=[container("app", IMG, ports=(8080,), readiness=http_probe("/ready", 80, delay=5))])
    e = event(p["metadata"]["name"], NS, "Unhealthy",
              'Readiness probe failed: Get "http://10.244.1.7:80/ready": dial tcp 10.244.1.7:80: connect: connection refused',
              count=120, field_path="spec.containers{app}")
    scenario("readiness-wrong-port", k8s_list(p, e, node()), [exp(p["metadata"]["name"], "KT006", "wrong-port")], "probe port not declared")
    p = dep_pod("catalog", "u1i2o", statuses=[cstatus("app", IMG, ready=True, state=running(), last=term(137), restarts=5)],
                containers=[container("app", IMG, ports=(8080,), liveness=http_probe("/healthz", 8080, delay=30))])
    evs = [event(p["metadata"]["name"], NS, "Unhealthy", "Liveness probe failed: HTTP probe failed with statuscode: 404", count=15,
                 field_path="spec.containers{app}"),
           event(p["metadata"]["name"], NS, "Killing", "Container app failed liveness probe, will be restarted", type_="Normal", count=5,
                 field_path="spec.containers{app}")]
    scenario("liveness-wrong-path", k8s_list(p, evs, node()), [exp(p["metadata"]["name"], "KT006", "wrong-path")], "404 on liveness path")

    # 17. evicted
    p = dep_pod("batch", "e5v6c", phase="Failed", reason="Evicted", statuses=[],
                message="The node was low on resource: memory. Threshold quantity: 100Mi, available: 38Mi. Container app was using 1.2Gi, request is 256Mi, has larger consumption of memory than request.")
    scenario("evicted-memory", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT007", "memory")], "evicted for memory")

    # 18. hygiene only
    p = dep_pod("legacy", "h7g8f", statuses=[cstatus("app", "registry.example.com/shop/legacy:latest", ready=True)],
                containers=[container("app", "registry.example.com/shop/legacy:latest", limits=None, requests={"memory": "128Mi", "cpu": "100m"})])
    scenario("latest-no-limits", k8s_list(p, node()),
             [exp(p["metadata"]["name"], "KT008", "latest"), exp(p["metadata"]["name"], "KT009", "no-memory-limit")], "hygiene findings")

    # 19. node not ready
    p = dep_pod("web", "a1s2d", statuses=[cstatus("app", IMG, ready=True)], node="worker-2")
    scenario("node-notready", k8s_list(p, node("worker-1"), node("worker-2", ready=False)),
             [exp("worker-2", "KT010", "not-ready")], "one node stopped reporting")

    # 20. healthy
    ps = [dep_pod("web", s, statuses=[cstatus("app", IMG, ready=True, started=True)],
                  containers=[container("app", IMG, liveness=http_probe("/healthz", 8080, delay=10), readiness=http_probe("/ready", 8080))])
          for s in ("z1x2c", "v3b4n")]
    scenario("healthy", k8s_list(ps, nodes3), [], "nothing wrong: guards against false positives")


if __name__ == "__main__":
    main()
