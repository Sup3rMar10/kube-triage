"""Live mode: fetch pods, events and nodes with kubectl. This is the only place that talks to a cluster."""
from __future__ import annotations

import json
import subprocess


class KubectlError(RuntimeError):
    pass


def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise KubectlError(f"{cmd[0]!r} not found; install kubectl or pass --kubectl PATH") from None
    except subprocess.TimeoutExpired:
        raise KubectlError(f"{' '.join(cmd)} timed out after {timeout}s") from None


def fetch(namespace=None, all_namespaces=False, kubectl="kubectl", context=None, timeout=60):
    """Return (document, warnings). Falls back to pods+events when nodes are not readable (RBAC)."""
    base = [kubectl] + (["--context", context] if context else [])
    scope = ["-A"] if all_namespaces else (["-n", namespace] if namespace else [])
    warnings = []
    for resources in ("pods,events,nodes", "pods,events"):
        proc = _run(base + ["get", resources, *scope, "-o", "json"], timeout)
        if proc.returncode == 0:
            if resources == "pods,events":
                warnings.append("could not read nodes (RBAC?); node capacity checks are skipped")
            try:
                return json.loads(proc.stdout), warnings
            except json.JSONDecodeError as e:
                raise KubectlError(f"kubectl returned invalid JSON: {e}") from None
        err = proc.stderr.strip()
        if resources == "pods,events" or "nodes" not in err.lower() or "forbidden" not in err.lower():
            raise KubectlError(err or f"kubectl exited with {proc.returncode}")
    raise KubectlError("unreachable")
