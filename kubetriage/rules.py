"""The diagnosis rules.

Each pod rule is a function (pod, cluster) -> list[Finding]. Rules only read the input JSON; they never
call the cluster. Every finding carries evidence lines quoted from that JSON, so a reader can check the
claim against `kubectl get -o json` themselves.
"""
from __future__ import annotations

import json
import re

from .findings import Finding
from .model import (all_statuses, condition, container_spec, containers, meta, parse_time, pod_name, pod_ns,
                    pod_phase, requests, terminated, waiting, workload_of)
from .quantity import fmt_bytes, fmt_cpu, mem_bytes

# id -> (short name, default severity, one-line description). The README table and `rules` command read this.
RULE_INFO = {
    "KT001": ("crash-loop", "high", "Container keeps exiting and is in CrashLoopBackOff; explains the exit code"),
    "KT002": ("image-pull", "high", "Image cannot be pulled: missing tag, auth or wrong repo, unreachable registry, bad name"),
    "KT003": ("oom-killed", "high", "Container was killed for exceeding its memory limit"),
    "KT004": ("unschedulable", "high", "Pod is Pending: capacity, taints, selectors, affinity or an unbound volume claim"),
    "KT005": ("config-missing", "high", "A ConfigMap or Secret (or a key in it) the pod needs does not exist"),
    "KT006": ("probe-failing", "medium", "Readiness, liveness or startup probe is failing; guesses wrong port, wrong path or slow start"),
    "KT007": ("evicted", "medium", "Pod was evicted by the kubelet under node pressure"),
    "KT008": ("floating-image-tag", "low", "Image uses :latest or no tag, so what runs can change without a deploy"),
    "KT009": ("no-memory-limit", "low", "Container has no memory limit, so one leak can take down the node"),
    "KT010": ("node-problem", "high", "Node is NotReady, under memory/disk/PID pressure, or cordoned"),
}


# ---- helpers -------------------------------------------------------------------------------------

def _q(value) -> str:
    return json.dumps(value, separators=(", ", ": "))


def _kv(path, value) -> str:
    return f"{path} = {_q(value)}"


