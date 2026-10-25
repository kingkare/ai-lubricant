# 12 · 双轨数据源 + 用例扫描（阶段 2 实施记录）

> 前置：`test-project-definition.md` §八（数据源双轨）/ §九（阶段 2）。
> 本文记录**阶段 2 已落地**的内容与约定。

## 一、双轨（A 轨内置 / B 轨 agent）

### A 轨（内置适配器，`knowledge/source_fetch.py`）

纯代码解析，**零 LLM**。支持格式：

| adapter_type | 解析器 | 说明 |
|---|---|---|
| `json` / `jsonl` | stdlib `json` | 结构化对象 / 行 JSON |
| `csv` | stdlib `csv` | `content.rows` + `content_text` |
| `markdown` / `text` / `txt` | 原样文本 | 只进 `content_text` |
| `excel` / `xlsx` | **stdlib**（zipfile + ElementTree） | 手写 xlsx 解析，**不依赖 openpyxl**；产出 `sheets[name].rows` |
| `word` / `docx` | **stdlib** | 段落 + 表格 → `paragraphs`/`tables` + markdown 文本 |
| `xmind` | **stdlib** | 兼容 XMind Zen（content.json）与 XMind 8（content.xml），产出 `sheets[].root` 主题树 |

### B 轨（agent 通道）

平台**不写解析器**——`source.fetch` 只提供一个能力面：agent（dsh）自己取内容后，
把 bytes 交回（`content_base64`），落进**同一份 L1**。飞书/钉钉/禅道/任意 DB 都是这条路。

### L1 原始层（`mc_knowledge_raw`，新增）

```
来源 → source.fetch → mc_knowledge_raw（不可变快照 + content_hash 幂等）
                              ↓
                    A 轨：parse 成结构化 content
                    B 轨：原样 content_text / content（解析是 agent 的活）
```

- **幂等**：`(project_id, source_id, content_hash)` 唯一——重拉同一份内容不产生新行。
- **版本即新行**：内容变了 content_hash 变 → append 新行（不可变层）。
- 小文本进 DB，大文件留对象存储（`locator`/`media_type` 描述）——§12 #3 决定按行落地。

## 二、用例扫描（`knowledge/case_scan.py`）

从一份 L1 raw 抽出 `mc_test_case`（`case.scan`）：

- **列名中英自动识别** + 显式 `column_map` 覆盖（`{"自定义列": "title", "结果": "expected"}`）。
- 支持 excel / csv / json / jsonl / xmind；word/text/markdown 是 PRD 源 → 空集。
- **幂等键 = `(project_id, scan_scope, external_key)`**，落在 `confirm_ref` 里：
  - `scan_scope`：有源 → `source:{id}`；无源有文件 → `locator:{path}`；都无 → `raw:{id}`。
  - 效果：重扫同文件更新而非重复；同文件内容变了 → 更新同一行（`confirm_ref.raw_id` 随新快照刷新）。
- 用例 `confirm_source="doc"`（源即依据，过确认门），`confirm_ref` 挂 raw/source/scope。
- XMind 主题树 → 叶子=用例、路径=module/feature 层级、labels=优先级/tag。

## 三、agent 落地（`knowledge/agent_ingest.py`，`knowledge.put`）

B 轨解析产物过**同一套门**再进 L2：

- **可测闸**：rule 需 `condition+expected`、requirement 需 `acceptance`、api_endpoint 需 `method+path`… 缺判据 → 落 `candidate` + `content._gate.testability`。
- **锚点闸**：`anchors[{path,symbol,kind}]` 解析不到 `mc_knowledge_anchor` → 记 `_gate.unresolved_anchors`，**不阻断**写入，但不凭空假装已锚定。
- **AI 不裁判**：agent 产出一律 `status=candidate`、置信度钳到 `0.95`（不得自封 1.0），升 confirmed 是人工决定。
- 幂等键 `(project_id, kind, key)`；内容变更版本 +1；`links` 建 `mc_knowledge_edge`（item→item / item→anchor，解析不到的目标静默跳过）。

## 四、新增能力面方法（96）

```
source.fetch        A 轨给 path / B 轨给 content_base64 → L1
source.list_raw     列 L1 快照摘要
source.adapters     列内置适配器（前端表单选项）
case.scan           用例扫描 → mc_test_case upsert
knowledge.put       agent 产出 → L2（过闸）
project.code.graph  拉快照 → Graphify → 图谱（= 初始化/更新图谱）
```

## 五、用例模型补齐（§20.1 字段）

`mc_test_case` 补 `module` / `feature` / `test_type`（design §20.1 声明但模型缺失）。
`test_type` 枚举：smoke/functional/boundary/exception/permission/security/compatibility。

## 六、测试

`tests/test_source_dual_track.py`（15 例）：A 轨 6 种解析器（含手写 xlsx/docx/xmind 构造）、
B 轨、幂等、excel/csv/xmind 用例扫描、knowledge.put 的闸与钳制。全绿。

## 七、留给阶段 3+

- B 轨的 **analyst 工作流**（dsh 驱动 `source.fetch → 解析 → knowledge.put` 的编排）未接——
  现在 agent 是"手动"走这条链，缺一条自动编排。
- `sync_policy=scheduled` 的 cron / `webhook` 触发未接（阶段 2 只建了手动路径）。
- 锚点闸通过后**自动**把 rule/case 挂到 `mc_knowledge_edge` 的 `covered_by` 关系未做（覆盖矩阵的边）。
