# 23 · 缺陷域对齐禅道 —— 补枚举、补字段、补状态机

> 2026-10-16。**触发**：用户反馈「目前缺陷的东西太少了，你看一下禅道有哪些枚举值哪些字段吧，
> 你这状态都没有一个」。核代码确认批评成立：``mc_test_bug`` 25 列里与缺陷管理相关的枚举只有
> ``BUG_STATUSES``（6 态）与 ``BUG_SEVERITIES``（5 级），**禅道的核心机制一个都没有**
> （无 resolution / bug_type / confirmed / activated_count / 双 build / deadline / escaped），
> **且没有任何状态流转动作**——bug 建出来永远停在 open。前端更把缺口放大：
> ``testingClient.ts`` 没有 ``BUG_STATUSES`` 常量，``bugs-page.tsx`` 把裸英文塞 Badge。
>
> **禅道实证**（本轮从 `easysoft/zentaopms` main 现拉 `module/bug/lang/zh-cn.php`、
> `module/bug/config.php`、`db/zentao.sql` 逐条核对，非二手）。
> 20 号文 §4 是设计源头（P3 段，此前零实现）；本文是**落地终版 + 实现差异**。

---

## 1. 禅道实证（源码逐条核过）

| 枚举/机制 | 禅道值 | 出处 |
|---|---|---|
| `statusList` | active 激活 / resolved 已解决 / closed 已关闭（**仅 3 态**） | `module/bug/lang/zh-cn.php:271-274` |
| `confirmedList` | 1=已确认 / 0=未确认（**正交标志，不占状态**） | 同文件 `:276-278` |
| `resolutionList` | fixed 已解决 / bydesign 设计如此 / duplicate 重复Bug / external 外部原因 / notrepro 无法重现 / postponed 延期处理 / willnotfix 不予解决 / tostory 转需求 | 同文件 `:280-288` |
| `typeList` | codeerror 代码错误 / config 配置相关 / install 安装部署 / security 安全相关 / performance 性能问题 / standard 标准规范 / automation 测试脚本 / designdefect 设计缺陷 / codeimprovement 代码改进 / others 其他 | 同文件 `:259-269` |
| `discardedTypes` | interface / designchange / newfeature / trackthings（**已废弃，不引入**） | `module/bug/config.php` |
| `severityList` / `priList` | 均 1-4，默认 3 | 同文件 `:213-223` |
| `osList` / `browserList` | 17 值 / 15 值 | 同文件 `:225-257` |
| `zt_bug` 列 | keywords/os/browser/**hardware**/**found**/subStatus/color/confirmed/**activatedCount**/activatedDate/feedbackBy/mailto/openedBuild/resolvedBuild/deadline/duplicateBug/relatedBug/… | `db/zentao.sql:331` |
| `longlife` | **7 天**（超长未解决阈值） | `config.php:4` |
| 必填门 | create→`title,openedBuild`；resolve→`resolution`；**条件必填**：`fixed`→`resolvedBuild`、`duplicate`→`duplicateBug` | `config.php:13-16` |
| 动作 | confirm / assignTo / resolve / close / **activate**（重开，`activatedCount+1`）/ toStory / toTask / createCase | `config.php:19-24` |

> **如实标注**：禅道 `found`（来源）与 `hardware` 现行语言包**无枚举**——列存在但自由文本、
> 语言包里相关项已注释。本文不编造其取值；`found_in` 用我们自己的三值口径
> （`test`/`production`/`feedback`，20 号文 §7 已有此分法）。
> 另：`injection` / `identify`（缺陷注入/识别）**仅 max/ipd 付费版**有，跳过。

## 2. 用户拍板的四条决策

1. **状态机保留我们更细的态**，不照搬禅道 3 态（20 号文 §11 问题 1 已答）。
2. **severity 留 5 级**（blocker/critical/major/minor/trivial，比禅道 1-4 更细）；
   **priority 从 1-3 扩到 1-4** 对齐禅道 pri（默认仍 2，不跟禅道的 3）。
3. **四组附加字段全落**：解决方案+缺陷类型 / 构建+周期+重复 / 运行环境+协作 / 确认位+激活计数。
4. **后端 + 前端全量落地**（不止数据模型）。

## 3. `confirmed` 态 vs `confirmed` 位的歧义（本文定规则）

禅道只有正交布尔位，我们多一个 `confirmed` **状态**。两者语义不同、都保留，规则写死：

