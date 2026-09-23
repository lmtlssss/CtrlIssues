# research and limits

these primary sources informed the v0.1.0 design. author measurements describe
their own workloads. CtrlIssues has no released benchmark or installed proof at
this construction stage.

| primary source | useful finding | limit for CtrlIssues |
| --- | --- | --- |
| [Diogo Almeida's public design notes](https://docs.google.com/document/d/1G61uUB0FifUnmmrPzFQojZ3KpczYKmXGpgEXDJ2l_Zg/edit) | models, routing, context, and compaction have different costs under a long KV cache; typed questions can use explicit state | these are design hypotheses and examples, with no CtrlIssues task measurement |
| [TypeSafe: how to build with System One](https://docs.typesafe.ai/concepts/how-to-build-with-system-one) | code keeps control flow and side effects; Jev answers bounded typed questions | a confidence score does not authorize an action or prove an outcome |
| [Codex lifecycle hooks](https://learn.chatgpt.com/docs/hooks) | native events expose session, compaction, prompt, and supported tool calls | tool coverage has limits; hooks do not replace native context or permission handling |
| [Codex effort-override change](https://github.com/openai/codex/pull/43110) | effort changes and prefix reuse have specific runtime conditions | this pack keeps Astra xhigh steady; no quota saving is inferred from the change |
| [Jev Ultrafast performance report, pinned source](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/performance.md) | on one Flights task, three paired runs changed median 9.450 s to 7.092 s and browser calls 1,092 to 101 | three pairs, one profile, p=0.25; navigation and independent post-run verification were outside the clock; `DONE` needs an app check |
| [Cua jev-use example](https://github.com/trycua/cua/blob/main/libs/cua-driver/examples/jev-use/README.md) and [action support](https://github.com/trycua/cua/blob/main/libs/cua-driver/docs/action-support.md) | observed candidate IDs, one-use captures, fresh state, and explicit app verification bound device actions | compositor support and physical input ownership vary; a selector alone cannot run a workflow |
| [Cua-S1 model card](https://github.com/trycua/cua/blob/main/libs/cua-s1/MODEL_CARD.md) | reports closed-option offline action scores | those scores are not live desktop or phone completion rates |
| [agent-device README](https://github.com/callstack/agent-device/blob/main/README.md) | device claims and fresh accessibility refs support bounded phone sessions | each physical device still needs one owner and a local result check |

public X reports supplied failure cases to preserve in the design. [Moinerus's
compaction fixture](https://x.com/moinerus/status/2101039090737303992) reported
loss of six of seven critical facts in one matched comparison; it is an author
report, with no local replication. [Francesco's jev-use discussion](https://x.com/francedot/status/2102154526522319164)
called for handoff rules and fresh-state checks after a closed choice; it gives
no end-to-end success rate. these reports support retaining original failure
records and checking the live application after a typed decision.

the package's own release evidence must separate source checks, actual command
reuse, model calls, browser and device actions, end-to-end time, and independent
result checks. author speed figures and quota anecdotes do not become local
savings claims.
