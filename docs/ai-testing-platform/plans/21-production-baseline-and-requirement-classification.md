# 21 · 生产基线版本模型 + 需求入库分类（修正 19 号文的线性血缘）

> 2026-10-13。用户补充两点，且**推翻了 19 号文 §3 的版本血缘模型**：
>
> **① 分析基线不是"上一个版本"，而是「当前版本 + 生产版本 + 已发布版本」三者**。
> 关键原话："我们是当前版本+生产版本+已发布版本，**不是历史版本**，因为可能我们有多个版本，
> 但是我只发布其中的一些版本。" → 开发线 ≠ 发布线 ≠ 生产线，**选择性发布**是常态。
>
> **② 需求入库整理时就要分类**（像用例分类一样），且需求要带"哪个模块、影响点是啥"。
> 需求/技术/用例**三个阶段都要做"当前 vs 生产"的差异分析**——
> 测试策略由"当前版本+实际生产"得出，**不仅仅是当前版本**。
>
> 本文修正 19 号文，并补需求入库分类。

---

## 1. 19 号文的错在哪（先认错）

19 号文 §3.2 给版本加了 `parent_version_id`（单亲链）+ `baseline_commit`，
§3.4 的继承用 `diff_versions(parent, head)`——**默认了"版本是一条线性开发链，
分析就跟上一个版本比"**。

**这个假设在选择性发布下是错的**。反例：

```
开发线：  v1 ──▶ v2 ──▶ v3 ──▶ v4   (v4 基于 v3 开发)
发布态：  v1✓    v2✗    v3✓    v4(当前待测)   (v2 没发布，被砍了)
生产线：  [当前生产 = v3]
```

- 若按 19 号文，v4 分析基线 = parent = v3 → `diff(v3,v4)`。这里恰好 v3 就是生产，对。
- 但若 v4 其实从**未发布的 v2** 分叉开发（`base=v2`），按 parent 比 →
  `diff(v2,v4)` **会把 v2 里那些从没进生产的东西也算成"变更"** → 回归范围虚高、测错东西。
- 正确基线永远是 **生产版本 v3**：`diff(production, current) = diff(v3, v4)`。

**根因**：`parent_version_id`（代码从哪分叉）是**开发事实**；分析要的是**发布/生产事实**。
两者在选择性发布下不是一回事。

**已核代码**：全仓无 `deploy`/`is_production`/`release_status`/`production_version` 任何承载
（`TestEnvironment` 只有 code/name/parent/is_default/status；版本表只有 `draft/graph_built`）。
生产版本概念**在数据模型里完全不存在**——所以回归/影响分析压根无法"对齐生产"。

---

## 2. 三条独立的时间线（模型地基）

**版本不是一个线性链，而是三条正交状态的叠加**：

| 线 | 含义 | 载体 | 基数 |
|---|---|---|---|
| **开发线** (development) | 代码从哪个点分叉出来 | `base_version_id` + `repos[].resolved_commit` | 每版本一个分叉点 |
| **发布线** (release) | 这个版本走没走完发布流程 | `release_status`（新） | 每版本一态 |
| **生产线** (production) | 现在生产上实际跑的是哪个版本 | **环境上的"生产版本指针"**（发布动作设定，可回滚） | **一个生产环境同一时刻只有一个版本** |

> **用户澄清（推翻本文初稿）**：生产版本**不是"查 active 部署"推断出来的**，而是
> **"点发布的那一刻，把该环境的指针指向所选版本"**——是**显式动作设定的指针**，
> 且**允许回滚**（把指针指回旧版本）。回滚本身不改动别的流程，只是"把版本撤回"。

**关键区别**：
- **已发布版本**（released）= 走完发布流程的版本集合——是**历史事实**，可能已被更新的生产版本取代。
- **生产版本**（in production）= **此刻环境指针指向的**——点发布时设定、可回滚。
- **历史版本** = 用户明确排除的——"只发布其中一些版本"，没发布的开发版本**不进分析基线**。

