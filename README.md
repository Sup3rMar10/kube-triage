# 🩺 kube-triage

A small, dependency-free CLI that looks at what `kubectl` already knows (pods, container states, events, nodes) and says **why a workload is unhealthy, what the evidence is, and what to try first**. Ten rules cover the failures people actually page each other about: crash loops, image pulls, OOM kills, pods that will not schedule, missing ConfigMaps and Secrets, failing probes, evictions, node trouble, and two hygiene checks.

It reads JSON, so it works on a saved file from a customer's cluster as well as on a live namespace, and every claim it makes is a line quoted from that JSON that you can check yourself.

This is original code. The only dependency is Python's standard library (pytest is needed to run the tests).

```
kubectl get pods,events,nodes -o json ──▶ rules ──▶ findings ──▶ rank, group by workload ──▶ text / --json
        (live, or a saved file)         KT001..KT010   with evidence          │
                                                                              └─▶ --explain (optional model rephrases the findings)
```

## Example

```
$ python -m kubetriage demo

kube-triage: 5 workloads need attention (7 findings: 5 high, 2 low) across 7 pods and 2 nodes

HIGH     Deployment/web  (namespace shop, 2 of 3 pods)
  KT003 Container "web" was OOM-killed at its 128Mi memory limit
    pods:     web-6b7c8d9e0-a1b2c, web-6b7c8d9e0-d3e4f
    why:      The container "web" used more memory than its 128Mi limit, so the kernel killed it (exit code 137,
              reason OOMKilled). Either the limit is too low for the workload or the process is leaking memory.
    evidence:
      - status.containerStatuses[web].lastState.terminated.reason = "OOMKilled"
      - status.containerStatuses[web].lastState.terminated.exitCode = 137
      - status.containerStatuses[web].restartCount = 14
      - spec.containers[web].resources.limits.memory = "128Mi"
    fix:
      - Raise resources.limits.memory (currently 128Mi); try 256Mi and watch usage.
      - Measure real usage: kubectl top pod web-6b7c8d9e0-a1b2c -n shop --containers
      ...
```

Workloads are ranked by severity, then by how many pods are affected. Identical findings on the replicas of one Deployment are merged into one entry.

## The rules

| Rule | Severity | What it finds | Variants it tells apart |
|---|---|---|---|
| KT001 crash-loop | high | Container is crash-looping (in `CrashLoopBackOff`, or caught between restarts) | exit code: 0, 1, 2, 126, 127, 137, 139, 143, 255, others |
| KT002 image-pull | high | `ImagePullBackOff`, `ErrImagePull`, `InvalidImageName` | tag-not-found, repo-or-auth, auth, registry-unreachable, invalid-image-name, rate-limited, never-pull-policy |
| KT003 oom-killed | high | Container killed for memory | limit-set, no-limit, limit-too-low-to-start |
| KT004 unschedulable | high | Pod stuck Pending | insufficient-cpu / memory (cluster full), cpu / memory-exceeds-node-capacity (can never fit), taint, node-selector, unbound-pvc, pod-affinity, no-free-ports, too-many-pods, volume-node-affinity, cordoned-nodes |
| KT005 config-missing | high | `CreateContainerConfigError`, or a `FailedMount` for a missing ConfigMap or Secret | missing-configmap, missing-secret, missing-configmap-key, missing-secret-key |
| KT006 probe-failing | medium / high | Readiness, liveness or startup probe failing | wrong-port, wrong-path, app-unhealthy, timeout, initial-delay-too-short, connection-refused, exec-failed |
| KT007 evicted | medium | Pod evicted by the kubelet | memory, ephemeral-storage, disk, pid |
| KT008 floating-image-tag | low | Image is `:latest` or has no tag | latest, untagged |
| KT009 no-memory-limit | low | Container has no memory limit | no-memory-limit, best-effort |
| KT010 node-problem | critical / high / medium | Node NotReady, under memory / disk / PID pressure, or cordoned | not-ready, memory-pressure, disk-pressure, pid-pressure, cordoned |

