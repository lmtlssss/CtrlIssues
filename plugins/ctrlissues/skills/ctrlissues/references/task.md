# Task core

Use the installed `ctrlissues` command for a task that needs a durable plan or proof record.

```text
ctrlissues status
ctrlissues status --compact
ctrlissues plan FILE
ctrlissues revise FILE
ctrlissues cursor NEXT_ACTION
ctrlissues mark COMPONENT EVIDENCE
ctrlissues advance
ctrlissues changed --component COMPONENT REASON
ctrlissues check KIND LABEL -- COMMAND [ARG ...]
ctrlissues finish
```

The explicit session identity binds the task. The compact view is a handoff pointer to the same committed revision and generation. Small work does not need a plan. A passing check with the same kind, component scope, command arguments, working directory, and current source generation reuses its receipt. A receipt from an older record with no working directory cannot satisfy that lookup. Failed and running checks are not reused.

Use `changed --component` when one component's inputs change. It invalidates that component and its dependents. Use unscoped `changed REASON` when the changed area is not known; it conservatively invalidates all check proof.

Native lifecycle hooks add a short task pointer only when a planned task exists. Before compaction, the binary writes an atomic snapshot of the committed cursor under the data directory. It does not read or hash the transcript. Native hooks do not choose models, rewrite the transcript, initialize Git, or inspect outer code-mode source strings.

During an active task, a repeated identical supported read observation is stopped after a known completed result. The receipt binds the canonical session, task generation, working directory, tool, and input. Pending and unknown outcomes stay unrecorded. The hook stores input and output digests only; it does not persist observation text. Its denial includes the receipt ID and outcome status.
