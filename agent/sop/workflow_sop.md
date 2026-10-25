# Workflow SOP

## What a workflow is

A workflow is a **saved, versioned graph** — a user-owned automation, drawn and
edited on the workflow page. You have the full management surface: read and
drive runs freely; **write operations (create/update/delete) need a one-time
approval card** — a workflow graph can execute code, write files and drive
browsers, so saving one is always the user's click:

- `workflow.list` — every workflow visible to you (latest version each).
- `workflow.get` — one definition in full. `args`: `def_id`, optional `version`.
- `workflow.run` — start a run in the background. `args`: `def_id`, `args` (run
  overrides for the declared custom inputs), optional `version`, optional
  `trigger_id`. Returns a `run_id` immediately and is **fire-and-forget**:
  poll `workflow.run_status(run_id=...)` or wait for the completion message.
  A run's outcome also reaches the user through the 结束 notification
  channels, and through `failure.agent_id` (which summons an agent with the
  failure context). **Do not re-run a workflow you already started** — wait
  for it, or cancel it first with `workflow.cancel`.
- `workflow.runs` — run history. `args`: `def_id`, optional `limit` (≤50).
- `workflow.run_status` — one run in full (per-node status/output/error/
  duration + variable snapshot). `args`: `run_id`.
- `workflow.cancel` — cancel an in-flight run (this process only; a finished
  or cross-process run returns `cancelled: false`). `args`: `run_id`.
- `workflow.create` / `update` / `delete` — manage the graph itself. Each one
  raises an approval card first (see below); without a user present they fail
  closed.

## The graph: nodes + edges

A definition is `{inputs?, nodes, edges?}`:

- `nodes` — typed steps (below). Each node: `id`, `name` (中文), `kind`,
  the kind's own params, optional `output_schema` (a `foreach` loop node instead
  carries `items`).
- `edges` — routing. An edge `{source, target, when?}` is active when its
  source succeeded AND `when` holds. A node runs when **at least one** incoming
  edge is active; nodes with no incoming edges run immediately. Branch logic
  lives on edges (the yes/no paths of a gateway), not on nodes.

## Step kinds

