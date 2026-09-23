# Typed execution

Use `ctrlissues work --input FILE --report FILE` for an explicit, bounded work
graph. The input uses `ctrlissues.work.v1`; `jmt.work.v1` is accepted when its
meaning is the same. Include a unique work id, phase, pinned `context.request`,
and one to 64 typed nodes. Each action or check that claims verification must
include an explicit postcondition.
Build-phase `check` nodes must declare `check_kind` as `smoke` or `safety`.
Work limits include execution count, concurrent work, rounds, semantic calls,
and elapsed time.

The backend reads `status --compact` once and binds an active work request to
that cursor's canonical session, revision, and generation. It does not capture
prompts or create a second task cursor. A command exit can mean `executed`;
only a satisfied postcondition means `verified`. Failed, pending, and unknown
results remain distinct. A completed identical input at the same generation
replays its report without executing actions. Reports are private output files
and do not change source generation.

Supported work operations are bounded file `read`, `stat`, and explicit
shell-free `argv` for action/check nodes. Arbitrary executable calls are not
observations. Declared resources use process-wide leases in the core SQLite
file; independent resources can proceed concurrently. Typed `decide`, `select`,
`assess`, `verify`, `guard`, `stats`, and `configure` routes reuse the local
JustMyType semantic primitives. Provider work is a foreground operation. It
uses an already-active TypeSafe credential environment or a configured
foreground credential runner. Browser actions and core-owned task operations
remain outside this backend.

The backend uses only `backend_*` tables in the core `state.sqlite` file.
Provider settings live in `backend_config.json` under `CTRLISSUES_DATA`. The
backend does not load dotenv files or forward provider credentials to control
subprocesses.