def _short(text, n=300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _make(pod, rule, severity, title, cause, evidence, fix, variant=None, container=None) -> Finding:
    kind, name = workload_of(pod)
    return Finding(rule=rule, severity=severity, namespace=pod_ns(pod), workload=f"{kind}/{name}",
                   subject=pod_name(pod), title=title, cause=cause, evidence=evidence, fix=fix,
                   variant=variant, container=container)


def _limit(spec, resource):
    return ((spec.get("resources") or {}).get("limits") or {}).get(resource)


def _doubled_memory(limit_text):
    try:
        return fmt_bytes(mem_bytes(limit_text) * 2)
    except ValueError:
        return None


def _runtime_seconds(term):
    a, b = parse_time(term.get("startedAt")), parse_time(term.get("finishedAt"))
    return None if a is None or b is None else max(0.0, (b - a).total_seconds())


_PROBE_RE = re.compile(r"^(Liveness|Readiness|Startup) probe (?:failed|errored)", re.I)
_PROBE_KEY = {"liveness": "livenessProbe", "readiness": "readinessProbe", "startup": "startupProbe"}


def probe_failures(pod, cluster) -> dict:
    """{(container, 'liveness'|'readiness'|'startup'): [Event, ...]} from Unhealthy events."""
    out: dict = {}
    spec_containers = [c for c, init in containers(pod) if not init]
    for e in cluster.events_for(pod):
        m = _PROBE_RE.match(e.message or "")
        if e.reason != "Unhealthy" or not m:
            continue
        ptype = m.group(1).lower()
        cname = e.container
        if cname is None:
            cname = next((c["name"] for c in spec_containers if _PROBE_KEY[ptype] in c),
                         spec_containers[0]["name"] if spec_containers else "")
        out.setdefault((cname, ptype), []).append(e)
    return out


# ---- KT001 crash loop ----------------------------------------------------------------------------

_EXIT = {
    0: ("exit-0", "exited with code 0 (success), but restartPolicy is Always so the kubelet starts it again, "
                  "forever. The command finishes instead of running as a long-lived process.",
        ["Run a long-lived process, or move one-off work to a Job (restartPolicy: OnFailure or Never)."]),
    1: ("exit-1", "exited with code 1, the generic application error: the process started and then failed on "
                  "its own, usually on bad config, a missing dependency or an unhandled exception.",
        ["Read the last words of the crashed container: kubectl logs {pod} -c {c} -n {ns} --previous"]),
    2: ("exit-2", "exited with code 2, which is usually a command-line usage error (bad flags or arguments).",
        ["Check spec command/args against the image's documented flags: kubectl logs {pod} -c {c} -n {ns} --previous"]),
    126: ("exit-126", "exited with code 126: the command was found but could not be executed (permissions, "
                      "wrong architecture, or a script without an interpreter).",
          ["Check the entrypoint file is executable and matches the node CPU architecture."]),
    127: ("exit-127", "exited with code 127: the command was not found inside the image, so command/args or "
                      "the image's PATH are wrong.",
          ["Run the image locally with the same command; check spec command/args and the image tag."]),
    137: ("exit-137", "exited with code 137: killed by SIGKILL (128 + 9) without an OOMKilled reason. Something "
                     "outside the process killed it: the kubelet after a failed probe or a stop timeout, the node under "
                     "memory pressure, or a manual kill.",
          ["Check events for probe failures: kubectl describe pod {pod} -n {ns}",
           "Check the node: kubectl describe node <node> (look for MemoryPressure)"]),
    139: ("exit-139", "exited with code 139: SIGSEGV (128 + 11), a segmentation fault. The program crashed in "
                     "native code, often a bad binary, wrong architecture or a library mismatch.",
          ["Check the image architecture matches the node; try a different image tag; check native libraries."]),
    143: ("exit-143", "exited with code 143: SIGTERM (128 + 15). The process was asked to stop and did, over and "
                     "over. Something keeps terminating it: a failing liveness probe, a rollout, or the app "
                     "exiting on a signal it should ignore.",
          ["Check probes and events: kubectl describe pod {pod} -n {ns}",
           "Check the app's SIGTERM handling and whether something is scaling or evicting it."]),
    255: ("exit-255", "exited with code 255 (out of range / unspecified); the application chose it.",
          ["Read the logs: kubectl logs {pod} -c {c} -n {ns} --previous"]),
}


def _restarting_after_exit(pod, st, cur) -> bool:
    """Currently terminated, restarted at least twice, and the pod's restartPolicy will start it again."""
    if not cur or int(st.get("restartCount") or 0) < 2 or pod_phase(pod) in ("Succeeded", "Failed"):
        return False
    policy = (pod.get("spec") or {}).get("restartPolicy", "Always")
    return policy == "Always" or (policy == "OnFailure" and cur.get("exitCode") != 0)


def rule_crashloop(pod, cluster):
    out = []
    probes = probe_failures(pod, cluster)
    for s in all_statuses(pod):
        st, name = s["status"], s["name"]
        cur = terminated(st.get("state"))
        if waiting(st).get("reason") == "CrashLoopBackOff":
            src, last = "lastState", terminated(st.get("lastState"))
        elif _restarting_after_exit(pod, st, cur):
            # A looping container alternates between `waiting: CrashLoopBackOff` and `terminated`;
            # a snapshot can land in the second phase, where the exit details are in `state`.
            src, last = "state", cur
        else:
            continue
        if last.get("reason") == "OOMKilled":
            continue  # KT003 owns it
        if (name, "liveness") in probes or (name, "startup") in probes:
            continue  # KT006 owns it: the probe is what keeps killing the container
        code = last.get("exitCode")
        if code in _EXIT:
            variant, meaning, fixes = _EXIT[code]
        elif code is not None:
            variant, meaning, fixes = (f"exit-{code}", f"exited with code {code}.",
                                       ["Read the logs: kubectl logs {pod} -c {c} -n {ns} --previous"])
        else:
            variant, meaning, fixes = ("unknown", "is crashing but the pod has no lastState to say how.",
                                       ["Read the logs: kubectl logs {pod} -c {c} -n {ns} --previous"])
        ev = [_kv(f"{s['path']}.state.waiting.reason", "CrashLoopBackOff")] if src == "lastState" else []
        if code is not None:
            ev.append(_kv(f"{s['path']}.{src}.terminated.exitCode", code))
            if last.get("reason"):
                ev.append(_kv(f"{s['path']}.{src}.terminated.reason", last["reason"]))
        ev.append(_kv(f"{s['path']}.restartCount", st.get("restartCount", 0)))
        ran = _runtime_seconds(last)
        if ran is not None:
            ev.append(f"ran for {ran:.0f}s before exiting (startedAt to finishedAt)")
        fixes = [f.format(pod=pod_name(pod), c=name, ns=pod_ns(pod)) for f in fixes]
        out.append(_make(pod, "KT001", "high", f'Container "{name}" is crash-looping ({variant})',
                         f'The container "{name}" {meaning}', ev, fixes, variant=variant, container=name))
    return out


# ---- KT003 OOM -----------------------------------------------------------------------------------

def rule_oom(pod, cluster):
    out = []
    for s in all_statuses(pod):
        st = s["status"]
        cur, last = terminated(st.get("state")), terminated(st.get("lastState"))
        if cur.get("reason") == "OOMKilled":
            hit, where = cur, "state"
        elif last.get("reason") == "OOMKilled":
            hit, where = last, "lastState"
        else:
            continue
        name = s["name"]
        limit = _limit(s["spec"], "memory")
        ev = [_kv(f"{s['path']}.{where}.terminated.reason", "OOMKilled")]
        if hit.get("exitCode") is not None:
            ev.append(_kv(f"{s['path']}.{where}.terminated.exitCode", hit["exitCode"]))
        ev.append(_kv(f"{s['path']}.restartCount", st.get("restartCount", 0)))
        if limit:
            ev.append(_kv(f"{s['spec_path']}.resources.limits.memory", limit))
            bump = _doubled_memory(limit)
            title = f'Container "{name}" was OOM-killed at its {limit} memory limit'
            cause = (f'The container "{name}" used more memory than its {limit} limit, so the kernel killed it '
                     "(exit code 137, reason OOMKilled). Either the limit is too low for the workload or the "
                     "process is leaking memory.")
            fixes = [f"Raise resources.limits.memory (currently {limit}); try {bump} and watch usage." if bump
                     else "Raise resources.limits.memory.",
                     f"Measure real usage: kubectl top pod {pod_name(pod)} -n {pod_ns(pod)} --containers",
                     "For JVM/Node/Python runtimes, size the heap below the limit (for example -XX:MaxRAMPercentage=75)."]
            variant = "limit-set"
        else:
            ev.append(f"{s['spec_path']}.resources.limits.memory is not set")
            title = f'Container "{name}" was OOM-killed with no memory limit set'
            cause = (f'The container "{name}" has no memory limit but was still OOM-killed, so the node (or a '
                     "namespace LimitRange default) ran out of memory. It is probably leaking or unbounded.")
            fixes = ["Set a memory request and limit so the damage stays inside this container.",
                     f"Check the node: kubectl describe node {(pod.get('spec') or {}).get('nodeName', '<node>')}"]
            variant = "no-limit"
        out.append(_make(pod, "KT003", "high", title, cause, ev, fixes, variant=variant, container=name))
    return out


# ---- KT002 image pull ----------------------------------------------------------------------------

_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}


