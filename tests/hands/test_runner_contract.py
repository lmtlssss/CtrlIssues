"""Focused fixtures for the bounded local browser runner; not run during construction."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "plugins/ctrlissues/hands/main.py"


def load_runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("ctrlissues_hands_main_fixture", RUNNER)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def mission():
    return {
        "schema": "ctrlissues.browser.v1",
        "url": "https://example.test/search",
        "goal": "Search for the supplied item.",
        "allowed_origins": ["https://example.test"],
        "allowed_actions": ["fill", "click"],
        "fields": [{"id": "query", "target": {"name": "q", "label": "Search"}, "value": "exact literal"}],
        "limits": {"steps": 4, "decisions": 6, "seconds": 30},
        "expected": {"url": {"equals": "https://example.test/results"}, "dom": [
            {"selector": "main h1", "property": "text", "equals": "Results"}
        ]},
    }


def test_result_page_can_omit_future_or_completed_fields(monkeypatch):
    runner = load_runner(monkeypatch)
    current = runner.validate_mission(mission())
    values, nodes, facts = runner.map_fields(current, [{"id": "continue", "kind": "click", "node": 7, "label": "Continue"}])
    offered = runner.filtered_page(
        {"actions": [{"id": "continue", "kind": "click", "node": 7, "label": "Continue"}]},
        current,
        values,
        nodes,
    )
    assert values == {} and nodes == set()
    assert facts == [{"field_id": "query", "status": "missing"}]
    assert [action["id"] for action in offered["actions"]] == ["continue"]


def test_harmless_email_field_is_mapped_and_not_classified_as_a_secret(monkeypatch):
    runner = load_runner(monkeypatch)
    value = mission()
    value["fields"] = [{"id": "contact", "target": {"name": "email", "label": "Contact email"}, "value": "person@example.test"}]
    actions = [{"id": "email", "kind": "fill", "node": 8, "identity": 8, "name": "email", "label": "Contact email"}]
    values, nodes, facts = runner.map_fields(runner.validate_mission(value), actions)
    assert values == {"email": "person@example.test"}
    assert nodes == {8}
    assert facts[0]["status"] == "mapped"
    assert not runner.field_is_sensitive(value["fields"][0])


def test_readback_mismatch_keeps_readable_expected_and_actual_values(monkeypatch):
    runner = load_runner(monkeypatch)

    class Browser:
        def evaluate(self, _script):
            return {"url": "https://example.test/results", "title": "Wrong page", "dom": [{"status": "observed", "value": "Not results"}]}

    facts = runner.fresh_readback(Browser(), runner.validate_mission(mission())["expected"])
    assert facts[0]["status"] == "verified"
    assert facts[1] == {"property": "dom[0].text", "status": "failed", "actual": "Not results", "expected": "Results"}


def test_narrow_action_selectors_are_batched_and_filter_observed_nodes(monkeypatch):
    runner = load_runner(monkeypatch)
    value = mission()
    value["action_selectors"] = {"click": ["button#continue"]}
    checked = []

    class Browser:
        def evaluate(self, script):
            checked.append(script)
            return [["click", 7]]

    current = runner.validate_mission(value)
    actions = [
        {"id": "continue", "kind": "click", "node": 7, "identity": 7},
        {"id": "other", "kind": "click", "node": 9, "identity": 9},
    ]
    assert [a["id"] for a in runner.filter_action_selectors(Browser(), current, actions)] == ["continue"]
    assert len(checked) == 1


def test_already_satisfied_goal_finishes_without_semantic_choice(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch)
    value = mission()
    value["fields"] = []
    data = tmp_path / "data"
    data.mkdir()
    source = tmp_path / "mission.json"
    source.write_text(json.dumps(value))
    monkeypatch.setenv("CTRLISSUES_DATA", str(data))
    monkeypatch.setenv("CTRLISSUES_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    page = {"url": "https://example.test/results", "fingerprint": "fp", "actions": []}
    choices = []

    class Browser:
        def __init__(self):
            self.observations = 0

        def evaluate(self, _script):
            return {"url": "https://example.test/results", "title": "Results", "dom": [{"status": "observed", "value": "Results"}]}

        def close(self):
            pass

    browser = Browser()

    class Agent:
        def __init__(self, *_args, **_kwargs):
            self.browser = browser
            self.state = {"page": page, "history": [], "status": "ready"}

        def close(self):
            pass

    monkeypatch.setattr(runner, "Agent", Agent)
    monkeypatch.setattr(runner, "model", SimpleNamespace(CLIENT=SimpleNamespace(timeout=None)))
    monkeypatch.setattr(runner, "choose", lambda *_args: choices.append("called"))
    monkeypatch.setattr(runner, "cdp", lambda *_args, **_kwargs: None)
    assert runner.run(["--input", str(source), "--report", "hands/reports/done.json"]) == 0
    assert choices == []
    result = json.loads((data / "hands/reports/done.json").read_text())
    assert result["status"] == "success"


def test_scoped_freshness_and_uncertain_dispatch_preserve_attempt_evidence(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch)
    value = mission()
    value["fields"] = []
    value["allowed_actions"] = ["click"]
    data = tmp_path / "data"
    report_dir = data / "hands/reports"
    report_dir.mkdir(parents=True)
    prior = report_dir / "attempt.json"
    prior.write_text('{"status":"success","old":true}')
    source = tmp_path / "mission.json"
    source.write_text(json.dumps(value))
    monkeypatch.setenv("CTRLISSUES_DATA", str(data))
    monkeypatch.setenv("CTRLISSUES_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    page = {"url": value["url"], "fingerprint": "before", "actions": [{"id": "e1", "kind": "click", "node": 7, "identity": 7, "label": "Continue", "role": "button"}]}
    fresh_calls = []

    class Browser:
        def __init__(self):
            self.observations = 0

        def evaluate(self, script):
            if script == "location.href":
                return value["url"]
            return {"url": value["url"], "title": "Search", "dom": [{"status": "missing"}]}

        def fresh(self, observed_page, action):
            fresh_calls.append((observed_page["fingerprint"], action["id"]))
            return True

    browser = Browser()

    class Agent:
        def __init__(self, *_args, **_kwargs):
            self.browser = browser
            self.state = {"page": page, "history": [], "status": "ready"}

        def command(self, name, _body):
            assert name == "act"
            raise RuntimeError("transport ended after dispatch began")

        def close(self):
            pass

    monkeypatch.setattr(runner, "Agent", Agent)
    monkeypatch.setattr(runner, "model", SimpleNamespace(CLIENT=SimpleNamespace(timeout=None)))
    monkeypatch.setattr(runner, "choose", lambda *_args: {"choice": "e1"})
    monkeypatch.setattr(runner, "cdp", lambda *_args, **_kwargs: None)
    assert runner.run(["--input", str(source), "--report", "hands/reports/attempt.json"]) == 1
    assert len(fresh_calls) == 1
    archives = list(report_dir.glob("attempt.json.previous-*"))
    assert len(archives) == 1 and json.loads(archives[0].read_text())["status"] == "success"
    report = json.loads(prior.read_text())
    assert report["status"] == "unknown"
    journal = [json.loads(line) for line in (data / "hands/runs.jsonl").read_text().splitlines()]
    phases = [item["phase"] for item in journal]
    assert phases == ["started", "before_action", "action_outcome_uncertain", "finished"]
    assert journal[1]["action_id"] == "e1"