> **分析基线公式**：
> `测试范围 = f(当前版本, 生产版本[=环境指针当前指向], 已发布版本历史)`，
> **不是** `f(当前版本, 父版本)`。

---

## 3. 数据模型（修正 19 号文 §3）

### 3.1 `mc_test_project_version` 列修正

```sql
-- 19 号文写的 parent_version_id → 语义澄清并更名：
ALTER TABLE mc_test_project_version ADD COLUMN base_version_id   UUID;   -- 开发分叉点（代码线，非分析基线）
ALTER TABLE mc_test_project_version ADD COLUMN release_status    VARCHAR(16) DEFAULT 'unreleased';
   -- unreleased | released | superseded | abandoned
ALTER TABLE mc_test_project_version ADD COLUMN released_at       TIMESTAMPTZ;
-- version_kind / baseline_commit（19 号文已有，保留）
```

**`RELEASE_STATUSES` 枚举**：
- `unreleased`：开发中，未发布（v2 被砍 = unreleased 且不再推进）
- `released`：已发布（发布流程走完）
- `superseded`：曾发布，已被更新的生产版本取代
- `abandoned`：开发过但决定不发布（**永不进分析基线**）

> **这就是用户"只发布其中一些版本"的落点**：`abandoned` 的版本不参与任何"当前 vs 生产"差异——
> 它从没进过生产，跟用户无关。

### 3.2 生产版本指针 + 版本操作记录（用户澄清后的定稿）

**生产版本 = 发布动作显式设定的指针**，不是查询推断：

```sql
-- mc_test_environment 加列：该环境当前指向的版本（发布/回滚时由动作更新）
ALTER TABLE mc_test_environment ADD COLUMN deployed_version_id UUID;  -- 该环境当前版本（生产环境=生产版本指针）
ALTER TABLE mc_test_environment ADD COLUMN deployed_at         TIMESTAMPTZ;  -- 指针设定时刻
```

**发布动作**（`env.release_version`）：
1. 人点发布 → 选环境 + 选版本（版本须 `release_status=released`，即走完发布流程）
2. 环境的 `deployed_version_id` → 指向该版本（旧版本自动变 `superseded`）
3. 记一条**版本操作记录**

**回滚动作**（`env.rollback_version`）：
1. 人点回滚 → 选要回退到的版本（必须是该环境**曾经部署过的**）
2. 指针指回旧版本（`release_status`: 新版本 → `superseded`，旧版本 → `released`）
3. 记一条**版本操作记录**

> **回滚不影响任何其他流程**（用户原话"版本回滚并不需要影响什么，就是将版本撤回"）——
> 指针改了，操作记录加了，仅此而已。需求/用例/报告全部不动。

**版本操作记录**（新表，用户点名"需要有版本操作记录，方便记录每一次状态时间的变更"）：

```sql
CREATE TABLE IF NOT EXISTS mc_test_version_log (
    id           UUID PRIMARY KEY,
    project_id   UUID NOT NULL,
    version_id   UUID NOT NULL,      -- 被操作的版本
    env_id       UUID,               -- 涉及的环境（发布/回滚时有；纯状态变更可空）
    action       VARCHAR(32) NOT NULL,  -- 操作：status_change|release|rollback|...
    from_value   VARCHAR(64),        -- 变更前（状态/版本）
    to_value     VARCHAR(64),        -- 变更后（状态/版本）
    actor        VARCHAR(64),        -- 人 | ai | system
    reason       TEXT,               -- 操作理由
    occurred_at  TIMESTAMPTZ DEFAULT now()
);
```

**这张表是"每一次状态时间的变更"的唯一真相源**，下游三处消费（用户点名）：
- **复盘**（20 号文 §6）：`variance` 的时间轴从 version_log 取——"这个版本什么时候进的评审、
  什么时候卡住、什么时候发布"，预期 vs 实际的时间偏差就有了事实依据
- **报告**（20 号文 §5）：报告的"版本时间线"区块直接渲染 version_log
- **审计**：谁在什么时候把状态改成什么、为什么改——变更门（change_reason）的完整台账