def _classify_pull(reason, text):
    t = text.lower()
    if reason == "InvalidImageName" or "invalid reference format" in t:
        return "invalid-image-name"
    if reason == "ErrImageNeverPull":
        return "never-pull-policy"
    if "toomanyrequests" in t or "rate limit" in t:
        return "rate-limited"
    if "pull access denied" in t or "repository does not exist" in t or "may require authorization" in t:
        return "repo-or-auth"
    if any(k in t for k in ("no such host", "i/o timeout", "connection refused", "x509", "tls handshake",
                            "server misbehaving", "network is unreachable")):
        return "registry-unreachable"
    if "unauthorized" in t or "authentication required" in t or "403" in t or "401" in t or " denied" in t:
        return "auth"
    if "manifest unknown" in t or "not found" in t or "notfound" in t:
        return "tag-not-found"
    return "unknown"


_PULL_TEXT = {
    "tag-not-found": ("The registry answered, but has no such image or tag, so the tag is misspelled, was never pushed, or was deleted.",
                      ["Check the exact tag exists in the registry (for Docker Hub: https://hub.docker.com/r/<repo>/tags).",
                       "Fix spec.containers[].image and re-apply; pin to a tag that CI has pushed."]),
    "repo-or-auth": ("The registry says the repository does not exist or needs credentials. For a public image this is "
                     "almost always a typo in the repository name; for a private image the pod has no pull secret.",
                     ["Check the repository name for typos.",
                      "If it is private: kubectl create secret docker-registry regcred ... and add it under imagePullSecrets."]),
    "auth": ("The registry refused the pull as unauthorized: the credentials are missing, wrong or expired.",
             ["kubectl create secret docker-registry regcred --docker-server=<host> --docker-username=<u> --docker-password=<p>",
              "Reference it from the pod or its ServiceAccount under imagePullSecrets."]),
    "registry-unreachable": ("The node could not reach the registry: the host name does not resolve, or the connection or TLS "
                             "handshake failed. Usually a typo in the registry host, a DNS or egress rule, or a proxy.",
                             ["Check the registry host in the image name for typos.",
                              "From the node or a debug pod: nslookup <registry-host>; check egress rules and any proxy settings."]),
    "invalid-image-name": ("The image reference is malformed (uppercase letters, stray characters, or a bad tag), so "
                           "no pull is even attempted.",
                           ["Fix the image reference: repository names must be lowercase; tags allow [A-Za-z0-9_.-] only."]),
    "rate-limited": ("The registry is rate limiting anonymous pulls from this node's address.",
                     ["Authenticate pulls with an imagePullSecret, or mirror the image into your own registry."]),
    "never-pull-policy": ("imagePullPolicy is Never but the image is not already on the node.",
                          ["Load the image onto the node, or change imagePullPolicy to IfNotPresent."]),
    "unknown": ("The image could not be pulled and the message does not match a known pattern.",
                ["Read the full message: kubectl describe pod <pod>"]),
}


