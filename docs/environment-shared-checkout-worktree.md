# 环境共享检出 + 任务 worktree

## 口径（用户定，2026-10-11 收敛版）

- **任务只有两个来源，派发判断只看 `Task.source_editor_session_id`**：
  - **新建任务（空）→ 默认切 worktree**：节点在环境的代码目录上 `git worktree add`，对话在里面跑。
    任务级「创建 worktree」开关（`create_worktree`，默认开）可关——关掉后：环境有现成目录
    （导入/fresh）就直接在环境目录里跑（external）；托管 clone 环境则任务自己 clone 一份
    （历史行为，不碰共享检出）。
  - **同步会话（非空）→ 不切**：工作目录原地不动（有 `external_path` 就钉在那个目录续跑）。
    同步会话**不受开关影响**（它本来就不切）。
  - 这个判断只发生在派发一次；会话跑起来后节点按 session_id 找目录，不再看任何模式/来源。
- **clone 属于环境这一侧，异步准备、页面可见、失败可重试**：
  - 环境行 `provision_state`：`ready` / `provisioning` / `failed`（+`provision_error`）。
  - 建环境秒回：external/shared 建好即 ready；worktree/fresh 置 provisioning 并起后台
    clone task（节点 `manageEnvironment(create)` 幂等）。成功写 ready，失败写 failed。
  - 环境页显示三态徽章；failed 给「重试」（`POST /environments/{id}/provision`）。
  - 服务启动扫 provisioning 重跑（`recover_pending_provisions`）——进程重启丢在途 task
    的恢复口，同时是存量 worktree 环境的首次验证。
  - **派发闸门**：环境非 ready → 拒派发（`environment_provisioning` / `environment_provision_failed`），
    前端环境选择列表同步拦截。**节点派发路径不再兜底 clone**（`prepareWorktree` 删掉了
    `ensureEnvRepo`）：目录缺失就报 `environment checkout ... is not provisioned`，
    去环境页重试，别让任务替环境补作业。
- **导入环境（external）与新建环境的代码目录是同一个事实**（`external_path`），派发层不分叉：
  有 `external_path` → 从它切（不 clone）；没有 → 节点托管检出 `envs/<id>/repo`（provisioning 放的）。

## 为什么

改之前：环境的共享检出要等**第一个任务派发**时才由节点 `ensureEnvRepo` clone
（`nodes/execution/worktree.go`）。大仓库是分钟级，用户点「创建任务」后长时间卡在
「准备中」。而环境是长期资源、任务是一次性的——clone 该发生在环境这一侧。

改之后：环境创建时异步 clone（worktree 模式进 `<envDir>/repo`，fresh 模式进用户选定的
`external_path`）；之后每个任务只做 `git worktree add`（秒级，共享对象库）。环境没备好
就拒任务，原因摆在环境页。

## 数据流

```
create_environment (platform)
  · 取 project.repo_url / branch / git_identity_id
  · 私有仓库 → environment_git_proxy_url(env_id, repo_url)   （签名 URL，环境长生命周期凭据）
    公共仓库 → repo_url 直接用
  · external/shared → provision_state=ready（没有东西可准备）
  · worktree/fresh  → provision_state=provisioning + 后台 task：
      nodes_service.manage_environment(node, env_id, "create", git=..., target_path=...)
        target_path 空  = clone 进环境共享检出 envRepoDir  (worktree 模式)
        target_path 非空 = clone 进该绝对路径             (fresh 模式)
      成功 → ready+provisioned_at；失败 → failed+provision_error（页面重试）
  → node_client (Connect JSON) → node_server.unary_api → node_server.service
      · 构 NodeManageEnvironment{env_id, action, git, target_path}
      · ack 超时：带 git 的 create 用 ARCHIVE_INSTALL_ACK_TIMEOUT（clone 分钟级）
  → 节点 handler.go：ManageEnvironment 分支跑在 DetachStreamContext（clone 期间流抖动
    不能取消它，ack 只在 clone 完成后落地）
  → 节点 environments.go manageEnvironment：CREATE 里按 target_path 调 ensureEnvRepo

服务启动 recover_pending_provisions()：扫 provisioning 的环境重跑（幂等）。

任务侧（只在派发这一次判断）：

  create_task / 懒派发 / 重派发 → _apply_environment_workspace：
      环境 disabled → environment_disabled（拒）
      环境 provision_state != ready → environment_provisioning / environment_provision_failed（拒）
      source_editor_session_id 非空（同步会话）
          → 有 external_path：workspace_mode=external + external_workdir（原地续跑，不切）
          → 没有：什么都不设（历史行为）
      新建任务
          → 一律 workspace_mode=worktree + env_id
          → 环境有 external_path（导入/fresh）：+ external_workdir，节点从它切，
            不 clone、不给凭据
          → 没有（worktree 环境）：节点用托管检出 envs/<id>/repo；
            私有仓库换环境级签名凭据（给环境补 clone 用，任务不 clone）
```

