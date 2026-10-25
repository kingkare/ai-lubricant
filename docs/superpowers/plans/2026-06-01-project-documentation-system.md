# Project Documentation System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new four-level project documentation center under `docs/project-system/` and migrate the highest-value existing project context into it with an explicit quality review.

**Architecture:** `docs/project-system/` becomes the current documentation entry point. `master-plan.md` owns project direction, `whitepapers/` owns long-lived reasons and principles, `plans/` owns implementation work, and `code-map.md` links code modules to the relevant reasons and active work. Historical documents remain readable but stop acting as the default source of truth for new requirements.

**Tech Stack:** Markdown documentation, existing repository docs, shell verification, Python one-off consistency checks.

---

## File Structure

Create these files and directories:

- Create: `docs/project-system/README.md` — entry point, reading order, four-level rules, demand sizing.
- Create: `docs/project-system/master-plan.md` — project-level roadmap, priorities, status, active themes.
- Create: `docs/project-system/code-map.md` — source-module-to-documentation index.
- Create: `docs/project-system/whitepapers/request-logging.md` — request log rationale, principles, confirmed decisions.
- Create: `docs/project-system/whitepapers/token-accounting.md` — token accounting rationale, project convention, confirmed decisions.
- Create: `docs/project-system/whitepapers/model-routing.md` — model routing rationale, principles, confirmed decisions.
- Create: `docs/project-system/whitepapers/protocol-passthrough.md` — same-protocol passthrough rationale and constraints.
- Create: `docs/project-system/plans/2026-06-01-project-documentation-system.md` — migration execution record for this documentation-system work.
- Create: `docs/project-system/archive/README.md` — states what archive is and is not.
- Create: `docs/project-system/archive/old-plans/README.md` — points to old plan locations during migration.
- Modify: `docs/README.md` — add `docs/project-system/README.md` as the first project documentation entry.

Do not move old files in this first implementation pass. Copying or moving historical files can happen in a follow-up after the new center proves useful. This avoids breaking existing references while still establishing the new source of truth for future requirements.

---

### Task 1: Create the documentation center skeleton

**Files:**
- Create: `docs/project-system/README.md`
- Create: `docs/project-system/archive/README.md`
- Create: `docs/project-system/archive/old-plans/README.md`

- [ ] **Step 1: Create `docs/project-system/README.md`**

Write this exact content:

```md
# 项目文档中枢

这里是本项目后续需求、设计、实施和追溯的默认入口。

## 四级文档

```text
代码 -> 计划 -> 白皮书 -> 总计划
```

- **代码层**：真实实现和测试，以源码为准。文档只通过 `code-map.md` 指向关键入口，不复制实现细节。
- **计划层**：回答“这次怎么做”。每个计划描述一轮可执行工作、验证结果和遗留问题。
- **白皮书层**：回答“为什么这样设计”。跨模块规则、长期口径、协议约束和已确认取舍写在这里。
- **总计划层**：回答“项目往哪走”。只维护方向、优先级、主题状态和下一步，不写实现步骤。

## 阅读顺序

新需求或新对话从这里开始：

1. 读 [`master-plan.md`](./master-plan.md)，确认当前项目优先级和主题状态。
2. 按主题读对应白皮书：[`whitepapers/`](./whitepapers/)。
3. 读相关实施计划：[`plans/`](./plans/)。
4. 用 [`code-map.md`](./code-map.md) 找源码入口和现有模块说明。
5. 最后再读源码和测试。

## 需求分级

- **S：小修小补** — 代码 + 计划条目。例如文案、样式、单个 bug、小范围测试补充。
- **M：普通功能/行为变化** — 代码 + 独立计划。计划必须链接白皮书；如果没有白皮书，写明原因。
- **L：跨模块或长期规则变化** — 代码 + 独立计划 + 白皮书章节或新白皮书 + 总计划条目。
- **XL：项目级方向调整** — 先改总计划，再写或改白皮书，再拆多个计划，最后动代码。

## 维护规则

- 新计划必须写 `Whitepaper:` 和 `Master plan item:`。
- 新白皮书必须写 `Plans:`。
- `master-plan.md` 必须链接所有 `active` 和 `planned` 白皮书。
- 计划只描述一轮实施；长期原则必须进入白皮书。
- 白皮书只写长期背景、原则和决策；具体步骤属于计划。
- 总计划只写方向、优先级和状态；实现步骤属于计划。
- `archive/` 只保存历史材料，不作为当前事实来源。

## 当前入口

- 总计划：[`master-plan.md`](./master-plan.md)
- 代码索引：[`code-map.md`](./code-map.md)
- 白皮书目录：[`whitepapers/`](./whitepapers/)
- 实施计划目录：[`plans/`](./plans/)
- 历史归档说明：[`archive/`](./archive/)
```

- [ ] **Step 2: Create `docs/project-system/archive/README.md`**