def rule_image_pull(pod, cluster):
    out = []
    events = cluster.events_for(pod)
    for s in all_statuses(pod):
        w = waiting(s["status"])
        if w.get("reason") not in _PULL_REASONS:
            continue
        name = s["name"]
        image = s["spec"].get("image") or s["status"].get("image", "")
        failed = [e for e in events if e.reason in ("Failed", "BackOff") and (image in e.message or "pull" in e.message.lower())]
        text = " ".join([w.get("message", "")] + [e.message for e in failed])
        variant = _classify_pull(w.get("reason"), text)
        cause, fixes = _PULL_TEXT[variant]
        ev = [_kv(f"{s['path']}.state.waiting.reason", w.get("reason")),
              _kv(f"{s['spec_path']}.image", image)]
        if w.get("message"):
            ev.append(_kv(f"{s['path']}.state.waiting.message", _short(w["message"])))
        for e in failed[-1:]:
            ev.append(f'events[{e.reason}] = {_q(_short(e.message))}')
        if variant in ("auth", "repo-or-auth") and not (pod.get("spec") or {}).get("imagePullSecrets"):
            ev.append("spec.imagePullSecrets is not set")
        out.append(_make(pod, "KT002", "high", f'Cannot pull image "{image}" for container "{name}" ({variant})',
                         cause, ev, fixes, variant=variant, container=name))
    return out


# ---- KT004 unschedulable ---------------------------------------------------------------------------

_SCHED = [
    ("unbound-pvc", re.compile(r'persistentvolumeclaim "([^"]+)" not found|unbound immediate persistentvolumeclaims|'
                               r"didn't find available persistent volumes to bind|pod has unbound", re.I), None),
    ("insufficient-cpu", re.compile(r"(\d+) Insufficient cpu", re.I), 1),
    ("insufficient-memory", re.compile(r"(\d+) Insufficient memory", re.I), 1),
    ("taint", re.compile(r"(\d+) node\(s\) had (?:untolerated )?taints?", re.I), 1),
    ("node-selector", re.compile(r"(\d+) node\(s\) didn't match Pod's node affinity", re.I), 1),
    ("pod-affinity", re.compile(r"(\d+) node\(s\) didn't match pod (?:anti-)?affinity", re.I), 1),
    ("no-free-ports", re.compile(r"(\d+) node\(s\) didn't have free ports", re.I), 1),
    ("too-many-pods", re.compile(r"(\d+) Too many pods", re.I), 1),
    ("volume-node-affinity", re.compile(r"(\d+) node\(s\) had volume node affinity conflict", re.I), 1),
    ("cordoned-nodes", re.compile(r"(\d+) node\(s\) were unschedulable", re.I), 1),
]


def _sched_reasons(message):
    """[(variant, node_count, position)] found in a scheduler message, ignoring the 'preemption:' tail."""
    head = re.split(r"\bpreemption:", message, flags=re.I)[0]
    found = []
    for variant, rx, group in _SCHED:
        m = rx.search(head)
        if m:
            count = int(m.group(group)) if group else 0
            found.append((variant, count, m.start()))
    return found


