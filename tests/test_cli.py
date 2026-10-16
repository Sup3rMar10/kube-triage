import json
import subprocess
import sys
from pathlib import Path

import pytest

from kubetriage import kubectl
from kubetriage.cli import main

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "fixtures" / "dev"


def cli(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "kubetriage", *args], cwd=ROOT, input=stdin,
                          capture_output=True, text=True)


def test_text_output_names_the_workload_cause_evidence_and_fix():
    r = cli("diagnose", "--file", str(DEV / "oom-crashloop.json"))
    assert r.returncode == 0
    assert "Deployment/report" in r.stdout and "KT003" in r.stdout
    assert "128Mi" in r.stdout and "evidence:" in r.stdout and "fix:" in r.stdout


def test_json_output_parses():
    r = cli("diagnose", "--file", str(DEV / "crashloop-exit1.json"), "--json")
    d = json.loads(r.stdout)
    assert d["summary"]["findings"] == 2
    assert d["workloads"][0]["findings"][0]["rule"] == "KT001"


def test_stdin_input():
    r = cli("diagnose", "--file", "-", "--json", stdin=(DEV / "evicted-memory.json").read_text(encoding="utf-8"))
    assert json.loads(r.stdout)["workloads"][0]["findings"][0]["rule"] == "KT007"


def test_fail_on_sets_exit_code():
    assert cli("diagnose", "--file", str(DEV / "crashloop-exit1.json"), "--fail-on", "high").returncode == 1
    assert cli("diagnose", "--file", str(DEV / "healthy.json"), "--fail-on", "info").returncode == 0
    assert cli("diagnose", "--file", str(DEV / "evicted-memory.json"), "--fail-on", "high").returncode == 0   # medium < high


def test_min_severity_hides_low_findings():
    out = cli("diagnose", "--file", str(DEV / "latest-no-limits.json"), "--min-severity", "medium").stdout
    assert "nothing to report" in out
    assert "KT008" in cli("diagnose", "--file", str(DEV / "latest-no-limits.json")).stdout


def test_missing_or_invalid_file_is_an_error_not_a_traceback(tmp_path):
    r = cli("diagnose", "--file", str(tmp_path / "nope.json"))
    assert r.returncode == 3 and "Traceback" not in r.stderr
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    r = cli("diagnose", "--file", str(bad))
    assert r.returncode == 3 and "Traceback" not in r.stderr


def test_rules_command_lists_every_rule():
    out = cli("rules").stdout
    assert all(f"KT0{i:02d}" in out for i in range(1, 11))


def test_explain_is_off_by_default_and_needs_a_key_when_asked(monkeypatch):
    out = cli("diagnose", "--file", str(DEV / "oom-crashloop.json")).stdout
    assert "Plain-English summary" not in out


# ---- live mode with a stand-in kubectl -------------------------------------------------------------

class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_live_mode_runs_one_kubectl_call_and_diagnoses(monkeypatch, capsys):
    calls = []
    doc = (DEV / "oom-crashloop.json").read_text(encoding="utf-8")
    monkeypatch.setattr(kubectl, "_run", lambda cmd, timeout: (calls.append(cmd), Proc(0, doc))[1])
    assert main(["diagnose", "--namespace", "shop", "--json"]) == 0
    assert calls == [["kubectl", "get", "pods,events,nodes", "-n", "shop", "-o", "json"]]
    assert json.loads(capsys.readouterr().out)["workloads"][0]["findings"][0]["rule"] == "KT003"


def test_live_mode_falls_back_when_nodes_are_forbidden(monkeypatch, capsys):
    doc = (DEV / "oom-crashloop.json").read_text(encoding="utf-8")
    seen = []

    def fake(cmd, timeout):
        seen.append(cmd[2])
        if "nodes" in cmd[2]:
            return Proc(1, "", 'Error from server (Forbidden): nodes is forbidden: User "dev" cannot list resource "nodes"')
        return Proc(0, doc)

    monkeypatch.setattr(kubectl, "_run", fake)
    assert main(["diagnose", "-n", "shop", "--json"]) == 0
    assert seen == ["pods,events,nodes", "pods,events"]
    assert "could not read nodes" in capsys.readouterr().err


def test_live_mode_passes_context_and_all_namespaces(monkeypatch):
    calls = []
    monkeypatch.setattr(kubectl, "_run", lambda cmd, timeout: (calls.append(cmd), Proc(0, '{"kind":"List","items":[]}'))[1])
    main(["diagnose", "-A", "--context", "prod", "--kubectl", "/opt/kubectl"])
    assert calls[0] == ["/opt/kubectl", "--context", "prod", "get", "pods,events,nodes", "-A", "-o", "json"]


def test_live_mode_reports_kubectl_errors(monkeypatch, capsys):
    monkeypatch.setattr(kubectl, "_run", lambda cmd, timeout: Proc(1, "", "Unable to connect to the server: dial tcp: connection refused"))
    assert main(["diagnose", "-n", "x"]) == 3
    assert "Unable to connect" in capsys.readouterr().err


def test_missing_kubectl_binary_is_a_clear_error(capsys):
    assert main(["diagnose", "--kubectl", "definitely-not-a-binary-xyz"]) == 3
    assert "not found" in capsys.readouterr().err