How the rules decide, in the cases that are not obvious:

- **Who owns the failure.** When a container is crash-looping because a liveness probe keeps killing it, the finding is the probe (KT006), not a generic crash (KT001). An OOM-killed crash loop is KT003, not KT001. Each failure gets one explanation, not three.
- **Capacity.** For a pod that will not schedule on CPU or memory, the pod's request is compared with the largest node's allocatable. "The cluster is full" and "this pod can never fit on any node" are different problems with different fixes, so they are different variants.
- **Mixed scheduler messages.** The scheduler often reports several reasons at once (`1 Insufficient memory, 5 node(s) had untolerated taint`). The reason that affects the most nodes is the primary one; the rest are listed.
- **Wrong port vs slow start.** A refused probe connection to a port the container never declared is `wrong-port`. A refused liveness probe with no initial delay on a container that has been restarted, and no startup probe, is `initial-delay-too-short`.
- **Evidence is quoted.** Each evidence line is `path = value`, copied from the input. A test resolves every such line back against the original document on all 53 fixtures and fails if any is not true.

## Quick start

```
python -m kubetriage demo                                     # the bundled sample namespace
python -m kubetriage diagnose --file pods.json                # from a saved file: kubectl get pods,events,nodes -n shop -o json > pods.json
python -m kubetriage diagnose -n shop                         # live, through kubectl
python -m kubetriage diagnose -A --json                       # every namespace, machine readable
python -m kubetriage diagnose -n shop --fail-on high          # exit code 1 if anything high or worse (for a post-deploy check)
python -m kubetriage rules                                    # list the rules
python -m kubetriage eval                                     # score the rules on fixtures/ (writes results/summary.json)
python -m pytest                                              # 149 tests, no cluster needed
```

Live mode runs one `kubectl get pods,events,nodes -o json`. If the user is not allowed to list nodes it falls back to pods and events and says that node-capacity checks were skipped. It needs only read access.

### Optional: `--explain`

`--explain anthropic` sends the findings (rule, title, cause, evidence, fix, after redacting passwords, tokens, bearer headers, URLs with credentials and long encoded blobs) to a language model and asks it to rephrase them in plain language, without adding causes. It never sees pod specs, environment variables, ConfigMaps, Secrets or the raw kubectl output. It is off by default, needs `pip install anthropic` and `ANTHROPIC_API_KEY`, and **has not been run against the live API in this repo**: only the prompt building, the redaction and the wiring are tested, with a fake model function.

## Results

Two kinds of evidence, and the second is the one to trust more.

### 1. Offline scoring on labelled fixtures

53 fixtures, each a `kubectl get pods,events,nodes -o json` document with the findings it should produce written beside it (`fixtures/manifest.json`). A prediction counts as correct when the same rule flags the same pod or node. Variant accuracy is measured on the correct ones.

- **dev** (21 fixtures): written while the rules were being written, and what the rules were adjusted against. 20 hand-built, 1 captured from a real cluster.
- **holdout** (32 fixtures): written after the rules were frozen (commit `395b153`). 17 hand-built, 15 captured from a real kind cluster.

<!-- RESULTS:START -->

| Split | Fixtures | Hand-built | Captured from kind | Precision | Recall | Exact-match fixtures | Variant accuracy |
|---|---|---|---|---|---|---|---|
| dev | 21 | 20 | 1 | 1.00 (22/22) | 1.00 (22/22) | 21/21 | 22/22 |
| holdout | 32 | 17 | 15 | 1.00 (32/32) | 1.00 (32/32) | 32/32 | 32/32 |

