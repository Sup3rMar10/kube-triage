"""One behaviour per test, built from small hand-made pods (see tools/fixturelib.py)."""
import pytest

from fixturelib import (container, cstatus, event, http_probe, k8s_list, node, pod, replicaset_owner, running, term,
                        unscheduled_conditions, waiting)
from kubetriage.engine import diagnose
from kubetriage.model import workload_of

NS = "t"
IMG = "registry.example.com/app:1.0"


def run(*items, min_severity="info"):
    return diagnose(k8s_list(*items), min_severity=min_severity).findings


def rules_of(findings):
    return sorted((f.rule, f.variant) for f in findings)


def crash(code, reason="Error", **kw):
    return pod("p", NS, statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"), last=term(code, reason=reason), restarts=5)], **kw)


# ---- KT001 -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("code", [0, 1, 2, 126, 127, 137, 139, 143, 255])
def test_crashloop_known_exit_codes(code):
    (f,) = run(crash(code))
    assert (f.rule, f.variant) == ("KT001", f"exit-{code}")
    assert f"exitCode = {code}" in " ".join(f.evidence)


def test_crashloop_unknown_exit_code_still_reported():
    (f,) = run(crash(42))
    assert f.variant == "exit-42"


def test_crashloop_without_last_state_is_unknown():
    p = pod("p", NS, statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"), restarts=1)])
    (f,) = run(p)
    assert f.variant == "unknown"


def test_crashloop_defers_to_oom():
    assert rules_of(run(crash(137, reason="OOMKilled"))) == [("KT003", "limit-set")]


def test_crashloop_defers_to_liveness_probe():
    p = pod("p", NS, containers=[container("app", IMG, liveness=http_probe("/h", 8080, delay=1))],
            statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"), last=term(137), restarts=5)])
    e = event("p", NS, "Unhealthy", 'Liveness probe failed: Get "http://10.0.0.1:8080/h": dial tcp 10.0.0.1:8080: connect: connection refused',
              field_path="spec.containers{app}")
    assert [f.rule for f in run(p, e)] == ["KT006"]


def test_crashloop_in_init_container():
    p = pod("p", NS, phase="Pending", init_containers=[container("migrate", IMG)],
            init_statuses=[cstatus("migrate", IMG, state=waiting("CrashLoopBackOff"), last=term(1), restarts=3)], statuses=[])
    (f,) = run(p)
    assert f.rule == "KT001" and f.container == "migrate"


def looping_between_restarts(code=1, restarts=5, policy="Always", phase="Running", reason="Error"):
    """A crash loop caught while the container is in its `terminated` phase (found on a real cluster)."""
    return pod("p", NS, phase=phase, restart_policy=policy,
               statuses=[cstatus("app", IMG, state=term(code, reason=reason), last=term(code), restarts=restarts)])


def test_crash_loop_caught_in_its_terminated_phase():
    (f,) = run(looping_between_restarts(1))
    assert (f.rule, f.variant) == ("KT001", "exit-1")
    assert any(".state.terminated.exitCode = 1" in e for e in f.evidence)
    assert not any("waiting.reason" in e for e in f.evidence)           # it is not in the waiting phase


@pytest.mark.parametrize("kwargs", [
    dict(restarts=1),                                  # one crash is not a loop yet
    dict(policy="Never"),                              # nothing will restart it
    dict(policy="OnFailure", code=0, reason="Completed"),   # finished successfully, will not restart
    dict(phase="Succeeded"), dict(phase="Failed"),
])
def test_terminated_container_is_not_called_a_loop_without_evidence(kwargs):
    assert not [f for f in run(looping_between_restarts(**kwargs)) if f.rule == "KT001"]


def test_terminated_phase_oom_goes_to_kt003_not_kt001():
    assert rules_of(run(looping_between_restarts(137, reason="OOMKilled"))) == [("KT003", "limit-set")]


# ---- KT003 -----------------------------------------------------------------------------------------

def test_oom_reports_limit_and_suggests_more():
    p = pod("p", NS, containers=[container("app", IMG, limits=("128Mi",))],
            statuses=[cstatus("app", IMG, last=term(137, reason="OOMKilled"), restarts=2, ready=True)])
    (f,) = run(p)
    assert f.variant == "limit-set"
    assert "128Mi" in f.title and any("256Mi" in x for x in f.fix)


def test_oom_without_limit_is_a_different_variant():
    p = pod("p", NS, containers=[container("app", IMG, limits=None, requests={"memory": "64Mi"})],
            statuses=[cstatus("app", IMG, last=term(137, reason="OOMKilled"), restarts=2, ready=True)])
    assert ("KT003", "no-limit") in rules_of(run(p))


START_OOM = ("failed to create containerd task: failed to create shim task: OCI runtime create failed: runc create failed: "
             "unable to start container process: container init was OOM-killed (memory limit too low?)")


def test_limit_so_low_the_container_cannot_start():
    """Real message from a kind cluster: a 24Mi limit on busybox is below what the runtime needs to set it up."""
    p = pod("p", NS, containers=[container("app", IMG, limits=("24Mi",))],
            statuses=[cstatus("app", IMG, state=waiting("RunContainerError", START_OOM),
                              last=term(128, reason="StartError", message=START_OOM), restarts=5)])
    assert rules_of(run(p)) == [("KT003", "limit-too-low-to-start")]


def test_start_oom_is_not_also_reported_as_a_generic_crash_loop():
    p = pod("p", NS, statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"),
                                       last=term(128, reason="StartError", message=START_OOM), restarts=5)])
    assert [f.rule for f in run(p)] == ["KT003"]


def test_oom_in_current_state():
    p = pod("p", NS, phase="Failed", statuses=[cstatus("app", IMG, state=term(137, reason="OOMKilled"))])
    assert ("KT003", "limit-set") in rules_of(run(p))


# ---- KT002 -----------------------------------------------------------------------------------------

def pull(reason, wait_msg, ev_msg=None, image=IMG, secrets=None):
    p = pod("p", NS, phase="Pending", image_pull_secrets=secrets, containers=[container("app", image)],
            statuses=[cstatus("app", image, state=waiting(reason, wait_msg))])
    items = [p] + ([event("p", NS, "Failed", ev_msg, field_path="spec.containers{app}")] if ev_msg else [])
    return [f for f in run(*items) if f.rule == "KT002"]


@pytest.mark.parametrize("message,variant", [
    ("manifest for x:9 not found: manifest unknown", "tag-not-found"),
    ("rpc error: code = NotFound desc = failed to resolve reference: x:9: not found", "tag-not-found"),
    ("pull access denied, repository does not exist or may require authorization", "repo-or-auth"),
    ("failed to authorize: 401 Unauthorized", "auth"),
    ("unauthorized: authentication required", "auth"),
    ("dial tcp: lookup nope.example on 10.96.0.10:53: no such host", "registry-unreachable"),
    ("Get https://r.example/v2/: net/http: TLS handshake timeout", "registry-unreachable"),
    ("toomanyrequests: You have reached your pull rate limit", "rate-limited"),
    ("something nobody has seen before", "unknown"),
])
def test_image_pull_classification(message, variant):
    (f,) = pull("ErrImagePull", message)
    assert f.variant == variant


def test_image_pull_uses_event_text_when_waiting_message_is_bare():
    (f,) = pull("ImagePullBackOff", 'Back-off pulling image "x"', "Failed to pull image: manifest unknown")
    assert f.variant == "tag-not-found"


def test_invalid_image_name_and_never_pull():
    assert pull("InvalidImageName", "Failed to apply default image tag")[0].variant == "invalid-image-name"
    assert pull("ErrImageNeverPull", "Container image is not present with pull policy of Never")[0].variant == "never-pull-policy"


def test_pull_secret_hint_only_when_missing():
    with_secret = pull("ErrImagePull", "401 Unauthorized", secrets=["regcred"])[0]
    without = pull("ErrImagePull", "401 Unauthorized")[0]
    assert "spec.imagePullSecrets is not set" in without.evidence
    assert "spec.imagePullSecrets is not set" not in with_secret.evidence


# ---- KT004 -----------------------------------------------------------------------------------------

def pending(message, requests=None, nodes=None, node_name=None, **kw):
    p = pod("p", NS, phase="Pending", node=node_name, statuses=[], conditions=unscheduled_conditions(message),
            containers=[container("app", IMG, requests=requests)], **kw)
    return run(p, *(nodes if nodes is not None else [node("n1", cpu="4", memory="8Gi")]))


def kt004(findings):
    return [f for f in findings if f.rule == "KT004"]


def test_primary_reason_is_the_one_affecting_most_nodes():
    (f,) = kt004(pending("0/5 nodes are available: 1 Insufficient memory, 4 node(s) had untolerated taint {a: b}. "
                         "preemption: 0/5 nodes are available: 5 Preemption is not helpful for scheduling."))
    assert f.variant == "taint" and "insufficient-memory" in f.cause


def test_preemption_tail_is_ignored():
    (f,) = kt004(pending("0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector. "
                         "preemption: 0/2 nodes are available: 2 Insufficient cpu."))
    assert f.variant == "node-selector"


def test_cpu_full_vs_exceeds_node():
    msg = "0/1 nodes are available: 1 Insufficient cpu."
    assert kt004(pending(msg, requests={"cpu": "2"}))[0].variant == "insufficient-cpu"
    assert kt004(pending(msg, requests={"cpu": "8"}))[0].variant == "cpu-exceeds-node-capacity"


def test_memory_exceeds_node():
    f = kt004(pending("0/1 nodes are available: 1 Insufficient memory.", requests={"memory": "32Gi"}))[0]
    assert f.variant == "memory-exceeds-node-capacity" and "8Gi" in " ".join(f.evidence)


def test_without_node_data_falls_back_to_insufficient_cpu():
    f = kt004(pending("0/1 nodes are available: 1 Insufficient cpu.", requests={"cpu": "64"}, nodes=[]))[0]
    assert f.variant == "insufficient-cpu"


@pytest.mark.parametrize("message,variant", [
    ('0/3 nodes are available: persistentvolumeclaim "d" not found.', "unbound-pvc"),
    ("0/3 nodes are available: pod has unbound immediate PersistentVolumeClaims.", "unbound-pvc"),
    ("0/3 nodes are available: 3 node(s) didn't have free ports for the requested pod ports.", "no-free-ports"),
    ("0/3 nodes are available: 3 Too many pods.", "too-many-pods"),
    ("0/3 nodes are available: 3 node(s) didn't match pod anti-affinity rules.", "pod-affinity"),
    ("0/3 nodes are available: 3 node(s) had volume node affinity conflict.", "volume-node-affinity"),
    ("0/3 nodes are available: 3 node(s) were unschedulable.", "cordoned-nodes"),
    ("0/3 nodes are available: something new.", "unknown"),
])
def test_scheduler_message_variants(message, variant):
    assert kt004(pending(message))[0].variant == variant


def test_scheduler_event_is_used_when_condition_has_no_message():
    p = pod("p", NS, phase="Pending", node=None, statuses=[], conditions=[{"type": "PodScheduled", "status": "False"}])
    e = event("p", NS, "FailedScheduling", "0/1 nodes are available: 1 Insufficient cpu.")
    assert kt004(run(p, e, node()))[0].variant == "insufficient-cpu"


def test_bound_or_running_pods_are_never_unschedulable():
    assert not kt004(pending("0/1 nodes are available: 1 Insufficient cpu.", node_name="n1"))
    p = pod("q", NS, statuses=[cstatus("app", IMG, ready=True)])
    assert not kt004(run(p, event("q", NS, "FailedScheduling", "0/1 nodes are available: 1 Insufficient cpu.")))


# ---- KT005 -----------------------------------------------------------------------------------------

def cfg_pod(message, **kw):
    return pod("p", NS, phase="Pending", statuses=[cstatus("app", IMG, state=waiting("CreateContainerConfigError", message))], **kw)


@pytest.mark.parametrize("message,variant", [
    ("couldn't find key K in ConfigMap t/app-config", "missing-configmap-key"),
    ("couldn't find key K in Secret t/db", "missing-secret-key"),
    ('configmap "app-config" not found', "missing-configmap"),
    ('secret "db" not found', "missing-secret"),
    ("something else is wrong", "config-error"),
])
def test_config_error_variants(message, variant):
    (f,) = [x for x in run(cfg_pod(message)) if x.rule == "KT005"]
    assert f.variant == variant


def test_config_error_cites_the_env_var_that_references_it():
    p = cfg_pod('secret "db" not found', containers=[container("app", IMG, env=[
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}}])])
    (f,) = [x for x in run(p) if x.rule == "KT005"]
    assert any("env[DB_PASSWORD].valueFrom.secretKeyRef" in e for e in f.evidence)