def rule_unschedulable(pod, cluster):
    if pod_phase(pod) != "Pending" or (pod.get("spec") or {}).get("nodeName"):
        return []
    cond = condition(pod, "PodScheduled")
    sched_events = [e for e in cluster.events_for(pod) if e.reason == "FailedScheduling"]
    if cond.get("status") == "False" and cond.get("message"):
        message, source = cond["message"], "status.conditions[PodScheduled].message"
    elif sched_events:
        message, source = sched_events[-1].message, "events[FailedScheduling]"
    else:
        return []
    reasons = _sched_reasons(message)
    ev = [f"{source} = {_q(_short(message))}"]
    if not reasons:
        return [_make(pod, "KT004", "high", "Pod cannot be scheduled", "The scheduler could not place the pod and "
                      "the message does not match a known pattern.", ev,
                      [f"kubectl describe pod {pod_name(pod)} -n {pod_ns(pod)}"], variant="unknown")]
    reasons.sort(key=lambda r: (-r[1], r[2]))
    variant = reasons[0][0]
    spec = pod.get("spec") or {}
    cpu_m, mem_b = requests(pod)
    biggest = cluster.largest_allocatable()
    fixes, cause = [], ""
    if variant == "insufficient-cpu":
        ev.append(f"requested CPU = {fmt_cpu(cpu_m)}")
        if biggest and cpu_m > biggest[0]:
            variant = "cpu-exceeds-node-capacity"
            ev.append(f"largest node allocatable CPU = {fmt_cpu(biggest[0])}")
            cause = (f"The pod requests {fmt_cpu(cpu_m)}, more than the largest node can ever offer "
                     f"({fmt_cpu(biggest[0])} allocatable). Adding nodes of the same size will not help.")
            fixes = ["Lower resources.requests.cpu to what the workload actually uses (kubectl top pod).",
                     "Or add a larger node type to the cluster."]
        else:
            cause = (f"The pod requests {fmt_cpu(cpu_m)} of CPU and no node has that much unreserved right now; "
                     "it would fit on an empty node, so the cluster is full.")
            fixes = ["Scale the node pool, or lower other workloads' CPU requests.",
                     "kubectl describe nodes | grep -A8 'Allocated resources'"]
    elif variant == "insufficient-memory":
        ev.append(f"requested memory = {fmt_bytes(mem_b)}")
        if biggest and mem_b > biggest[1]:
            variant = "memory-exceeds-node-capacity"
            ev.append(f"largest node allocatable memory = {fmt_bytes(biggest[1])}")
            cause = (f"The pod requests {fmt_bytes(mem_b)}, more than the largest node can ever offer "
                     f"({fmt_bytes(biggest[1])} allocatable). Adding same-sized nodes will not help.")
            fixes = ["Lower resources.requests.memory to what the workload actually uses.",
                     "Or add a larger node type to the cluster."]
        else:
            cause = (f"The pod requests {fmt_bytes(mem_b)} of memory and no node has that much unreserved right "
                     "now; it would fit on an empty node, so the cluster is full.")
            fixes = ["Scale the node pool, or lower other workloads' memory requests.",
                     "kubectl describe nodes | grep -A8 'Allocated resources'"]
    elif variant == "taint":
        ev.append(_kv("spec.tolerations", spec["tolerations"]) if spec.get("tolerations") else "spec.tolerations is not set")
        cause = ("Every node that could fit the pod carries a taint the pod does not tolerate. Taints keep pods "
                 "off nodes (dedicated pools, control plane, nodes marked unhealthy).")
        fixes = ["Add a matching toleration under spec.tolerations, or remove the taint if it should not be there.",
                 "kubectl describe nodes | grep Taints"]
    elif variant == "node-selector":
        ev.append(_kv("spec.nodeSelector", spec["nodeSelector"]) if spec.get("nodeSelector") else "spec.nodeSelector is not set")
        cause = ("No node matches the pod's nodeSelector or node affinity, so there is nowhere to put it. Usually a "
                 "label that is misspelled or was never applied to any node.")
        fixes = ["kubectl get nodes --show-labels   # is there a node with that label?",
                 "Fix the nodeSelector, or label a node: kubectl label node <node> <key>=<value>"]
    elif variant == "unbound-pvc":
        claims = [(v.get("persistentVolumeClaim") or {}).get("claimName") for v in spec.get("volumes") or []
                  if v.get("persistentVolumeClaim")]
        for c in claims:
            ev.append(f"spec.volumes[].persistentVolumeClaim.claimName = {_q(c)}")
        cause = ("The pod mounts a PersistentVolumeClaim that does not exist or is not bound to a volume, so "
                 "the scheduler cannot place it.")
        fixes = ["kubectl get pvc -n " + pod_ns(pod) + "   # does the claim exist, and is it Bound?",
                 "Create the claim, fix claimName, or check that a StorageClass can provision it."]
    elif variant == "pod-affinity":
        cause = "The pod's affinity or anti-affinity rules cannot be satisfied by any node."
        fixes = ["Relax requiredDuringScheduling anti-affinity to preferred, or add nodes/zones."]
    elif variant == "no-free-ports":
        cause = "The pod asks for a hostPort that is already taken on every node."
        fixes = ["Stop using hostPort (use a Service), or run fewer replicas than nodes."]
    elif variant == "too-many-pods":
        cause = "Every node is at its maximum pod count."
        fixes = ["Add nodes, or raise the kubelet's maxPods."]
    elif variant == "volume-node-affinity":
        cause = "The pod's volume lives in a zone or node the pod cannot be scheduled to."
        fixes = ["Check the PersistentVolume's nodeAffinity against the pod's zone constraints."]
    else:
        cause = "All nodes are cordoned (marked unschedulable)."
        fixes = ["kubectl uncordon <node>"]
    others = [r[0] for r in reasons if r[0] != reasons[0][0]]
    if others:
        cause += f" The scheduler also reported: {', '.join(others)}."
    return [_make(pod, "KT004", "high", f"Pod cannot be scheduled ({variant})", cause, ev, fixes, variant=variant)]


# ---- KT005 missing ConfigMap / Secret ------------------------------------------------------------

_CFG = [
    ("missing-configmap-key", re.compile(r"couldn't find key (\S+) in ConfigMap (\S+)", re.I)),
    ("missing-secret-key", re.compile(r"couldn't find key (\S+) in Secret (\S+)", re.I)),
    ("missing-configmap", re.compile(r'configmap "([^"]+)" not found', re.I)),
    ("missing-secret", re.compile(r'secret "([^"]+)" not found', re.I)),
]


