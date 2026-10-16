"""The broken/ manifests and the live-check script's parsing and judging, which need no cluster."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import live_check  # noqa: E402
from kubetriage.rules import RULE_INFO  # noqa: E402

BROKEN = ROOT / "broken"


def test_every_manifest_declares_its_expectation():
    wanted = live_check.scenarios(BROKEN)
    assert len(wanted) >= 15
    for name, expect in wanted.items():
        assert (BROKEN / f"{name}.yaml").exists()
        for rule, variant in expect:
            assert rule in RULE_INFO


def test_manifest_names_match_scenarios_and_use_the_throwaway_namespace():
    for p in BROKEN.glob("*.yaml"):
        text = p.read_text(encoding="utf-8")
        assert f"# scenario: {p.stem}" in text
        assert "namespace: kt-demo" in text
        assert f"name: {p.stem}" in text and f"kt-scenario: {p.stem}" in text


def test_every_cluster_observable_rule_has_a_scenario():
    covered = {r for expect in live_check.scenarios(BROKEN).values() for r, _ in expect}
    # KT007 (eviction under node pressure) and KT010 (node conditions) cannot be provoked safely on a one-node kind cluster
    assert covered == set(RULE_INFO) - {"KT007", "KT010"}


def test_only_the_taint_scenario_lacks_the_toleration():
    for p in BROKEN.glob("*.yaml"):
        has = "key: kt-demo, operator: Exists" in p.read_text(encoding="utf-8")
        assert has == (p.stem != "pending-taint"), p.name


def test_expect_header_parsing(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("# expect: KT008/latest, KT009\n", encoding="utf-8")
    assert live_check.parse_expect(f) == [("KT008", "latest"), ("KT009", None)]
    f.write_text("# expect: none\n", encoding="utf-8")
    assert live_check.parse_expect(f) == []
    f.write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(ValueError):
        live_check.parse_expect(f)


def report(**by_workload):
    return {"workloads": [{"workload": f"Deployment/{n}", "findings": [{"rule": r, "variant": v} for r, v in fs]}
                          for n, fs in by_workload.items()]}


def test_judge_requires_every_expected_finding_and_allows_unspecified_variants():
    wanted = {"a": [("KT001", "exit-1")], "b": [("KT008", None), ("KT009", None)], "c": []}
    r = live_check.judge(report(a=[("KT001", "exit-1")], b=[("KT008", "latest")]), wanted)
    assert r["a"]["passed"] is True
    assert r["b"]["passed"] is False and r["b"]["missing"] == ["KT009"]
    assert r["c"]["passed"] is True                      # expected nothing, found nothing
    r = live_check.judge(report(a=[("KT001", "exit-2")], c=[("KT004", "taint")]), wanted)
    assert r["a"]["passed"] is False and r["c"]["passed"] is False


def test_demo_runs_on_the_bundled_sample():
    out = subprocess.run([sys.executable, "-m", "kubetriage", "demo"], cwd=ROOT, capture_output=True, text=True).stdout
    for needle in ("Deployment/web", "2 of 3 pods", "KT003", "KT005", "KT002", "KT004", "KT008", "StatefulSet/orders-db"):
        assert needle in out, needle
    first_workloads = [l.split()[1] for l in out.splitlines() if l[:4] in ("HIGH", "LOW ")]
    assert first_workloads[-1] == "Deployment/cache"       # hygiene-only workload ranks last


def test_fixture_counts_meet_the_published_claim():
    from kubetriage.evaluate import load_manifest
    entries = load_manifest(ROOT / "fixtures")
    assert len(entries) >= 25
    assert sum(1 for e in entries if e["split"] == "holdout") >= 15
    assert sum(1 for e in entries if e["source"] == "kind") >= 15