Write this exact content:

```md
# 历史归档

这里保存不再主动维护的历史材料或迁移过程中的旧入口说明。

归档内容不能作为当前事实来源。需要判断当前行为时，按顺序读取：

1. `docs/project-system/master-plan.md`
2. 相关 `docs/project-system/whitepapers/*.md`
3. 相关 `docs/project-system/plans/*.md`
4. `docs/project-system/code-map.md`
5. 源码和测试

如果归档内容和当前文档或代码冲突，以当前文档和代码为准。
```

- [ ] **Step 3: Create `docs/project-system/archive/old-plans/README.md`**

Write this exact content:

```md
# 旧计划入口说明

旧计划目前仍保留在原位置，避免迁移初期破坏已有引用：

- 根目录 `plans/`
- `docs/superpowers/plans/`
- `docs/superpowers/specs/`

后续只在 `docs/project-system/plans/` 新增计划。旧计划被再次使用时，应把仍然有效的内容迁入新的计划或白皮书，并在迁移计划中记录审查结论。
```

- [ ] **Step 4: Verify skeleton files exist**

Run:

```bash
test -f docs/project-system/README.md && test -f docs/project-system/archive/README.md && test -f docs/project-system/archive/old-plans/README.md
```

Expected: command exits with status 0 and prints no output.

- [ ] **Step 5: Commit skeleton**

Run:

```bash
git add docs/project-system/README.md docs/project-system/archive/README.md docs/project-system/archive/old-plans/README.md
git commit -m "docs: add project documentation center skeleton"
```

Expected: commit succeeds.

---

### Task 2: Add the master plan and code map

**Files:**
- Create: `docs/project-system/master-plan.md`
- Create: `docs/project-system/code-map.md`

- [ ] **Step 1: Create `docs/project-system/master-plan.md`**

Write this exact content:

```md
# 项目总计划

## 当前阶段

项目正在从“需求和原因分散在代码、计划、聊天记录和 spec 中”迁移到“四级文档体系”。当前阶段的重点是先把长期规则和高频变更主题收拢，减少后续需求反复丢上下文。

## 主题优先级

| 优先级 | 主题 | 状态 | 白皮书 | 当前下一步 |
| --- | --- | --- | --- | --- |
| P0 | 请求日志 | active | [`request-logging.md`](./whitepapers/request-logging.md) | 把请求路径、渠道请求记录、详情展示规则收敛到白皮书和新计划入口。 |
| P0 | Token 计费口径 | active | [`token-accounting.md`](./whitepapers/token-accounting.md) | 确认所有统计和展示都使用 total=输入+输出，缓存读/写只作为输入明细。 |
| P1 | 模型路由 | active | [`model-routing.md`](./whitepapers/model-routing.md) | 收敛模型组、模型元数据、渠道筛选和 Public API 语义。 |
| P1 | 同协议直通 | planned | [`protocol-passthrough.md`](./whitepapers/protocol-passthrough.md) | 明确 anthropic 到 anthropic 原始转发边界，避免跨协议逻辑污染同协议路径。 |
| P2 | 文档体系迁移 | active | 本文件和 [`README.md`](./README.md) | 执行迁移计划并做迁移中、迁移后合理性审查。 |

## 状态定义

- `planned`：已确认需要做，但当前不是主线。
- `active`：正在设计、迁移或实现。
- `paused`：暂停推进，需要原因。
- `done`：当前目标完成并已验证。

## 本阶段完成标准

- 新需求默认从 `docs/project-system/README.md` 进入。
- P0/P1 主题都有白皮书，且白皮书只记录长期原因和原则。
- 新计划都写明 `Whitepaper:` 和 `Master plan item:`。
- 至少完成一次从“总计划 -> 白皮书 -> 计划 -> 代码索引”的追溯检查。
- 至少完成一次从“代码模块 -> 代码索引 -> 白皮书/计划”的反向追溯检查。

## 当前最应该做的下一步

执行 [`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)，先建立新中枢和四篇高价值白皮书，再审查结构是否真的降低混乱。
```

- [ ] **Step 2: Create `docs/project-system/code-map.md`**

Write this exact content:

```md
# 代码索引

这个文件只指向源码入口和相关文档，不复制实现细节。真实行为以源码和测试为准。

## 公共 API 与请求生命周期

- 关键文件：`main.py`
- 模块说明：[`../modules/main-app.md`](../modules/main-app.md)
- 相关白皮书：
  - [`whitepapers/request-logging.md`](./whitepapers/request-logging.md)
  - [`whitepapers/token-accounting.md`](./whitepapers/token-accounting.md)
  - [`whitepapers/protocol-passthrough.md`](./whitepapers/protocol-passthrough.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)

## 管理端

- 关键文件：`admin.py`、`static/admin.html`
- 模块说明：[`../modules/admin.md`](../modules/admin.md)、[`../modules/static-scripts.md`](../modules/static-scripts.md)
- 相关白皮书：
  - [`whitepapers/request-logging.md`](./whitepapers/request-logging.md)
  - [`whitepapers/model-routing.md`](./whitepapers/model-routing.md)
  - [`whitepapers/token-accounting.md`](./whitepapers/token-accounting.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)

## 数据库和请求日志

- 关键文件：`db.py`、`sql/init.sql`
- 模块说明：[`../modules/database.md`](../modules/database.md)
- 相关白皮书：
  - [`whitepapers/request-logging.md`](./whitepapers/request-logging.md)
  - [`whitepapers/token-accounting.md`](./whitepapers/token-accounting.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)

## 路由、限流和账号池

- 关键文件：`rate_limiter.py`
- 模块说明：[`../modules/routing-rate-limit.md`](../modules/routing-rate-limit.md)
- 相关白皮书：[`whitepapers/model-routing.md`](./whitepapers/model-routing.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)

## Provider 层

- 关键文件：`providers/`
- 模块说明：[`../modules/providers-layer.md`](../modules/providers-layer.md)、[`../providers/README.md`](../providers/README.md)
- 相关白皮书：
  - [`whitepapers/model-routing.md`](./whitepapers/model-routing.md)
  - [`whitepapers/protocol-passthrough.md`](./whitepapers/protocol-passthrough.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)

## 协议转换和工具调用

- 关键文件：`message_utils.py`、`tool_utils.py`
- 模块说明：[`../modules/protocol-tools.md`](../modules/protocol-tools.md)
- 相关白皮书：[`whitepapers/protocol-passthrough.md`](./whitepapers/protocol-passthrough.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)

## 模型元数据

- 关键文件：`model_metadata.py`、`model_metadata.json`
- 模块说明：[`../modules/model-metadata.md`](../modules/model-metadata.md)
- 相关白皮书：[`whitepapers/model-routing.md`](./whitepapers/model-routing.md)
- 活跃计划：[`plans/2026-06-01-project-documentation-system.md`](./plans/2026-06-01-project-documentation-system.md)
```

- [ ] **Step 3: Verify master plan links planned and active whitepapers**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path('docs/project-system/master-plan.md').read_text(encoding='utf-8')
required = [
    './whitepapers/request-logging.md',
    './whitepapers/token-accounting.md',
    './whitepapers/model-routing.md',
    './whitepapers/protocol-passthrough.md',
]
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit(f'missing links: {missing}')
print('master plan links ok')
PY
```

Expected output:

```text
master plan links ok
```

- [ ] **Step 4: Commit master plan and code map**

Run:

```bash
git add docs/project-system/master-plan.md docs/project-system/code-map.md
git commit -m "docs: add project master plan and code map"
```

Expected: commit succeeds.

---

### Task 3: Add request logging and token accounting whitepapers

**Files:**
- Create: `docs/project-system/whitepapers/request-logging.md`
- Create: `docs/project-system/whitepapers/token-accounting.md`

- [ ] **Step 1: Create `docs/project-system/whitepapers/request-logging.md`**

Write this exact content:

```md
# 请求日志白皮书

## 背景

请求日志是排查路由、重试、渠道调用、耗时、token 和管理端展示问题的核心事实来源。过去相关规则分散在计划、spec、代码和聊天上下文中，导致“一个用户请求”和“多次渠道请求”的关系容易被误解。

## 问题

旧设计容易把请求关系表达成父子 ID 或前端拼接路径，维护者难以判断哪条记录是真实渠道调用，哪条记录只是用户请求视角。请求 ID、数据库 ID、重试路径、渠道尝试和详情展示之间缺少稳定说明。

## 原则

- 渠道请求必须逐条记录到现有请求日志表格。
- 请求日志表是请求观测的事实来源。
- 一次用户请求使用一个用户可见 request ID。
- 渠道尝试之间的关系通过后端真实记录表达，不由前端伪造。
- 详情页应显示请求路径，让用户能看懂每次渠道尝试。
- 请求日志展示必须能区分请求模型、实际模型、渠道、账号、状态、耗时和 token。

## 决策

- 渠道请求逐条写入日志表，而不是只记录最终成功或失败结果。
- 请求路径应由后端保存的真实日志行或路径字段展开。
- 管理端详情页展示请求路径，不把请求关系只藏在原始 JSON 里。
- 请求统计和渠道调用统计不能混为一谈：用户请求数按用户请求口径统计，渠道调用按实际日志行或路径统计。

## 非目标

- 不把请求日志改成独立的全新观测系统。
- 不让前端通过猜测生成请求路径。
- 不把历史计划中的临时字段名自动提升为长期规则。

## Plans

- [`../plans/2026-06-01-project-documentation-system.md`](../plans/2026-06-01-project-documentation-system.md)
- 旧参考：`../../superpowers/plans/2026-06-01-request-log-path-db-id.md`
- 旧参考：`../../superpowers/specs/2026-05-30-request-logs-model-groups-design.md`
```

- [ ] **Step 2: Create `docs/project-system/whitepapers/token-accounting.md`**

Write this exact content:

```md
# Token 计费口径白皮书

## 背景

项目同时处理 OpenAI、Anthropic 和多个上游渠道的 usage 字段。不同上游字段名不同，缓存读写字段也不同。如果没有统一内部口径，管理端展示、请求日志和统计会出现重复计算或错位。

## 问题

缓存读和缓存写曾被误当成 total 之外的额外 token 加项，导致 total 统计偏大。另一个问题是输入、输出、缓存读、缓存写在不同入口各自解析，字段映射不一致。

## 原则

- `total_tokens = prompt_tokens + completion_tokens`。
- `prompt_tokens` 表示输入 token 总数。
- `completion_tokens` 表示输出 token 总数。
- `cached_tokens` 是输入 token 的缓存读明细。
- `cache_creation_tokens` 是输入 token 的缓存写明细。
- 缓存读和缓存写要展示，但不能额外加到 total。
- 后端统计和前端展示必须使用同一套项目口径。

## 决策

内部归一化时按以下含义理解字段：

```text
raw_input = usage.prompt_tokens 或 usage.input_tokens
raw_output = usage.completion_tokens 或 usage.output_tokens
cached = usage.cached_tokens / usage.cache_read_input_tokens / details.cached_tokens
cache_creation = usage.cache_creation_tokens / usage.cache_creation_input_tokens / details.cache_creation_input_tokens
prompt = raw_input
completion = raw_output
total = prompt + completion
```

展示层可以显示输入、输出、缓存读、缓存写四类，但 total 只显示输入加输出。

## 非目标

- 不为每个上游定义独立计费语义。
- 不把缓存读写隐藏起来。
- 不在前端二次修正后端统计口径。

## Plans

- [`../plans/2026-06-01-project-documentation-system.md`](../plans/2026-06-01-project-documentation-system.md)
- 旧参考：`../../superpowers/plans/2026-06-01-request-log-path-db-id.md`
```

- [ ] **Step 3: Verify both whitepapers expose `Plans:` sections**

Run:

```bash
python - <<'PY'
from pathlib import Path
for path in [
    Path('docs/project-system/whitepapers/request-logging.md'),
    Path('docs/project-system/whitepapers/token-accounting.md'),
]:
    text = path.read_text(encoding='utf-8')
    if '## Plans' not in text:
        raise SystemExit(f'{path} missing Plans section')
print('request logging and token accounting whitepapers ok')
PY
```

Expected output:

```text
request logging and token accounting whitepapers ok
```

- [ ] **Step 4: Commit first whitepapers**

Run:

```bash
git add docs/project-system/whitepapers/request-logging.md docs/project-system/whitepapers/token-accounting.md
git commit -m "docs: add request logging and token accounting whitepapers"
```

Expected: commit succeeds.

---

### Task 4: Add model routing and protocol passthrough whitepapers

**Files:**
- Create: `docs/project-system/whitepapers/model-routing.md`
- Create: `docs/project-system/whitepapers/protocol-passthrough.md`

- [ ] **Step 1: Create `docs/project-system/whitepapers/model-routing.md`**

Write this exact content:

```md
# 模型路由白皮书

## 背景

项目统一暴露 OpenAI 和 Anthropic 兼容接口，并把请求路由到多个上游渠道。模型组、模型元数据、渠道模型列表、账号池和 Public API 展示都会影响用户实际能请求到什么模型。

## 问题

模型组和模型专用线路相关需求分散在管理端 UI、路由代码、数据库配置和实施计划里。缺少长期规则时，容易出现 Public API 暴露语义、渠道筛选和管理端展示不一致。

## 原则

- 模型路由必须以真实可用渠道和账号能力为基础。
- 管理端展示应帮助用户理解请求模型和实际模型之间的关系。
- 模型组是用户侧入口，实际模型是路由后的执行目标，两者不能混淆。
- Public API 返回内容必须符合 API Key 可用范围。
- 渠道支持关系变化后，管理端和路由结果应反映最新配置。

## 决策

- 请求日志中模型组请求应能展示请求组和实际模型。
- 模型组名称不应被当作可随意重命名的临时字段；复制比重命名更安全。
- 模型组 Public API 应按 API Key 范围和可用性返回结果。
- 模型专用线路配置应围绕目标模型筛选支持渠道。

## 非目标

- 不在白皮书中固定某个具体 UI 布局。
- 不把某次模型路由实现中的临时字段名当作长期 API。
- 不承诺所有上游 provider 都拥有相同能力。

## Plans

- [`../plans/2026-06-01-project-documentation-system.md`](../plans/2026-06-01-project-documentation-system.md)
- 旧参考：`../../superpowers/plans/2026-06-01-model-group-public-api.md`
- 旧参考：`../../superpowers/plans/2026-05-30-request-logs-model-groups.md`
- 旧参考：`../../superpowers/specs/2026-05-30-request-logs-model-groups-design.md`
```

- [ ] **Step 2: Create `docs/project-system/whitepapers/protocol-passthrough.md`**

Write this exact content:

```md
# 同协议直通白皮书

## 背景

项目同时支持 OpenAI 兼容和 Anthropic 兼容请求。跨协议转换需要字段映射，但同协议请求不应该被跨协议逻辑污染。

## 问题

如果 anthropic 到 anthropic 的路径也进入通用字段补齐或协议映射逻辑，可能改变原始请求语义，造成上游行为与用户请求不一致。默认字段补齐也可能掩盖调用方真实输入。

## 原则

- anthropic 到 anthropic 必须原始字节流转发。
- 只有跨协议时才做字段映射。
- 任何路径都不补默认字段。
- 同协议直通路径的目标是保持用户请求语义，而不是让请求看起来更完整。

## 决策

- 同协议直通是协议层长期约束，不应被单次 provider 适配需求覆盖。
- 跨协议转换和同协议直通应在设计和测试中明确区分。
- 如果上游要求特殊字段，应作为该上游适配的显式规则处理，不能隐式污染所有同协议请求。

## 非目标

- 不禁止跨协议转换。
- 不为缺字段请求自动生成默认业务含义。
- 不把 provider 特殊兼容逻辑提升为全局协议规则。

## Plans

- [`../plans/2026-06-01-project-documentation-system.md`](../plans/2026-06-01-project-documentation-system.md)
```

- [ ] **Step 3: Verify both whitepapers expose `Plans:` sections**

Run:

```bash
python - <<'PY'
from pathlib import Path
for path in [
    Path('docs/project-system/whitepapers/model-routing.md'),
    Path('docs/project-system/whitepapers/protocol-passthrough.md'),
]:
    text = path.read_text(encoding='utf-8')
    if '## Plans' not in text:
        raise SystemExit(f'{path} missing Plans section')
print('model routing and protocol passthrough whitepapers ok')
PY
```

Expected output:

```text
model routing and protocol passthrough whitepapers ok
```

- [ ] **Step 4: Commit second whitepapers**

Run:

```bash
git add docs/project-system/whitepapers/model-routing.md docs/project-system/whitepapers/protocol-passthrough.md
git commit -m "docs: add routing and protocol whitepapers"
```

Expected: commit succeeds.

---

### Task 5: Add the migration execution plan to the new plan layer

**Files:**
- Create: `docs/project-system/plans/2026-06-01-project-documentation-system.md`

- [ ] **Step 1: Create `docs/project-system/plans/2026-06-01-project-documentation-system.md`**

Write this exact content:

```md
# 项目文档体系迁移计划

Whitepaper: `docs/superpowers/specs/2026-06-01-project-documentation-system-design.md`
Master plan item: `docs/project-system/master-plan.md` 中的 “文档体系迁移”

## 目标

建立 `docs/project-system/` 四级文档中枢，并把请求日志、Token 计费口径、模型路由、同协议直通四个高价值主题迁移为白皮书。

## 已确认需求

- 使用“代码 -> 计划 -> 白皮书 -> 总计划”的四层关系。
- 白皮书记录长期主题的背景、原则和决策依据。
- 总计划记录全项目路线图、优先级和主题状态。
- 文档体系优先服务用户快速看懂、验收、追溯和做决策，其次服务 AI 接手。
- 需求按 S/M/L/XL 分级，不强制所有需求补满四层。
- 采用彻底重整的新目录：`docs/project-system/`。
- 迁移中和迁移完成后都要审查结构是否合理。

## 影响文件

- `docs/project-system/README.md`
- `docs/project-system/master-plan.md`
- `docs/project-system/code-map.md`
- `docs/project-system/whitepapers/request-logging.md`
- `docs/project-system/whitepapers/token-accounting.md`
- `docs/project-system/whitepapers/model-routing.md`
- `docs/project-system/whitepapers/protocol-passthrough.md`
- `docs/project-system/archive/README.md`
- `docs/project-system/archive/old-plans/README.md`
- `docs/README.md`

## 实施步骤

- [ ] 建立 `docs/project-system/` 入口、归档说明和旧计划入口说明。
- [ ] 创建 `master-plan.md` 和 `code-map.md`。
- [ ] 创建请求日志和 Token 计费口径白皮书。
- [ ] 创建模型路由和同协议直通白皮书。
- [ ] 更新 `docs/README.md`，把新文档中枢放到推荐阅读路径第一位。
- [ ] 检查所有计划和白皮书链接规则。
- [ ] 做迁移中合理性审查。
- [ ] 做迁移后合理性审查。

## 迁移中合理性审查

每个主题迁移时检查：

| 主题 | 是否适合白皮书 | 是否只保留长期原则 | 是否避免复制实现细节 | 结论 |
| --- | --- | --- | --- | --- |
| 请求日志 | 是 | 是 | 是 | 请求日志影响观测、统计和管理端展示，适合白皮书。 |
| Token 计费口径 | 是 | 是 | 是 | Token total 和缓存明细是长期统计口径，适合白皮书。 |
| 模型路由 | 是 | 是 | 是 | 模型组、渠道、Public API 语义跨模块，适合白皮书。 |
| 同协议直通 | 是 | 是 | 是 | 协议边界是长期约束，适合白皮书。 |

## 迁移后合理性审查

检查项：

- [ ] 是否存在两个当前事实来源互相冲突。
- [ ] 是否有白皮书写成实施步骤。
- [ ] 是否有计划承担长期原则。
- [ ] 是否有总计划写得过细。
- [ ] 是否能从总计划追溯到白皮书、计划和代码索引。
- [ ] 是否能从代码模块反向追溯到相关白皮书和计划。

## 验证方式

运行链接和结构检查：

```bash
python - <<'PY'
from pathlib import Path
root = Path('docs/project-system')
required = [
    root / 'README.md',
    root / 'master-plan.md',
    root / 'code-map.md',
    root / 'whitepapers/request-logging.md',
    root / 'whitepapers/token-accounting.md',
    root / 'whitepapers/model-routing.md',
    root / 'whitepapers/protocol-passthrough.md',
    root / 'plans/2026-06-01-project-documentation-system.md',
    root / 'archive/README.md',
    root / 'archive/old-plans/README.md',
]
missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit(f'missing files: {missing}')

for path in (root / 'whitepapers').glob('*.md'):
    text = path.read_text(encoding='utf-8')
    if '## Plans' not in text:
        raise SystemExit(f'{path} missing Plans section')

plan = (root / 'plans/2026-06-01-project-documentation-system.md').read_text(encoding='utf-8')
for marker in ['Whitepaper:', 'Master plan item:']:
    if marker not in plan:
        raise SystemExit(f'plan missing {marker}')

master = (root / 'master-plan.md').read_text(encoding='utf-8')
for link in [
    './whitepapers/request-logging.md',
    './whitepapers/token-accounting.md',
    './whitepapers/model-routing.md',
    './whitepapers/protocol-passthrough.md',
]:
    if link not in master:
        raise SystemExit(f'master plan missing {link}')

print('project documentation system structure ok')
PY
```

Expected output:

```text
project documentation system structure ok
```

## 完成状态

- 创建后更新本节，写明验证命令和结果。
- 若审查发现结构问题，先修正文档规则，再继续迁移旧内容。

## 遗留问题

- 旧计划是否迁入 `archive/old-plans/`，需要等新中枢使用一轮后决定。
- `docs/superpowers/specs/` 和 `docs/superpowers/plans/` 是否保留为工具产物目录，需要等执行流程稳定后决定。
```

- [ ] **Step 2: Verify the new plan has required links**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path('docs/project-system/plans/2026-06-01-project-documentation-system.md').read_text(encoding='utf-8')
for marker in ['Whitepaper:', 'Master plan item:', '## 迁移中合理性审查', '## 迁移后合理性审查']:
    if marker not in text:
        raise SystemExit(f'missing marker: {marker}')
print('migration plan links and review sections ok')
PY
```

Expected output:

```text
migration plan links and review sections ok
```

- [ ] **Step 3: Commit migration plan**

Run:

```bash
git add docs/project-system/plans/2026-06-01-project-documentation-system.md
git commit -m "docs: add documentation system migration plan"
```

Expected: commit succeeds.

---

### Task 6: Update the existing docs entry point

**Files:**
- Modify: `docs/README.md`

- [ ] **Step 1: Modify the recommended reading path in `docs/README.md`**

Change this section:

```md
## 推荐阅读路径

1. [平台说明](./platform.md)
2. [系统架构](./architecture.md)
3. [系统模块索引](./modules/README.md)
4. [渠道文档索引](./providers/README.md)
5. [统一问题与改进清单](./issues.md)
6. [新对话交接指南](./guides/new-chat-handoff.md)
```

To this exact content:

```md
## 推荐阅读路径

1. [项目文档中枢](./project-system/README.md)
2. [平台说明](./platform.md)
3. [系统架构](./architecture.md)
4. [系统模块索引](./modules/README.md)
5. [渠道文档索引](./providers/README.md)
6. [统一问题与改进清单](./issues.md)
7. [新对话交接指南](./guides/new-chat-handoff.md)
```

- [ ] **Step 2: Add the project system to topic docs in `docs/README.md`**

Change this section:

```md
## 专题文档

- [完善建议](./improvements.md)：较高层面的后续建设建议。
- [Codex 工作提示词](./codex-prompt.md)：目录结构、计划编写、改动规范和搜索规则。
- [新对话交接指南](./guides/new-chat-handoff.md)：开启新对话时如何让 AI 继续使用本项目的约定和模式。
- [指南索引](./guides/README.md)：跨模块工作指南和交接说明。
```

To this exact content:

```md
## 专题文档

- [项目文档中枢](./project-system/README.md)：四级文档体系、总计划、白皮书、实施计划和代码索引。
- [完善建议](./improvements.md)：较高层面的后续建设建议。
- [Codex 工作提示词](./codex-prompt.md)：目录结构、计划编写、改动规范和搜索规则。
- [新对话交接指南](./guides/new-chat-handoff.md)：开启新对话时如何让 AI 继续使用本项目的约定和模式。
- [指南索引](./guides/README.md)：跨模块工作指南和交接说明。
```

- [ ] **Step 3: Verify `docs/README.md` points to the new center**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path('docs/README.md').read_text(encoding='utf-8')
needle = '[项目文档中枢](./project-system/README.md)'
count = text.count(needle)
if count != 2:
    raise SystemExit(f'expected 2 project-system links, found {count}')
print('docs README entrypoint ok')
PY
```

Expected output:

```text
docs README entrypoint ok
```

- [ ] **Step 4: Commit docs entry update**

Run:

```bash
git add docs/README.md
git commit -m "docs: point documentation entry to project system"
```

Expected: commit succeeds.

---

### Task 7: Run structure verification and complete review notes

**Files:**
- Modify: `docs/project-system/plans/2026-06-01-project-documentation-system.md`

- [ ] **Step 1: Run full structure verification**

Run:

```bash
python - <<'PY'
from pathlib import Path
root = Path('docs/project-system')
required = [
    root / 'README.md',
    root / 'master-plan.md',
    root / 'code-map.md',
    root / 'whitepapers/request-logging.md',
    root / 'whitepapers/token-accounting.md',
    root / 'whitepapers/model-routing.md',
    root / 'whitepapers/protocol-passthrough.md',
    root / 'plans/2026-06-01-project-documentation-system.md',
    root / 'archive/README.md',
    root / 'archive/old-plans/README.md',
]
missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit(f'missing files: {missing}')

for path in (root / 'whitepapers').glob('*.md'):
    text = path.read_text(encoding='utf-8')
    if '## Plans' not in text:
        raise SystemExit(f'{path} missing Plans section')

plan = (root / 'plans/2026-06-01-project-documentation-system.md').read_text(encoding='utf-8')
for marker in ['Whitepaper:', 'Master plan item:', '## 迁移中合理性审查', '## 迁移后合理性审查']:
    if marker not in plan:
        raise SystemExit(f'plan missing {marker}')

master = (root / 'master-plan.md').read_text(encoding='utf-8')
for link in [
    './whitepapers/request-logging.md',
    './whitepapers/token-accounting.md',
    './whitepapers/model-routing.md',
    './whitepapers/protocol-passthrough.md',
]:
    if link not in master:
        raise SystemExit(f'master plan missing {link}')

readme = Path('docs/README.md').read_text(encoding='utf-8')
if readme.count('[项目文档中枢](./project-system/README.md)') != 2:
    raise SystemExit('docs README does not point to project-system in both required places')

print('project documentation system structure ok')
PY
```

Expected output:

```text
project documentation system structure ok
```

- [ ] **Step 2: Update completion status in the migration plan**

In `docs/project-system/plans/2026-06-01-project-documentation-system.md`, replace this section:

```md
## 完成状态

- 创建后更新本节，写明验证命令和结果。
- 若审查发现结构问题，先修正文档规则，再继续迁移旧内容。
```

With this exact content after Step 1 passes:

```md
## 完成状态

- 已建立 `docs/project-system/` 文档中枢。
- 已创建总计划、代码索引、四篇主题白皮书和迁移计划。
- 已更新 `docs/README.md`，把项目文档中枢放到推荐阅读路径第一位。
- 已运行结构检查，结果为 `project documentation system structure ok`。
- 本轮迁移没有移动旧文件，避免破坏现有引用；旧内容仍可通过归档说明追溯。
```

- [ ] **Step 3: Mark post-migration review items as checked**

In `docs/project-system/plans/2026-06-01-project-documentation-system.md`, replace this section:

```md
- [ ] 是否存在两个当前事实来源互相冲突。
- [ ] 是否有白皮书写成实施步骤。
- [ ] 是否有计划承担长期原则。
- [ ] 是否有总计划写得过细。
- [ ] 是否能从总计划追溯到白皮书、计划和代码索引。
- [ ] 是否能从代码模块反向追溯到相关白皮书和计划。
```

With this exact content:

```md
- [x] 是否存在两个当前事实来源互相冲突。结论：新中枢声明为后续默认入口，旧目录保留为历史参考，未形成两个当前事实来源。
- [x] 是否有白皮书写成实施步骤。结论：四篇白皮书只记录背景、问题、原则、决策、非目标和关联计划。
- [x] 是否有计划承担长期原则。结论：迁移计划只记录本轮迁移工作，长期规则保留在白皮书。
- [x] 是否有总计划写得过细。结论：总计划只记录主题状态、优先级和下一步，没有实施步骤。
- [x] 是否能从总计划追溯到白皮书、计划和代码索引。结论：`master-plan.md` 链接白皮书，白皮书链接迁移计划，`code-map.md` 链接相关白皮书和计划。
- [x] 是否能从代码模块反向追溯到相关白皮书和计划。结论：`code-map.md` 按模块列出关键文件、白皮书和活跃计划。
```

- [ ] **Step 4: Re-run full structure verification**

Run the same command from Step 1 again.

Expected output:

```text
project documentation system structure ok
```

- [ ] **Step 5: Commit review completion**

Run:

```bash
git add docs/project-system/plans/2026-06-01-project-documentation-system.md
git commit -m "docs: record documentation migration review"
```

Expected: commit succeeds.

---

### Task 8: Final review of repository documentation state

**Files:**
- Inspect: `docs/project-system/README.md`
- Inspect: `docs/project-system/master-plan.md`
- Inspect: `docs/project-system/code-map.md`
- Inspect: `docs/project-system/whitepapers/*.md`
- Inspect: `docs/project-system/plans/2026-06-01-project-documentation-system.md`
- Inspect: `docs/README.md`

- [ ] **Step 1: Check for forbidden placeholder text**

Run:

```bash
python - <<'PY'
from pathlib import Path
root = Path('docs/project-system')
for path in root.rglob('*.md'):
    text = path.read_text(encoding='utf-8')
    for marker in ['TBD', 'FIXME']:
        if marker in text:
            raise SystemExit(f'{path} contains {marker}')
print('no forbidden placeholders')
PY
```

Expected output:

```text
no forbidden placeholders
```

- [ ] **Step 2: Check current git diff**

Run:

```bash
git status --short
git diff -- docs/README.md docs/project-system
```

Expected: status shows only intentional documentation changes if commits were not created during execution; diff contains only the project-system docs and `docs/README.md` entrypoint update.

- [ ] **Step 3: Write final review note in the migration plan if commits were not created**

If the executor did not commit after each task, append this exact section to `docs/project-system/plans/2026-06-01-project-documentation-system.md`:

```md
## 执行复盘

本轮执行以未提交工作树形式完成。最终检查确认：

- `docs/project-system/` 成为后续文档中枢。
- 旧目录没有被删除或移动，因此不会破坏已有引用。
- 四篇白皮书记录长期原因和规则，没有写成任务清单。
- 迁移计划记录了迁移中和迁移后的合理性审查。
```

If the executor did create commits after each task, append this exact section instead:

```md
## 执行复盘

本轮执行按任务分批提交。最终检查确认：

- `docs/project-system/` 成为后续文档中枢。
- 旧目录没有被删除或移动，因此不会破坏已有引用。
- 四篇白皮书记录长期原因和规则，没有写成任务清单。
- 迁移计划记录了迁移中和迁移后的合理性审查。
```

- [ ] **Step 4: Run final structure verification one more time**

Run the full structure verification command from Task 7 Step 1.

Expected output:

```text
project documentation system structure ok
```

- [ ] **Step 5: Commit final review if needed**

If Step 3 changed the migration plan, run:

```bash
git add docs/project-system/plans/2026-06-01-project-documentation-system.md
git commit -m "docs: add documentation migration execution review"
```

Expected: commit succeeds.

---

## Plan Self-Review

Spec coverage:

- Four-level structure is implemented by Tasks 1, 2, 3, 4, and 5.
- New `docs/project-system/` directory target is implemented by Tasks 1 through 5.
- Demand sizing rules are implemented in Task 1.
- Master plan, whitepapers, plans, and code map templates are implemented in Tasks 1 through 5.
- Strong link rules are verified in Tasks 2, 3, 4, 5, and 7.
- Migration-in-progress review is recorded in Task 5.
- Post-migration review is completed in Task 7.
- Existing docs entrypoint is updated in Task 6.
- Final quality review is completed in Task 8.

Placeholder scan:

- The plan does not use unresolved placeholder markers.
- All created files have exact content.
- All verification commands include expected output.

Consistency check:

- The plan consistently uses `docs/project-system/` as the new documentation center.
- Whitepapers consistently use `## Plans`.
- New plans consistently use `Whitepaper:` and `Master plan item:`.
- Verification commands check the same file names used by creation tasks.