- `confirmed` **布尔位** = 「这 bug 属实、已被人认可」——纯事实标记，只由 `confirm` 动作置真，
  **一旦为真永不回退**（重开也不清）。
- `confirmed` **状态** = 生命周期节点，表示已确认并进入处理队列。
- 不变式：`status == "confirmed"` ⟹ `confirmed == True`；状态更靠后时布尔位仍为真。
- **重开回到 `confirmed` 而非 `open`**（若布尔位为真）——不逼人已确认过的 bug 重确认一遍。
- 禅道 `unconfirmed` / `needconfirm` 快捷筛选用 `confirmed == False` 表达，**不新增状态**。

## 4. 状态机与动作守卫表（实现终版）

**7 态**：`open → confirmed → triaged → resolved → verified → closed`，
`rejected` 为 `resolution ∈ {willnotfix, bydesign}` 的终态。
**不设 `reopened` 态**——reopen 是**动作**，`activated_count + 1`（对齐禅道 `activatedCount`）。

⚠️ 20 号文 §4.3 / §8.3 / §11 写的「8 态」是笔误，清单实数 7 态，本文一并订正。

守卫表集中在 `testing_platform/defects/models.py` 的 `BUG_ACTIONS`
（对标禅道 `isClickable`；值用 frozen dataclass，因要携带条件必填与副作用，
比 `user_platform/project_service.py:166` 的 `ISSUE_TRANSITIONS` 多一维）：

| 动作 | from-status | 目标态 | 必填 / 条件必填 | 副作用 |
|---|---|---|---|---|
| `confirm` | open | confirmed | — | `confirmed=True` + `confirmed_at` |
| `assign` | open/confirmed/triaged/resolved | 保持 | `assignee_id` | `assigned_at` |
| `triage` | open/confirmed | triaged | — | — |
| `resolve` | open/confirmed/triaged | `resolution ∈ {willnotfix,bydesign}` → **rejected**，其余 → resolved | `resolution`；**`fixed`→`resolved_build`、`duplicate`→`duplicate_bug_id`** | `resolved_at` + `resolved_by` |
| `verify` | resolved | verified | — | `verified_at` + `verified_by` |
| `close` | resolved/verified/rejected | closed | — | `closed_at` + `closed_by`；清 assignee |
| `reopen` | confirmed/triaged/resolved/verified/closed/rejected | confirmed 位真 → **confirmed**，否则 open | — | `activated_count+1`；清 resolution/resolved_build/resolved_at/verified_at/closed_at |

**closed 终态不可编辑**（禅道 `bug_model.php:412`；`TERMINAL_NOT_EDITABLE`）。
仓库内无 `testing_platform` 侧的动作守卫先例（strategy/projects 都只做枚举成员校验），
守卫范式取自 `user_platform/project_service.py` 的 `ISSUE_TRANSITIONS` +
`_validate_transition`。

## 5. 数据模型改动（`testing_platform/defects/models.py`）

**新增枚举常量**（均配中文标签 dict，供 `bug.enums` 与报错文案复用）：
`BUG_STATUSES`（改）`BUG_STATUS_ALIASES``BUG_STATUS_LABELS``BUG_RESOLUTIONS`
`BUG_RESOLUTION_LABELS``BUG_REJECT_RESOLUTIONS``BUG_TYPES``BUG_TYPE_LABELS`
`BUG_PRIORITIES``BUG_SEVERITY_LABELS``BUG_FOUND_INS``BUG_FOUND_IN_LABELS`
`BUG_OS_LIST``BUG_BROWSER_LIST``BUG_LONG_LIFE_DAYS``TERMINAL_NOT_EDITABLE`。

**`TestBug` 加列**（~27 根）：`resolution` `bug_type` `confirmed` `confirmed_at`
`activated_count` `activated_at` `opened_build` `resolved_build` `duplicate_bug_id`
`related_bug_ids`(JSONB) `deadline` `resolved_at` `resolved_by` `verified_at` `verified_by`
`closed_by` `found_in` `escaped` `os` `browser` `hardware` `keywords` `mailto`(JSONB)
`feedback_by` `sub_status` `color` `assigned_at` `last_edited_by`；
索引补 `(project_id,status)`/`assignee_id`/`resolution`/`bug_type`/`duplicate_bug_id`/
`found_in`/`deadline`。

