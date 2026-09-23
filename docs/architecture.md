# architecture

this is the v0.1.0 construction contract. release behavior is accepted only by
the joined proof and the installed result.

```text
person ──► Astra xhigh ──► task and work orders
                           │
                  ┌────────┼────────┐
                  ▼        ▼        ▼
              task core  work DAG  owned app surface
                  │        │        │
                  └────────┼────────┘
                           ▼
                    result + receipt
                           │
                    Astra accepts it
                           │
               native compact / resume
                           │
                     same task cursor
```

## task owner

the Rust `ctrlissues` binary owns the transactional SQLite task store. a task
has an explicit session identity, revision, generation, ordered layers, current
cursor, and check receipts. the cwd helps identify a check; it does not name a
task. `status --compact` emits `ctrlissues.cursor.v1`: session ID, revision,
generation, phase, cursor, objective, plan path, and up to eight recent receipt
references. full task state remains available through `status`.

`plan`, `revise`, `mark`, `advance`, `changed`, `check`, and `finish` use the
existing GraphFather whole-layer gates. a steer revises the accepted blueprint
and preserves unaffected completion. a check key includes canonical cwd,
arguments, kind, component, and source generation. a matching passing check
returns its receipt with `reused:true`; failed, running, and legacy records
without a known cwd cannot create a new success. historical receipts remain.

## native continuity

one local handler serves `SessionStart`, `UserPromptSubmit`, `PreCompact`,
`PreToolUse`, and `PostToolUse`. `PreCompact` saves a bounded task snapshot;
`SessionStart` can restore the cursor after compact or resume. Codex performs
its native compaction and keeps its model context. hooks read local state and
do not call a model. they check supported tool shapes, including code-mode
nested calls; Codex remains the permission authority. a short task with no
task state gets no forced blueprint.

## execution backend

the optional Python backend is reached through the same `ctrlissues` CLI. its
work input is `ctrlissues.work.v1`, with `jmt.work.v1` accepted where the meaning
matches. a work request pins its own ID and request text to the current task
revision/generation when a task exists. nodes declare dependencies, resource
needs, bounded actions, and explicit postconditions. a node may be executed,
verified, failed, pending, unknown, or reused. one process-wide lease owns a
shared resource while a node acts on it.

`work --input FILE|- --report FILE` writes full bounded evidence to a private
artifact and prints a compact status with counts, failed facts, and its path.
an exact completed replay executes zero actions. a successful process exit
does not satisfy a postcondition. the backend may store its own prefixed tables
in the same SQLite file; it does not alter core-owned rows or hold a database
transaction during an external call.

## typed decisions and hands

`decide`, `select`, `assess`, and related semantic calls are explicit foreground
operations. independent typed questions can be batched. only exact scoped
inputs at the same task generation may reuse a semantic result. a choice never
grants permission or verifies the result. model failure yields one bounded
unassessed fact while deterministic work remains usable. the local hook path
never depends on a provider response.

application work uses an authorized API for exact operations, then a persistent
DOM or accessibility session where supported, then bounded pixel/keyboard
control. each physical seat, page workflow, and phone has one owner. the action
runner rechecks fresh state and returns a current postcondition. a source that
cannot expose a safe action returns an explicit handoff.

the browser path may reuse [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast)
with scoped action/domain filters, exact supplied field values, finite budgets,
an existing daemon that fails closed, and fresh app-owned verification. the
provider loop stays one foreground operation. unsupported frames, canvas, and
other missing controls return to Astra. third-party browser measurements are
summarized in [research](research.md); they are not package proof.

## recovery

the installer validates and stages a versioned payload before it switches
CtrlIssues registration. rollback selects the previous payload and retains
newer task history. the installer changes its own plugin and hook entries;
whole-profile backups belong to the host's separate configuration workflow.
source histories and separate plugins remain recoverable.