def _references(pod, kind, name):
    """Spec lines that reference a ConfigMap/Secret by (short) name."""
    short = name.split("/")[-1]
    key = "configMapKeyRef" if kind == "ConfigMap" else "secretKeyRef"
    ref = "configMapRef" if kind == "ConfigMap" else "secretRef"
    vol = "configMap" if kind == "ConfigMap" else "secret"
    lines = []
    for c, init in containers(pod):
        base = f"spec.{'initContainers' if init else 'containers'}[{c.get('name')}]"
        for e in c.get("env") or []:
            r = (e.get("valueFrom") or {}).get(key)
            if r and r.get("name") == short:
                lines.append(f"{base}.env[{e.get('name')}].valueFrom.{key} = {_q(r)}")
        for ef in c.get("envFrom") or []:
            r = ef.get(ref)
            if r and r.get("name") == short:
                lines.append(f"{base}.envFrom[].{ref} = {_q(r)}")
    for v in (pod.get("spec") or {}).get("volumes") or []:
        r = v.get(vol)
        if r and (r.get("name") == short or r.get("secretName") == short):
            lines.append(f"spec.volumes[{v.get('name')}].{vol} = {_q(r)}")
    return lines


def _config_finding(pod, variant, match, evidence_head, container):
    ns = pod_ns(pod)
    if variant.endswith("-key"):
        key, obj = match.group(1), match.group(2)
        kind = "ConfigMap" if "configmap" in variant else "Secret"
        short = obj.split("/")[-1]
        title = f'{kind} "{short}" has no key "{key}"'
        cause = (f'The pod reads key "{key}" from {kind} "{short}", but that key is not in it. The container cannot be '
                 "created until the key exists.")
        fixes = [f"kubectl get {kind.lower()} {short} -n {ns} -o yaml   # which keys exist?",
                 f'Add the key, or fix the reference (it is case-sensitive): "{key}".']
    else:
        short = match.group(1)
        kind = "ConfigMap" if "configmap" in variant else "Secret"
        title = f'{kind} "{short}" does not exist in namespace {ns}'
        cause = (f'The pod references {kind} "{short}", which is not in namespace "{ns}". The container cannot be '
                 "created until it exists (a misspelled name or a different namespace are the usual reasons).")
        fixes = [f"kubectl get {kind.lower()} -n {ns}   # is it there under another name or namespace?",
                 f"Create it: kubectl create {kind.lower()} {short} -n {ns} ..."]
    ev = list(evidence_head) + _references(pod, kind, short)
    return _make(pod, "KT005", "high", title, cause, ev, fixes, variant=variant, container=container)


def rule_config(pod, cluster):
    out, seen = [], set()
    for s in all_statuses(pod):
        w = waiting(s["status"])
        if w.get("reason") != "CreateContainerConfigError":
            continue
        msg = w.get("message", "")
        head = [_kv(f"{s['path']}.state.waiting.reason", w["reason"]),
                _kv(f"{s['path']}.state.waiting.message", _short(msg))]
        for variant, rx in _CFG:
            m = rx.search(msg)
            if m:
                out.append(_config_finding(pod, variant, m, head, s["name"]))
                seen.add(variant)
                break
        else:
            out.append(_make(pod, "KT005", "high", "Container config error", "The container could not be created "
                             "because of a configuration problem.", head,
                             [f"kubectl describe pod {pod_name(pod)} -n {pod_ns(pod)}"], variant="config-error",
                             container=s["name"]))
    for e in cluster.events_for(pod):
        if e.reason != "FailedMount":
            continue
        for variant, rx in _CFG:
            m = rx.search(e.message)
            if m and not variant.endswith("-key") and variant not in seen:
                head = [f"events[FailedMount] = {_q(_short(e.message))}"]
                out.append(_config_finding(pod, variant, m, head, None))
                seen.add(variant)
                break
    return out


# ---- KT006 probes ----------------------------------------------------------------------------------

def _probe_port(probe, spec):
    if not probe:
        return None
    port = (probe.get("httpGet") or {}).get("port") or (probe.get("tcpSocket") or {}).get("port")
    if isinstance(port, str):
        for p in spec.get("ports") or []:
            if p.get("name") == port:
                return p.get("containerPort")
        return None
    return port