**新表 `mc_test_bug_log`**（活动流，命名对齐 21 号文 §3.2 的 `mc_test_version_log`）：
`bug_id / action / from_status / to_status / actor / comment / extra(JSONB) / occurred_at`。
消费三处：详情页时间线、MTTR/重开时间线复算、复盘事实源。

**守卫异常**（自带 `.code` + `.details`，`_translate_error` 里加分支并**排在通用
`DefectValidationError` 分支之前**，否则被塌缩成 `invalid_request`）：
`BugTransitionNotAllowedError`（`bug_transition_not_allowed`，带 `allowed_actions`）、
`BugFieldRequiredError`（`bug_field_required`，带 `field`）。

## 6. 迁移（`user_platform/database.py`）

- **加列零工作量**：`_ensure_additive_columns()`（`:335`）已遍历 `testing_platform` app
  全部模型自动 `ADD COLUMN ... NULL`。⚠️ 它一律加为 NULL、不填默认值，所以另写一次性迁移回填。
- **`_migrate_bug_status_rename()`**（新，挂在 `:130` 附近，须在 additive pass 之后）：
  ① `status='fixed'` → `'resolved'`（幂等：无匹配行即 no-op）；② 回填
  `activated_count=0` / `confirmed=false` / `found_in='test'`。
- **读侧别名**：`BUG_STATUS_ALIASES = {"fixed": "resolved"}` + service `_normalize_status()`，
  入参接受旧值并映射；写侧不再接受 `fixed`。
- 镜像 DDL 同步（`DDL` 列表 + `create_tables_via_pg()` 无人调用，但它是
  PostgresClient-only 部署的唯一真相）。

**风险已核**：`"fixed"` 字符串在全仓只出现在 `defects/models.py` 的枚举里；
coding 的 `ProjectIssue` 流（`issues-tab.tsx` / `issue-meta.ts` / `project_service.py`）
是**另一个域**的 bug 状态机，互不影响。`reports/service.py:_collect_bugs` 返回 `[]`
（不读 `mc_test_bug`），`projects` 域只按 `project_id` 计数不看 status——旁路干净。

## 7. 服务层（`testing_platform/defects/service.py`）

| 函数 | 说明 |
|---|---|
| `apply_bug_action(bug_id, action, *, actor_id, comment, **fields)` | **唯一流转入口**，守卫表驱动：校验 from-status → required/conditional → 应用 sets_flags/stamps/clears/bumps → 落 `mc_test_bug_log`。单入口 + 表驱动，新增动作只改表 |
| `update_bug(bug_id, values, *, change_reason, actor_id)` | 只改**非状态**白名单字段；**变更门**（reason 必填）；**`status` 直传拒**（防绕过守卫表）；**closed 拒编辑** |
| `delete_bug(bug_id)` | 真删主表 + 根因 + 活动流 |
| `list_bug_log(bug_id)` | 活动流时间线（升序） |
| `list_bugs(..., status/severity/priority/bug_type/resolution/assignee/found_in/escaped/unconfirmed/overdue/longlife/module/keyword)` | 筛选维度照禅道 `report->charts` + `browseTypeList`；`overdue` = deadline 已过且未关闭；`longlife` = 存活 ≥ 7 天未解决 |
| `_bug_document` | 输出全字段 + **`available_actions`**（当前 status 的合法动作名）+ `is_overdue`/`is_longlife` 派生位 |

`resolution` 只经 `resolve` 动作写（不进 `update_bug` 白名单）——`_BUG_ACTION_FIELDS =
_BUG_EDITABLE_FIELDS + ("resolution",)`。
`create_bug_from_failure` 收全部新列；`priority` 校验 `(1,2,3)`→`(1,2,3,4)`；
`confirmed=True` 时状态直落 `confirmed`。**`opened_build` 保持可选**（禅道必填，
但我们手动录入的 bug 无构建，不照抄这条）。

## 8. 周期报表（`testing_platform/reports/cycle.py`）

按 20 号文 §8.4 落 `reports/cycle.py`：`compute_cycle_report(bugs, *, now, longlife_days)`
**纯函数无 DB**（便于单测）返回：
MTTR `avg(resolved_at-created_at)`（**禅道没落地，我们补**）、平均验证时长、总生命周期、
重开率 `count(activated_count>0)/total`、超期数、逃逸率 `escaped/total`，
及按 severity/bug_type/resolution/status/module/assignee/found_in 的分布
（对标禅道 `report->charts` 16 项）。DB 取数留在 service 层 `bug.cycle_report` 薄包装。