**发布/回滚/状态转换全部走 version_log**：19 号文 `version.transition` 的每次转换、
20 号文的 release、本文的 rollback，都各记一行。

> **不设 `in_production` 版本状态**（用户拍板）——"进生产"不是版本的状态，
> 是**环境指针的指向**（`env.deployed_version_id`）。版本自己的 `release_status` 只表达
> 发布流程走到哪（released），环境跑不跑它是另一条线（指针），两线正交。

### 3.3 `diff_against_production`（新函数，替代 19 号文 `diff_versions(parent,head)` 的默认用法）

```
diff_against_production(current_version_id) ->
    prod = 生产环境的 deployed_version_id（发布/回滚动作设定的指针，一个环境一个版本）
    三层差异（见 §4）：需求 / 技术 / 用例，各自 diff(prod_version, current_version)
```

19 号文的 `diff_versions(base, head)` **保留但降级**为"两个任意版本比"的底层工具；
上层策略/回归一律走 `diff_against_production`（以生产为基线）。

---

## 4. 三阶段"当前 vs 生产"差异分析（用户点名：需求、技术、用例都要做）

用户明确"包括需求、技术、用例阶段都需要做分析"。差异不是一层，是**三层**，
每层都有独立的"当前 vs 生产"对账：

| 阶段 | diff 对象 | 数据源 | 产出 |
|---|---|---|---|
| **需求差异** | 当前版本需求集 vs 生产版本已实现需求集 | `mc_test_requirement`（挂 version）| 新增需求 / 变更需求 / 生产有但当前没动的需求（可能回归） |
| **技术差异** | 当前 commit vs 生产 commit 的代码/接口/数据 | `diff_against_production` → anchor/transport/db_table | 变更文件→影响 anchor→接口 diff→数据 diff（19 号文 §5 四类外在）|
| **用例差异** | 当前用例集 vs 生产版本验证过的用例集 | `mc_test_case` × `mc_test_case_version` | 新增用例 / 受影响用例（要回归）/ 过时用例（要 deprecated）|

**三层是串联的**：技术差异算出的影响 anchor → 反查关联需求（需求层）→ 反查关联用例（用例层）→
**这就是回归范围**。19 号文 §3.4 的 `mc_test_case_version.scope` 的判定输入，
从"parent vs head"改成"**production vs current**"。

---

## 5. 需求评审工作流 + 影响点（用户澄清后的定稿）

> **用户澄清（推翻本文初稿"入库时自动抽影响点"）**：影响点不是导入那一刻自动抽的，
> 是**评审流程的产物**。用户给的完整链路：
> **导入需求 → 人点评审 → AI 评审分析 → 出具评估报告（含影响点）→ 用户评审通过
> → 进入需求评审阶段（人机来回交互确认需求）→ 形成确定的需求
> → 根据确定的需求生成影响面、注意点、功能划分**。

### 5.1 需求评审两段式（用户原话的流程化）

```
① 评估阶段（导入后，人点"评审"触发）
   导入需求（入库，状态 draft）
      ↓ 人点击"评审"
   AI 评审分析：需求完整性/可测性/清晰度 + 影响点初判
      ↓
   AI 出具【需求评估报告】：
      - 需求质量评估（六项评审，D）
      - 分类预判（req_type/module/priority）
      - 影响点初判（初步 impact_points）
      - 疑问清单（question.raise）
      ↓
   用户评审评估报告 → 通过 → 进入 ②；不通过 → 修改需求重来

② 确认阶段（链路长，人机来回交互）
   需求评审阶段：用户 ↔ AI 来回交互（question.raise/resolve、补充信息、确认边界）
      ↓
   形成确定的需求（confirmed，acceptance_criteria 定稿）
      ↓
   根据确定的需求生成：
      - 影响面（impact_points 定稿：模块/接口/数据/锚点/广度/核心链路）
      - 注意点（pitfall 风险提示 → knowledge_item）
      - 功能划分（feature 拆分 → 挂测试点的父粒度）
```

