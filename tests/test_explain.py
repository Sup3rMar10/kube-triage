import json

import pytest

from fixturelib import container, cstatus, k8s_list, pod, term, waiting
from kubetriage.engine import diagnose
from kubetriage.explain import SYSTEM, AnthropicExplainer, build_prompt, explain, redact

SECRET = "hunter2-super-secret-value"


def report_with_secret_in_pod():
    c = container("app", "registry.example.com/app:1.0", env=[
        {"name": "DB_PASSWORD", "value": SECRET},
        {"name": "DB_HOST", "valueFrom": {"configMapKeyRef": {"name": "cfg", "key": "DB_HOST"}}}])
    p = pod("p", "t", containers=[c], phase="Pending", statuses=[cstatus("app", "registry.example.com/app:1.0",
            state=waiting("CreateContainerConfigError", "couldn't find key DB_HOST in ConfigMap t/cfg"))])
    return diagnose(k8s_list(p)).to_dict()


def test_literal_env_values_never_reach_the_report_or_the_prompt():
    d = report_with_secret_in_pod()
    assert SECRET not in json.dumps(d)
    assert SECRET not in build_prompt(d)


@pytest.mark.parametrize("text,gone", [
    ("password=hunter2", "hunter2"), ('"token": "abc123def456"', "abc123def456"), ("api_key: sk-live-9999", "sk-live-9999"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
    ("pull https://user:p4ss@registry.example.com/x", "p4ss"),
    ("blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5MDEyMzQ1Njc4OQ==", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVow"),
])
def test_redaction(text, gone):
    assert gone not in redact(text)
    assert "<redacted" in redact(text)


def test_ordinary_text_is_left_alone():
    s = 'status.containerStatuses[app].lastState.terminated.exitCode = 137'
    assert redact(s) == s


def test_explain_passes_findings_only_and_returns_the_models_text():
    d = report_with_secret_in_pod()
    seen = {}

    def fake_llm(system, prompt):
        seen["system"], seen["prompt"] = system, prompt
        return "  The ConfigMap is missing a key.  "

    assert explain(d, fake_llm) == "The ConfigMap is missing a key."
    assert seen["system"] == SYSTEM and "do not add causes" in SYSTEM.lower()
    assert "KT005" in seen["prompt"] and "Deployment" not in seen["prompt"] or "Pod/p" in seen["prompt"]
    assert "kubectl get" not in seen["prompt"].split("fix:")[0]      # raw kubectl output is not included


def test_explain_with_nothing_to_report_does_not_call_the_model():
    def boom(*a):
        raise AssertionError("should not be called")

    assert explain(diagnose(k8s_list()).to_dict(), boom) == ""


def test_anthropic_adapter_fails_clearly_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", type("m", (), {})())   # present, but no key
    with pytest.raises(SystemExit) as e:
        AnthropicExplainer()("sys", "prompt")
    assert "ANTHROPIC_API_KEY" in str(e.value)


def test_model_name_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("KUBETRIAGE_MODEL", "some-model")
    assert AnthropicExplainer().model == "some-model"
