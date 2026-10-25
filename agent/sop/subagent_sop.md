# Subagent SOP

## When to delegate

Use `spawn_subagent` for an independent investigation, a focused verification pass, or a
parallel slice over separate inputs. Do not delegate a tiny edit whose context setup costs
more than the work, and do not parallelise one coherent task into sub-checks (that is one
job in several costumes).

**Delegation is omission control, not throughput.** Assume one capable executor can handle
long files and coherent multi-file edits. Fan out only when something would otherwise be
*missed*. Read `memory/sop/subagent_sop.md` (this file) before the first delegation.

## What a sub-agent is (one-shot, foreground, fresh)

`spawn_subagent(task="...", model="optional")` starts a **fresh** child agent and waits for
its final answer:

- **Fresh context.** The child does not see this conversation — give it a self-contained
  task (the goal, the constraints, and absolute file paths). Do not paste the transcript.
- **Inherits your tools, scene and capability boundaries.** The child shares your bound
  browser/MCP services and your workspace rules. Its `capability_call` reaches exactly
  what yours can reach.
- **Cannot delegate further.** The child's registry does not register `spawn_subagent` or
  `start_long_term_update`. If it needs either, it reports the need in its result; the
  parent decides whether to spawn another child or distil memory.
- **Approval flows through you.** The child's `code_run` and out-of-bounds writes route
  through your conversation's approval flow: in **auto** mode they are auto-approved; in
  **interact** mode an approval card surfaces in your stream and the child blocks until it
  resolves. `ask_user` in the child ends the child's run with a question; you receive it
  as the result — answer it yourself, or surface it to the human.

## When NOT to delegate

- A single, coherent multi-file edit: one executor keeps it consistent.
- A task whose result you could produce faster by `file_read` + one tool call.
- "Check the API / check parity / check side effects" on the same files — that is one job.
- Anything where the child would need to see this conversation to make sense.

## The task you pass

Write the task as a self-contained brief:

1. The goal in one or two sentences.
2. The constraints it must respect (a governing SOP path to `file_read` first; a format it
   must produce; files it must not touch).
3. Absolute paths for any files it must read or write.
4. The verification you expect (a command to run, a screenshot to take, a check to perform).

The child cannot see this conversation, so anything you do not put in the task, it does not
know. Long context belongs as a file path, not pasted into the task.

## Receiving the result

The tool returns the child's final text as `result`. Two terminal shapes:

- `status: "ok"` — the child finished. Read `result`; if it reports something worth
  remembering, call `start_long_term_update` yourself (the child cannot).
- `status: "needs_input"` — the child ended on `ask_user`. The question is in `result`.
  Answer it yourself and continue (you may spawn a new child with the answer), or surface
  it to the human via your own `ask_user`.

## Stopping a child

A child runs as a foreground coroutine inside this agent's process; the runtime publishes
its events to `subagent/<child_id>` on the realtime WS as well as to this conversation's
stream. To stop a runaway child, send `kill` on the `subagent` channel (target = the
`subagent_id` the tool returned). The child's `asyncio.Task` is cancelled; any in-flight
approval/question card it raised is cleared. Cancelling a child does not cancel this agent.

## Depth

A child cannot spawn its own children. If a piece of work needs further delegation, the
child reports it; the parent spawns the next child. There is no recursion and no depth
counter — the registry-level disable is the boundary.

## Map-Reduce

1. **Map** — spawn one child per independent slice (one file, provider, or dimension per
   child). Spawn them in one assistant message when independent.
2. **Reduce** — collect each child's `result`; reconcile conflicts before editing.
3. **Verify** — spawn a fresh reviewer that did not perform the changes to return
   PASS / FAIL / PARTIAL.

Parallel children must not edit the same file. The parent owns integration and final writes.

## Safety

Children inherit your approval, workspace and MCP boundaries. Do not use delegation to
bypass user confirmation: the child's `code_run` still surfaces a card in your stream in
interact mode. If a child is blocked, it reports the blocker in its result; escalate with
`ask_user` if needed.