def test_failed_mount_event_for_missing_secret_volume():
    p = pod("p", NS, phase="Pending", statuses=[cstatus("app", IMG, state=waiting("ContainerCreating"))],
            volumes=[{"name": "creds", "secret": {"secretName": "tls-cert"}}])
    e = event("p", NS, "FailedMount", 'MountVolume.SetUp failed for volume "creds" : secret "tls-cert" not found')
    (f,) = [x for x in run(p, e) if x.rule == "KT005"]
    assert f.variant == "missing-secret" and any("spec.volumes[creds].secret" in x for x in f.evidence)


def test_config_error_and_mount_event_do_not_double_report():
    p = cfg_pod('secret "db" not found')
    e = event("p", NS, "FailedMount", 'MountVolume.SetUp failed: secret "db" not found')
    assert len([x for x in run(p, e) if x.rule == "KT005"]) == 1


# ---- KT006 -----------------------------------------------------------------------------------------

def probe_case(ptype, message, probe, restarts=0, ports=(8080,), startup=None):
    key = {"liveness": "liveness", "readiness": "readiness", "startup": "startup"}[ptype]
    c = container("app", IMG, ports=ports, **{key: probe}, **({"startup": startup} if startup else {}))
    p = pod("p", NS, containers=[c], statuses=[cstatus("app", IMG, ready=False, restarts=restarts)])
    e = event("p", NS, "Unhealthy", f"{ptype.capitalize()} probe failed: {message}", count=20, field_path="spec.containers{app}")
    return [f for f in run(p, e) if f.rule == "KT006"]