## 9. 能力面（`testing_platform/surface/methods.py`，新增 7 方法）

| 方法 | kind | 说明 |
|---|---|---|
| `bug.update` | write | 改非状态字段；`change_reason` 必填；closed 拒 |
| `bug.transition` | write | 动作流转（action ∈ 7 动作）；返回完整文档 |
| `bug.history` | read | 活动流时间线 |
| `bug.enums` | read | 全枚举 + 中文标签 + 动作守卫表（前端/Agent 单一真相） |
| `bug.cycle_report` | read | 周期报表，按 project/version/strategy 收窄 |
| `bug.delete` | write | 真删主表 + 根因 + 活动流 |
| `bug.available_actions` | read | 某状态可执行动作（纯函数查询，传 bug_id 读实际状态） |

`bug.create` / `bug.list` 的 `params_schema` 补全部新字段与新筛选键，
**每个枚举属性都写真实 `"enum"`**——`validate_params` 会按 enum 硬校验并拒绝未声明的多余字段。
两处硬编码账本同步：`tests/test_surface_methods.py` 的 `IMPLEMENTED`（相等断言）、
`tests/test_surface_writes.py` 的 `EXPECTED_TOTAL`（184 → **191**）。
`surface/mcp_server.py` 的「107 个工具方法」instruction 改为**动态取数**
（数字早已过期，发给 agent 的自我介绍不能撒谎）。

## 10. 前端

- `user-frontend/src/api/testingClient.ts`：新增 `BUG_ACTIONS/BUG_STATUSES/BUG_RESOLUTIONS/
  BUG_TYPES/BUG_SEVERITIES/BUG_PRIORITIES/BUG_FOUND_INS` 常量；`TestingBug` 扩全字段 +
  `available_actions` + 派生位；`BugLogEntry`/`BugEnums`/`BugCycleReport` 类型；
  包装 `updateBug`/`transitionBug`/`deleteBug`/`getBugHistory`/`getBugEnums`/`getBugCycleReport`；
  `getBugs` filters 扩到全筛选集。
- 新建 `testing/bug-filters.ts`：枚举中文标签（三段式 `*_CN` + `*_I18N` + `xxxLabel(t,s)`
  i18n idiom，照 `apis-page.tsx:155-173`）、Badge variant 表、严重度色点（禅道 colorList 语义）、
  快捷页签判定、客户端过滤/排序、`formatDuration`。
- `testing/bugs-page.tsx` 升级：快捷页签（全部/未解决/未确认/超期/长期未决/逃逸）+
  状态/严重度/类型/解决方案筛选 + 关键字；状态中文彩标（超期 / 逃逸加角标）；
  列补类型/解决方案/指派/重开次数；行内动作菜单**读后端 `available_actions`**
  （非法动作不出现；closed 行只剩重开）；点行开详情。
- 新建 `testing/bug-dialogs.tsx`：`BugDetailDialog`（就地编辑 + 变更门理由 + 生命周期时间线 +
  根因列表）、`ResolveDialog`（条件显式必填 resolved_build/duplicate_bug_id，前端预判 + 后端权威，
  `bug_field_required` 落回字段）、`BugActionDialog`（指派）、`BugCreateDialog`（新字段）、
  `CycleReportDialog`（MTTR/重开率/逃逸率 + 分布）。
- i18n：`cn.ts` / `en.ts` 的 `testing.bug.*` 段**两边成对**补 ~90 个键
  （状态/解决方案/类型/严重度/发现阶段/动作/页签/详情/报表）。

## 11. 实现差异（与 20 号文 §4 设计原文）

| 20 号文原设计 | 实现 | 原因 |
|---|---|---|
| 加列 `os` | 同（列名照禅道原词） | `os` 非 PG 保留字，Tortoise 一律加引号 |
| 加列 `steps`（重现步骤） | **不加** | 我们已有 `symptom` + `repro.steps`，语义已覆盖 |
| `found` | 改名 `found_in`，三值 `test/production/feedback` | 禅道 `found` 无枚举；用我们自己的口径 |
| `opened_build` 必填 | **可选** | 手动录入的 bug 无构建，不照抄禅道必填门 |
| （无） | 新增 `mc_test_bug_log` 活动流表 | MTTR/时间线/复盘需要事实源 |
| `bug.confirm/assign/resolve/...` 7 个方法 | 收成 `bug.transition` 单入口 + `bug.available_actions` | 表驱动少一半注册面；新增动作只改表 |

