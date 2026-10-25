# 16 · 四件遗留收口（对话持久化 / 覆盖矩阵边 / analyst 自动收口 / 定时源同步）

> 承接 15 号计划文末的「仍未做」清单。本文记录四件**已全部落地**。

## 一、对话持久化（`testing_platform/chat/`）

新表 `mc_test_chat_session` / `mc_test_chat_message`（已注册进 `TESTING_MODEL_MODULES`）：

| 表 | 关键字段 | 说明 |
|---|---|---|
| session | `project_id` / `dsh_session_id` / `model` / `api_key_id` / `title` / `message_count` | dsh session 由 dsh 维持，平台存它以便跨刷新续聊；`api_key_id` **不存明文 key** |
| message | `session_id` / `seq` / `role`(user/assistant/tool/system) / `content` / `tool_name` / `tool_payload` | seq 会话内单调；工具调用/结果落 `role=tool`（回放成卡片） |

- `chat/send` 内 **best-effort 落库**：失败只记日志，**绝不影响对话流**。
  - 首轮无 `chat_session_id` → 新建会话，SSE 首事件 `{type:session,data:{id}}` 回传 id。
  - assistant 先建 `status=streaming`，`done` 回写 content/status + dsh_session_id。
  - 工具调用/结果落 `role=tool` 消息。
- 会话 REST：`GET/DELETE /chat/sessions/{id}`、`PATCH` 重命名、`GET /chat/sessions?project_id=` 列表。

## 二、覆盖矩阵边（`knowledge/agent_ingest.put_items`）

锚点闸通过 → 自动建 `item --grounded_in--> anchor` 边（`get_or_create` 幂等）。
下游 `release.impact` / `knowledge.profile` / 覆盖缺口分析直接读边，不再逐条查锚点。

## 三、analyst 自动收口（`analyst.commit` 新能力面方法）

agent 不用手拼 `workflow.complete`：
```
analyst.commit({run_id, project_id, items, gaps, source_id?, agent?, raise_questions?})
  → knowledge.put(items)（过可测闸 + 锚点闸 + 置信度钳 0.95）
  → gaps 可选 question.raise（origin=design_doubt）
  → 合成契约合法产出（knowledge_model + gaps）→ workflow.complete 收口
```
返回 `workflow_status`（done/failed）+ put 摘要 + 契约 issues。

## 四、定时源同步 + webhook（`testing_platform/scheduler.py` + `surface/source_sync.py`）

设计 §六「每种源配自己的触发」的落地：

- **到点扫描** `find_due_sources`：复用 `tasks.scheduler.next_fire`（纯函数，无 croniter，
  自带 5 字段 cron 解析）；水位 = `last_synced_at`，从未同步用 `created_at` ——
  **新源不立刻触发**（"每天 9 点"建在 10 点 → 明天 9 点才跑，符合 cron 语义）。
- **单源同步** `run_source_sync`：A 轨文件 → `fetch_file`；code 源 → Graphify 图谱；
  B 轨 → `skipped`（agent 自己取）。**成败都回写水位**（失败防疯狂重试），单源失败不拖垮整批。
- **后台 worker** `SourceSyncScheduler`：惰性自启（`ensure_started` 幂等），
  **不往 main.py 塞启动钩子**（测试平台是独立能力域）。
- **触发路由**：
  - `POST /source/{id}/sync`  手动（auth 门）
  - `POST /source/sync-due`   扫描到点源（也可外部 cron 兜底）
  - `POST /source/{id}/webhook`  git push 回调；`config.webhook_secret` 配了校验
    `X-Webhook-Secret`；恒 200（webhook 语义，防上游重试风暴）

## 五、测试

`test_phase3_commit_and_chat.py` 6 例 + `test_source_scheduler.py` 9 例。
**关键坑**：源同步调度器的后台轮询任务**必须**在测试 fixture 的 `finally` 里 `stop()`
——否则跨 pytest 事件循环泄漏，整套测试挂死（初版就是这么挂的，修成真起循环 + 清理收口）。

能力面 107 → 108。相关 292 例全绿。

## 七、事后补修（前端选择器 + 建表迁移）

**选择器位置纠正**：项目选择不是每个页头各放一个，而是**侧栏模式级**（对齐 coding
的 ProjectSwitcher）——`TestingProjectSwitcher` 在下拉选择框 + 齿轮按钮；无选中项目
时侧栏菜单整体不渲染；首次无测试项目时非管理页整页走引导创建（`/testing/projects`
豁免，它就是创建入口）。详见 `14`/`15` 的后续提交。

**建表迁移缺口**：`Tortoise.generate_schemas(safe=True)` **只建缺失的表，不 ALTER 已存在表补新列**。
`mc_test_project` 加 `coding_project_id`（coding 桥）后，旧库建项目直接报
`OperationalError: column "coding_project_id" does not exist`。

根因：`_ensure_additive_columns` 原只遍历 `APP_LABEL`（`user_platform`）——
测试平台是独立 app label（`testing_platform`），**从来没进过补列范围**。

修：`wanted_labels = {APP_LABEL, TESTING_APP_LABEL}`，两个 app 的模型一并补列
（缺表 / 缺 `SQL_TYPE` 仍跳过，语义不变）。**已建库重启服务即自动补列**，无需手工 DDL。

> ⚠️ 新增测试域模型的列时记住：仅改 `models.py` 不够，生产旧库靠这个补列逻辑；
> 若改动是**改类型/加约束**（不是加列），仍需手工迁移。

## 八、仍未做

- `covered_by` 的**用例侧**单向导出到开发侧 `ProjectIssue`（测试→开发只读）未做——
  `grounded_in` 边现在是「规则→锚点」，还没到「锚点→用例」的覆盖生成（阶段 3 的
  designer 角色驱动）。
- 对话的 `mc_test_chat_session` 未接能力面方法（走 REST 而非 MCP 工具——dsh 调的是
  testing 域业务方法，不是对话本身）。