CONN = 'Get "http://10.0.0.1:{port}/x": dial tcp 10.0.0.1:{port}: connect: connection refused'


def test_wrong_port_is_found_by_comparing_with_declared_ports():
    (f,) = probe_case("readiness", CONN.format(port=80), http_probe("/x", 80))
    assert f.variant == "wrong-port" and f.severity == "medium"


def test_wrong_path_and_unhealthy_app():
    assert probe_case("readiness", "HTTP probe failed with statuscode: 404", http_probe())[0].variant == "wrong-path"
    assert probe_case("readiness", "HTTP probe failed with statuscode: 503", http_probe())[0].variant == "app-unhealthy"
    assert probe_case("readiness", "HTTP probe failed with statuscode: 418", http_probe())[0].variant == "http-status"


def test_liveness_with_short_delay_and_restarts_suggests_slow_start():
    (f,) = probe_case("liveness", CONN.format(port=8080), http_probe("/x", 8080, delay=0), restarts=4)
    assert f.variant == "initial-delay-too-short" and f.severity == "high"


def test_a_startup_probe_rules_out_the_short_delay_diagnosis():
    (f,) = probe_case("liveness", CONN.format(port=8080), http_probe("/x", 8080, delay=0), restarts=4,
                      startup=http_probe("/x", 8080))
    assert f.variant == "connection-refused"


