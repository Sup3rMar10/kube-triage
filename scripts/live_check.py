"""Apply the deliberately broken manifests in broken/ to a cluster and check that `kubetriage diagnose` finds each.

Needs a throwaway cluster (for example `kind create cluster`) and kubectl pointed at it (KUBECONFIG or --context).
It creates the namespace kt-demo, taints every node with kt-demo=true:NoSchedule (so the pending-taint scenario
is real; every other manifest tolerates the taint), applies broken/*.yaml, then polls the live diagnosis until each
scenario shows its expected finding or the timeout passes. It deletes the namespace and the taint when it is done.

    python scripts/live_check.py                     # check, write results/live_check.json, clean up
    python scripts/live_check.py --capture           # also save the cluster's JSON as fixtures/holdout/kind-*.json
    python scripts/live_check.py --keep              # leave the scenarios running

Do not point this at a cluster you care about: it creates failing workloads and taints nodes.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
NAMESPACE = "kt-demo"
TAINT = "kt-demo=true:NoSchedule"


def parse_expect(path: Path):
    """[(rule, variant|None)] from the manifest's `# expect:` header; `none` means no findings."""
    m = re.search(r"^# expect: (.+)$", path.read_text(encoding="utf-8"), re.M)
    if not m:
        raise ValueError(f"{path.name}: missing '# expect:' header")
    if m.group(1).strip() == "none":
        return []
    out = []
    for tok in m.group(1).split(","):
        rule, _, variant = tok.strip().partition("/")
        out.append((rule, variant or None))
    return out


def scenarios(broken_dir: Path):
    return {p.stem: parse_expect(p) for p in sorted(broken_dir.glob("*.yaml"))}


def kubectl(args, kube, check=True, input_=None):
    cmd = [kube["bin"], *(["--context", kube["context"]] if kube["context"] else []), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, input=input_)
    if check and proc.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def diagnose_live(kube):
    cmd = [sys.executable, "-m", "kubetriage", "diagnose", "-n", NAMESPACE, "--json", "--min-severity", "info", "--kubectl", kube["bin"]]
    if kube["context"]:
        cmd += ["--context", kube["context"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode not in (0, 1):
        raise SystemExit(f"kubetriage failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def judge(report, wanted):
    """{scenario: {found, missing, extras, passed}} from a diagnose --json report."""
    by_workload = {}
    for w in report["workloads"]:
        name = w["workload"].split("/", 1)[1]
        by_workload[name] = {(f["rule"], f["variant"]) for f in w["findings"]}
    out = {}
    for name, expect in wanted.items():
        got = by_workload.get(name, set())
        missing = [e for e in expect if not any(g[0] == e[0] and (e[1] is None or g[1] == e[1]) for g in got)]
        matched = {g for g in got if any(g[0] == e[0] and (e[1] is None or g[1] == e[1]) for e in expect)}
        out[name] = {"expected": [f"{r}/{v}" if v else r for r, v in expect],
                     "found": sorted(f"{r}/{v}" for r, v in got), "missing": [f"{r}/{v}" if v else r for r, v in missing],
                     "extras": sorted(f"{r}/{v}" for r, v in got - matched), "passed": not missing and not (not expect and got)}
    return out


def capture(kube, wanted, split_dir_name="holdout"):
    sys.path.insert(0, str(ROOT / "tools"))
    from fixturelib import write_fixture
    from kubetriage.kubectl import fetch

    doc, _ = fetch(namespace=NAMESPACE, kubectl=kube["bin"], context=kube["context"])
    version = json.loads(kubectl(["version", "-o", "json"], kube).stdout).get("serverVersion", {}).get("gitVersion", "unknown")
    items = doc["items"]
    nodes = [i for i in items if i["kind"] == "Node"]
    for name, expect in wanted.items():
        pods = [i for i in items if i["kind"] == "Pod" and (i["metadata"].get("labels") or {}).get("kt-scenario") == name]
        names = {p["metadata"]["name"] for p in pods}
        events = [i for i in items if i["kind"] == "Event" and i["involvedObject"].get("kind") == "Pod" and i["involvedObject"].get("name") in names]
        expected = [{"subject": p["metadata"]["name"], "rule": r, **({"variant": v} if v else {})}
                    for p in sorted(pods, key=lambda p: p["metadata"]["name"]) for r, v in expect]
        sub = {"apiVersion": "v1", "kind": "List", "metadata": {"resourceVersion": ""}, "items": pods + events + nodes}
        write_fixture(split_dir_name, f"kind-{name}", sub, expected, source="kind",
                      note=f"captured from a kind cluster running Kubernetes {version}; labels written in broken/{name}.yaml before capture")
        print(f"captured {name}: {len(pods)} pod(s), {len(events)} event(s)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for every scenario (default 300)")
    ap.add_argument("--settle", type=int, default=20, help="extra seconds after all pass, before capturing")
    ap.add_argument("--capture", action="store_true", help="save the cluster JSON as fixtures/holdout/kind-*.json")
    ap.add_argument("--keep", action="store_true", help="do not delete the namespace or taint afterwards")
    ap.add_argument("--out", default=str(ROOT / "results" / "live_check.json"))
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--context")
    ap.add_argument("--broken", default=str(ROOT / "broken"))
    ap.add_argument("--only", help="comma-separated scenario names to run (default: all)")
    args = ap.parse_args(argv)
    kube = {"bin": args.kubectl, "context": args.context}
    wanted = scenarios(Path(args.broken))
    if args.only:
        wanted = {n: wanted[n] for n in args.only.split(",")}

    kubectl(["cluster-info"], kube)
    print(f"cluster ok; applying {len(wanted)} scenarios to namespace {NAMESPACE}")
    for _ in range(30):      # a namespace deleted a moment ago may still be terminating
        if kubectl(["get", "namespace", NAMESPACE, "-o", "jsonpath={.status.phase}"], kube, check=False).stdout != "Terminating":
            break
        time.sleep(5)
    kubectl(["create", "namespace", NAMESPACE], kube, check=False)
    kubectl(["taint", "nodes", "--all", TAINT, "--overwrite"], kube)
    for name in wanted:
        kubectl(["apply", "-f", str(Path(args.broken) / f"{name}.yaml")], kube)
    start, last, results = time.time(), None, {}
    try:
        while True:
            time.sleep(10)
            results = judge(diagnose_live(kube), wanted)
            waiting_on = [n for n, r in results.items() if not r["passed"]]
            elapsed = int(time.time() - start)
            if waiting_on != last:
                print(f"[{elapsed:>3}s] still waiting on: {', '.join(waiting_on) or 'nothing'}")
                last = waiting_on
            if not waiting_on or elapsed >= args.timeout:
                break
        if args.capture:
            time.sleep(args.settle)
            results = judge(diagnose_live(kube), wanted)
            capture(kube, wanted)
    finally:
        if not args.keep:
            kubectl(["delete", "namespace", NAMESPACE, "--wait=true", "--timeout=180s"], kube, check=False)
            kubectl(["taint", "nodes", "--all", "kt-demo-"], kube, check=False)

    passed = sum(1 for r in results.values() if r["passed"])
    summary = {"scenarios": len(results), "passed": passed, "seconds": int(time.time() - start), "results": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for name, r in results.items():
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {name:<24} expected {r['expected'] or 'none'}  found {r['found'] or 'none'}")
    print(f"{passed}/{len(results)} scenarios diagnosed as expected; wrote {args.out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
