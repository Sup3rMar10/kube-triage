"""Hand-built HOLDOUT fixtures, written AFTER the rules were frozen (commit 395b153) and never used to adjust them.

Run: python tools/make_holdout_fixtures.py
Labels below were written before the first holdout run. Wording and shapes differ on purpose from the dev set.
(Fixtures captured from a real cluster are added separately by scripts/capture_fixtures.py.)
"""
from fixturelib import (cstatus, container, event, exp, http_probe, k8s_list, node, pod, replicaset_owner, running,
                        term, unscheduled_conditions, waiting, write_fixture)

NS = "payments"
IMG = "registry.example.com/payments/svc:3.2.1"


def scenario(name, doc, expected, note):
    write_fixture("holdout", name, doc, expected, source="hand", note=note)


def main():
    # 1. SIGTERM crash loop on a StatefulSet pod
    p = pod("queue-1", NS, owner={"kind": "StatefulSet", "name": "queue"},
            statuses=[cstatus("broker", IMG, state=waiting("CrashLoopBackOff", "back-off 40s restarting failed container=broker pod=queue-1_payments"),
                              last=term(143, finished="2026-10-01T10:00:31Z"), restarts=7)],
            containers=[container("broker", IMG)])
    scenario("crashloop-sigterm-143", k8s_list(p, node()), [exp("queue-1", "KT001", "exit-143")], "StatefulSet, SIGTERM")

    # 2. segfault
    p = pod("native-5c8b9d7f4-q2w3e", NS, owner=replicaset_owner("native", "5c8b9d7f4"),
            containers=[container("native", IMG)],
            statuses=[cstatus("native", IMG, state=waiting("CrashLoopBackOff"), last=term(139, reason="Error", finished="2026-10-01T10:00:01Z"), restarts=12)])
    scenario("crashloop-segfault-139", k8s_list(p, node()), [exp("native-5c8b9d7f4-q2w3e", "KT001", "exit-139")], "SIGSEGV")

    # 3. process finishes successfully under restartPolicy Always
    p = pod("cron-like-6d5c4b3a2-aa11b", NS, owner=replicaset_owner("cron-like", "6d5c4b3a2"),
            statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"), last=term(0, reason="Completed", finished="2026-10-01T10:00:01Z"), restarts=30)])
    scenario("crashloop-exit0", k8s_list(p, node()), [exp("cron-like-6d5c4b3a2-aa11b", "KT001", "exit-0")], "finishes and is restarted")

    # 4. liveness killing a container that is slow to answer: KT006 owns it, KT001 must stay quiet
    c = container("api", IMG, ports=(9000,), liveness=http_probe("/live", 9000, delay=20, timeout=1))
    p = pod("api-8f7e6d5c4-zz90a", NS, owner=replicaset_owner("api", "8f7e6d5c4"), containers=[c],
            statuses=[cstatus("api", IMG, state=waiting("CrashLoopBackOff"), last=term(137), restarts=11)])
    evs = [event(p["metadata"]["name"], NS, "Unhealthy",
                 'Liveness probe failed: Get "http://10.244.2.9:9000/live": context deadline exceeded (Client.Timeout exceeded while awaiting headers)',
                 count=33, field_path="spec.containers{api}"),
           event(p["metadata"]["name"], NS, "Killing", "Container api failed liveness probe, will be restarted", type_="Normal", count=11,
                 field_path="spec.containers{api}")]
    scenario("liveness-timeout-killing", k8s_list(p, evs, node()), [exp(p["metadata"]["name"], "KT006", "timeout")], "probe kills a slow container")

    # 5. OOM with no limit
    c = container("worker", IMG, limits=None, requests={"memory": "64Mi"})
    p = pod("worker-3a2b1c0d9-m1n2o", NS, owner=replicaset_owner("worker", "3a2b1c0d9"), containers=[c],
            statuses=[cstatus("worker", IMG, ready=True, last=term(137, reason="OOMKilled"), restarts=3)])
    scenario("oom-no-limit", k8s_list(p, node()),
             [exp(p["metadata"]["name"], "KT003", "no-limit"), exp(p["metadata"]["name"], "KT009", "no-memory-limit")], "OOM kill without a limit")

    # 6. invalid image name
    bad = "Registry.Example.com/Payments/API:2.0"
    p = pod("shouty-4b3a29180-x9y8z", NS, owner=replicaset_owner("shouty", "4b3a29180"), phase="Pending", containers=[container("app", bad)],
            statuses=[cstatus("app", bad, state=waiting("InvalidImageName",
                      f'Failed to apply default image tag "{bad}": couldn\'t parse image name "{bad}": invalid reference format: repository name (Payments/API) must be lowercase'))])
    scenario("imagepull-invalid-name", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT002", "invalid-image-name")], "uppercase repo")

    # 7. mixed scheduling reasons: most nodes are tainted, one lacks memory
    msg = ("0/6 nodes are available: 1 Insufficient memory, 5 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }. "
           "preemption: 0/6 nodes are available: 1 No preemption victims found for incoming pod, 5 Preemption is not helpful for scheduling.")
    p = pod("analytics-7a6b5c4d3-k1k2k", NS, owner=replicaset_owner("analytics", "7a6b5c4d3"), phase="Pending", node=None, statuses=[],
            containers=[container("app", IMG, requests={"memory": "4Gi", "cpu": "500m"})], conditions=unscheduled_conditions(msg))
    nodes = [node("cp-1", cpu="4", memory="16Gi", taints=[{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]),
             node("w-1", cpu="4", memory="16Gi")]
    scenario("pending-mixed-reasons", k8s_list(p, event(p["metadata"]["name"], NS, "FailedScheduling", msg, count=3), nodes),
             [exp(p["metadata"]["name"], "KT004", "taint")], "taint dominates a mixed message")

    # 8. whole ConfigMap missing, referenced with envFrom
    p = pod("flags-2c1b0a9f8-r5r6r", NS, owner=replicaset_owner("flags", "2c1b0a9f8"), phase="Pending",
            containers=[container("app", IMG, env_from=[{"configMapRef": {"name": "feature-flags"}}])],
            statuses=[cstatus("app", IMG, state=waiting("CreateContainerConfigError", 'configmap "feature-flags" not found'))])
    scenario("config-missing-configmap", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT005", "missing-configmap")], "whole ConfigMap absent")

    # 9. Secret volume missing: pod stuck ContainerCreating, found only through a FailedMount event
    p = pod("web-1d0c9b8a7-t7t8t", NS, owner=replicaset_owner("web", "1d0c9b8a7"), phase="Pending",
            volumes=[{"name": "tls", "secret": {"secretName": "web-tls"}}],
            statuses=[cstatus("app", IMG, state=waiting("ContainerCreating"))])
    e = event(p["metadata"]["name"], NS, "FailedMount", 'MountVolume.SetUp failed for volume "tls" : secret "web-tls" not found', count=9)
    scenario("failedmount-secret", k8s_list(p, e, node()), [exp(p["metadata"]["name"], "KT005", "missing-secret")], "volume Secret absent")

    # 10. evicted for ephemeral storage
    p = pod("batch-9e8d7c6b5-u3u4u", NS, owner=replicaset_owner("batch", "9e8d7c6b5"), phase="Failed", reason="Evicted", statuses=[],
            message="The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 4%. Container worker was using 8Gi, request is 0, has larger consumption of ephemeral-storage than request.")
    scenario("evicted-ephemeral-storage", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT007", "ephemeral-storage")], "ephemeral storage eviction")

    # 11. node under memory pressure
    scenario("node-memory-pressure", k8s_list(node("w-1"), node("w-2", pressure=("memory",))),
             [exp("w-2", "KT010", "memory-pressure")], "kubelet reports MemoryPressure")

    # 12. healthy and tidy, including a finished Job pod that would trip the hygiene rules if it were running
    ps = [pod("ledger-0", NS, owner={"kind": "StatefulSet", "name": "ledger"}, containers=[container("app", "registry.example.com/payments/ledger@sha256:" + "c" * 64)],
              statuses=[cstatus("app", "registry.example.com/payments/ledger@sha256:" + "c" * 64, ready=True)]),
          pod("agent-x1y2z", "kube-system", owner={"kind": "DaemonSet", "name": "agent"}, containers=[container("agent", "registry.example.com/ops/agent:5.0", limits=("64Mi",))],
              statuses=[cstatus("agent", "registry.example.com/ops/agent:5.0", ready=True)]),
          pod("backup-28765432-abcde", NS, owner={"kind": "Job", "name": "backup-28765432"}, phase="Succeeded",
              containers=[container("backup", "registry.example.com/ops/backup:latest", limits=None)],
              statuses=[cstatus("backup", "registry.example.com/ops/backup:latest", state=term(0, reason="Completed"))])]
    scenario("healthy-mixed-owners", k8s_list(ps, node()), [], "no findings, several owner kinds, a completed Job pod")

    # 13. dependency is down: readiness gets a 503
    c = container("app", IMG, ports=(8443,), readiness=http_probe("/ready", 8443))
    p = pod("gateway-0b9a8c7d6-v5v6v", NS, owner=replicaset_owner("gateway", "0b9a8c7d6"), containers=[c], statuses=[cstatus("app", IMG, ready=False, started=True)])
    e = event(p["metadata"]["name"], NS, "Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 503", count=240, field_path="spec.containers{app}")
    scenario("readiness-503", k8s_list(p, e, node()), [exp(p["metadata"]["name"], "KT006", "app-unhealthy")], "5xx from the health endpoint")

    # 14. a rollout where only the new ReplicaSet's pod cannot pull
    bad = "docker.io/payments/ap1:1.2"
    good = [pod(f"billing-6a5b4c3d2-{s}", NS, owner=replicaset_owner("billing", "6a5b4c3d2"), statuses=[cstatus("app", IMG, ready=True)]) for s in ("aaaaa", "bbbbb")]
    new = pod("billing-7b6c5d4e3-ccccc", NS, owner=replicaset_owner("billing", "7b6c5d4e3"), phase="Pending", containers=[container("app", bad)],
              statuses=[cstatus("app", bad, state=waiting("ErrImagePull", f'rpc error: code = Unknown desc = failed to pull and unpack image "{bad}": failed to resolve reference "{bad}": '
                                "pull access denied, repository does not exist or may require authorization: server message: insufficient_scope: authorization failed"))])
    scenario("rollout-one-bad-pod", k8s_list(good, new, node()), [exp(new["metadata"]["name"], "KT002", "repo-or-auth")], "only the new pod is flagged")

    # 15. hostPort clash
    msg = "0/2 nodes are available: 2 node(s) didn't have free ports for the requested pod ports. preemption: 0/2 nodes are available: 2 No preemption victims found for incoming pod."
    p = pod("proxy-5f4e3d2c1-w1w2w", NS, owner=replicaset_owner("proxy", "5f4e3d2c1"), phase="Pending", node=None, statuses=[], conditions=unscheduled_conditions(msg))
    scenario("pending-hostport", k8s_list(p, node("a"), node("b")), [exp(p["metadata"]["name"], "KT004", "no-free-ports")], "hostPort taken everywhere")

    # 16. init container crash loop
    p = pod("app-4d3c2b1a0-i1i2i", NS, owner=replicaset_owner("app", "4d3c2b1a0"), phase="Pending", statuses=[],
            init_containers=[container("migrate", IMG)],
            init_statuses=[cstatus("migrate", IMG, state=waiting("CrashLoopBackOff", "back-off 1m20s restarting failed container=migrate pod=app"), last=term(1), restarts=4)])
    scenario("init-container-crashloop", k8s_list(p, node()), [exp(p["metadata"]["name"], "KT001", "exit-1")], "Init:CrashLoopBackOff")

    # 17. liveness with no initial delay on a slow starter
    c = container("app", IMG, ports=(8080,), liveness=http_probe("/healthz", 8080))
    p = pod("slowstart-3c2b1a0f9-s1s2s", NS, owner=replicaset_owner("slowstart", "3c2b1a0f9"), containers=[c],
            statuses=[cstatus("app", IMG, ready=False, last=term(137), restarts=6)])
    e = event(p["metadata"]["name"], NS, "Unhealthy", 'Liveness probe failed: Get "http://10.244.3.4:8080/healthz": dial tcp 10.244.3.4:8080: connect: connection refused',
              count=18, field_path="spec.containers{app}")
    scenario("liveness-no-initial-delay", k8s_list(p, e, node()), [exp(p["metadata"]["name"], "KT006", "initial-delay-too-short")], "slow starter killed early")


if __name__ == "__main__":
    main()
