function listUnder(key) {
  return (raw) => {
    const rows = Array.isArray(raw) ? raw : raw?.[key] ?? raw?.rows ?? raw?.data ?? [];
    return { [key]: rows, total: raw?.total, page: raw?.page, page_size: raw?.page_size };
  };
}
function taskConversations(raw) {
  const rows = Array.isArray(raw) ? raw : raw?.rows ?? raw?.data ?? [];
  const conversations = rows.map((row) => ({
    ...row,
    task_id: row.task_id ?? row.id,
    task_title: row.task_title ?? row.title,
    content: row.content ?? row.title ?? ""
  }));
  return { conversations, total: raw?.total, page: raw?.page, page_size: raw?.page_size };
}
export const ENDPOINT_MAP = {
  // ---- Auth / user (Phase A) ---------------------------------------------
  v1UsersPasswordLoginCreate: { method: "POST", path: "/api/v1/users/password-login" },
  v1UsersStatusList: { method: "GET", path: "/api/v1/users/status" },
  v1UsersLogoutCreate: { method: "POST", path: "/api/v1/users/logout" },
  v1UsersMembersList: { method: "GET", path: "/api/v1/users/members" },
  // ---- Runtime keys (Phase B) --------------------------------------------
  v1UsersModelGatewayRuntimeKeysList: {
    method: "GET",
    path: "/api/v1/users/model-gateway/runtime-keys"
  },
  v1UsersModelGatewayRuntimeKeysCreate: {
    method: "POST",
    path: "/api/v1/users/model-gateway/runtime-keys"
  },
  v1UsersModelGatewayRuntimeKeysDelete: {
    method: "DELETE",
    path: "/api/v1/users/model-gateway/runtime-keys/{0}"
  },
  // ---- Tasks (Phase C) ----------------------------------------------------
  v1UsersTasksList: { method: "GET", path: "/api/v1/users/tasks", transform: listUnder("tasks") },
  v1UsersTasksDetail: { method: "GET", path: "/api/v1/users/tasks/{0}" },
  v1UsersTasksCreate: { method: "POST", path: "/api/v1/users/tasks" },
  v1UsersTasksUpdate: { method: "PUT", path: "/api/v1/users/tasks/{0}" },
  v1UsersTasksDelete: { method: "DELETE", path: "/api/v1/users/tasks/{0}" },
  v1UsersTasksStopUpdate: { method: "PUT", path: "/api/v1/users/tasks/stop" },
  // ---- Projects (Phase C) -------------------------------------------------
  v1UsersProjectsList: {
    method: "GET",
    path: "/api/v1/users/projects",
    transform: listUnder("projects")
  },
  v1UsersProjectsDetail: { method: "GET", path: "/api/v1/users/projects/{0}" },
  v1UsersProjectsCreate: { method: "POST", path: "/api/v1/users/projects" },
  v1UsersProjectsUpdate: { method: "PUT", path: "/api/v1/users/projects/{0}" },
  v1UsersProjectsDelete: { method: "DELETE", path: "/api/v1/users/projects/{0}" },
  v1UsersProjectsCollaboratorsList: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/collaborators"
  },
  v1UsersProjectsCollaboratorsCreate: {
    method: "POST",
    path: "/api/v1/users/projects/{0}/collaborators"
  },
  v1UsersProjectsCollaboratorsDelete: {
    method: "DELETE",
    path: "/api/v1/users/projects/{0}/collaborators/{1}"
  },
  // 项目关联：定向引用关系，权限按源项目 token 对目标仓库的实际可达性判定。
  v1UsersProjectsAssociationsList: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/associations"
  },
  v1UsersProjectsAssociationsCreate: {
    method: "POST",
    path: "/api/v1/users/projects/{0}/associations"
  },
  v1UsersProjectsAssociationsDelete: {
    method: "DELETE",
    path: "/api/v1/users/projects/{0}/associations/{1}"
  },
  // 生成的 Api.ts 方法名是 ``...IssuesDetail``（GET 问题列表）；保留 ``...List`` 别名。
  v1UsersProjectsIssuesDetail: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/issues",
    transform: listUnder("issues")
  },
  v1UsersProjectsIssuesList: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/issues",
    transform: listUnder("issues")
  },
  v1UsersProjectsIssuesCreate: { method: "POST", path: "/api/v1/users/projects/{0}/issues" },
  v1UsersProjectsIssuesUpdate: {
    method: "PUT",
    path: "/api/v1/users/projects/{0}/issues/{1}"
  },
  v1UsersProjectsIssuesDelete: {
    method: "DELETE",
    path: "/api/v1/users/projects/{0}/issues/{1}"
  },
  // 需求/bug 分配：创建首个任务并推进状态（服务端两阶段，失败保持可分配）。
  v1UsersProjectsIssuesAssign: {
    method: "POST",
    path: "/api/v1/users/projects/{0}/issues/{1}/assign"
  },
  // 人工确认/退回设计文档或 bug 根因。agent 无权走这个口。
  v1UsersProjectsIssuesConfirm: {
    method: "POST",
    path: "/api/v1/users/projects/{0}/issues/{1}/confirm"
  },
  v1UsersProjectsIssuesCommentsList: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/issues/{1}/comments"
  },
  v1UsersProjectsIssuesCommentsCreate: {
    method: "POST",
    path: "/api/v1/users/projects/{0}/issues/{1}/comments"
  },
  // 仓库读:目录树 + 文件内容。query(recursive/ref/path)由 toQuery 自动拼接。
  v1UsersProjectsTreeDetail: { method: "GET", path: "/api/v1/users/projects/{0}/tree" },
  v1UsersProjectsTreeBlobDetail: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/tree/blob"
  },
  // Git 子模块：后端读仓库 .gitmodules 派生，不落库；相对地址已按父仓解析为绝对地址。
  v1UsersProjectsSubmodulesList: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/submodules"
  },
  // 技术栈识别：stack 是建项目后异步扫的缓存（null = 未扫/扫描中/失败），rescan 同步重扫。
  v1UsersProjectsStackDetail: {
    method: "GET",
    path: "/api/v1/users/projects/{0}/stack"
  },
  v1UsersProjectsStackRescanCreate: {
    method: "POST",
    path: "/api/v1/users/projects/{0}/stack/rescan"
  },
  // ---- Git identities / bots (Phase C) -----------------------------------
  v1UsersGitIdentitiesList: { method: "GET", path: "/api/v1/users/git-identities" },
  v1UsersGitIdentitiesDetail: { method: "GET", path: "/api/v1/users/git-identities/{0}" },
  // {1} 是仓库 full_name（owner/repo）；前端已 encodeURIComponent 一次，fillPath 再编码一次，
  // 后端路由用 ``:path`` + unquote 还原。分支列表用于项目详情页解析默认分支。
  v1UsersGitIdentitiesBranchesDetail: {
    method: "GET",
    path: "/api/v1/users/git-identities/{0}/{1}/branches"
  },
  v1UsersGitIdentitiesCreate: { method: "POST", path: "/api/v1/users/git-identities" },
  v1UsersGitIdentitiesUpdate: { method: "PUT", path: "/api/v1/users/git-identities/{0}" },
  v1UsersGitIdentitiesDelete: { method: "DELETE", path: "/api/v1/users/git-identities/{0}" },
  v1UsersGitBotsList: { method: "GET", path: "/api/v1/users/git-bots" },
  v1UsersGitBotsCreate: { method: "POST", path: "/api/v1/users/git-bots" },
  v1UsersGitBotsUpdate: { method: "PUT", path: "/api/v1/users/git-bots/{0}" },
  v1UsersGitBotsDelete: { method: "DELETE", path: "/api/v1/users/git-bots/{0}" },
  // ---- Nodes (agent-compose, read-only "use" view) -----------------------
  // The task runtime is now a chosen agent-compose node (hosts/images/VMs were
  // removed). A user sees the nodes their groups were granted, aggregated by the
  // backend at /api/v1/teams/my-nodes.
  v1UsersNodesList: { method: "GET", path: "/api/v1/teams/my-nodes", transform: listUnder("nodes") },
  v1UsersModelsList: {
    method: "GET",
    path: "/api/v1/users/models",
    transform: listUnder("models")
  },
  v1UsersModelsAvailableList: {
    method: "GET",
    path: "/api/v1/users/models/available",
    transform: listUnder("models")
  },
  v1UsersModelsCreate: { method: "POST", path: "/api/v1/users/models" },
  v1UsersModelsUpdate: { method: "PUT", path: "/api/v1/users/models/{0}" },
  v1UsersModelsDelete: { method: "DELETE", path: "/api/v1/users/models/{0}" },
  v1UsersModelsHealthCheckCreate: {
    method: "POST",
    path: "/api/v1/users/models/health-check"
  },
  v1UsersModelsHealthCheckDetail: {
    method: "GET",
    path: "/api/v1/users/models/{0}/health-check"
  },
  // ---- Skills / plugins (Phase D) ----------------------------------------
  v1SkillsList: { method: "GET", path: "/api/v1/skills" },
  v1PluginsList: { method: "GET", path: "/api/v1/plugins" },
  // ---- Notify (Phase D) ---------------------------------------------------
  v1UsersNotifyChannelsList: { method: "GET", path: "/api/v1/users/notify/channels" },
  v1UsersNotifyChannelsCreate: { method: "POST", path: "/api/v1/users/notify/channels" },
  v1UsersNotifyChannelsUpdate: { method: "PUT", path: "/api/v1/users/notify/channels/{0}" },
  v1UsersNotifyChannelsDelete: { method: "DELETE", path: "/api/v1/users/notify/channels/{0}" },
  v1UsersNotifyEventTypesList: { method: "GET", path: "/api/v1/users/notify/event-types" },
  // ---- Team administration (MonkeyCode manager) ---------------------------
  v1TeamsDashboardList: { method: "GET", path: "/api/v1/teams/dashboard" },
  // Users / passwords / membership
  v1TeamsUsersList: { method: "GET", path: "/api/v1/teams/users" },
  v1TeamsUsersStatusList: { method: "GET", path: "/api/v1/teams/users/status" },
  v1TeamsUsersLogoutCreate: { method: "POST", path: "/api/v1/teams/users/logout" },
  // 用户头像菜单使用此生成方法名；当前用户密码由团队用户路由处理。
  v1UsersPasswordsChangeUpdate: {
    method: "PUT",
    path: "/api/v1/teams/users/passwords/change"
  },
  v1TeamsUsersWithPasswordCreate: { method: "POST", path: "/api/v1/teams/users/with-password" },
  // Keep the misspelled swagger operationId used by older generated clients.
  v1TeamsUsersWithPaswordCreate: { method: "POST", path: "/api/v1/teams/users/with-password" },
  v1TeamsUsersDelete: { method: "DELETE", path: "/api/v1/teams/users/{0}" },
  v1TeamsUsersPasswordsChangeUpdate: {
    method: "PUT",
    path: "/api/v1/teams/users/passwords/change"
  },
  v1TeamsUsersPasswordsResetUpdate: {
    method: "PUT",
    path: "/api/v1/teams/users/{0}/passwords/reset"
  },
  // 统一「用户与权限」页：一张全量用户表 + 每行管理员开关。
  v1TeamsUsersAllList: { method: "GET", path: "/api/v1/teams/users/all" },
  v1TeamsUsersAdminUpdate: { method: "PUT", path: "/api/v1/teams/users/{0}/admin" },
  v1TeamsUsersCreate: { method: "POST", path: "/api/v1/teams/users" },
  v1TeamsUsersUpdate: { method: "PUT", path: "/api/v1/teams/users/{0}" },
  // Team groups
  v1TeamsGroupsList: { method: "GET", path: "/api/v1/teams/groups" },
  v1TeamsGroupsCreate: { method: "POST", path: "/api/v1/teams/groups" },
  v1TeamsGroupsUpdate: { method: "PUT", path: "/api/v1/teams/groups/{0}" },
  v1TeamsGroupsDelete: { method: "DELETE", path: "/api/v1/teams/groups/{0}" },
  v1TeamsGroupsUsersUpdate: { method: "PUT", path: "/api/v1/teams/groups/{0}/users" },
  // 分组侧配置该组能用哪些系统 Key（权限统一在分组侧，不再从 key 侧配「适用分组」）。
  v1TeamsGroupsApiKeysList: { method: "GET", path: "/api/v1/teams/groups/{0}/api-keys" },
  v1TeamsGroupsApiKeysUpdate: { method: "PUT", path: "/api/v1/teams/groups/{0}/api-keys" },
  v1TeamsGroupsMcpServicesList: { method: "GET", path: "/api/v1/teams/groups/{0}/mcp-services" },
  v1TeamsGroupsMcpServicesUpdate: { method: "PUT", path: "/api/v1/teams/groups/{0}/mcp-services" },
  v1TeamsGroupsNodePickerList: { method: "GET", path: "/api/v1/teams/groups/{0}/node-picker" },
  v1TeamsGroupsNodesBind: { method: "POST", path: "/api/v1/teams/groups/{0}/nodes" },
  v1TeamsGroupsNodesUnbind: { method: "DELETE", path: "/api/v1/teams/groups/{0}/nodes/{1}" },
  v1TeamsSkillsList: { method: "GET", path: "/api/v1/teams/skills" },
  v1TeamsGroupsSkillsList: { method: "GET", path: "/api/v1/teams/groups/{0}/skills" },
  v1TeamsGroupsSkillsUpdate: { method: "PUT", path: "/api/v1/teams/groups/{0}/skills" },
  // Team models / health-check (backed by mc_team_models; mobile + console settings depend on it)
  v1TeamsModelsList: { method: "GET", path: "/api/v1/teams/models" },
  v1TeamsModelsCreate: { method: "POST", path: "/api/v1/teams/models" },
  v1TeamsModelsUpdate: { method: "PUT", path: "/api/v1/teams/models/{0}" },
  v1TeamsModelsDelete: { method: "DELETE", path: "/api/v1/teams/models/{0}" },
  v1TeamsModelsHealthCheckCreate: {
    method: "POST",
    path: "/api/v1/teams/models/health-check"
  },
  v1TeamsModelsHealthCheckDetail: {
    method: "GET",
    path: "/api/v1/teams/models/{0}/health-check"
  },
  // Login methods (OIDC / OAuth providers) / audit
  v1TeamsLoginMethodsList: { method: "GET", path: "/api/v1/teams/login-methods" },
  v1TeamsLoginMethodsCreate: { method: "POST", path: "/api/v1/teams/login-methods" },
  v1TeamsLoginMethodsUpdate: { method: "PUT", path: "/api/v1/teams/login-methods/{0}" },
  v1TeamsLoginMethodsDelete: { method: "DELETE", path: "/api/v1/teams/login-methods/{0}" },
  v1TeamsLoginMethodsTestCreate: { method: "POST", path: "/api/v1/teams/login-methods/test" },
  v1TeamsAuditsList: { method: "GET", path: "/api/v1/teams/audits" },
  // ---- Team-scoped resource lists (MonkeyCode manager 团队视图) ------------
  // These console pages want a team-wide view. The backend exposes the same
  // resources as user-scoped routes; the admin role is privileged there and
  // returns every row, so we point the team-list methods at the user routes
  // and reshape ``{rows}`` into the key each page reads. Cursor pagination
  // degrades to page-based (no ``page.cursor`` ⇒ single page) until the
  // backend grows real cursor endpoints. Task is the "conversation" unit, so
  // conversations reuses the tasks route.
  v1TeamsProjectsList: {
    method: "GET",
    path: "/api/v1/users/projects",
    transform: listUnder("projects")
  },
  v1TeamsTasksList: {
    method: "GET",
    path: "/api/v1/users/tasks",
    transform: listUnder("tasks")
  },
  v1TeamsConversationsList: {
    method: "GET",
    path: "/api/v1/users/tasks",
    transform: taskConversations
  },
  // Agent / 聊天 对话（管理端）：数据存 ClickHouse，走 ai-lubricant 的 /admin/* 端点
  // （admin token Bearer 鉴权，executeMapped 已带上）。后端直接返回
  // {conversations, page}，listUnder 提取 conversations 并透传 page 游标。
  v1AdminAgentConversationsList: {
    method: "GET",
    path: "/admin/agent/conversations",
    transform: listUnder("conversations")
  },
  v1AdminChatConversationsList: {
    method: "GET",
    path: "/admin/chat/conversations",
    transform: listUnder("conversations")
  },
  // 管理端单条对话详情：与列表同走 /admin/*（admin token Bearer 鉴权，
  // executeMapped 已带上），可跨用户读取。user_id 由列表接口提供。
  v1AdminAgentConversationsDetail: {
    method: "GET",
    path: "/admin/agent/conversations/{0}"
  },
  v1AdminChatConversationsDetail: {
    method: "GET",
    path: "/admin/chat/conversations/{0}"
  }
  // Team skills are team-owned resources; group binding has dedicated endpoints above.
};
function fillPath(template, extrax) {
  return template.replace(/\{(\d+)\}/g, (_m, idx) => {
    const value = extrax[Number(idx)];
    return value != null ? encodeURIComponent(String(value)) : "";
  });
}
export function resolveEndpoint(methodName, extrax) {
  const spec = ENDPOINT_MAP[methodName];
  if (!spec) return null;
  return { spec, url: fillPath(spec.path, extrax) };
}