| kind | runs | required fields |
|------|------|-----------------|
| `code_run` | shell/python/powershell in the workflow's own workspace | `code` (+ `type`, `timeout`) |
| `file_read` | read a file under the workflow workspace | `path` (+ `start`/`count`/`keyword`) |
| `file_write` | write/append a file under the workflow workspace | `path`, `content` (+ `mode`) |
| `web` | one browser operation (self-selected browser client) | `operation` (+ `client_id` — often `"$input.<cdp_client key>"`, `url`, `script`, `tab`…) — login/2FA without a password → `fill_credential` (see § Logging in below) |
| `run_mcp` | one MCP service method call | `service`, `method`, `args` |
| `run_session` | a bounded agent run whose prompt is an SOP-style text | `sop` (+ `agent_id` or `"any"`, + `model` — often `"$input.<model key>"`, runs that step with that model on the agent's own key) |
| `cache` | Redis get/set/delete under the fixed `wf:` key prefix | `action`, `key` (+ `value`, `ttl`) |
| `http` | one outbound HTTP request through the proxy pool | `url` (+ `method`, `headers`, `body`, `timeout`, `proxy`) |
| `select` | pick a set of values by condition (the 判断/赋值 node) | `cases` (`[{when, set}]`, first match wins) + `default` |
| `foreach` | iterate a list, running its whole downstream once per element | `items` (a list expression) |

The `http` step's `proxy` field picks the outbound route: empty = direct,
a proxy-pool id = that entry, `"$input.<key>"` = a `proxy`-typed input's value.

### Iteration is a node: `foreach`

**遍历是图里的一个节点**，不是定时配置。`{id, kind: "foreach", items: "$list"}`
连同**它的全部下游**，按 `items` 的每个元素各跑一遍——下游是几步就连几步，
每个元素都把整段跑一次；`$item` 对体内所有节点可见。循环结束后，体内每个
节点的输出 = 各元素产出的列表（循环外用 `$bodynode` 读到数组）。体内可以再
放 `foreach`/`select`，自然嵌套。

- `items` — 数组表达式：`$fetch.list`（上游步骤输出）、`$input.accounts`（入参）等。
- `$item` — 当前元素，**形状跟随列表来源**：
  - 列表来自 `account` 类型入参（引擎灌 username 标量列表）→ `$item` 就是
    **账号名字符串**，用裸 `$item`（插值 `{$item}`、谓词 `$item == '12345'`），
    写 `$item.username` 会在运行时炸 `cannot descend into str`；
  - 列表来自上游节点输出的对象数组 → 元素是对象，`$item.username` / `$item.field`。
- 标量也能遍历（手动运行只挑了一个账号 = username 字符串）：当单元素跑一遍。
- `foreach` 节点的自身参数不能用 `$item`（它解析的是整个列表）。
- 体内节点报错会透传到运行记录，并标注失败元素（`[<元素>] 错误原文`）。

Canonical flow — per-account work with a chosen proxy/browser:

1. `http` fetches the account list (`output_schema` array), OR an `account` input
   carries it (username strings — the element is then the bare `$item`).
2. `foreach` with `items: "$fetch.list"` starts the loop.
3. A `select` node **in the body** branches on `$item`:
   `cases: [{when: "$item == 'vip'", set: {proxy_id: "proxy_vip", browser_id: "c1"}}, …]`
   (object lists branch on `$item.username == 'vip'` instead),
   `default: {proxy_id: "proxy_default"}`.
4. Body steps reference the chosen values: `http.proxy = "$pick.proxy_id"`,
   `web.client_id = "$pick.browser_id"`.

The same shape iterates a proxy list or a browser list — point `items` at the
list and branch in the body with `select`.

Prefer deterministic kinds. Reserve `run_session` for steps that genuinely need
an agent to think or explore; a fixed procedure is `code_run` / `run_mcp` / `web`.

## Inputs: declared, typed `$input.*`

The definition declares an inputs table — `{key, type?, note?, options?, default?,
required?, site_url?}` per entry (the **declaration**, edited on the 开始 node).
`type` is one of `string` (default) / `number` / `boolean` / `json` / `array` **or
a resource type** — `model` / `account` / `cdp_client` / `proxy`: a resource type
carries **what the value selects**, not its JSON shape, so the run panel renders
a picker from a platform-managed list instead of a free-form field.

- `model` — a gateway model name (the run panel shows the caller's model list).
  Consumed by a `run_session` step's `model` param, which forks the agent's bridge
  to that model for that step (the agent's own key is reused — no new credentials).
- `account` — credential **username(s)**. The site is declared on the input
  (`site_url`, matched by domain against the owner's browser passwords; metadata
  only, never passwords). Value shape depends on how it is supplied: a single
  username (the run panel picked one / a fan-out run's account), a list of
  usernames (`account_list(multiple:false)` cron seed — this is what a `foreach`
  sweep iterates), or whatever `args[key]` carried. Because the value is a
  *username string*, a loop over it sees the bare `$item`, not `$item.username`.
- `cdp_client` — a connected browser's `client_id`. Feed it to a `web` step by
  writing `client_id: "$input.<key>"`; the engine forwards it verbatim.
- `proxy` — a proxy-pool entry id (the run panel shows the caller's proxies, by
  ownership). Feed it to an `http` step's `proxy` field, or write the id directly;
  empty = direct.

Values you pass via `workflow.run`'s `args` are coerced to the declared type, so
`"7"` for a `number` arrives as `7`; a value that cannot be read as its type fails
the run with a clear message.

Three **属性** shape the run panel and the run-time contract:

- `options` — a non-empty list of scalar values (not allowed for `json`/`array`
  or the resource types, whose options are platform-supplied); the run panel
  renders a **dropdown** and the value must be one of them.
- `default` — the value the run panel pre-fills (the user may override); it must
  be readable as `type` and, when `options` is set, one of them. A run that omits
  the input gets this default.
- `required` — the run must supply a value; a run that leaves it blank *and* has
  no `default` fails up front (before the graph runs), not deep in the DAG.

How each input is *supplied* is decided per launch method (see 启动方式 below):
a cron trigger can seed a fixed value per input, the run dialog fills typed
fields, and the list a `foreach` node iterates usually comes from an upstream
step's own output (`$fetch.list`).

Nodes reference inputs as `$input.key`:

- `{$input.accounts}` inside any string interpolates (whatever the launch
  supplied — a list from the trigger, the run dialog, or an upstream step).
- A `foreach` node with `items: "$input.accounts"` (or `"$fetch.list"`) runs
  its whole downstream once per element, `$item` visible in every body node —
  the standard per-account sweep. `account`-typed inputs resolve to username
  strings, so the element is the bare `$item` (see § Iteration).

Runtime scopes: `$input.*` (declared inputs), `$args.*` (run overrides),
`$node.field` (an upstream node's validated output), `$item.*` (current foreach
element, anywhere in the loop body). A reference must point at an ancestor (an
edge path leads from it), and the field must exist in that node's
`output_schema` — the whole graph is checked at save time.

## Logging in without knowing the password (fill_credential)

A workflow can drive a full login while **no password or 2FA code ever enters
the graph, the run record, or your context**. The credentials live in the
user's browser-password / authenticator stores (owner-scoped); the login page
URL — or the account name a `foreach` sweep is currently on (`$item`) — is all
you carry.

The flow: `web(navigate, url=$input.login_url)` →
`web(fill_credential, ...)` → `web(execute, ...)` to click submit.

```json
{"id": "fill_login", "name": "填充登录凭据", "kind": "web",
 "operation": "fill_credential", "credential_kind": "password",
 "url": "$input.login_url", "account": "$item",
 "client_id": "$input.browser_id"}
```

- **`credential_kind`** (`"otp"` | `"password"`) is the credential type — the
  natural name `kind` is the *step type* field and cannot carry it (a step
  with a bare `kind: "otp"` fails at save time with exactly this message).
- `url` (the login page; its host selects the saved entry) or bare `domain`.
- `account` picks among several saved entries — inside a `foreach` over an
  `account`-typed input that is the bare `"$item"` (the element *is* the username);
  over an object list it is `"$item.username"`. Omit it and multiple matches
  return a labels-only candidate list with nothing filled.
- The result is a **receipt only** (`status`/`filled`/`field`/`entry`, and
  `expiresAt` for otp). The value travels 服务端→扩展→DOM for otp, and for
  password the extension resolves it from its own store by `entryId` — the
  server itself never sees the plaintext on the password path.
- **Filling does not submit** — a follow-up `web(execute)` clicks the button.
- `client_id` picks the browser the same way as any web step: the owner's
  single connected browser is chosen automatically; with several connected
  browsers a step without `client_id` is **refused at run time**, so a login
  graph meant to fan out across browsers should take it from a declared input.
- Empty result / `filled: false` with no entry means the site is not saved:
  that is a user action (extension popup 🛡 / web console). Never invent a
  value, and never substitute a hand-typed credential to work around it.

**Do not use `totp_code` in a workflow.** It *returns* the current 2FA code
into the step output, and step outputs are persisted in the run record
(`workflow_runs.nodes`/`context`) — that writes a live 2FA code into stored
run history. Its legitimate use is relaying a code into a non-DOM step, which
a workflow login never needs; a graph that contains `totp_code` is a design
smell worth flagging to the user.

## Launch methods (启动方式)

Only **cron** triggers are declared: `triggers: [{id, kind: "cron", cron, label?,
enabled?, params?}]`. `manual` (the UI run button) and `agent` (`workflow.run`)
are implicit — always available, never stored, shown as fixed nodes above「开始」.

Each trigger's `params.value` is a **static JSON object** — each key becomes
`$input.<key>` (an explicit run-time `args[key]` overrides it key by key). It is
the *fallback* for an unattended cron fire; a real agent launch passes its own
`args`.

**Iteration is NOT a trigger concern — it is the `foreach` node** (§ Step kinds /
Iteration above). A trigger only decides what values seed the inputs.

Two ways to seed them:

- `params.value` (source `custom`/`agent`) — a **static** JSON object; each key
  becomes `$input.<key>`. Use it for fixed values (a model name, a URL).
- `params: {source: "account_list", url, multiple: false}` — at cron time the
  engine resolves that site's saved accounts (usernames only, never passwords)
  and seeds every `account`-typed input with the **username list**. This is the
  pairing a `foreach` sweep wants: the trigger supplies the list once, the loop
  node iterates it. (`multiple: true` is the old whole-graph fan-out — N separate
  runs, one per account; keep it only for graphs already saved that way, since
  one run record per account is noisier than one loop.)

So: cron `account_list(multiple:false)` → `foreach items="$input.accounts"` →
body uses `{$item}` (the bare username). That is the canonical per-account sweep.

Several cron triggers can coexist (a nightly full sweep and an hourly probe of
the same graph, each with its own value). Triggers live in the definition — they
save, export and import with it. A run-time args object overrides a trigger's
value key by key; pass `trigger_id` to select which trigger to launch with.

## Terminal node (结束，含失败处理)

**结束** is the single terminal — its panel merges the notification and the
failure handling:

- **通知** `{notify_template?, notify_channels?, notify_when?}`: reuse the
  platform's channels (`mc_notify_channels`, webhook/钉钉/飞书/站内…); the user
  multi-selects which to deliver to. The template is the message body and
  supports `{$workflow.name}`, `{$workflow.label}`, `{$run.id}`, `{$run.status}`,
  `{$failed.node}`, `{$failed.error}`. `notify_when` decides when it fires —
  `success` (ok/partial only) | `failed` (failed only) | `always` (default).
- **失败处理** `{agent_id?}`: a non-null `agent_id` = on a failed run, hand the
  failure context (failed step, error) to that agent, which investigates and
  decides whether to re-run via `workflow.run` — the workflow never loops on its
  own. Null = no retry. (Whether a failure *notifies* is decided by
  `notify_when`, not here.)

## Conditions on edges

`when` is a small closed grammar, not Python: `$x == 'ok'`, `$x != 1`,
`$x >= 3`, `$x in ['a','b']`, `exists($x)`, plus `not` / `and` / `or`. A
condition may reference the edge's source or its ancestors. A `when` that
cannot be evaluated is treated as not-taken — never fatal.

## cache keys

Cache steps may only touch keys starting with the fixed prefix `wf:` (the editor
remark states it; validation enforces it at save and again at run time). The
platform's global `REDIS_PREFIX_KEY` is added on top, so a stored key reads
`<prefix>:wf:<key>`. Anything outside `wf:` is invisible to workflows.

## Failure semantics

- A node that raises → `failed`; its outgoing edges deactivate; dependents with
  no other active edge → `skipped`; unrelated branches keep running.
- An output violating its `output_schema` → the node fails (this is how you
  catch an agent that ignored your format).

## When to use a workflow at all

Use one when work **fans out across independent units** (N accounts, N pages,
N files) or when a fixed multi-step procedure with branches should run
unattended and repeatably. A single linear task is a plain agent turn — do not
start a one-node workflow.

## Saving a graph (create / update / delete)

You may build and save a graph yourself, but every definition write goes
through a one-time approval card — the user confirms the exact payload (name,
def_id, and a sha256 digest of the definition) before it lands. Auto-execute
mode auto-approves; an unattended context (no interactive caller) fails closed.
The card dedups its payload by digest: if you change anything after the card is
shown, the decision no longer applies and the write is denied — re-issue the
call to get a fresh card.

- `workflow.create` — `args`: `{name, label, description?, definition,
  retry_on_reboot?}`. New `v1` owned by the caller. The graph must be a
  non-empty `definition` (no empty shells — those are only for the UI
  create-form).
- `workflow.update` — `args`: `{def_id, definition, label?, description?,
  retry_on_reboot?}`. Saves a **new version** (old rows stay; runs keep
  pointing at their version); then reloads that def's cron jobs so an edited
  schedule takes effect immediately.
- `workflow.delete` — `args`: `{def_id}`. Deletes the whole version family
  (runs cascade with it) and removes the def's cron jobs. Destructive — prefer
  proposing it to the user unless they already asked.

You can still hand a JSON document to the user for 导入 / JSON-tab paste (the
format below); that path needs no approval because the user saves it
themselves. Use it when you want the user to retain full authorship, or when
you are in an unattended context where the approval card is unavailable.

## Drafting a definition (给用户起草工作流)

A definition is plain JSON. Build it, then either call `workflow.create`/
`workflow.update` yourself (the user approves via the one-time card), or hand
the JSON to the user for a self-serve import — both land in the user's name and
run the same static validation. The hand-off paths:

1. **Import file** (preferred for self-serve): give the user a complete JSON
   document and tell them to use 导入 on the workflow page. The document is
   exactly the export format — top-level `{kind, name, label, description,
   definition}`:
   - `kind` — literal `"workflow"`.
   - `name` — machine id, ASCII, unique per owner (e.g. `daily_site_sweep`).
   - `label` — the Chinese display name (required by the UI).
   - `description` — what it does and when to run it.
   - `definition` — the graph (shape below). `retry_on_reboot` defaults on and
     is not part of the import file.
2. **New + JSON tab**: the user clicks 新建 (name/label only, empty shell),
   opens the detail page's JSON tab, and pastes your full document. The JSON
   tab's object carries `name/label/description/retry_on_reboot` plus
   `inputs/triggers/nodes/edges/end/failure` — the same fields, one level up.

### The definition object

A per-account sweep with a chosen proxy/browser per account — the canonical
iteration shape: the **`foreach` node** iterates the list and its whole
downstream (a `select` that branches on `$item`, then the work step) runs once
per element:

```json
{
  "inputs":   [{"key": "model", "type": "model", "note": "跑哪个模型"}],
  "triggers": [{"id": "nightly", "kind": "cron", "cron": "17 2 * * *",
                "label": "夜间全量",
                "params": {"value": {"model": "claude-opus-4-8"}}}],
  "nodes": [
    {"id": "fetch", "name": "取账号列表", "kind": "http", "method": "GET",
     "url": "https://example.com/accounts",
     "output_schema": {"type": "object", "properties": {"list": {"type": "array"}}}},
    {"id": "each", "name": "遍历账号", "kind": "foreach", "items": "$fetch.list"},
    {"id": "pick", "name": "按账号选路由", "kind": "select",
     "cases": [{"when": "$item.username == 'vip'", "set": {"proxy_id": "proxy_vip", "browser_id": "c1"}},
               {"when": "$item.username == 'free'", "set": {"proxy_id": "proxy_free", "browser_id": "c2"}}],
     "default": {"proxy_id": "proxy_default", "browser_id": "c1"}},
    {"id": "work", "name": "逐账号干活", "kind": "http", "method": "POST",
     "url": "https://example.com/run", "proxy": "$pick.proxy_id",
     "body": "{\"user\": \"{$item.username}\"}"}
  ],
  "edges":    [{"source": "fetch", "target": "each"}, {"source": "each", "target": "pick"},
               {"source": "pick", "target": "work"}],
  "end":      {"notify_template": "「{$workflow.label}」{$run.status}", "notify_channels": [], "notify_when": "always"},
  "failure":  {"agent_id": null}
}
```

`each` is the loop node: `pick` and `work` are its downstream, so they run once
per account with `$item` = that account and `$pick.proxy_id` = the value chosen
for it. A `web` step would reference the browser the same way
(`client_id: "$pick.browser_id"`). Proxy/browser lists iterate identically —
point `items` at the list and branch in the body with `select`.

Second canonical shape — **`account` input, no fetch step** (token-refresh sweeps
like `agent/workflows/duanyan_token_refresh.json`): the list comes from the launch
method, so the element is a **username string** and every body step uses the bare
`$item`. The trigger's `account_list(multiple: false)` seeds the whole list at
fire time; the `foreach` node does the looping:

```json
{
  "inputs":   [{"key": "accounts", "type": "account",
                "site_url": "https://sso.example.com"}],
  "triggers": [{"id": "daily", "kind": "cron", "cron": "0 10 * * *", "enabled": true,
                "params": {"source": "account_list",
                           "url": "https://sso.example.com", "multiple": false}}],
  "nodes": [
    {"id": "each", "name": "遍历账号", "kind": "foreach", "items": "$input.accounts"},
    {"id": "read", "name": "读票", "kind": "cache", "action": "get",
     "key": "wf:site:token:{$item}"},
    {"id": "check", "name": "判过期", "kind": "code_run", "type": "python",
     "input_mapping": {"stored": "$read.value", "uid": "$item"},
     "code": "result = {'need_login': not bool(data.get('stored')), 'uid': data.get('uid')}",
     "output_schema": {"type": "object", "required": ["need_login"],
                       "properties": {"need_login": {"type": "boolean"}, "uid": {"type": "string"}}}},
    {"id": "login", "name": "按需登录", "kind": "run_session",
     "sop": "Log in as {$item} …"},
    {"id": "store", "name": "写票", "kind": "cache", "action": "set",
     "key": "wf:site:token:{$item}", "value": "$login", "ttl": 1209600}
  ],
  "edges":    [{"source": "each", "target": "read"},
               {"source": "read", "target": "check"},
               {"source": "check", "target": "login", "when": "$check.need_login == true"},
               {"source": "login", "target": "store"}]
}
```

Here the edge condition routes **inside the loop** — an account whose ticket is
still valid skips `login`/`store` for that iteration only; the next account runs
the whole segment again.

If the list is not fetched by a step but declared as an `account` input (the
common token-refresh case, see `agent/workflows/duanyan_token_refresh.json`),
the loop reads `{"items": "$input.accounts"}` and every body step uses the bare
`{$item}`:

```json
"triggers": [{"id": "daily", "kind": "cron", "cron": "0 10 * * *",
              "params": {"source": "account_list",
                         "url": "https://sso.example.com", "multiple": false}}]
```

with the matching input `{"key": "accounts", "type": "account", "site_url": "https://sso.example.com"}`
and body keys like `"key": "wf:site:token:{$item}"`.

Every field except `nodes` is optional; `inputs`/`triggers`/`edges`/`end`/
`failure` may be omitted entirely. Node/edge/trigger shapes are the ones
described above (步骤 kinds, 条件 on edges, 启动方式, 结束).

### Validation checklist (the save rejects the whole graph otherwise)

Run through this **before** handing the JSON over — every item is a hard
`WorkflowDefError` at save time:

- [ ] `nodes` non-empty (an import with `nodes: []` is rejected; only the
      create-form's first shell save tolerates empty).
- [ ] Node `id`s unique, identifier-shaped, not `args`/`input`/`item`.
- [ ] Every node has a non-empty `name` (步骤中文名) and a valid `kind`.
- [ ] No node carries `when` or `depends_on` — those fields no longer exist;
      routing lives on edges. Seeing them rejects the graph.
- [ ] No self-edges, no cycles.
- [ ] Every `$ref` in node params/edge conditions resolves: `$input.x` must be
      a declared input; `$node.f`/`$node.f.g` must point at an **ancestor**
      node, and `f` (and `g`) must exist in that node's `output_schema` when it
      declares one; `$item.*` only inside a `foreach` scope.
- [ ] `code_run` python steps assign `result` (the executor reads
      `__WF_RESULT__`; a python step without it fails at run time).
- [ ] `cache` keys start with `wf:`; `cache` action is `get`/`set`/`delete`.
- [ ] `web` `operation: "fill_credential"` carries `credential_kind` =
      `"otp"` | `"password"` (the field `kind` is the step type, not the
      credential type). `totp_code` is discouraged — it writes a live 2FA code
      into the run record.
- [ ] `run_session` `agent_id` is a number, `"any"`, or empty; optional
      `model` is a model name (or a `$input.<model key>` reference).
- [ ] `account`-typed inputs carry a `site_url` (the account list is resolved
      from it); `cdp_client`/`model`/`proxy` inputs need no `options`.
- [ ] `http` `method ∈ {GET, POST, PUT, PATCH, DELETE, HEAD}`; `url` non-empty;
      `proxy` is empty (direct), a proxy-pool id, or `$input.<key>` referencing a
      `proxy`-typed input.
- [ ] `select` `cases` is an array of `{when, set}` (first matching `set` wins);
      when `cases` is non-empty, `default` is required; `set`/`default` are
      objects whose values may carry `$ref`s.
- [ ] `foreach` node's `items` is a list expression resolving to an **ancestor**
      (or input); `$item` only in its downstream. The loop runs the foreach node
      plus its whole downstream once per element. Element shape follows the list:
      `account`-typed input → bare `$item` (username string); object list →
      `$item.field`. A scalar items (single account picked) loops once.
- [ ] Trigger `id`s unique identifiers; only `kind: "cron"`; `params.value` is
      an object (per-key static values).
- [ ] Legacy `params.source ∈ {agent, custom, account_list}` still accepted
      (`account_list` needs `url`); `params.values` (per-param modes) is legacy —
      new graphs express iteration with a `foreach` node.
- [ ] `end.notify_when ∈ {success, failed, always}`; template ≤ 2000 chars.
- [ ] `failure` has **no** `action` field (rejected — retry config is only
      `failure.agent_id` now).

### Drafting etiquette

- Ask the user for the pieces only they know (which accounts/sites, which
  notify channels, whether a failure should summon an agent) — do not invent
  channel ids or agent ids.
- Reuse an existing workflow when one fits: `workflow.get` it, propose a
  modified copy rather than a near-duplicate.
- An import creates **v1 under the importing user** — tell the user the
  schedule (triggers) travels inside the definition, so nothing else needs
  re-entering after import.
- Prefer deterministic steps; keep `run_session` for the steps that genuinely
  need an agent (see the step-kind table above).