## 各模式行为表

| workspace_mode | 建环境时 | 新建任务 | 同步为任务 | 概览页显示 |
|---|---|---|---|---|
| `external`（导入） | 不 mkdir、不 clone（目录是操作者的），ready | 从操作者那棵树切 worktree（秒级） | 直接在原目录里跑 | 工作目录 + **当前分支**；准备态徽章 |
| `fresh`（新建到指定目录） | clone 进 `external_path`（异步，ready/failed） | 从该目录切 worktree | —（无 UI 入口） | 工作目录；准备态徽章 |
| `worktree`（新建·托管） | clone 进环境共享检出 `envs/<id>/repo`（异步） | 从共享检出切 worktree | — | 工作目录；准备态徽章 |
| `shared`（历史） | 只 mkdir，ready | 任务自己 clone（历史行为，不碰环境检出） | — | 工作目录 |

节点侧 `prepareWorktree` **只切不 clone**：检出（导入树或托管 clone）必须已在，缺失即
`not provisioned` / `not a git working tree` 报错——这是「clone 归环境」口径的落点。

### 导入环境的「当前分支」

`environment_detail` 对 external 模式调 `_read_checkout_branch(node_id, external_path)`：
用 `host_exec` 跑 `git -C <path> rev-parse --abbrev-ref HEAD`（Windows 走 PowerShell），
读节点上该 checkout **实际**所在的分支——不是项目配置的 branch（操作者可能已在本地切走）。
节点离线 / 非 git 目录 / 命令失败都返回空串，前端显示「读取失败或非 git 目录」。
**不新增 proto、不重打包节点**（复用已有的 `host_exec`）。

## 相关代码

| 层 | 位置 |
|---|---|
| proto | `node_server/proto/agentcompose_v2.proto` `NodeManageEnvironment{git, target_path}` |
| 平台触发 | `user_platform/environment_service.py::create_environment`（异步 provision）+ `provision_environment`（重试）+ `recover_pending_provisions`（启动扫） |
| 平台透传 | `nodes_service.manage_environment` → `node_client/client.py` → `node_server/unary_api.py` → `node_server/service.py` |
| 节点执行 | `nodes/execution/environments.go::manageEnvironment` → `ensureEnvRepo`（建环境用）；任务派发 `worktree.go::prepareWorktree`（**只切不 clone**） |
| 任务判断 | `user_platform/task_service.py` `_apply_environment_workspace`（只看 `source_editor_session_id` + provision 闸门） |
| 准备状态 | `user_platform/models_environment.py` `provision_state/provision_error/provisioned_at` |
| 前端 | `user-frontend/src/components/console/environment/environment-panel.tsx`（徽章+重试+选择拦截） |

## 改节点后

`nodes/` 有改动 → 必须重新打包分发（`bash nodes/pack-release.sh`，见
`node-packaging-use-pack-release`）。proto 改动需重生成两侧绑定：
`bash nodes/gen-proto.sh`（Go）+ `python -m grpc_tools.protoc ...`（Python，版本锁 7.35.1）。