//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJzb3VyY2VzIjpbImVuZHBvaW50TWFwLnRzIl0sInNvdXJjZXNDb250ZW50IjpbIi8qKlxyXG4gKiBFbmRwb2ludCBtYXBwaW5nIGxheWVyIChwYXRoIDIpOiBtYXAgdGhlIGdlbmVyYXRlZCBgYEFwaS50c2BgIG1ldGhvZCBuYW1lcyB0b1xyXG4gKiBvdXIgRmFzdEFQSSByb3V0ZXMgd2l0aG91dCByZWdlbmVyYXRpbmcgdGhlIDgxMjItbGluZSBjbGllbnQuXHJcbiAqXHJcbiAqIEJhY2tncm91bmQ6IGBgc3JjL2FwaS9BcGkudHNgYCBpcyBnZW5lcmF0ZWQgZnJvbSBNb25rZXlDb2RlJ3MgR28gYmFja2VuZFxyXG4gKiBzd2FnZ2VyLiBPdXIgYmFja2VuZCBpcyBGYXN0QVBJIHdpdGggcm91dGVzIHVuZGVyIGBgL2FwaS92MS91c2Vycy8qYGAuIFJhdGhlclxyXG4gKiB0aGFuIGZvcmNlIG91ciByb3V0ZXMgdG8gbWF0Y2ggdGhlIHN3YWdnZXIgb3BlcmF0aW9uSWRzLCB3ZSBtYXAgdGhlIG1ldGhvZFxyXG4gKiBuYW1lcyB0byByZWFsIHJvdXRlcyBoZXJlIGFuZCBhZGFwdCB0aGUgcmVzcG9uc2UgZW52ZWxvcGUgdG8gdGhlIHNoYXBlIHRoZVxyXG4gKiBmcm9udGVuZCBleHBlY3RzIChgYHtjb2RlLCBtZXNzYWdlLCBkYXRhfWBgIHdpdGggYGBjb2RlID09PSAwYGAgb24gc3VjY2VzcykuXHJcbiAqXHJcbiAqIE1ldGhvZC1uYW1lIGNvbnZlbnRpb24gKHN3YWdnZXItZ2VuZXJhdGVkKTogYSBwYXRoIGlzIGVuY29kZWQgYXMgUGFzY2FsQ2FzZVxyXG4gKiBzZWdtZW50cyArIGEgdmVyYiBzdWZmaXggKExpc3QvRGV0YWlsL0NyZWF0ZS9VcGRhdGUvRGVsZXRlKS4gUGF0aCBwYXJhbXMgYXJlXHJcbiAqIHBhc3NlZCBwb3NpdGlvbmFsbHkgdmlhIGBgZXh0cmF4YGAgYXQgY2FsbCBzaXRlcy4gYGBwYXJhbXNgYCBpcyB0aGUgcXVlcnkgZm9yXHJcbiAqIEdFVC9ERUxFVEUgYW5kIHRoZSBKU09OIGJvZHkgZm9yIFBPU1QvUFVULlxyXG4gKlxyXG4gKiBPbmx5IHRoZSBkb21haW5zIHdlIGhhdmUgYWN0dWFsbHkgaW1wbGVtZW50ZWQgYXJlIG1hcHBlZC4gVW5tYXBwZWQgbWV0aG9kc1xyXG4gKiByZXNvbHZlIHRvIGEgY2xlYW4gXCJub3QgaW1wbGVtZW50ZWRcIiBlbnZlbG9wZSBzbyB0aGUgVUkgZGVncmFkZXMgZ3JhY2VmdWxseVxyXG4gKiBpbnN0ZWFkIG9mIGNyYXNoaW5nIOKAlCB0aGlzIGxldHMgdGhlIHBvcnRhbCBib290IGFuZCB0aGUgaW1wbGVtZW50ZWQgcGFnZXNcclxuICogd29yayB3aGlsZSB0aGUgVk0vYWdlbnQtY29tcG9zZS1kZXBlbmRlbnQgc3VyZmFjZXMgc3RheSBkb3JtYW50LlxyXG4gKi9cclxuXHJcbmV4cG9ydCB0eXBlIEh0dHBWZXJiID0gXCJHRVRcIiB8IFwiUE9TVFwiIHwgXCJQVVRcIiB8IFwiREVMRVRFXCI7XHJcblxyXG5leHBvcnQgaW50ZXJmYWNlIEVuZHBvaW50U3BlYyB7XHJcbiAgbWV0aG9kOiBIdHRwVmVyYjtcclxuICAvKipcclxuICAgKiBQYXRoIHRlbXBsYXRlLiBgYHswfWBgL2BgezF9YGAgYXJlIGZpbGxlZCBwb3NpdGlvbmFsbHkgZnJvbSBgYGV4dHJheGBgLlxyXG4gICAqIEFsbCBwYXRocyBhcmUgcmVsYXRpdmUgdG8gdGhlIGFwcCBvcmlnaW4gKHNhbWUtb3JpZ2luOyBkZXYgc2VydmVyIHByb3hpZXNcclxuICAgKiBgYC9hcGlgYCB0byB0aGUgYmFja2VuZCwgbWF0Y2hpbmcgdGhlIHZlbmRvcmVkIHZpdGUgY29uZmlnKS5cclxuICAgKi9cclxuICBwYXRoOiBzdHJpbmc7XHJcbiAgLyoqXHJcbiAgICogT3B0aW9uYWwgcmVzcG9uc2UgcmVzaGFwZXI6IG91ciBSRVNUIHJlc3BvbnNlcyAoZS5nLiBgYHt0b3RhbCwgcm93c31gYCkgYXJlXHJcbiAgICogYWRhcHRlZCB0byB0aGUga2V5IHRoZSBmcm9udGVuZCByZWFkcyAoZS5nLiBgYHt0YXNrczogWy4uLl19YGApLiBSZWNlaXZlcyB0aGVcclxuICAgKiByYXcgcGFyc2VkIEpTT04gYm9keSBhbmQgcmV0dXJucyB0aGUgYGBkYXRhYGAgcGF5bG9hZC5cclxuICAgKi9cclxuICB0cmFuc2Zvcm0/OiAocmF3OiBhbnkpID0+IGFueTtcclxufVxyXG5cclxuLyoqXHJcbiAqIFdyYXAgYSBsaXN0IHVuZGVyIGEgbmFtZWQga2V5LCB0b2xlcmF0aW5nIHNldmVyYWwgYmFja2VuZCBzaGFwZXM6IGEgYmFyZVxyXG4gKiBhcnJheSwgYGB7cm93c31gYCwgYGB7ZGF0YX1gYCwgb3IgdGhlIG5hbWVkIGtleSBpdHNlbGYgKGBge25vZGVzfWBgIGZvclxyXG4gKiBgYGxpc3RVbmRlcihcIm5vZGVzXCIpYGApLiBUaGUgbmFtZWQta2V5IGNhc2UgbWF0dGVycyBmb3IgZW5kcG9pbnRzIGxpa2VcclxuICogYGAvdGVhbXMvbXktbm9kZXNgYCB0aGF0IGFscmVhZHkgcmV0dXJuIGBge25vZGVzOiBbLi4uXX1gYCDigJQgd2l0aG91dCBpdCB0aGVcclxuICogbGlzdCBpcyBzaWxlbnRseSBkcm9wcGVkIHRvIGBgW11gYC5cclxuICovXHJcbmZ1bmN0aW9uIGxpc3RVbmRlcihrZXk6IHN0cmluZyk6IChyYXc6IGFueSkgPT4gYW55IHtcclxuICByZXR1cm4gKHJhdzogYW55KSA9PiB7XHJcbiAgICBjb25zdCByb3dzID0gQXJyYXkuaXNBcnJheShyYXcpXHJcbiAgICAgID8gcmF3XHJcbiAgICAgIDogcmF3Py5ba2V5XSA/PyByYXc/LnJvd3MgPz8gcmF3Py5kYXRhID8/IFtdO1xyXG4gICAgcmV0dXJuIHsgW2tleV06IHJvd3MsIHRvdGFsOiByYXc/LnRvdGFsLCBwYWdlOiByYXc/LnBhZ2UsIHBhZ2Vfc2l6ZTogcmF3Py5wYWdlX3NpemUgfTtcclxuICB9O1xyXG59XHJcblxyXG4vKipcclxuICogVGhlIHRhc2sgdGFibGUgaXMgYWxzbyB0aGUgcHJvamVjdC1jb252ZXJzYXRpb24gbGlzdCBpbiB0aGUgcG9ydGVkIG1hbmFnZXJcclxuICogVUkuIE5vcm1hbGl6ZSB0YXNrIHN1bW1hcnkgZmllbGRzIHRvIHRoZSBnZW5lcmF0ZWQgVGVhbUNvbnZlcnNhdGlvbkl0ZW1cclxuICogc2hhcGUgc28gaXRzIHByb2plY3RfaWQvZGV0YWlsIGxpbmsgYW5kIGNvbnRlbnQvdGl0bGUgc3RheSBhdmFpbGFibGUuXHJcbiAqL1xyXG5mdW5jdGlvbiB0YXNrQ29udmVyc2F0aW9ucyhyYXc6IGFueSk6IGFueSB7XHJcbiAgY29uc3Qgcm93cyA9IEFycmF5LmlzQXJyYXkocmF3KSA/IHJhdyA6IHJhdz8ucm93cyA/PyByYXc/LmRhdGEgPz8gW107XHJcbiAgY29uc3QgY29udmVyc2F0aW9ucyA9IHJvd3MubWFwKChyb3c6IGFueSkgPT4gKHtcclxuICAgIC4uLnJvdyxcclxuICAgIHRhc2tfaWQ6IHJvdy50YXNrX2lkID8/IHJvdy5pZCxcclxuICAgIHRhc2tfdGl0bGU6IHJvdy50YXNrX3RpdGxlID8/IHJvdy50aXRsZSxcclxuICAgIGNvbnRlbnQ6IHJvdy5jb250ZW50ID8/IHJvdy50aXRsZSA/PyBcIlwiLFxyXG4gIH0pKTtcclxuICByZXR1cm4geyBjb252ZXJzYXRpb25zLCB0b3RhbDogcmF3Py50b3RhbCwgcGFnZTogcmF3Py5wYWdlLCBwYWdlX3NpemU6IHJhdz8ucGFnZV9zaXplIH07XHJcbn1cclxuXHJcbi8qKlxyXG4gKiBUaGUgZW5kcG9pbnQgdGFibGUuIEtleXMgYXJlIGBgQXBpLnRzYGAgbWV0aG9kIG5hbWVzLiBFeHRlbmQgdGhpcyBhcyBtb3JlXHJcbiAqIGJhY2tlbmQgZG9tYWlucyBsYW5kLiBBIG1pc3NpbmcgZW50cnkgaXMgTk9UIGFuIGVycm9yIOKAlCBzZWUgYGByZXNvbHZlRW5kcG9pbnRgYC5cclxuICovXHJcbmV4cG9ydCBjb25zdCBFTkRQT0lOVF9NQVA6IFJlY29yZDxzdHJpbmcsIEVuZHBvaW50U3BlYz4gPSB7XHJcbiAgLy8gLS0tLSBBdXRoIC8gdXNlciAoUGhhc2UgQSkgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tXHJcbiAgdjFVc2Vyc1Bhc3N3b3JkTG9naW5DcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Bhc3N3b3JkLWxvZ2luXCIgfSxcclxuICB2MVVzZXJzU3RhdHVzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9zdGF0dXNcIiB9LFxyXG4gIHYxVXNlcnNMb2dvdXRDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL2xvZ291dFwiIH0sXHJcbiAgdjFVc2Vyc01lbWJlcnNMaXN0OiB7IG1ldGhvZDogXCJHRVRcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL21lbWJlcnNcIiB9LFxyXG5cclxuICAvLyAtLS0tIFJ1bnRpbWUga2V5cyAoUGhhc2UgQikgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS1cclxuICB2MVVzZXJzTW9kZWxHYXRld2F5UnVudGltZUtleXNMaXN0OiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvbW9kZWwtZ2F0ZXdheS9ydW50aW1lLWtleXNcIixcclxuICB9LFxyXG4gIHYxVXNlcnNNb2RlbEdhdGV3YXlSdW50aW1lS2V5c0NyZWF0ZToge1xyXG4gICAgbWV0aG9kOiBcIlBPU1RcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9tb2RlbC1nYXRld2F5L3J1bnRpbWUta2V5c1wiLFxyXG4gIH0sXHJcbiAgdjFVc2Vyc01vZGVsR2F0ZXdheVJ1bnRpbWVLZXlzRGVsZXRlOiB7XHJcbiAgICBtZXRob2Q6IFwiREVMRVRFXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvbW9kZWwtZ2F0ZXdheS9ydW50aW1lLWtleXMvezB9XCIsXHJcbiAgfSxcclxuXHJcbiAgLy8gLS0tLSBUYXNrcyAoUGhhc2UgQykgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLVxyXG4gIHYxVXNlcnNUYXNrc0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvdGFza3NcIiwgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJ0YXNrc1wiKSB9LFxyXG4gIHYxVXNlcnNUYXNrc0RldGFpbDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy90YXNrcy97MH1cIiB9LFxyXG4gIHYxVXNlcnNUYXNrc0NyZWF0ZTogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvdGFza3NcIiB9LFxyXG4gIHYxVXNlcnNUYXNrc1VwZGF0ZTogeyBtZXRob2Q6IFwiUFVUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy90YXNrcy97MH1cIiB9LFxyXG4gIHYxVXNlcnNUYXNrc0RlbGV0ZTogeyBtZXRob2Q6IFwiREVMRVRFXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy90YXNrcy97MH1cIiB9LFxyXG4gIHYxVXNlcnNUYXNrc1N0b3BVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvdGFza3Mvc3RvcFwiIH0sXHJcblxyXG4gIC8vIC0tLS0gUHJvamVjdHMgKFBoYXNlIEMpIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS1cclxuICB2MVVzZXJzUHJvamVjdHNMaXN0OiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHNcIixcclxuICAgIHRyYW5zZm9ybTogbGlzdFVuZGVyKFwicHJvamVjdHNcIiksXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNEZXRhaWw6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9XCIgfSxcclxuICB2MVVzZXJzUHJvamVjdHNDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzXCIgfSxcclxuICB2MVVzZXJzUHJvamVjdHNVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9XCIgfSxcclxuICB2MVVzZXJzUHJvamVjdHNEZWxldGU6IHsgbWV0aG9kOiBcIkRFTEVURVwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9XCIgfSxcclxuICB2MVVzZXJzUHJvamVjdHNDb2xsYWJvcmF0b3JzTGlzdDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9jb2xsYWJvcmF0b3JzXCIsXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNDb2xsYWJvcmF0b3JzQ3JlYXRlOiB7XHJcbiAgICBtZXRob2Q6IFwiUE9TVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9jb2xsYWJvcmF0b3JzXCIsXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNDb2xsYWJvcmF0b3JzRGVsZXRlOiB7XHJcbiAgICBtZXRob2Q6IFwiREVMRVRFXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9L2NvbGxhYm9yYXRvcnMvezF9XCIsXHJcbiAgfSxcclxuICAvLyDpobnnm67lhbPogZTvvJrlrprlkJHlvJXnlKjlhbPns7vvvIzmnYPpmZDmjInmupDpobnnm64gdG9rZW4g5a+555uu5qCH5LuT5bqT55qE5a6e6ZmF5Y+v6L6+5oCn5Yik5a6a44CCXHJcbiAgdjFVc2Vyc1Byb2plY3RzQXNzb2NpYXRpb25zTGlzdDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9hc3NvY2lhdGlvbnNcIixcclxuICB9LFxyXG4gIHYxVXNlcnNQcm9qZWN0c0Fzc29jaWF0aW9uc0NyZWF0ZToge1xyXG4gICAgbWV0aG9kOiBcIlBPU1RcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0cy97MH0vYXNzb2NpYXRpb25zXCIsXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNBc3NvY2lhdGlvbnNEZWxldGU6IHtcclxuICAgIG1ldGhvZDogXCJERUxFVEVcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0cy97MH0vYXNzb2NpYXRpb25zL3sxfVwiLFxyXG4gIH0sXHJcbiAgLy8g55Sf5oiQ55qEIEFwaS50cyDmlrnms5XlkI3mmK8gYGAuLi5Jc3N1ZXNEZXRhaWxgYO+8iEdFVCDpl67popjliJfooajvvInvvJvkv53nlZkgYGAuLi5MaXN0YGAg5Yir5ZCN44CCXHJcbiAgdjFVc2Vyc1Byb2plY3RzSXNzdWVzRGV0YWlsOiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9L2lzc3Vlc1wiLFxyXG4gICAgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJpc3N1ZXNcIiksXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNJc3N1ZXNMaXN0OiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9L2lzc3Vlc1wiLFxyXG4gICAgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJpc3N1ZXNcIiksXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNJc3N1ZXNDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9pc3N1ZXNcIiB9LFxyXG4gIHYxVXNlcnNQcm9qZWN0c0lzc3Vlc1VwZGF0ZToge1xyXG4gICAgbWV0aG9kOiBcIlBVVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9pc3N1ZXMvezF9XCIsXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNJc3N1ZXNEZWxldGU6IHtcclxuICAgIG1ldGhvZDogXCJERUxFVEVcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0cy97MH0vaXNzdWVzL3sxfVwiLFxyXG4gIH0sXHJcbiAgLy8g6ZyA5rGCL2J1ZyDliIbphY3vvJrliJvlu7rpppbkuKrku7vliqHlubbmjqjov5vnirbmgIHvvIjmnI3liqHnq6/kuKTpmLbmrrXvvIzlpLHotKXkv53mjIHlj6/liIbphY3vvInjgIJcclxuICB2MVVzZXJzUHJvamVjdHNJc3N1ZXNBc3NpZ246IHtcclxuICAgIG1ldGhvZDogXCJQT1NUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9L2lzc3Vlcy97MX0vYXNzaWduXCIsXHJcbiAgfSxcclxuICAvLyDkurrlt6Xnoa7orqQv6YCA5Zue6K6+6K6h5paH5qGj5oiWIGJ1ZyDmoLnlm6DjgIJhZ2VudCDml6DmnYPotbDov5nkuKrlj6PjgIJcclxuICB2MVVzZXJzUHJvamVjdHNJc3N1ZXNDb25maXJtOiB7XHJcbiAgICBtZXRob2Q6IFwiUE9TVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9pc3N1ZXMvezF9L2NvbmZpcm1cIixcclxuICB9LFxyXG4gIHYxVXNlcnNQcm9qZWN0c0lzc3Vlc0NvbW1lbnRzTGlzdDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS9pc3N1ZXMvezF9L2NvbW1lbnRzXCIsXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNJc3N1ZXNDb21tZW50c0NyZWF0ZToge1xyXG4gICAgbWV0aG9kOiBcIlBPU1RcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0cy97MH0vaXNzdWVzL3sxfS9jb21tZW50c1wiLFxyXG4gIH0sXHJcbiAgLy8g5LuT5bqT6K+7OuebruW9leagkSArIOaWh+S7tuWGheWuueOAgnF1ZXJ5KHJlY3Vyc2l2ZS9yZWYvcGF0aCnnlLEgdG9RdWVyeSDoh6rliqjmi7zmjqXjgIJcclxuICB2MVVzZXJzUHJvamVjdHNUcmVlRGV0YWlsOiB7IG1ldGhvZDogXCJHRVRcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS90cmVlXCIgfSxcclxuICB2MVVzZXJzUHJvamVjdHNUcmVlQmxvYkRldGFpbDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Byb2plY3RzL3swfS90cmVlL2Jsb2JcIixcclxuICB9LFxyXG4gIC8vIEdpdCDlrZDmqKHlnZfvvJrlkI7nq6/or7vku5PlupMgLmdpdG1vZHVsZXMg5rS+55Sf77yM5LiN6JC95bqT77yb55u45a+55Zyw5Z2A5bey5oyJ54i25LuT6Kej5p6Q5Li657ud5a+55Zyw5Z2A44CCXHJcbiAgdjFVc2Vyc1Byb2plY3RzU3VibW9kdWxlc0xpc3Q6IHtcclxuICAgIG1ldGhvZDogXCJHRVRcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0cy97MH0vc3VibW9kdWxlc1wiLFxyXG4gIH0sXHJcbiAgLy8g5oqA5pyv5qCI6K+G5Yir77yac3RhY2sg5piv5bu66aG555uu5ZCO5byC5q2l5omr55qE57yT5a2Y77yIbnVsbCA9IOacquaJqy/miavmj4/kuK0v5aSx6LSl77yJ77yMcmVzY2FuIOWQjOatpemHjeaJq+OAglxyXG4gIHYxVXNlcnNQcm9qZWN0c1N0YWNrRGV0YWlsOiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvcHJvamVjdHMvezB9L3N0YWNrXCIsXHJcbiAgfSxcclxuICB2MVVzZXJzUHJvamVjdHNTdGFja1Jlc2NhbkNyZWF0ZToge1xyXG4gICAgbWV0aG9kOiBcIlBPU1RcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0cy97MH0vc3RhY2svcmVzY2FuXCIsXHJcbiAgfSxcclxuXHJcbiAgLy8gLS0tLSBHaXQgaWRlbnRpdGllcyAvIGJvdHMgKFBoYXNlIEMpIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tXHJcbiAgdjFVc2Vyc0dpdElkZW50aXRpZXNMaXN0OiB7IG1ldGhvZDogXCJHRVRcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL2dpdC1pZGVudGl0aWVzXCIgfSxcclxuICB2MVVzZXJzR2l0SWRlbnRpdGllc0RldGFpbDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9naXQtaWRlbnRpdGllcy97MH1cIiB9LFxyXG4gIC8vIHsxfSDmmK/ku5PlupMgZnVsbF9uYW1l77yIb3duZXIvcmVwb++8ie+8m+WJjeerr+W3siBlbmNvZGVVUklDb21wb25lbnQg5LiA5qyh77yMZmlsbFBhdGgg5YaN57yW56CB5LiA5qyh77yMXHJcbiAgLy8g5ZCO56uv6Lev55Sx55SoIGBgOnBhdGhgYCArIHVucXVvdGUg6L+Y5Y6f44CC5YiG5pSv5YiX6KGo55So5LqO6aG555uu6K+m5oOF6aG16Kej5p6Q6buY6K6k5YiG5pSv44CCXHJcbiAgdjFVc2Vyc0dpdElkZW50aXRpZXNCcmFuY2hlc0RldGFpbDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL2dpdC1pZGVudGl0aWVzL3swfS97MX0vYnJhbmNoZXNcIixcclxuICB9LFxyXG4gIHYxVXNlcnNHaXRJZGVudGl0aWVzQ3JlYXRlOiB7IG1ldGhvZDogXCJQT1NUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9naXQtaWRlbnRpdGllc1wiIH0sXHJcbiAgdjFVc2Vyc0dpdElkZW50aXRpZXNVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvZ2l0LWlkZW50aXRpZXMvezB9XCIgfSxcclxuICB2MVVzZXJzR2l0SWRlbnRpdGllc0RlbGV0ZTogeyBtZXRob2Q6IFwiREVMRVRFXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9naXQtaWRlbnRpdGllcy97MH1cIiB9LFxyXG4gIHYxVXNlcnNHaXRCb3RzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9naXQtYm90c1wiIH0sXHJcbiAgdjFVc2Vyc0dpdEJvdHNDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL2dpdC1ib3RzXCIgfSxcclxuICB2MVVzZXJzR2l0Qm90c1VwZGF0ZTogeyBtZXRob2Q6IFwiUFVUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9naXQtYm90cy97MH1cIiB9LFxyXG4gIHYxVXNlcnNHaXRCb3RzRGVsZXRlOiB7IG1ldGhvZDogXCJERUxFVEVcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL2dpdC1ib3RzL3swfVwiIH0sXHJcblxyXG4gIC8vIC0tLS0gTm9kZXMgKGFnZW50LWNvbXBvc2UsIHJlYWQtb25seSBcInVzZVwiIHZpZXcpIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tXHJcbiAgLy8gVGhlIHRhc2sgcnVudGltZSBpcyBub3cgYSBjaG9zZW4gYWdlbnQtY29tcG9zZSBub2RlIChob3N0cy9pbWFnZXMvVk1zIHdlcmVcclxuICAvLyByZW1vdmVkKS4gQSB1c2VyIHNlZXMgdGhlIG5vZGVzIHRoZWlyIGdyb3VwcyB3ZXJlIGdyYW50ZWQsIGFnZ3JlZ2F0ZWQgYnkgdGhlXHJcbiAgLy8gYmFja2VuZCBhdCAvYXBpL3YxL3RlYW1zL215LW5vZGVzLlxyXG4gIHYxVXNlcnNOb2Rlc0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbXktbm9kZXNcIiwgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJub2Rlc1wiKSB9LFxyXG5cclxuICB2MVVzZXJzTW9kZWxzTGlzdDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL21vZGVsc1wiLFxyXG4gICAgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJtb2RlbHNcIiksXHJcbiAgfSxcclxuICB2MVVzZXJzTW9kZWxzQXZhaWxhYmxlTGlzdDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL21vZGVscy9hdmFpbGFibGVcIixcclxuICAgIHRyYW5zZm9ybTogbGlzdFVuZGVyKFwibW9kZWxzXCIpLFxyXG4gIH0sXHJcbiAgdjFVc2Vyc01vZGVsc0NyZWF0ZTogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvbW9kZWxzXCIgfSxcclxuICB2MVVzZXJzTW9kZWxzVXBkYXRlOiB7IG1ldGhvZDogXCJQVVRcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL21vZGVscy97MH1cIiB9LFxyXG4gIHYxVXNlcnNNb2RlbHNEZWxldGU6IHsgbWV0aG9kOiBcIkRFTEVURVwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvbW9kZWxzL3swfVwiIH0sXHJcbiAgdjFVc2Vyc01vZGVsc0hlYWx0aENoZWNrQ3JlYXRlOiB7XHJcbiAgICBtZXRob2Q6IFwiUE9TVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL21vZGVscy9oZWFsdGgtY2hlY2tcIixcclxuICB9LFxyXG4gIHYxVXNlcnNNb2RlbHNIZWFsdGhDaGVja0RldGFpbDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL21vZGVscy97MH0vaGVhbHRoLWNoZWNrXCIsXHJcbiAgfSxcclxuICAvLyAtLS0tIFNraWxscyAvIHBsdWdpbnMgKFBoYXNlIEQpIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS1cclxuICB2MVNraWxsc0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvc2tpbGxzXCIgfSxcclxuICB2MVBsdWdpbnNMaXN0OiB7IG1ldGhvZDogXCJHRVRcIiwgcGF0aDogXCIvYXBpL3YxL3BsdWdpbnNcIiB9LFxyXG5cclxuICAvLyAtLS0tIE5vdGlmeSAoUGhhc2UgRCkgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tXHJcbiAgdjFVc2Vyc05vdGlmeUNoYW5uZWxzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9ub3RpZnkvY2hhbm5lbHNcIiB9LFxyXG4gIHYxVXNlcnNOb3RpZnlDaGFubmVsc0NyZWF0ZTogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvbm90aWZ5L2NoYW5uZWxzXCIgfSxcclxuICB2MVVzZXJzTm90aWZ5Q2hhbm5lbHNVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdXNlcnMvbm90aWZ5L2NoYW5uZWxzL3swfVwiIH0sXHJcbiAgdjFVc2Vyc05vdGlmeUNoYW5uZWxzRGVsZXRlOiB7IG1ldGhvZDogXCJERUxFVEVcIiwgcGF0aDogXCIvYXBpL3YxL3VzZXJzL25vdGlmeS9jaGFubmVscy97MH1cIiB9LFxyXG4gIHYxVXNlcnNOb3RpZnlFdmVudFR5cGVzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS91c2Vycy9ub3RpZnkvZXZlbnQtdHlwZXNcIiB9LFxyXG5cclxuICAvLyAtLS0tIFRlYW0gYWRtaW5pc3RyYXRpb24gKE1vbmtleUNvZGUgbWFuYWdlcikgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tXHJcbiAgdjFUZWFtc0Rhc2hib2FyZExpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvZGFzaGJvYXJkXCIgfSxcclxuXHJcbiAgLy8gVXNlcnMgLyBwYXNzd29yZHMgLyBtZW1iZXJzaGlwXHJcbiAgdjFUZWFtc1VzZXJzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy91c2Vyc1wiIH0sXHJcbiAgdjFUZWFtc1VzZXJzU3RhdHVzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy91c2Vycy9zdGF0dXNcIiB9LFxyXG4gIHYxVGVhbXNVc2Vyc0xvZ291dENyZWF0ZTogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvdXNlcnMvbG9nb3V0XCIgfSxcclxuICAvLyDnlKjmiLflpLTlg4/oj5zljZXkvb/nlKjmraTnlJ/miJDmlrnms5XlkI3vvJvlvZPliY3nlKjmiLflr4bnoIHnlLHlm6LpmJ/nlKjmiLfot6/nlLHlpITnkIbjgIJcclxuICB2MVVzZXJzUGFzc3dvcmRzQ2hhbmdlVXBkYXRlOiB7XHJcbiAgICBtZXRob2Q6IFwiUFVUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvdXNlcnMvcGFzc3dvcmRzL2NoYW5nZVwiLFxyXG4gIH0sXHJcbiAgdjFUZWFtc1VzZXJzV2l0aFBhc3N3b3JkQ3JlYXRlOiB7IG1ldGhvZDogXCJQT1NUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy91c2Vycy93aXRoLXBhc3N3b3JkXCIgfSxcclxuICAvLyBLZWVwIHRoZSBtaXNzcGVsbGVkIHN3YWdnZXIgb3BlcmF0aW9uSWQgdXNlZCBieSBvbGRlciBnZW5lcmF0ZWQgY2xpZW50cy5cclxuICB2MVRlYW1zVXNlcnNXaXRoUGFzd29yZENyZWF0ZTogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvdXNlcnMvd2l0aC1wYXNzd29yZFwiIH0sXHJcbiAgdjFUZWFtc1VzZXJzRGVsZXRlOiB7IG1ldGhvZDogXCJERUxFVEVcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL3VzZXJzL3swfVwiIH0sXHJcbiAgdjFUZWFtc1VzZXJzUGFzc3dvcmRzQ2hhbmdlVXBkYXRlOiB7XHJcbiAgICBtZXRob2Q6IFwiUFVUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvdXNlcnMvcGFzc3dvcmRzL2NoYW5nZVwiLFxyXG4gIH0sXHJcbiAgdjFUZWFtc1VzZXJzUGFzc3dvcmRzUmVzZXRVcGRhdGU6IHtcclxuICAgIG1ldGhvZDogXCJQVVRcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS90ZWFtcy91c2Vycy97MH0vcGFzc3dvcmRzL3Jlc2V0XCIsXHJcbiAgfSxcclxuICAvLyDnu5/kuIDjgIznlKjmiLfkuI7mnYPpmZDjgI3pobXvvJrkuIDlvKDlhajph4/nlKjmiLfooaggKyDmr4/ooYznrqHnkIblkZjlvIDlhbPjgIJcclxuICB2MVRlYW1zVXNlcnNBbGxMaXN0OiB7IG1ldGhvZDogXCJHRVRcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL3VzZXJzL2FsbFwiIH0sXHJcbiAgdjFUZWFtc1VzZXJzQWRtaW5VcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvdXNlcnMvezB9L2FkbWluXCIgfSxcclxuICB2MVRlYW1zVXNlcnNDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL3VzZXJzXCIgfSxcclxuICB2MVRlYW1zVXNlcnNVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvdXNlcnMvezB9XCIgfSxcclxuXHJcbiAgLy8gVGVhbSBncm91cHNcclxuICB2MVRlYW1zR3JvdXBzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9ncm91cHNcIiB9LFxyXG4gIHYxVGVhbXNHcm91cHNDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2dyb3Vwc1wiIH0sXHJcbiAgdjFUZWFtc0dyb3Vwc1VwZGF0ZTogeyBtZXRob2Q6IFwiUFVUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9ncm91cHMvezB9XCIgfSxcclxuICB2MVRlYW1zR3JvdXBzRGVsZXRlOiB7IG1ldGhvZDogXCJERUxFVEVcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2dyb3Vwcy97MH1cIiB9LFxyXG4gIHYxVGVhbXNHcm91cHNVc2Vyc1VwZGF0ZTogeyBtZXRob2Q6IFwiUFVUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9ncm91cHMvezB9L3VzZXJzXCIgfSxcclxuICAvLyDliIbnu4TkvqfphY3nva7or6Xnu4Tog73nlKjlk6rkupvns7vnu58gS2V577yI5p2D6ZmQ57uf5LiA5Zyo5YiG57uE5L6n77yM5LiN5YaN5LuOIGtleSDkvqfphY3jgIzpgILnlKjliIbnu4TjgI3vvInjgIJcclxuICB2MVRlYW1zR3JvdXBzQXBpS2V5c0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvZ3JvdXBzL3swfS9hcGkta2V5c1wiIH0sXHJcbiAgdjFUZWFtc0dyb3Vwc0FwaUtleXNVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvZ3JvdXBzL3swfS9hcGkta2V5c1wiIH0sXHJcbiAgdjFUZWFtc0dyb3Vwc01jcFNlcnZpY2VzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9ncm91cHMvezB9L21jcC1zZXJ2aWNlc1wiIH0sXHJcbiAgdjFUZWFtc0dyb3Vwc01jcFNlcnZpY2VzVXBkYXRlOiB7IG1ldGhvZDogXCJQVVRcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2dyb3Vwcy97MH0vbWNwLXNlcnZpY2VzXCIgfSxcclxuICB2MVRlYW1zR3JvdXBzTm9kZVBpY2tlckxpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvZ3JvdXBzL3swfS9ub2RlLXBpY2tlclwiIH0sXHJcbiAgdjFUZWFtc0dyb3Vwc05vZGVzQmluZDogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvZ3JvdXBzL3swfS9ub2Rlc1wiIH0sXHJcbiAgdjFUZWFtc0dyb3Vwc05vZGVzVW5iaW5kOiB7IG1ldGhvZDogXCJERUxFVEVcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2dyb3Vwcy97MH0vbm9kZXMvezF9XCIgfSxcclxuICB2MVRlYW1zU2tpbGxzTGlzdDogeyBtZXRob2Q6IFwiR0VUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9za2lsbHNcIiB9LFxyXG4gIHYxVGVhbXNHcm91cHNTa2lsbHNMaXN0OiB7IG1ldGhvZDogXCJHRVRcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2dyb3Vwcy97MH0vc2tpbGxzXCIgfSxcclxuICB2MVRlYW1zR3JvdXBzU2tpbGxzVXBkYXRlOiB7IG1ldGhvZDogXCJQVVRcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2dyb3Vwcy97MH0vc2tpbGxzXCIgfSxcclxuXHJcbiAgLy8gVGVhbSBtb2RlbHMgLyBoZWFsdGgtY2hlY2sgKGJhY2tlZCBieSBtY190ZWFtX21vZGVsczsgbW9iaWxlICsgY29uc29sZSBzZXR0aW5ncyBkZXBlbmQgb24gaXQpXHJcbiAgdjFUZWFtc01vZGVsc0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbW9kZWxzXCIgfSxcclxuICB2MVRlYW1zTW9kZWxzQ3JlYXRlOiB7IG1ldGhvZDogXCJQT1NUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9tb2RlbHNcIiB9LFxyXG4gIHYxVGVhbXNNb2RlbHNVcGRhdGU6IHsgbWV0aG9kOiBcIlBVVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbW9kZWxzL3swfVwiIH0sXHJcbiAgdjFUZWFtc01vZGVsc0RlbGV0ZTogeyBtZXRob2Q6IFwiREVMRVRFXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9tb2RlbHMvezB9XCIgfSxcclxuICB2MVRlYW1zTW9kZWxzSGVhbHRoQ2hlY2tDcmVhdGU6IHtcclxuICAgIG1ldGhvZDogXCJQT1NUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbW9kZWxzL2hlYWx0aC1jaGVja1wiLFxyXG4gIH0sXHJcbiAgdjFUZWFtc01vZGVsc0hlYWx0aENoZWNrRGV0YWlsOiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbW9kZWxzL3swfS9oZWFsdGgtY2hlY2tcIixcclxuICB9LFxyXG5cclxuICAvLyBMb2dpbiBtZXRob2RzIChPSURDIC8gT0F1dGggcHJvdmlkZXJzKSAvIGF1ZGl0XHJcbiAgdjFUZWFtc0xvZ2luTWV0aG9kc0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbG9naW4tbWV0aG9kc1wiIH0sXHJcbiAgdjFUZWFtc0xvZ2luTWV0aG9kc0NyZWF0ZTogeyBtZXRob2Q6IFwiUE9TVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvbG9naW4tbWV0aG9kc1wiIH0sXHJcbiAgdjFUZWFtc0xvZ2luTWV0aG9kc1VwZGF0ZTogeyBtZXRob2Q6IFwiUFVUXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9sb2dpbi1tZXRob2RzL3swfVwiIH0sXHJcbiAgdjFUZWFtc0xvZ2luTWV0aG9kc0RlbGV0ZTogeyBtZXRob2Q6IFwiREVMRVRFXCIsIHBhdGg6IFwiL2FwaS92MS90ZWFtcy9sb2dpbi1tZXRob2RzL3swfVwiIH0sXHJcbiAgdjFUZWFtc0xvZ2luTWV0aG9kc1Rlc3RDcmVhdGU6IHsgbWV0aG9kOiBcIlBPU1RcIiwgcGF0aDogXCIvYXBpL3YxL3RlYW1zL2xvZ2luLW1ldGhvZHMvdGVzdFwiIH0sXHJcbiAgdjFUZWFtc0F1ZGl0c0xpc3Q6IHsgbWV0aG9kOiBcIkdFVFwiLCBwYXRoOiBcIi9hcGkvdjEvdGVhbXMvYXVkaXRzXCIgfSxcclxuXHJcbiAgLy8gLS0tLSBUZWFtLXNjb3BlZCByZXNvdXJjZSBsaXN0cyAoTW9ua2V5Q29kZSBtYW5hZ2VyIOWboumYn+inhuWbvikgLS0tLS0tLS0tLS0tXHJcbiAgLy8gVGhlc2UgY29uc29sZSBwYWdlcyB3YW50IGEgdGVhbS13aWRlIHZpZXcuIFRoZSBiYWNrZW5kIGV4cG9zZXMgdGhlIHNhbWVcclxuICAvLyByZXNvdXJjZXMgYXMgdXNlci1zY29wZWQgcm91dGVzOyB0aGUgYWRtaW4gcm9sZSBpcyBwcml2aWxlZ2VkIHRoZXJlIGFuZFxyXG4gIC8vIHJldHVybnMgZXZlcnkgcm93LCBzbyB3ZSBwb2ludCB0aGUgdGVhbS1saXN0IG1ldGhvZHMgYXQgdGhlIHVzZXIgcm91dGVzXHJcbiAgLy8gYW5kIHJlc2hhcGUgYGB7cm93c31gYCBpbnRvIHRoZSBrZXkgZWFjaCBwYWdlIHJlYWRzLiBDdXJzb3IgcGFnaW5hdGlvblxyXG4gIC8vIGRlZ3JhZGVzIHRvIHBhZ2UtYmFzZWQgKG5vIGBgcGFnZS5jdXJzb3JgYCDih5Igc2luZ2xlIHBhZ2UpIHVudGlsIHRoZVxyXG4gIC8vIGJhY2tlbmQgZ3Jvd3MgcmVhbCBjdXJzb3IgZW5kcG9pbnRzLiBUYXNrIGlzIHRoZSBcImNvbnZlcnNhdGlvblwiIHVuaXQsIHNvXHJcbiAgLy8gY29udmVyc2F0aW9ucyByZXVzZXMgdGhlIHRhc2tzIHJvdXRlLlxyXG4gIHYxVGVhbXNQcm9qZWN0c0xpc3Q6IHtcclxuICAgIG1ldGhvZDogXCJHRVRcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy9wcm9qZWN0c1wiLFxyXG4gICAgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJwcm9qZWN0c1wiKSxcclxuICB9LFxyXG4gIHYxVGVhbXNUYXNrc0xpc3Q6IHtcclxuICAgIG1ldGhvZDogXCJHRVRcIixcclxuICAgIHBhdGg6IFwiL2FwaS92MS91c2Vycy90YXNrc1wiLFxyXG4gICAgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJ0YXNrc1wiKSxcclxuICB9LFxyXG4gIHYxVGVhbXNDb252ZXJzYXRpb25zTGlzdDoge1xyXG4gICAgbWV0aG9kOiBcIkdFVFwiLFxyXG4gICAgcGF0aDogXCIvYXBpL3YxL3VzZXJzL3Rhc2tzXCIsXHJcbiAgICB0cmFuc2Zvcm06IHRhc2tDb252ZXJzYXRpb25zLFxyXG4gIH0sXHJcbiAgLy8gQWdlbnQgLyDogYrlpKkg5a+56K+d77yI566h55CG56uv77yJ77ya5pWw5o2u5a2YIENsaWNrSG91c2XvvIzotbAgYWktbHVicmljYW50IOeahCAvYWRtaW4vKiDnq6/ngrlcclxuICAvLyDvvIhhZG1pbiB0b2tlbiBCZWFyZXIg6Ym05p2D77yMZXhlY3V0ZU1hcHBlZCDlt7LluKbkuIrvvInjgILlkI7nq6/nm7TmjqXov5Tlm55cclxuICAvLyB7Y29udmVyc2F0aW9ucywgcGFnZX3vvIxsaXN0VW5kZXIg5o+Q5Y+WIGNvbnZlcnNhdGlvbnMg5bm26YCP5LygIHBhZ2Ug5ri45qCH44CCXHJcbiAgdjFBZG1pbkFnZW50Q29udmVyc2F0aW9uc0xpc3Q6IHtcclxuICAgIG1ldGhvZDogXCJHRVRcIixcclxuICAgIHBhdGg6IFwiL2FkbWluL2FnZW50L2NvbnZlcnNhdGlvbnNcIixcclxuICAgIHRyYW5zZm9ybTogbGlzdFVuZGVyKFwiY29udmVyc2F0aW9uc1wiKSxcclxuICB9LFxyXG4gIHYxQWRtaW5DaGF0Q29udmVyc2F0aW9uc0xpc3Q6IHtcclxuICAgIG1ldGhvZDogXCJHRVRcIixcclxuICAgIHBhdGg6IFwiL2FkbWluL2NoYXQvY29udmVyc2F0aW9uc1wiLFxyXG4gICAgdHJhbnNmb3JtOiBsaXN0VW5kZXIoXCJjb252ZXJzYXRpb25zXCIpLFxyXG4gIH0sXHJcbiAgLy8g566h55CG56uv5Y2V5p2h5a+56K+d6K+m5oOF77ya5LiO5YiX6KGo5ZCM6LWwIC9hZG1pbi8q77yIYWRtaW4gdG9rZW4gQmVhcmVyIOmJtOadg++8jFxyXG4gIC8vIGV4ZWN1dGVNYXBwZWQg5bey5bim5LiK77yJ77yM5Y+v6Leo55So5oi36K+75Y+W44CCdXNlcl9pZCDnlLHliJfooajmjqXlj6Pmj5DkvpvjgIJcclxuICB2MUFkbWluQWdlbnRDb252ZXJzYXRpb25zRGV0YWlsOiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hZG1pbi9hZ2VudC9jb252ZXJzYXRpb25zL3swfVwiLFxyXG4gIH0sXHJcbiAgdjFBZG1pbkNoYXRDb252ZXJzYXRpb25zRGV0YWlsOiB7XHJcbiAgICBtZXRob2Q6IFwiR0VUXCIsXHJcbiAgICBwYXRoOiBcIi9hZG1pbi9jaGF0L2NvbnZlcnNhdGlvbnMvezB9XCIsXHJcbiAgfSxcclxuICAvLyBUZWFtIHNraWxscyBhcmUgdGVhbS1vd25lZCByZXNvdXJjZXM7IGdyb3VwIGJpbmRpbmcgaGFzIGRlZGljYXRlZCBlbmRwb2ludHMgYWJvdmUuXHJcbn07XHJcblxyXG5leHBvcnQgaW50ZXJmYWNlIFJlc29sdmVkRW5kcG9pbnQge1xyXG4gIHNwZWM6IEVuZHBvaW50U3BlYztcclxuICB1cmw6IHN0cmluZztcclxufVxyXG5cclxuLyoqIEZpbGwgYGB7MH1gYC9gYHsxfWBgIHBsYWNlaG9sZGVycyBmcm9tIHRoZSBwb3NpdGlvbmFsIGBgZXh0cmF4YGAgYXJyYXkuICovXHJcbmZ1bmN0aW9uIGZpbGxQYXRoKHRlbXBsYXRlOiBzdHJpbmcsIGV4dHJheDogc3RyaW5nW10pOiBzdHJpbmcge1xyXG4gIHJldHVybiB0ZW1wbGF0ZS5yZXBsYWNlKC9cXHsoXFxkKylcXH0vZywgKF9tLCBpZHgpID0+IHtcclxuICAgIGNvbnN0IHZhbHVlID0gZXh0cmF4W051bWJlcihpZHgpXTtcclxuICAgIHJldHVybiB2YWx1ZSAhPSBudWxsID8gZW5jb2RlVVJJQ29tcG9uZW50KFN0cmluZyh2YWx1ZSkpIDogXCJcIjtcclxuICB9KTtcclxufVxyXG5cclxuLyoqXHJcbiAqIFJlc29sdmUgYSBtZXRob2QgbmFtZSB0byBhIGNvbmNyZXRlIHJlcXVlc3QsIG9yIGBgbnVsbGBgIHdoZW4gdW5tYXBwZWQuXHJcbiAqIENhbGxlcnMgdHJlYXQgYGBudWxsYGAgYXMgXCJub3QgaW1wbGVtZW50ZWRcIiBhbmQgcmV0dXJuIGEgY2xlYW4gZW52ZWxvcGUuXHJcbiAqL1xyXG5leHBvcnQgZnVuY3Rpb24gcmVzb2x2ZUVuZHBvaW50KG1ldGhvZE5hbWU6IHN0cmluZywgZXh0cmF4OiBzdHJpbmdbXSk6IFJlc29sdmVkRW5kcG9pbnQgfCBudWxsIHtcclxuICBjb25zdCBzcGVjID0gRU5EUE9JTlRfTUFQW21ldGhvZE5hbWVdO1xyXG4gIGlmICghc3BlYykgcmV0dXJuIG51bGw7XHJcbiAgcmV0dXJuIHsgc3BlYywgdXJsOiBmaWxsUGF0aChzcGVjLnBhdGgsIGV4dHJheCkgfTtcclxufVxyXG4iXSwibWFwcGluZ3MiOiJBQThDQSxTQUFTLFVBQVUsS0FBZ0M7QUFDakQsU0FBTyxDQUFDLFFBQWE7QUFDbkIsVUFBTSxPQUFPLE1BQU0sUUFBUSxHQUFHLElBQzFCLE1BQ0EsTUFBTSxHQUFHLEtBQUssS0FBSyxRQUFRLEtBQUssUUFBUSxDQUFDO0FBQzdDLFdBQU8sRUFBRSxDQUFDLEdBQUcsR0FBRyxNQUFNLE9BQU8sS0FBSyxPQUFPLE1BQU0sS0FBSyxNQUFNLFdBQVcsS0FBSyxVQUFVO0FBQUEsRUFDdEY7QUFDRjtBQU9BLFNBQVMsa0JBQWtCLEtBQWU7QUFDeEMsUUFBTSxPQUFPLE1BQU0sUUFBUSxHQUFHLElBQUksTUFBTSxLQUFLLFFBQVEsS0FBSyxRQUFRLENBQUM7QUFDbkUsUUFBTSxnQkFBZ0IsS0FBSyxJQUFJLENBQUMsU0FBYztBQUFBLElBQzVDLEdBQUc7QUFBQSxJQUNILFNBQVMsSUFBSSxXQUFXLElBQUk7QUFBQSxJQUM1QixZQUFZLElBQUksY0FBYyxJQUFJO0FBQUEsSUFDbEMsU0FBUyxJQUFJLFdBQVcsSUFBSSxTQUFTO0FBQUEsRUFDdkMsRUFBRTtBQUNGLFNBQU8sRUFBRSxlQUFlLE9BQU8sS0FBSyxPQUFPLE1BQU0sS0FBSyxNQUFNLFdBQVcsS0FBSyxVQUFVO0FBQ3hGO0FBTU8sYUFBTSxlQUE2QztBQUFBO0FBQUEsRUFFeEQsNEJBQTRCLEVBQUUsUUFBUSxRQUFRLE1BQU0sK0JBQStCO0FBQUEsRUFDbkYsbUJBQW1CLEVBQUUsUUFBUSxPQUFPLE1BQU0sdUJBQXVCO0FBQUEsRUFDakUscUJBQXFCLEVBQUUsUUFBUSxRQUFRLE1BQU0sdUJBQXVCO0FBQUEsRUFDcEUsb0JBQW9CLEVBQUUsUUFBUSxPQUFPLE1BQU0sd0JBQXdC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