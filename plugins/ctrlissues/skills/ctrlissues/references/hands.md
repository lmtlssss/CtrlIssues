# Browser work

Use the bounded local browser route when a task needs repeated DOM actions:

```text
ctrlissues browser --input mission.json --report "$CTRLISSUES_DATA/hands/reports/task.json"
```

The input can be `-` for JSON on standard input. Its schema is `ctrlissues.browser.v1`:

```json
{
  "schema": "ctrlissues.browser.v1",
  "url": "https://example.test/search",
  "goal": "Search for the named item and open its result.",
  "allowed_origins": ["https://example.test"],
  "allowed_actions": ["click", "fill", "select", "scroll", "wait"],
  "fields": [
    {"id": "query", "target": {"name": "q", "label": "Search"}, "value": "exact supplied text"}
  ],
  "limits": {"steps": 8, "decisions": 12, "seconds": 60},
  "expected": {
    "url": {"equals": "https://example.test/search?q=exact%20supplied%20text"},
    "title": {"equals": "Search results"},
    "dom": [{"selector": "main h1", "property": "text", "equals": "Results"}]
  }
}
```

Each field target uses exact observed identity (`identity`), HTML id (`html_id`), HTML name, accessible label, or role criteria. Use criteria that resolve to one observed field. A field that is missing on the current page is recorded as unresolved, while unrelated safe actions remain available. An ambiguous field is never filled. A fill action is available only when the field is mapped to an exact literal. Ordinary contact fields such as email remain usable; password, token, one-time-code, and payment-secret widgets are excluded. Readback selectors must match exactly one visible, non-credential element. Supported DOM properties are `text`, `value`, `checked`, `selected`, and `visible`; values are checked with exact typed equality.

An optional `action_selectors` object narrows element actions by kind, for example `{"click":["button#continue"]}`. Each CSS list is checked in one batched DOM query and checked again before the selected action. These selectors can only reduce the observed candidate set.

Origins must be exact HTTP(S) origins without wildcards. Limits cannot exceed 60 actions, 120 semantic choices, or 300 seconds. Supported actions are click, exact fill, observed select, scroll, and wait. Credential widgets are removed from the chooser view. Bounded readable action labels and expected/actual readback facts are stored in a mode-0600 report capped at 16 MB inside CtrlIssues data; likely token values are redacted. Each run archives an existing report, replaces the requested report with a running record before browser work, and appends mutation intent/outcome records to `hands/runs.jsonl`. Standard output is a compact status/count/report object.

The optional runtime needs `uv` and Python 3.12. Use the locked bootstrap from the active versioned plugin root, outside ENVSTACK; [docs/install.md](../../../../../docs/install.md) shows how to resolve that root from the current payload pointer. A named `ctrlissues` Browser Harness daemon must already be healthy and point to the loopback Brave CDP endpoint (`BU_CDP_URL=http://127.0.0.1:9222`). Provision or verify that daemon separately in a clean environment with `BU_NAME=ctrlissues`; never start or restart it inside the provider call. The upstream dotenv loading sites are disabled by the local bootstrap patch. The native `ctrlissues browser` route itself invokes `envstack run --` in the foreground, so a compatible `envstack` command must be on `PATH` on every platform. Windows browser use depends on that command and has not been established by building the native Windows package. The mission owns and closes one new tab, not the user's other tabs. Its lock serializes CtrlIssues browser missions only.

Frames, shadow DOM, canvas, uploads, pop-up tabs, nested scrolling, arbitrary native widgets, and non-loopback browser control are unsupported. A blocked or unknown result needs a native handoff. A semantic `DONE` choice is only a request to run the independent URL/title/DOM checks. Only all passing fresh checks return `success`.
