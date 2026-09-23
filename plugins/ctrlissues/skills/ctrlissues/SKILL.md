---
name: ctrlissues
description: Keep a Codex task moving with a durable cursor, bounded work, and evidence-backed receipts. Use when a task needs continuity across steers or compaction, explicit work replay, or typed semantic checks.
---

# CtrlIssues

for agents with executive dysfunction.

Use the native `ctrlissues` command for task state and proof receipts. Do not create a plan for small work. Preserve the active cursor when the person steers or the conversation compacts.

- Read [task.md](references/task.md) when a task needs a durable plan, revision, or proof receipt.
- Read [execution.md](references/execution.md) when you need typed work, decisions, semantic checks, or compact execution reports.
- Read [hands.md](references/hands.md) when a task needs a bounded browser loop.

The native core owns task identity, revisions, checks, and lifecycle hooks. The optional Python backend owns bounded typed work and semantic calls. A successful command is an execution result; accept an application effect only when its explicit postcondition or independent readback proves it.
