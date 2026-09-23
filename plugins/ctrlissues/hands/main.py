"""Bounded CtrlIssues browser runner over pinned Jev and Browser Harness."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

if os.name == "nt":
    import msvcrt
else:
    import fcntl

VENDORED_JEV = Path(__file__).resolve().parent / "vendor" / "jev_ultrafast"
if str(VENDORED_JEV) not in sys.path:
    sys.path.insert(0, str(VENDORED_JEV))

Agent = None
model = None
choose = None
cdp = None


def require_patched_runtime() -> None:
    if importlib.metadata.version("browser-harness") != "0.1.13":
        raise RuntimeError("pinned browser-harness 0.1.13 runtime is not installed")
    distribution = importlib.metadata.distribution("browser-harness")
    marker = "# CtrlIssues: dotenv loading disabled for foreground browser runs."
    for module in ("admin.py", "helpers.py", "daemon.py"):
        path = Path(distribution.locate_file("browser_harness")) / module
        if marker not in path.read_text(encoding="utf-8"):
            raise RuntimeError("optional browser runtime needs the documented local bootstrap patch")


MAX_INPUT = 65_536
MAX_REPORT = 16_000_000
MAX_STEPS = 60
MAX_DECISIONS = 120
MAX_SECONDS = 300
CDP_DEFAULT = "http://127.0.0.1:9222"
SENSITIVE = re.compile(
    r"(?:^|[\s_-])(password|passcode|secret|token|credential|otp|2fa|mfa|one.?time|verification|cc.?number|cc.?exp|card.?number|card.?exp|cvv|cvc)(?:$|[\s_-])",
    re.I,
)
POSSIBLE_SECRET = re.compile(r"\b(?:[A-Za-z0-9_-]{32,})\b")


class MissionError(ValueError):
    pass


def digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def load_runtime(cdp_url: str) -> None:
    global Agent, model, choose, cdp
    if Agent is not None:
        return
    os.environ["BU_NAME"] = "ctrlissues"
    os.environ["BU_CDP_URL"] = cdp_url
    require_patched_runtime()
    from browser_harness.helpers import cdp as browser_cdp
    from jev_ultrafast import Agent as jev_agent
    from jev_ultrafast import model as jev_model
    from jev_ultrafast.model import choose as jev_choose

    cdp, Agent, model, choose = browser_cdp, jev_agent, jev_model, jev_choose


def set_transport_timeout(seconds: float) -> None:
    defaults = cdp.__defaults__ or ()
    timeout = max(0.1, min(5.0, seconds))
    if len(defaults) == 2:
        cdp.__defaults__ = (defaults[0], timeout)


def remaining_seconds(started: float, maximum: int) -> float:
    return max(0.1, maximum - (time.monotonic() - started))


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise MissionError("URL must be an HTTP(S) URL without credentials")
    port = parts.port
    default = (parts.scheme == "http" and port in (None, 80)) or (parts.scheme == "https" and port in (None, 443))
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if not default else "")


def redact_exact(value: str, literals: list[str]) -> str:
    for literal in sorted((item for item in literals if item), key=len, reverse=True):
        value = value.replace(literal, "[exact field value]")
    return value


def safe_label(value: object, literals: list[str]) -> str:
    label = str(value)[:1000]
    label = redact_exact(label, literals)
    return POSSIBLE_SECRET.sub("[redacted]", label)[:100]


def field_is_sensitive(spec: dict) -> bool:
    target = spec.get("target", {})
    return any(SENSITIVE.search(str(value)) for value in target.values())


def safe_value(value: object) -> object:
    if isinstance(value, str):
        value = value[:8000]
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", value)
        value = re.sub(r"(?i)(password|passcode|secret|token|credential|otp|access[_-]?key|api[_-]?key)(=|%3d)([^&#\s]*)", r"\1\2[redacted]", value)
        return POSSIBLE_SECRET.sub("[redacted]", value)
    return value


def validate_mission(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema") != "ctrlissues.browser.v1":
        raise MissionError("schema must be ctrlissues.browser.v1")
    url, goal = value.get("url"), value.get("goal")
    if not isinstance(url, str) or not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
        raise MissionError("url and bounded goal are required")
    allowed_origins = value.get("allowed_origins")
    if not isinstance(allowed_origins, list) or not allowed_origins or any(not isinstance(item, str) for item in allowed_origins):
        raise MissionError("allowed_origins must be a non-empty exact origin list")
    try:
        normalized = set()
        for item in allowed_origins:
            if not isinstance(item, str):
                continue
            parts = urlsplit(item)
            if parts.path not in {"", "/"} or parts.query or parts.fragment:
                raise MissionError("allowed_origins entries must be exact origins")
            normalized.add(origin(item))
        allowed_origins = normalized
    except ValueError as exc:
        raise MissionError("allowed_origins contains an invalid origin") from exc
    if not allowed_origins or origin(url) not in allowed_origins:
        raise MissionError("initial URL origin is not allowed")
    actions = value.get("allowed_actions")
    supported = {"click", "fill", "select", "scroll", "wait"}
    if not isinstance(actions, list) or not actions or any(not isinstance(x, str) or x not in supported for x in actions):
        raise MissionError("allowed_actions must list supported action kinds")
    action_selectors = value.get("action_selectors", {})
    if not isinstance(action_selectors, dict) or any(k not in supported for k in action_selectors):
        raise MissionError("action_selectors must map supported action kinds to CSS selector lists")
    for kind, selectors in action_selectors.items():
        if kind not in actions or kind in {"scroll", "wait"} or not isinstance(selectors, list) or not selectors or len(selectors) > 16:
            raise MissionError("each action selector list must narrow an allowed element action")
        if any(not isinstance(selector, str) or not selector or len(selector) > 500 for selector in selectors):
            raise MissionError("action selectors must be bounded non-empty CSS")
    limits = value.get("limits")
    if not isinstance(limits, dict):
        raise MissionError("bounded limits are required")
    bounds = {"steps": MAX_STEPS, "decisions": MAX_DECISIONS, "seconds": MAX_SECONDS}
    for key, maximum in bounds.items():
        number = limits.get(key)
        if type(number) is not int or number < 1 or number > maximum:
            raise MissionError(f"limits.{key} must be from 1 to {maximum}")
    fields = value.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 50:
        raise MissionError("fields must be a list of at most 50 entries")
    field_ids = set()
    for item in fields:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise MissionError("each field needs a unique id")
        if item["id"] in field_ids:
            raise MissionError("field ids must be unique")
        field_ids.add(item["id"])
        if not isinstance(item.get("value"), str) or len(item["value"]) > 2000:
            raise MissionError("each exact field value must be a string of at most 2000 characters")
        target = item.get("target")
        if not isinstance(target, dict) or not target or any(k not in {"identity", "html_id", "name", "label", "role"} for k in target):
            raise MissionError("each field target needs exact identity, html_id, name, label, or role criteria")
        for key, match in target.items():
            if key == "identity":
                if type(match) is not int or match <= 0:
                    raise MissionError("field identity must be a positive observed node id")
            elif not isinstance(match, str) or not match or len(match) > 200:
                raise MissionError("field target criteria must be bounded exact strings")
    expected = value.get("expected")
    if not isinstance(expected, dict) or not any(k in expected for k in ("url", "title", "dom")):
        raise MissionError("at least one typed expected URL, title, or DOM property is required")
    for key in ("url", "title"):
        spec = expected.get(key)
        if spec is not None and (not isinstance(spec, dict) or set(spec) != {"equals"} or not isinstance(spec["equals"], str)):
            raise MissionError(f"expected.{key} must use exact string equality")
    dom = expected.get("dom", [])
    if not isinstance(dom, list) or len(dom) > 32:
        raise MissionError("expected.dom must be a bounded list")
    allowed_properties = {"text", "value", "checked", "selected", "visible"}
    for spec in dom:
        if not isinstance(spec, dict) or set(spec) != {"selector", "property", "equals"}:
            raise MissionError("each DOM check needs selector, property, and equals")
        if not isinstance(spec["selector"], str) or not spec["selector"] or len(spec["selector"]) > 500:
            raise MissionError("DOM selectors must be bounded non-empty CSS")
        if spec["property"] not in allowed_properties:
            raise MissionError("unsupported DOM property")
        expected_type = bool if spec["property"] in {"checked", "selected", "visible"} else str
        if type(spec["equals"]) is not expected_type:
            raise MissionError("DOM expected value has the wrong type")
    return {
        **value,
        "allowed_origins": sorted(allowed_origins),
        "allowed_actions": sorted(set(actions)),
        "action_selectors": action_selectors,
        "fields": fields,
        "expected": {"url": expected.get("url"), "title": expected.get("title"), "dom": dom},
    }


def field_match(action: dict, target: dict) -> bool:
    for key, expected in target.items():
        if key == "identity":
            actual = action.get("identity", action.get("node"))
        elif key == "html_id":
            actual = action.get("html_id", "")
        elif key == "name":
            actual = action.get("name", "")
        else:
            actual = action.get(key, "")
        if actual != expected:
            return False
    return True


def map_fields(mission: dict, actions: list[dict]) -> tuple[dict[str, str], set[int], list[dict]]:
    values: dict[str, str] = {}
    facts = []
    assignments = []
    for spec in mission["fields"]:
        matching = [a for a in actions if a.get("kind") in {"fill", "select"} and field_match(a, spec["target"])]
        node_ids = {a.get("identity", a.get("node")) for a in matching}
        if len(node_ids) != 1:
            facts.append({"field_id": spec["id"], "status": "ambiguous" if node_ids else "missing"})
            continue
        node = next(iter(node_ids))
        if type(node) is not int:
            facts.append({"field_id": spec["id"], "status": "missing"})
            continue
        exact_select = [a for a in matching if a.get("kind") == "select" and a.get("value") == spec["value"]]
        fills = [a for a in matching if a.get("kind") == "fill"]
        chosen = exact_select if exact_select else fills
        ids = {a["id"] for a in chosen}
        if len(ids) != 1:
            facts.append({"field_id": spec["id"], "status": "ambiguous" if ids else "missing"})
            continue
        action_id = next(iter(ids))
        values[action_id] = spec["value"]
        assignments.append((len(facts), action_id, node))
        facts.append({"field_id": spec["id"], "status": "mapped", "identity": node})
    assigned_nodes = [node for _, _, node in assignments]
    node_counts = {}
    for node in assigned_nodes:
        node_counts[node] = node_counts.get(node, 0) + 1
    duplicated_nodes = {node for node, count in node_counts.items() if count > 1}
    nodes = set()
    for index, action_id, node in assignments:
        if node in duplicated_nodes:
            values.pop(action_id, None)
            facts[index] = {"field_id": facts[index]["field_id"], "status": "ambiguous"}
        else:
            nodes.add(node)
    return values, nodes, facts


def filtered_page(page: dict, mission: dict, mapped_values: dict, mapped_nodes: set[int]) -> dict:
    allow = set(mission["allowed_actions"])
    kept = []
    for action in page.get("actions", []):
        kind = action.get("kind")
        if kind not in allow:
            continue
        if kind in {"fill", "select"} and action.get("id") not in mapped_values:
            continue
        if kind == "click" and action.get("role") in {"textbox", "searchbox", "combobox"} and action.get("node") not in mapped_nodes:
            continue
        kept.append(action)
    return {**page, "actions": kept}


def selector_nodes(browser, selectors: dict[str, list[str]]) -> set[tuple[str, int]]:
    if not selectors:
        return set()
    script = r"""(groups => {
      const cache=window.__jevFast;
      if (!cache) return null;
      const out=[];
      for (const [kind,selectors] of Object.entries(groups)) for (const selector of selectors) {
        for (const e of document.querySelectorAll(selector)) {
          const id=cache.ids.get(e);
          if (Number.isInteger(id)) out.push([kind,id]);
        }
      }
      return out;
    })""" + "(" + json.dumps(selectors) + ")"
    result = browser.evaluate(script)
    if not isinstance(result, list):
        raise RuntimeError("action selector check unavailable")
    return {(kind, node) for kind, node in result if isinstance(kind, str) and type(node) is int}


def filter_action_selectors(browser, mission: dict, actions: list[dict]) -> list[dict]:
    selectors = mission["action_selectors"]
    allowed = selector_nodes(browser, selectors)
    if not selectors:
        return actions
    return [action for action in actions if action.get("kind") not in selectors or
            (action.get("kind"), action.get("identity", action.get("node"))) in allowed]


def candidate_summary(actions: list[dict], literals: list[str]) -> list[dict]:
    return [
        {
            "id": action.get("id"),
            "kind": action.get("kind"),
            "identity": action.get("identity", action.get("node")),
            "role": action.get("role"),
            "label": safe_label(action.get("label", ""), literals),
            "value": safe_value(action.get("value")) if action.get("kind") == "select" else None,
        }
        for action in actions
    ]


def fresh_readback(browser, expected: dict) -> list[dict]:
    script = r"""(checks => {
      const sensitive = e => {
        const text=[e.type,e.autocomplete,e.name,e.id,e.getAttribute('aria-label'),e.getAttribute('placeholder')]
          .filter(Boolean).join(' ').toLowerCase();
        return ['password','file','hidden'].includes(e.type) ||
          /(?:^|[\s_-])(password|passcode|secret|token|credential|otp|2fa|mfa|one.?time|verification|cc.?number|cc.?exp|card.?number|card.?exp|cvv|cvc)(?:$|[\s_-])/.test(text) ||
          /^(?:current-password|new-password|one-time-code|cc-number|cc-exp|cc-csc)$/.test((e.autocomplete||'').toLowerCase());
      };
      const rows=[];
      for (const c of checks) {
        const found=[...document.querySelectorAll(c.selector)];
        if (found.length!==1) { rows.push({status:found.length ? 'ambiguous' : 'missing'}); continue; }
        const e=found[0], visible=!!(e.getBoundingClientRect().width && e.getBoundingClientRect().height &&
          e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
        if (sensitive(e)) { rows.push({status:'credential_widget'}); continue; }
        let value;
        if (c.property==='text') value=(e.innerText||'').trim().slice(0,8000);
        else if (c.property==='value') value='value' in e ? String(e.value).slice(0,8000) : null;
        else if (c.property==='checked') value=typeof e.checked==='boolean' ? e.checked : e.getAttribute('aria-checked')==='true' ? true : e.getAttribute('aria-checked')==='false' ? false : null;
        else if (c.property==='selected') value=typeof e.selected==='boolean' ? e.selected : e.getAttribute('aria-selected')==='true' ? true : e.getAttribute('aria-selected')==='false' ? false : null;
        else value=visible;
        rows.push({status:'observed',value});
      }
      return {url:location.href,title:document.title,dom:rows};
    })""" + "(" + json.dumps(expected.get("dom", []), ensure_ascii=False) + ")"
    state = browser.evaluate(script)
    if not isinstance(state, dict) or not isinstance(state.get("dom"), list) or len(state["dom"]) != len(expected.get("dom", [])):
        raise RuntimeError("independent readback unavailable")
    facts = []
    if expected.get("url"):
        wanted = expected["url"]["equals"]
        ok = state["url"] == wanted
        facts.append({"property": "url", "status": "verified" if ok else "failed", "actual": safe_value(state["url"]), "expected": safe_value(wanted)})
    if expected.get("title"):
        wanted = expected["title"]["equals"]
        ok = state["title"] == wanted
        facts.append({"property": "title", "status": "verified" if ok else "failed", "actual": safe_value(state["title"]), "expected": safe_value(wanted)})
    for index, (spec, observed) in enumerate(zip(expected.get("dom", []), state.get("dom", []))):
        ok = observed.get("status") == "observed" and observed.get("value") == spec["equals"]
        facts.append({"property": f"dom[{index}].{spec['property']}", "status": "verified" if ok else ("failed" if observed.get("status") == "observed" else observed.get("status", "unknown")), "actual": safe_value(observed.get("value")), "expected": safe_value(spec["equals"])})
    return facts


def verify_completion(browser, mission: dict, report: dict) -> None:
    try:
        checks = fresh_readback(browser, mission["expected"])
    except Exception:
        report["status"] = "unknown"
        report["failed"].append({"fact": "fresh independent readback unavailable"})
        return
    report["readback"] = checks
    report["verified"] = sum(fact["status"] == "verified" for fact in checks)
    if checks and report["verified"] == len(checks):
        report["status"] = "success"
    else:
        report["status"] = "failed"
        report["failed"].append({"fact": "independent readback did not satisfy every typed expectation"})


def expectations_met(browser, mission: dict) -> tuple[bool, list[dict] | None]:
    try:
        checks = fresh_readback(browser, mission["expected"])
    except Exception:
        return False, None
    return bool(checks) and all(fact["status"] == "verified" for fact in checks), checks


def lease(data: Path, timeout: float):
    class Held:
        def __enter__(self):
            directory = data / "hands"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / "browser-profile.lock"
            self.file = path.open("a+b")
            os.chmod(path, 0o600)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        self.file.seek(0)
                        if self.file.read(1) == b"":
                            self.file.seek(0)
                            self.file.write(b"0")
                            self.file.flush()
                        self.file.seek(0)
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        self.file.close()
                        raise TimeoutError("CtrlIssues browser profile lease timed out")
                    time.sleep(0.05)

        def __exit__(self, *_):
            if os.name == "nt":
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()

    return Held()


def report_path(value: str, data: Path) -> Path:
    root = data.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path == root or root not in path.parents:
        raise MissionError("report must be inside the private CtrlIssues data directory")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_report(path: Path, report: dict) -> None:
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_REPORT:
        raise MissionError(f"bounded private report exceeds {MAX_REPORT} bytes")
    fd, temp_name = tempfile.mkstemp(prefix=".ctrlissues-browser-", suffix=".tmp", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_journal(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_previous_report(destination: Path, run_id: str) -> Path | None:
    if not destination.exists():
        return None
    archived = destination.with_name(f"{destination.name}.previous-{run_id}")
    shutil.copyfile(destination, archived)
    os.chmod(archived, 0o600)
    return archived


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ctrlissues browser")
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    data_value = os.environ.get("CTRLISSUES_DATA")
    if not data_value:
        raise MissionError("CTRLISSUES_DATA locator is required")
    data = Path(data_value)
    destination = report_path(args.report, data)
    if args.input == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    else:
        with Path(args.input).open("rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise MissionError("mission exceeds 64 KiB")
    mission_hash = hashlib.sha256(raw).hexdigest()
    mission = validate_mission(json.loads(raw))
    cdp_url = os.environ.get("CTRLISSUES_BROWSER_CDP_URL", CDP_DEFAULT)
    endpoint = urlsplit(cdp_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} or endpoint.port is None or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise MissionError("browser transport must use an explicit loopback HTTP CDP endpoint")
    report = {
        "schema": "ctrlissues.browser.report.v1",
        "mission_sha256": mission_hash,
        "status": "unknown",
        "executed": 0,
        "verified": 0,
        "failed": [],
        "field_resolution": [],
        "actions": [],
        "readback": [],
    }
    run_id = f"{time.time_ns():x}-{os.getpid():x}"
    journal_path = data / "hands" / "runs.jsonl"
    previous_report = archive_previous_report(destination, run_id)
    report["run_id"] = run_id
    report["status"] = "running"
    save_report(destination, report)
    append_journal(journal_path, {"run_id": run_id, "phase": "started", "mission_sha256": mission_hash, "report": str(destination), "previous_report": str(previous_report) if previous_report else None})
    started = time.monotonic()
    agent = None
    try:
        load_runtime(cdp_url)
        with lease(data, min(30, mission["limits"]["seconds"])):
            remaining = remaining_seconds(started, mission["limits"]["seconds"])
            set_transport_timeout(remaining)
            agent = Agent(mission["url"], mission["goal"], timeout=remaining)
            # This adapter supplies the choice directly, so Agent.predict does
            # not run its usual clock initialization before Agent.act.
            agent.state["started_at"] = time.perf_counter()
            browser = agent.browser
            page = agent.state["page"]
            values, nodes, resolution = map_fields(mission, page.get("actions", []))
            history = []
            for decision_index in range(mission["limits"]["decisions"]):
                if time.monotonic() - started > mission["limits"]["seconds"]:
                    report["status"] = "unknown"
                    report["failed"].append({"fact": "time limit reached"})
                    break
                if origin(page["url"]) not in mission["allowed_origins"]:
                    report["status"] = "blocked"
                    report["failed"].append({"fact": "observed page origin is outside allowed_origins"})
                    break
                values, nodes, resolution = map_fields(mission, page.get("actions", []))
                report["field_resolution"].append({"observation": decision_index + 1, "fields": resolution})
                agent.field_values = values
                filtered = filtered_page(page, mission, values, nodes)
                complete, current_checks = expectations_met(browser, mission)
                if current_checks is not None:
                    report["readback"] = current_checks
                    report["verified"] = sum(fact["status"] == "verified" for fact in current_checks)
                if complete:
                    report["status"] = "success"
                    break
                offered = filter_action_selectors(browser, mission, filtered.get("actions", []))
                if not offered:
                    report["status"] = "blocked"
                    report["failed"].append({"fact": "no supported allowed action is observed"})
                    break
                literals = [field["value"] for field in mission["fields"] if field_is_sensitive(field)]
                policy = dict(filtered)
                policy["url"] = urlsplit(page["url"])._replace(query="", fragment="").geturl()
                policy["title"] = redact_exact(str(page.get("title", "")), literals)
                policy["text"] = redact_exact(str(page.get("text", "")), literals)
                policy["actions"] = [
                    {key: redact_exact(value, literals) if isinstance(value, str) else value for key, value in action.items()}
                    for action in offered
                ]
                model.CLIENT.timeout = max(0.1, min(25.0, remaining_seconds(started, mission["limits"]["seconds"])))
                decision = choose(policy, redact_exact(mission["goal"], literals), history)
                if time.monotonic() - started > mission["limits"]["seconds"]:
                    report["status"] = "unknown"
                    report["failed"].append({"fact": "time limit reached during semantic choice"})
                    break
                selected = decision.get("choice")
                record = {
                    "decision": decision_index + 1,
                    "page_sha256": digest(page.get("fingerprint", "")),
                    "offered": candidate_summary(offered, literals),
                    "choice": selected if selected in {"DONE", "BLOCKED"} else str(selected),
                    "status": "chosen",
                }
                report["actions"].append(record)
                if selected == "DONE":
                    verify_completion(browser, mission, report)
                    break
                if selected == "BLOCKED":
                    report["status"] = "blocked"
                    report["failed"].append({"fact": "chooser reports no supported progress"})
                    break
                set_transport_timeout(remaining_seconds(started, mission["limits"]["seconds"]))
                action = next((a for a in offered if a.get("id") == selected), None)
                if action is None or action.get("kind") not in mission["allowed_actions"]:
                    report["status"] = "blocked"
                    record["status"] = "candidate_revalidation_failed"
                    report["failed"].append({"fact": "selected action is outside the filtered candidate set"})
                    break
                set_transport_timeout(remaining_seconds(started, mission["limits"]["seconds"]))
                if origin(browser.evaluate("location.href")) not in mission["allowed_origins"]:
                    report["status"] = "unknown"
                    record["status"] = "stale_before_action"
                    report["failed"].append({"fact": "origin changed before action"})
                    break
                allowed_nodes = selector_nodes(browser, mission["action_selectors"])
                if mission["action_selectors"] and action.get("kind") in mission["action_selectors"] and (action.get("kind"), action.get("identity", action.get("node"))) not in allowed_nodes:
                    report["status"] = "blocked"
                    record["status"] = "candidate_revalidation_failed"
                    report["failed"].append({"fact": "selected node no longer matches the mission selector"})
                    break
                if not browser.fresh(page, action):
                    report["status"] = "unknown"
                    record["status"] = "stale_before_action"
                    agent.state["decision"] = None
                    report["failed"].append({"fact": "selected target failed the upstream freshness guard"})
                    break
                record["action"] = {
                    "id": action["id"],
                    "kind": action["kind"],
                    "identity": action.get("identity", action.get("node")),
                    "label": safe_label(action.get("label", ""), literals),
                }
                try:
                    append_journal(journal_path, {"run_id": run_id, "phase": "before_action", "decision": decision_index + 1, "action_id": selected, "kind": action["kind"]})
                    set_transport_timeout(remaining_seconds(started, mission["limits"]["seconds"]))
                    agent.state["decision"] = decision
                    agent.state["status"] = "predicted"
                    agent.command("act", {"fingerprint": page["fingerprint"]})
                    record["status"] = "executed"
                    report["executed"] += 1
                    last = agent.state["history"][-1]
                    history.append({"kind": action["kind"], "action": "observed action", "text": None, "page_changed": last.get("page_changed")})
                    append_journal(journal_path, {"run_id": run_id, "phase": "after_action", "decision": decision_index + 1, "action_id": selected, "kind": action["kind"], "page_changed": last.get("page_changed")})
                except Exception as exc:
                    record["status"] = "unknown_after_dispatch"
                    report["status"] = "unknown"
                    report["failed"].append({"fact": "action outcome is uncertain", "error_type": type(exc).__name__})
                    append_journal(journal_path, {"run_id": run_id, "phase": "action_outcome_uncertain", "decision": decision_index + 1, "action_id": selected, "kind": action["kind"], "error_type": type(exc).__name__})
                    break
                if len(history) >= mission["limits"]["steps"]:
                    record["status"] = "executed_at_step_limit"
                    verify_completion(browser, mission, report)
                    if report["status"] != "success":
                        report["failed"].append({"fact": "action step limit reached before independent completion"})
                    break
                if time.monotonic() - started > mission["limits"]["seconds"]:
                    report["status"] = "unknown"
                    report["failed"].append({"fact": "time limit reached during action or observation"})
                    break
                page = agent.state["page"]
                complete, current_checks = expectations_met(browser, mission)
                if current_checks is not None:
                    report["readback"] = current_checks
                    report["verified"] = sum(fact["status"] == "verified" for fact in current_checks)
                if complete:
                    report["status"] = "success"
                    break
                if agent.state["status"] == "blocked":
                    report["status"] = "blocked"
                    report["failed"].append({"fact": "upstream guard stopped repeated unchanged actions"})
                    break
            else:
                if report["status"] != "blocked":
                    verify_completion(browser, mission, report)
                    if report["status"] != "success":
                        report["status"] = "unknown"
                        report["failed"].append({"fact": "decision limit reached before independent completion"})
    except Exception as exc:
        report["status"] = "unknown"
        report["failed"].append({"fact": "browser runner failed", "error_type": type(exc).__name__})
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                if report["status"] == "success":
                    report["status"] = "unknown"
                    report["failed"].append({"fact": "owned browser target could not be confirmed closed"})
        append_journal(journal_path, {"run_id": run_id, "phase": "finished", "status": report["status"], "executed": report["executed"]})
    return finish(report, destination)


def finish(report: dict, destination: Path) -> int:
    save_report(destination, report)
    print(json.dumps({
        "status": report["status"],
        "executed": report["executed"],
        "verified": report["verified"],
        "failed": report["failed"],
        "report": str(destination),
    }, separators=(",", ":")))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(run(sys.argv[1:]))
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