def test_timeout_and_exec_failures():
    assert probe_case("liveness", "Get \"http://10.0.0.1:8080/x\": context deadline exceeded (Client.Timeout exceeded)",
                      http_probe("/x", 8080, delay=60))[0].variant == "timeout"
    ex = {"exec": {"command": ["/bin/check"]}}
    assert probe_case("readiness", 'OCI runtime exec failed: exec failed: unable to start container process: exec: "/bin/check": no such file or directory',
                      ex)[0].variant == "exec-failed"


def test_named_probe_port_is_resolved_through_container_ports():
    c = container("app", IMG, ports=(8080,), readiness=http_probe("/x", "http"))
    c["ports"][0]["name"] = "http"
    p = pod("p", NS, containers=[c], statuses=[cstatus("app", IMG, ready=False)])
    e = event("p", NS, "Unhealthy", "Readiness probe failed: " + CONN.format(port=8080), field_path="spec.containers{app}")
    (f,) = [x for x in run(p, e) if x.rule == "KT006"]
    assert f.variant == "connection-refused"        # port 8080 is declared, so not "wrong-port"


# ---- KT007 -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,variant", [
    ("The node was low on resource: memory. Container app was using 1Gi", "memory"),
    ("The node was low on resource: ephemeral-storage. Container app was using 2Gi", "ephemeral-storage"),
    ("The node had condition: [DiskPressure].", "disk"),
    ("The node was low on resource: pids.", "pid"),
])
def test_evicted_variants(message, variant):
    p = pod("p", NS, phase="Failed", reason="Evicted", message=message, statuses=[])
    (f,) = run(p)
    assert (f.rule, f.variant) == ("KT007", variant)