| Rule | dev P / R (n) | holdout P / R (n) |
|---|---|---|
| KT001 crash-loop | 1.00 / 1.00 (3) | 1.00 / 1.00 (6) |
| KT002 image-pull | 1.00 / 1.00 (3) | 1.00 / 1.00 (4) |
| KT003 oom-killed | 1.00 / 1.00 (3) | 1.00 / 1.00 (2) |
| KT004 unschedulable | 1.00 / 1.00 (5) | 1.00 / 1.00 (6) |
| KT005 config-missing | 1.00 / 1.00 (2) | 1.00 / 1.00 (4) |
| KT006 probe-failing | 1.00 / 1.00 (2) | 1.00 / 1.00 (5) |
| KT007 evicted | 1.00 / 1.00 (1) | 1.00 / 1.00 (1) |
| KT008 floating-image-tag | 1.00 / 1.00 (1) | 1.00 / 1.00 (1) |
| KT009 no-memory-limit | 1.00 / 1.00 (1) | 1.00 / 1.00 (2) |
| KT010 node-problem | 1.00 / 1.00 (1) | 1.00 / 1.00 (1) |

<!-- RESULTS:END -->

**Read the holdout row carefully.** The holdout stopped being clean the first time it was run. The first run, with the rules exactly as frozen, scored:

| First run on the holdout (rules unchanged) | Precision | Recall |
|---|---|---|
| Hand-built fixtures (17) | 1.00 (17/17) | 1.00 (17/17) |
| Captured from a real kind cluster (15) | 1.00 (12/12) | 0.80 (12/15) |
| All 32 | 1.00 (29/29), 95% interval 0.88 to 1.00 | 0.91 (29/32), 95% interval 0.76 to 0.97 |

That run found two real defects (below). I fixed them, and the table above, at 1.00 everywhere, is the result **after** those fixes: it is a regression suite now, not an unbiased estimate of accuracy. The honest numbers are the first-run ones. The saved first-run results are in `results/holdout_first_run.json` and `results/live_check_first_run.json`, committed before any fix (`97a6320`), so the history can be checked.

The gap between the two lines of that first run is the main lesson: the fixtures I built by hand, from my own understanding of what Kubernetes prints, passed 17 of 17. A real cluster found three misses in 15.

### 2. A live check against a real cluster

`broken/` holds 15 manifests, each with a header saying what the tool should say about it (`# expect: KT004/taint`). `scripts/live_check.py` applies them to a throwaway cluster, waits, runs `python -m kubetriage diagnose -n kt-demo` and checks each. The same run captures the cluster's JSON as the real-cluster fixtures above.

| | Result |
|---|---|
| First run (rules as frozen) | 12 of 15 scenarios diagnosed as expected |
| After the fixes below | 15 of 15 (`results/live_check.json`), no unexpected extra findings |

Environment: kind v0.33.0 (binary sha256-verified), node image Kubernetes v1.37.0 on containerd, one node, Docker Desktop on WSL2; kubectl v1.36.1.

### What the real cluster found

1. **A crash loop looks different between restarts.** A looping container alternates between `waiting: CrashLoopBackOff` and `terminated: Error`. My rule only recognised the first, because every hand-built fixture showed that pose. A snapshot taken in the second phase was missed (2 of the 3 misses). KT001 now also fires for a container that is terminated, has restarted at least twice, and will be restarted by its `restartPolicy`.
2. **A failure class I had not named.** My first OOM scenario used a 24Mi limit. That is too low for the container runtime to even set the container up (`RunContainerError`, "container init was OOM-killed (memory limit too low?)"), so it never ran, let alone leaked. The scenario was my mistake, but the output is a real thing people hit, so KT003 now has a `limit-too-low-to-start` variant. That capture's label was written after I saw the output, so it lives in **dev**, not holdout.
3. **My replacement scenario was also wrong.** A 48Mi limit still failed to start on this WSL2 node. The manifest now uses 96Mi and a shell that holds about 200 MB; it OOM-kills at run time as intended and was re-captured. The label for that scenario (`KT003/limit-set`) was written before either capture.

One more bug was caught by a test rather than the cluster: two evidence lines said `spec.tolerations = []` when the field was absent. They now say `spec.tolerations is not set`, which is what the evidence test checks.

## How the measurement is kept honest

