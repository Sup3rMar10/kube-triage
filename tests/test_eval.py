import json
import re
from pathlib import Path

import pytest

from fixturelib import cstatus, container, k8s_list, node, pod, term, waiting
from kubetriage import evaluate as ev
from kubetriage.evaluate import evaluate, load_manifest, render_markdown, update_readme, wilson
from kubetriage.rules import RULE_INFO

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
IMG = "registry.example.com/app:1.0"


def crash_pod(code, reason="Error"):
    return pod("p", "t", statuses=[cstatus("app", IMG, state=waiting("CrashLoopBackOff"), last=term(code, reason=reason), restarts=3)])


def make_dir(tmp_path, holdout_expected=None):
    """dev: A (1 TP), B (1 FP), C (1 TP + 1 FN). holdout: D (one more TP)."""
    files = {
        "dev/a.json": k8s_list(crash_pod(1)),
        "dev/b.json": k8s_list(crash_pod(139)),
        "dev/c.json": k8s_list(crash_pod(137, reason="OOMKilled")),
        "holdout/d.json": k8s_list(crash_pod(1)),
    }
    for rel, doc in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(json.dumps(doc), encoding="utf-8")
    fx = [
        {"name": "dev-a", "file": "dev/a.json", "split": "dev", "source": "hand", "expected": [{"subject": "p", "rule": "KT001", "variant": "exit-1"}]},
        {"name": "dev-b", "file": "dev/b.json", "split": "dev", "source": "hand", "expected": []},
        {"name": "dev-c", "file": "dev/c.json", "split": "dev", "source": "hand",
         "expected": [{"subject": "p", "rule": "KT003", "variant": "limit-set"}, {"subject": "p", "rule": "KT001", "variant": "exit-137"}]},
        {"name": "hold-d", "file": "holdout/d.json", "split": "holdout", "source": "kind",
         "expected": holdout_expected if holdout_expected is not None else [{"subject": "p", "rule": "KT001", "variant": "exit-1"}]},
    ]
    (tmp_path / "manifest.json").write_text(json.dumps({"fixtures": fx}), encoding="utf-8")
    return tmp_path


def test_metrics_match_a_hand_computation(tmp_path):
    s = evaluate(make_dir(tmp_path))
    dev = s["splits"]["dev"]
    assert (dev["overall"]["tp"], dev["overall"]["fp"], dev["overall"]["fn"]) == (2, 1, 1)
    assert dev["overall"]["precision"] == pytest.approx(0.667, abs=1e-3)
    assert dev["overall"]["recall"] == pytest.approx(0.667, abs=1e-3)
    assert dev["per_rule"]["KT001"] == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5}
    assert dev["per_rule"]["KT003"]["precision"] == 1.0
    assert dev["variant_accuracy"] == {"correct": 2, "total": 2, "accuracy": 1.0}
    assert dev["fixtures_exact"] == {"exact": 1, "total": 3}
    kinds = sorted((m["fixture"], m["kind"]) for m in s["mismatches"]["dev"])
    assert kinds == [("dev-b", "false-positive"), ("dev-c", "missed")]
    assert s["fixtures"]["holdout"] == {"count": 1, "hand": 0, "kind": 1}


def test_wrong_variant_is_counted_separately_from_a_wrong_rule(tmp_path):
    d = make_dir(tmp_path, holdout_expected=[{"subject": "p", "rule": "KT001", "variant": "exit-2"}])
    h = evaluate(d)["splits"]["holdout"]
    assert h["overall"]["tp"] == 1 and h["overall"]["fp"] == 0       # the rule was right
    assert h["variant_accuracy"] == {"correct": 0, "total": 1, "accuracy": 0.0}


def test_dev_evaluation_never_opens_holdout_files(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    opened = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    evaluate(d, splits=("dev",))
    assert "d.json" not in opened and {"a.json", "b.json", "c.json"} <= set(opened)


def test_changing_holdout_labels_cannot_change_dev_results(tmp_path):
    a = evaluate(make_dir(tmp_path / "x", holdout_expected=[]), splits=("dev",))
    b = evaluate(make_dir(tmp_path / "y", holdout_expected=[{"subject": "p", "rule": "KT010"}]), splits=("dev",))
    assert a["splits"] == b["splits"] and a["mismatches"] == b["mismatches"]


def test_the_rules_never_read_fixtures_or_labels():
    for name in ("rules.py", "engine.py", "model.py", "render.py", "findings.py", "quantity.py"):
        text = (ROOT / "kubetriage" / name).read_text(encoding="utf-8").lower()
        assert not re.search(r"fixtures|holdout|manifest\.json|expected", text), name


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(10, 10)
    assert 0.70 < lo < 0.75 and hi == 1.0
    lo, hi = wilson(50, 100)
    assert 0.39 < lo < 0.41 and 0.59 < hi < 0.61


def test_eval_is_deterministic(tmp_path):
    d = make_dir(tmp_path)
    assert evaluate(d) == evaluate(d)


def test_markdown_and_readme_update(tmp_path):
    md = render_markdown(evaluate(make_dir(tmp_path / "f")))
    assert "KT001 crash-loop" in md and "dev" in md and "holdout" in md
    readme = tmp_path / "README.md"
    readme.write_text("a\n<!-- RESULTS:START -->\nSTALE-CONTENT\n<!-- RESULTS:END -->\nb\n", encoding="utf-8")
    assert update_readme(readme, md) is True
    text = readme.read_text(encoding="utf-8")
    assert "STALE-CONTENT" not in text and "KT001" in text and text.startswith("a\n") and text.rstrip().endswith("b")
    readme.write_text("no markers here", encoding="utf-8")
    assert update_readme(readme, md) is False


# ---- the real fixtures -----------------------------------------------------------------------------

def test_manifest_is_consistent_with_the_files():
    entries = load_manifest(FIXTURES)
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names))
    on_disk = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.glob("*/*.json")}
    assert on_disk == {e["file"] for e in entries}, "fixture files and manifest entries differ"
    for e in entries:
        assert e["split"] in ("dev", "holdout") and e["source"] in ("hand", "kind")
        assert e["file"].startswith(e["split"] + "/")
        doc = json.loads((FIXTURES / e["file"]).read_text(encoding="utf-8"))
        subjects = {i["metadata"]["name"] for i in doc["items"] if i["kind"] in ("Pod", "Node")}
        for x in e["expected"]:
            assert x["rule"] in RULE_INFO, f"{e['name']}: unknown rule {x['rule']}"
            assert x["subject"] in subjects, f"{e['name']}: expected subject {x['subject']} is not in the document"


def test_fixtures_contain_no_machine_specific_details():
    banned = ["sudh1", "C:\\", "C:/", "/Users/", "AppData", "kiran", "@gmail", "billtrust"]
    for base in ("fixtures", "sample", "broken"):
        for p in (ROOT / base).rglob("*"):
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                for b in banned:
                    assert b.lower() not in text.lower(), f"{p.relative_to(ROOT)} contains {b!r}"