def test_failed_pod_that_is_not_evicted_is_not_flagged():
    assert not run(pod("p", NS, phase="Failed", reason="DeadlineExceeded", statuses=[]))


# ---- KT008 / KT009 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("image,variant", [
    ("nginx:latest", "latest"), ("nginx", "untagged"), ("localhost:5000/app", "untagged"),
    ("registry.example.com:5000/app:latest", "latest")])
def test_floating_tags_flagged(image, variant):
    p = pod("p", NS, containers=[container("app", image)], statuses=[cstatus("app", image, ready=True)])
    (f,) = [x for x in run(p) if x.rule == "KT008"]
    assert f.variant == variant


@pytest.mark.parametrize("image", ["nginx:1.27", "localhost:5000/app:2", "app@sha256:" + "a" * 64, "app:v1@sha256:" + "b" * 64])
def test_pinned_images_not_flagged(image):
    p = pod("p", NS, containers=[container("app", image)], statuses=[cstatus("app", image, ready=True)])
    assert not [x for x in run(p) if x.rule == "KT008"]


def test_missing_memory_limit_variants():
    with_req = pod("p", NS, containers=[container("app", IMG, limits=None, requests={"memory": "64Mi"})], statuses=[cstatus("app", IMG, ready=True)])
    bare = pod("q", NS, containers=[container("app", IMG, limits=None)], statuses=[cstatus("app", IMG, ready=True)], qos="BestEffort")
    assert ("KT009", "no-memory-limit") in rules_of(run(with_req))
    assert ("KT009", "best-effort") in rules_of(run(bare))


def test_hygiene_ignores_completed_pods_and_init_containers():
    done = pod("p", NS, phase="Succeeded", containers=[container("app", "x:latest", limits=None)], statuses=[])
    init_only = pod("q", NS, init_containers=[container("setup", "x:latest", limits=None)], statuses=[cstatus("app", IMG, ready=True)])
    assert not [f for f in run(done, init_only) if f.rule == "KT009"]
    assert [f for f in run(init_only) if f.rule == "KT008"]      # a floating init image is still a floating image


# ---- KT010 -----------------------------------------------------------------------------------------

def test_node_conditions():
    assert rules_of(run(node("n", ready=False))) == [("KT010", "not-ready")]
    assert rules_of(run(node("n", pressure=("memory", "disk")))) == [("KT010", "disk-pressure"), ("KT010", "memory-pressure")]
    assert rules_of(run(node("n", unschedulable=True))) == [("KT010", "cordoned")]
    assert not run(node("n"))


# ---- ownership -------------------------------------------------------------------------------------

def test_workload_of():
    assert workload_of(pod("web-7d9f8c6b5-abcde", NS, owner=replicaset_owner("web", "7d9f8c6b5"))) == ("Deployment", "web")
    assert workload_of(pod("db-0", NS, owner={"kind": "StatefulSet", "name": "db"})) == ("StatefulSet", "db")
    assert workload_of(pod("agent-xk2lp", NS, owner={"kind": "DaemonSet", "name": "agent"})) == ("DaemonSet", "agent")
    assert workload_of(pod("nightly-28765432-zzz", NS, owner={"kind": "Job", "name": "nightly-28765432"})) == ("CronJob", "nightly")
    assert workload_of(pod("migrate-abc", NS, owner={"kind": "Job", "name": "migrate"})) == ("Job", "migrate")
    assert workload_of(pod("debug", NS)) == ("Pod", "debug")


def test_events_are_matched_by_namespace_and_pod_name():
    p = pod("p", "a", containers=[container("app", IMG, readiness=http_probe("/x", 80))], statuses=[cstatus("app", IMG, ready=False)])
    other_ns_event = event("p", "b", "Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 404", field_path="spec.containers{app}")
    assert not [f for f in run(p, other_ns_event) if f.rule == "KT006"]