def _classify_probe(ptype, message, probe, spec, restarts):
    t = message.lower()
    declared = [p.get("containerPort") for p in spec.get("ports") or []]
    port = _probe_port(probe, spec)
    if "oci runtime exec failed" in t or (probe and "exec" in probe and "no such file" in t):
        return "exec-failed"
    if "connection refused" in t:
        if declared and port is not None and port not in declared:
            return "wrong-port"
        if (ptype == "liveness" and probe is not None and not spec.get("startupProbe")
                and int(probe.get("initialDelaySeconds") or 0) <= 5 and restarts > 0):
            return "initial-delay-too-short"
        return "connection-refused"
    m = re.search(r"statuscode: (\d{3})", t)
    if m:
        code = int(m.group(1))
        return "wrong-path" if code == 404 else "app-unhealthy" if code >= 500 else "http-status"
    if "deadline exceeded" in t or "timeout" in t or "timed out" in t:
        return "timeout"
    return "failing"


_PROBE_FIX = {
    "wrong-port": ["Point the probe at the port the app actually listens on (check containerPort and the app's own config)."],
    "wrong-path": ["Use a path the app really serves for health (a 404 means the route does not exist); test it with curl from a debug pod."],
    "app-unhealthy": ["The app answered with a 5xx: look at its logs and at the dependencies its health endpoint checks."],
    "timeout": ["Raise timeoutSeconds, or find why the endpoint is slow (CPU throttling from a low CPU limit is a common cause)."],
    "initial-delay-too-short": ["Add a startupProbe (preferred) or raise initialDelaySeconds so the app has time to start before liveness begins."],
    "connection-refused": ["Nothing is listening on the probed port yet: check the app started, that it binds 0.0.0.0 (not 127.0.0.1), and the port number."],
    "exec-failed": ["The probe command itself fails to run: check it exists in the image and the shell it needs is installed."],
    "http-status": ["The endpoint returns a non-2xx/3xx status: check what that path should return."],
    "failing": ["kubectl describe pod to read the full probe message."],
}


def rule_probes(pod, cluster):
    out = []
    statuses = {s["name"]: s for s in all_statuses(pod)}
    for (cname, ptype), events in probe_failures(pod, cluster).items():
        s = statuses.get(cname)
        spec = (s or {}).get("spec") or container_spec(pod, cname) or {}
        probe = spec.get(_PROBE_KEY[ptype])
        restarts = int(((s or {}).get("status") or {}).get("restartCount") or 0)
        last = events[-1]
        variant = _classify_probe(ptype, last.message, probe, spec, restarts)
        total = sum(e.count for e in events)
        sev = "medium" if ptype == "readiness" else "high"
        path = f"spec.containers[{cname}].{_PROBE_KEY[ptype]}"
        ev = [f"events[Unhealthy] x{total} = {_q(_short(last.message))}"]
        if probe:
            ev.append(_kv(path, probe))
        port = _probe_port(probe, spec)
        declared = [p.get("containerPort") for p in spec.get("ports") or []]
        if variant == "wrong-port":
            ev.append(f"probe port {port} is not in the container's declared ports {declared}")
        if s:
            ev.append(_kv(f"{s['path']}.ready", s["status"].get("ready")))
            ev.append(_kv(f"{s['path']}.restartCount", restarts))
        effect = {"readiness": "The pod is kept out of Service endpoints, so it gets no traffic",
                  "liveness": "The kubelet keeps killing and restarting the container",
                  "startup": "The kubelet kills the container before it finishes starting"}[ptype]
        cause = f'The {ptype} probe on container "{cname}" keeps failing ({variant}). {effect}.'
        out.append(_make(pod, "KT006", sev, f'{ptype.capitalize()} probe failing on "{cname}" ({variant})', cause, ev,
                         _PROBE_FIX[variant] + [f"kubectl describe pod {pod_name(pod)} -n {pod_ns(pod)}"],
                         variant=variant, container=cname))
    return out


# ---- KT007 evicted ---------------------------------------------------------------------------------

def rule_evicted(pod, cluster):
    st = pod.get("status") or {}
    if pod_phase(pod) != "Failed" or st.get("reason") != "Evicted":
        return []
    msg = st.get("message", "")
    low = msg.lower()
    variant = ("ephemeral-storage" if "ephemeral-storage" in low or "ephemeral storage" in low
               else "memory" if "memory" in low
               else "disk" if "disk" in low or "nodefs" in low or "imagefs" in low
               else "pid" if "pid" in low else "unknown")
    ev = [_kv("status.reason", "Evicted"), _kv("status.message", _short(msg))]
    cause = (f"The kubelet evicted this pod because the node was running low on {variant.replace('-', ' ')}. "
             "Evicted pods stay behind as Failed records; the controller has already started a replacement.")
    fixes = ["Give pods realistic resource requests so the scheduler spreads them, and set limits so one pod cannot starve the node.",
             "Clean up leftovers: kubectl delete pods -n " + pod_ns(pod) + " --field-selector=status.phase=Failed",
             "Check the node: kubectl describe node " + str((pod.get("spec") or {}).get("nodeName", "<node>"))]
    return [_make(pod, "KT007", "medium", f"Pod was evicted ({variant} pressure)", cause, ev, fixes, variant=variant)]