**两段式与 19 号文状态机的对接**：`draft → req_review（①评估）→ req_confirmed（②确认完）`。
①的产出是**评估报告**（AI 判断+人过目），②的产出是**定稿需求 + 影响面/注意点/功能划分**。

### 5.2 需求分类维度（对齐用例分类）

用例分类有 module/feature/test_type/stage/coverage_axis（18/20 号文）。需求镜像这套：

| 需求分类维度 | 字段 | 何时定 | 对标用例 |
|---|---|---|---|
| **模块归属** | `module` + `feature`（已有） | ①AI 预判 → ②定稿 | 用例 module/feature |
| **需求类型** | `req_type`（19 号文） | ①AI 预判 → ②定稿（人可改） | 用例 test_type |
| **优先级** | `priority`（已有） | ①AI 预判 → ②定稿 | 用例 priority |
| **影响点** | **`impact_points`（新）** | ①初判 → ②定稿（**评审流程的产物**） | —— |

**加列**：
```sql
ALTER TABLE mc_test_requirement ADD COLUMN impact_points JSONB DEFAULT '{}';
```

### 5.3 影响点 `impact_points`（"他是哪个模块的，影响点是啥"）

**影响点是需求→代码/接口/数据的映射**——这是 17 号文 §5.2 说的"知识图谱的正经用途之一：
影响分析"，在这里落地：

```json
"impact_points": {
  "modules":     ["支付", "订单"],           // 触及业务模块
  "transports":  ["PAY-001","ORDER-003"],   // 触及接口（→ mc_test_transport）
  "data_entities":["order","payment"],      // 触及数据表（anchor kind=db_table）
  "anchors":     [...],                     // 触及代码锚点（图谱）
  "core_path":   true,                       // 是否触及核心链路（决定要不要全量）
  "breadth":     "wide"                      // 影响面广度 narrow/medium/wide
}
```

**评估报告（①）与定稿（②）对影响点的两轮产出**：
- **①评估报告里的影响点**：AI 基于**当前版本 vs 生产版本的差异**（技术 diff：生产 commit→当前
  需求涉及的模块在当前版本有多少改动）+ 图谱边（需求↔transport/anchor）初判——**给评审人看**，
  作为"这个需求好不好做、会不会伤到别人"的判断依据
- **②定稿后的影响面**：需求确认后，结合技术方案（19 号文 §5 四类外在）精化——
  **给测试策略用**（breadth/core_path 直接进 full_regression_policy）

**与生产版本的关系（用户点名"根据当前版本+实际生产环境得出"）**：
影响点不是孤立分析的——评估报告必须回答"**这个需求相对线上现状，改动落在哪**"：
`diff_against_production` 的技术层 diff 是影响点初判的输入之一。

### 5.4 影响点驱动测试策略（闭环到用户的问题）

用户问："这个需求设计是哪个模块的、影响点是啥、测试策略应该怎么指定，
都是根据当前版本+实际生产环境而得出的。"

**串起来**：
```
需求导入 → 人点评审 → AI 评估报告(分类预判+影响点初判) → 人评审通过
   ↓
需求评审阶段（人机来回确认）→ 定稿需求
   ↓ 生成：影响面(定稿) + 注意点 + 功能划分
   ↓
diff_against_production(需求/技术/用例三层差异)
   ↓  变更面 = 生产→当前的实际 delta（不是开发 parent）
strategy.propose 输入：
   ① 影响面广度(breadth) + 核心链路(core_path)   ← 需求定稿时定
   ② 三层 diff 的实际变更集                       ← 生产基线算出
   ③ 已发布历史(哪些模块最近老出问题)             ← version_log
   ④ 生产版本现状(线上是什么，决定回归起点)       ← env.deployed_version_id
   ▼
full_regression_policy 决策（20 号文 §3.4）→ 冒烟/选择性回归/全量
```

**关键**：策略的输入里，**基线是生产版本**（不是上一开发版本）——这条修正是整个链路的枢纽。
一个"改了很多但都没发布"的开发分支，对生产 diff 才反映真实要测的东西。