- **Split before looking.** Rules and dev fixtures were committed first (`395b153`). Holdout fixtures were written afterwards and labelled before the first run.
- **Real data, not only my own.** 16 of 53 fixtures come from a real cluster, with labels written in `broken/*.yaml` before capture.
- **The evaluator cannot leak.** Tests check that `evaluate(splits=("dev",))` never opens a holdout file, that changing holdout labels cannot change dev results, and that the rule code never reads fixtures or labels.
- **Hand-computed metrics.** The scoring code is tested against a small case worked out by hand.
- **Disclosed changes.** Every change made after the first holdout run is listed above, with commits (`8ce678c` crash loop, `ecfae7e` OOM variant).

## Layout

```
kubetriage/        quantity, model (reads kubectl JSON), rules (KT001..KT010), engine (rank + group), render,
                   kubectl (live fetch), explain (optional model), evaluate (scoring), cli
fixtures/          dev/ and holdout/ documents, manifest.json with the expected findings
broken/            15 deliberately broken manifests, each with an `# expect:` header
scripts/           live_check.py (apply broken/ to a cluster, check, optionally capture fixtures)
tools/             fixture builders (independent of the rules) and the demo sample generator
sample/shop.json   the namespace the demo diagnoses
results/           summary.json, live_check.json, and the first-run records before any fix
tests/             149 tests
```

To repeat the live check yourself (it creates failing workloads and taints nodes, so use a throwaway cluster):

```
kind create cluster --name kubetriage-demo
python scripts/live_check.py                 # add --capture to refresh the real-cluster fixtures
kind delete cluster --name kubetriage-demo
```

## Limits

- **Rules, not machine learning.** It recognises failure shapes it has been taught. An unfamiliar failure produces no finding, or an `unknown` variant, rather than a guess.
- **It reads message text.** Several rules depend on the wording of kubelet, scheduler and container-runtime messages, which differ across versions and runtimes. Real messages were checked on one stack only: containerd on Kubernetes v1.37.0. Docker-style and older messages are hand-written from memory.
- **Mostly my own fixtures.** 37 of 53 fixtures are hand-built by the same person who wrote the rules, so they test my understanding as much as the code. Only 16 are real, and they come from one single-node cluster.
- **Small samples.** 32 holdout predictions; the 95% intervals are wide (shown above). Nothing here estimates how it would do on a customer's cluster.
- **Two rules are fixture-only.** KT007 (eviction) and KT010 (node conditions) cannot be provoked safely on a one-node kind cluster, so they have never seen real cluster output.
- **A snapshot, not a history.** It reads current state and events, which expire after about an hour. It does not read logs or metrics, so a crash with exit code 1 is reported as "read the logs", not explained.
- **Capacity checks are rough.** It compares a request with the largest node's allocatable, not with what is free. With `-n` it sees only that namespace's pods, so it cannot compute headroom.
- **Ownership is guessed from names.** A Deployment is recovered from its ReplicaSet's name and hash, and a CronJob from a numeric Job suffix.
- **Out of scope.** CRDs and operators, Helm and GitOps controllers, service meshes, network policy, RBAC denials, and a PodDisruptionBudget hint.
- **Optional model path is untested live** (see `--explain`).
- **Python.** Tested on 3.13 only.

## What I would do at a customer

- Ask for a read-only role (`get` and `list` on pods, events and nodes) and run it on their staging cluster first. It never writes.
- Collect their real incident JSON, scrubbed, with their SRE's one-line label for each, and use it to replace my hand-built fixtures. Their failures are the ones that matter.
- Agree the metric before tuning: time from alert to correct cause, measured on past incidents, against what their on-call does today.
- Add logs and previous-container state as an opt-in input, since the exit code alone leaves the biggest class (exit 1) unexplained.
- Run `diagnose --fail-on high` as a post-deploy smoke check in their pipeline, so a bad rollout is explained in the pipeline output.
- Treat event text and image names as customer data: nothing leaves the machine unless `--explain` is on, and that path is redacted and limited to the findings.

## License

MIT