# ---- KT008 / KT009 hygiene -----------------------------------------------------------------------

def _tag_problem(image):
    if "@sha256:" in image:
        return None
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return "untagged"
    return "latest" if last.rsplit(":", 1)[1] == "latest" else None


def rule_floating_tag(pod, cluster):
    if pod_phase(pod) in ("Succeeded", "Failed"):
        return []
    bad = []
    for c, init in containers(pod):
        problem = _tag_problem(c.get("image", ""))
        if problem:
            bad.append((c, init, problem))
    if not bad:
        return []
    c0, init0, variant = bad[0]
    ev = [_kv(f"spec.{'initContainers' if i else 'containers'}[{c.get('name')}].image", c.get("image")) for c, i, _ in bad]
    cause = ("The image has no fixed version (" + (":latest" if variant == "latest" else "no tag, which means :latest") +
             "), so a restart can silently pull different code, rollbacks are not reproducible, and imagePullPolicy "
             "defaults to Always.")
    fixes = ["Pin the image to a version tag, or better to a digest (image@sha256:...)."]
    return [_make(pod, "KT008", "low", f"Image is not pinned ({variant})", cause, ev, fixes, variant=variant,
                  container=c0.get("name"))]


def rule_no_memory_limit(pod, cluster):
    if pod_phase(pod) in ("Succeeded", "Failed"):
        return []
    missing = [c for c, init in containers(pod) if not init and not _limit(c, "memory")]
    if not missing:
        return []
    qos = (pod.get("status") or {}).get("qosClass")
    best_effort = all(not (c.get("resources") or {}).get("requests") and not (c.get("resources") or {}).get("limits")
                      for c in missing) and len(missing) == len([c for c, i in containers(pod) if not i])
    ev = [f"spec.containers[{c.get('name')}].resources.limits.memory is not set" for c in missing]
    if qos:
        ev.append(_kv("status.qosClass", qos))
    cause = ("A container without a memory limit can grow until the node runs out of memory, which evicts or kills "
             "other pods." + (" With no requests either, the pod is BestEffort and is the first to be evicted."
                              if best_effort else ""))
    fixes = ["Set resources.requests.memory (typical use) and resources.limits.memory (a ceiling above peak use).",
             "Namespace-wide safety net: create a LimitRange with a default memory limit."]
    return [_make(pod, "KT009", "low", "No memory limit set", cause, ev, fixes,
                  variant="best-effort" if best_effort else "no-memory-limit", container=missing[0].get("name"))]


POD_RULES = [rule_crashloop, rule_oom, rule_image_pull, rule_unschedulable, rule_config, rule_probes,
             rule_evicted, rule_floating_tag, rule_no_memory_limit]


# ---- KT010 nodes -----------------------------------------------------------------------------------

def rule_node(node, cluster):
    name = meta(node).get("name", "")
    out = []

    def add(severity, variant, title, cause, evidence, fixes):
        out.append(Finding(rule="KT010", severity=severity, namespace="", workload=f"Node/{name}", subject=name,
                           title=title, cause=cause, evidence=evidence, fix=fixes, variant=variant))

    conds = {c.get("type"): c for c in (node.get("status") or {}).get("conditions") or []}
    ready = conds.get("Ready")
    if ready is not None and ready.get("status") != "True":
        add("critical", "not-ready", f'Node "{name}" is not Ready',
            "The kubelet on this node is not reporting healthy, so the control plane will stop scheduling to it and "
            "evict its pods after the toleration timeout.",
            [_kv("status.conditions[Ready].status", ready.get("status")),
             _kv("status.conditions[Ready].message", _short(ready.get("message", "")))],
            [f"kubectl describe node {name}", "Check the kubelet and container runtime on the machine (journalctl -u kubelet)."])
    for ctype, variant, what in (("MemoryPressure", "memory-pressure", "memory"), ("DiskPressure", "disk-pressure", "disk"),
                                 ("PIDPressure", "pid-pressure", "process IDs")):
        c = conds.get(ctype)
        if c is not None and c.get("status") == "True":
            add("high", variant, f'Node "{name}" is under {what} pressure',
                f"The node is running low on {what}; the kubelet starts evicting pods to recover.",
                [_kv(f"status.conditions[{ctype}].status", "True"), _kv(f"status.conditions[{ctype}].message", _short(c.get("message", "")))],
                [f"kubectl describe node {name}", "Find the heaviest pods: kubectl top pods -A --sort-by=memory"])
    if (node.get("spec") or {}).get("unschedulable"):
        add("medium", "cordoned", f'Node "{name}" is cordoned',
            "The node is marked unschedulable: running pods stay, but no new pods are placed on it.",
            [_kv("spec.unschedulable", True)], [f"If the maintenance is over: kubectl uncordon {name}"])
    return out


NODE_RULES = [rule_node]