---

## 6. 与 19/20 号文的修正关系

| 19/20 号文原设计 | 本文修正 |
|---|---|
| 19 §3.2 `parent_version_id` 单亲链 | → `base_version_id`（仅开发分叉点）+ `release_status` + **环境上的生产版本指针**（发布动作设定，可回滚）。**分析基线 = 生产，不是 parent** |
| 19 §3.2 `diff_versions(parent,head)` 作回归起点 | → 上层改走 `diff_against_production`；`diff_versions` 降为底层工具 |
| 19 §3.4 用例继承 scope 判定用 parent | → scope 判定用 production（§4 三层差异） |
| 19 §4 需求分类未定时机、无影响点 | → 评审两段式（评估报告→人审→交互确认→定稿）+ `impact_points`（§5，**评审流程的产物**） |
| 20 §3.4 策略输入"改动面"来源不明 | → 输入=需求影响面(§5.4) + 生产基线三层 diff(§4) + 已发布历史 |
| 20 §7.1 上线只置 `released` | → 上线 = 版本 `released` + **发布动作把环境指针指向该版本**；旧版本自动 superseded |
| 20 §6 复盘 variance 无事实源 | → **`mc_test_version_log` 版本操作记录**：每次状态/发布/回滚记一行，复盘/报告/审计三处消费 |
| （本文初稿）生产版本=查 active 部署推断 | → **用户拍板推翻**：生产版本=发布动作显式设定的指针，可回滚 |

**没有推翻的**：19 的 `version_kind`/`req_type` 枚举、`mc_test_case_version` 表、状态机主体；
20 的策略表/报告/bug/复盘主体——本文只**修血缘基线**和**补需求评审流 + 生产指针 + 操作记录**。

---

## 7. 术语表（上一轮两个没看懂的词）

- **建模（modeling）**：指"在数据结构里怎么表达一个概念"——比如"进生产"这件事，是给版本表
  加一个状态字段（`in_production`）来表达，还是建一张"部署记录"表来表达。**上一轮拍板结果**：
  用**环境指针**（`env.deployed_version_id`）+ **操作记录**（`version_log`）表达，不加版本状态。
- **abandoned（放弃）**：`release_status` 的一个枚举值——**开发过、但决定不发布的版本**
  （比如做到一半被砍掉的需求分支）。标了 abandoned 的版本**永不参与任何"当前 vs 生产"差异
  分析**——它从没进过生产，跟线上无关。数据保留（日后可能重启开发），只是不进基线。

---

## 8. 待确认（需用户拍板）

> 上一轮 5 问中，第 1/2/3/5 问已随你的澄清解决（生产=发布动作设定的指针；
> 发布的版本是哪个就以哪个为标准；影响点走评审两段式；分类 AI 预判+人确认）。
> 剩余与新增：

1. **回滚的粒度**：回滚是"指针指回旧版本"（本文假设，不改任何流程），
   还是要触发"回滚后重新巡检/通知"等后续动作？我倾向**纯指针回退 + 记录，零后续动作**（按你原话）。
2. **一个环境同一时刻只有一个版本**——灰度发布（canary+stable 并存）要不要支持？
   若要，指针升级为列表。我倾向**起步一环境一版本**（你已拍板"发布的时候的版本是哪个
   就以哪个为标准"），灰度后置。
3. **需求评审的"评估报告"要不要独立实体**（`mc_test_review_report`）？还是复用
   `change_record`/报告体系？我倾向**独立轻表**（评估报告有独立的评审通过/不通过语义，
   与执行报告不同类）。
4. **注意点（pitfall）的载体**：定稿需求生成的"注意点"进 `knowledge_item(kind=业务坑)`
   还是需求表的 JSONB 字段？我倾向 **knowledge_item**（可跨版本复用、可喂 18 号文生成）。
5. **版本操作记录要不要记录"需求/用例级"的变更**（目前只记版本级状态/发布/回滚）？
   我倾向**起步只记版本级**（现有 `change_record` 已覆盖需求/用例变更），版本级是缺口。