**不在本次范围**：`injection`/`identify`（仅禅道付费版）；`build` 实体
（20 号文 §11 问题 2 倾向「version 即 build」，沿用 `version_id`）；
`toStory`/`toTask`（我们已有 `requirement_id` 直连，`tostory` 作为 resolution 值存在即可）。

## 12. 缺陷目录（接公共目录组件，用户追加定版 2026-10-17）

缺陷原本只有 `module` **自由文本分组**（不能嵌套、不能建子目录）。本轮接上
**统一目录表** `mc_test_catalog`——与需求/用例/场景/接口**同一套** `catalog.*` 方法、
同一个前端组件，靠 `type` 区分各自的树。

**后端**（改动小，全走既有泛化路径）：
- `CATALOG_TYPES`（`assets/models.py`）追加 `"bug"` → `catalog.create/list` 的
  `type` 枚举校验自动放行；`catalog.list` 的 `params_schema` 引用同一常量，无需另改。
- `delete_catalog`（`assets/service.py`）加 `bug` 分支：删目录时把挂着的
  `mc_test_bug.folder_id` 置 NULL 归根（缺陷本体不删）。跨包引用 `TestBug`
  用**函数内 import**（assets 不被 defects 反向依赖，零环）。
- `TestBug` 加 `folder_id`（裸 UUID 无 FK，与 case/scenario/requirement 同惯例）
  + 索引 + DDL 镜像；`create_bug_from_failure` / `update_bug` / `list_bugs` /
  `_bug_document` 全链路收 `folder_id`。`list_bugs` 的 `folder_id` 是**精确匹配**，
  子树过滤由前端组树做（与场景页同一口径）。

**前端**（复用组件，不新造）：
- `CatalogTreePanel`（`catalog-tree.tsx`，公共目录树 + CRUD）传 `type="bug"`，
  缺陷页改成**左目录树 + 右表格**两栏（照 `scenarios-page.tsx:1862` 用法）；
  目录 CRUD 由组件自管，项数据经 `fetchItems` 注入、按 `folder_id` 分发。
- `CatalogTreeSelect`（`catalog-tree-select.tsx`，只读树形下拉）进「新建缺陷」弹框，
  目录树「+」建缺陷时带入选中目录。
- `CATALOG_ITEM_ICONS` 注册 `bug` 图标（rose 色）。
- `bug-filters.ts` 加 `folderKey` + `bugSubtreeFolderIds()`，过滤链插到「版本之后、
  页签之前」：选目录含其子孙，`UNGROUPED_KEY` 只看未分组。

**没动的**：`module` 文本字段保留（目录内的二级分组仍可用），不强行迁移成目录。

## 13. 测试与出处

- `tests/test_defects_lifecycle.py`（新，9 例）：7 动作 × 7 状态守卫矩阵、resolve 条件必填、
  willnotfix/bydesign → rejected、reopen 语义（计数 +1 / 清字段 / confirmed 位保留 / 目标态）、
  update 三闸（变更门 / status 拒 / closed 拒）、create 新字段、delete、
  `available_actions` 纯函数、**缺陷目录**（type=bug 建树 → 挂 bug → folder 过滤 → 删目录归根）。
- `tests/test_defects_flow.py`：表注册断言补 `mc_test_bug_log`。
- `tests/test_surface_methods.py` / `tests/test_surface_writes.py`：账本同步。

| 机制 | 出处 |
|---|---|
| 3 态机 + confirmed 正交位 + activatedCount + isClickable | 禅道 Z（`buglang.php`/`bug_config.php`/`db/zentao.sql`） |
| resolution 8 值 + 条件必填 | 禅道 Z（`buglang.php:280`/`config.php:16`） |
| bug_type 10 值、双 build、discardedTypes 不引入 | 禅道 Z（`buglang.php:259`/`config.php`） |
| 逃逸 bug + longlife 7 天 | 禅道 Z（`release` 模块 / `config.php:4`） |
| 7 态细化 + verified 独立 + MTTR | 20 号文 §4.2b/§4.5（我们的改进——禅道未落地） |
| 动作守卫表 / 变更门 / 错误码信封 | `user_platform/project_service.py:166` / `assets/service.py` / `defects/models.py:49` |
