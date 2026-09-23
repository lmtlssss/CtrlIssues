# CtrlIssues

for agents with executive dysfunction.

one task cursor and one receipt ledger for Codex. Astra xhigh keeps the goal,
chooses the next bounded piece of work, and accepts its result. local code tracks
progress, runs known checks once per input generation, and carries the cursor
through native compaction. explicit typed decisions and owned browser sessions
handle supported action batches.

```text
CTRLISSUES
──────────────────────────────────────────────────────────────

person + goal  ──►  Astra xhigh  ──►  task cursor
                        │                 │
                        │                 ├──► bounded work
                        │                 └──► check receipts
                        │                         │
                        └◄── observed result ◄─────┘

native context  ──►  compact / resume  ──►  same task
one browser profile  ──►  one CtrlIssues action owner
```

## install

The v0.1.1 release installer selects a Linux x86_64 musl or macOS package and
checks its SHA-256 file. Python 3.11 or newer is needed for installation and
the Python backend. Rust is needed only when building from source.

Run it with:

```sh
curl -fsSL https://github.com/lmtlssss/CtrlIssues/releases/download/v0.1.1/install.sh | sh
```

Windows users can install a local package with Python 3.11 or newer:

```powershell
python scripts/install.py --package ctrlissues-v0.1.1-windows-x86_64.zip
```

For a source install, use Rust and Python 3.11 or newer:

```sh
cargo build --release --locked --manifest-path plugins/ctrlissues/runtime/Cargo.toml
./install.sh --binary plugins/ctrlissues/runtime/target/release/ctrlissues
```

Windows source installs use `install.ps1` and the built `ctrlissues.exe`. The
installer uses the native Codex CLI to add the local marketplace and plugin
and to trust CtrlIssues hooks. Details and isolated install options are in
[docs/install.md](docs/install.md).

## use

Use Codex with Astra xhigh as the configured primary. Start with the person's
goal. Substantial work gets an ordered plan; the agent finishes the current
layer and accepts a result only with a current receipt. Short direct tasks need
no blueprint. Later steers revise the same task and retain valid completed work.

```sh
ctrlissues status --compact
ctrlissues check smoke build -- cargo build --locked --manifest-path plugins/ctrlissues/runtime/Cargo.toml
ctrlissues work --input work.json --report artifacts/private-report.json
ctrlissues browser --input mission.json --report hands/reports/task.json
```

`work` takes a bounded node list and saves its full result in a private report.
`browser` takes a bounded mission and verifies the application with fresh typed
readback. stdout gives a short status and report path. `decide`, `select`, and
`assess` are explicit foreground calls. Routine hooks make no cloud call.

## controls

```text
command                         purpose
──────────────────────────────────────────────────────────────
status / status --compact       full task state / bounded handoff cursor
plan / revise                  blueprint / guarded change
cursor / mark / advance        next action / completed work / layer gate
check / changed / finish       proof receipt / input change / completion
work                           bounded action graph and compact report
decide / select / assess        typed questions / selection / claim check
browser                        bounded local browser task
doctor                         local store inspection
```

Native `SessionStart`, `SubagentStart`, `UserPromptSubmit`, `PreCompact`,
`PreToolUse`, and `PostToolUse` hooks connect task state to Codex events.
Code-mode nested tool calls remain supported. Codex keeps its own context and
permission rules. Hooks cover supported event and command shapes; an arbitrary
program or an unobserved edit needs direct review.

## state

The default store is
`${CODEX_HOME:-$HOME/.codex}/plugins/data/ctrlissues-ctrlissues/`. Each task has
an explicit session identity, even when two tasks share a directory. SQLite
owns the task, plan, revision, generation, and receipts. `status --compact`
derives a small cursor and plan locator from that state. It carries no raw
transcript or provider credential.

A passing check with the same working directory, command arguments, kind,
component, and source generation reuses its stored receipt. Failures remain
failures until a named input or dependency changes. A command exit, typed score,
or `DONE` choice does not establish an application result. The owner reads the
actual result before marking the task complete.

Browser runs use one owned tab, check the selected target before input, and read
the application again afterward. The optional browser runtime, daemon setup,
and platform limits are in [hands.md](plugins/ctrlissues/skills/ctrlissues/references/hands.md).

## recovery / remove

The installer validates each package before selecting its immutable payload.
`--rollback` selects the previous payload and retains task state in the shared
data directory. Install and removal change CtrlIssues marketplace, plugin, and
hook entries in Codex configuration; they do not snapshot or restore the whole
Codex profile. The installer also manages its own launchers and private data.
`--uninstall` unregisters CtrlIssues and retains private task data.

## build

The Rust core can be built from the source tree:

```sh
cargo build --locked --manifest-path plugins/ctrlissues/runtime/Cargo.toml
```

Source proof and per-platform package checks run through CI. The package matrix
does not prove the optional browser runtime or live application behavior.

[architecture](docs/architecture.md) · [research and limits](docs/research.md) · [license](LICENSE)
