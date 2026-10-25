# React 管理端重写实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 React + TypeScript 1:1 重写 `static/admin.html`（6057 行）到 `/admin/v2`，旧 `/admin` 保留。后端只动 1 个文件（admin.py 新增 1 个路由），其余零改动。

**Architecture:** 前端代码放 `web/admin-react/`，Vite 构建产物输出到 `static/admin-react/`；hash 路由避免后端 fallback；axios 拦截器统一处理 token/401/错误；feature-based 目录结构；Antd 5 + 自定义主题 token 复刻 4 套主题。TDD 范围仅限纯函数层与无 UI 副作用的 hooks，UI 走手工验收。

**Tech Stack:** Vite、React 18、TypeScript、React Router v6（hash 模式）、Ant Design 5、axios、dayjs、vitest、@testing-library/react（仅用于纯逻辑与 hooks）、pnpm。

---

## File Structure

- Create `web/admin-react/package.json` — pnpm 工程清单、依赖、scripts。
- Create `web/admin-react/vite.config.ts` — base、build.outDir (`../static/admin-react`)、server.port=5173、server.proxy `/admin` 与 `/v1` 到 FastAPI 默认 host。
- Create `web/admin-react/tsconfig.json` — strict TS 配置。
- Create `web/admin-react/index.html` — 入口 HTML，挂载 `#root`。
- Create `web/admin-react/src/main.tsx` — 启动入口，注入 ThemeProvider、Router、Antd ConfigProvider。
- Create `web/admin-react/src/App.tsx` — 顶层布局与 AuthGate 包装。
- Create `web/admin-react/src/router.tsx` — hash 路由表。
- Create `web/admin-react/src/api/client.ts` — axios 实例与拦截器。
- Create `web/admin-react/src/api/auth.ts`、`dashboard.ts`、`providers.ts`、`accounts.ts`、`models.ts`、`keys.ts`、`logs.ts`、`config.ts`、`balance.ts`、`docs.ts`、`proxies.ts`、`customProviders.ts`、`requestLogs.ts`、`modelMetadata.ts`、`testModel.ts`、`usageGuide.ts`。
- Create `web/admin-react/src/auth/token.ts` — localStorage + BroadcastChannel 同步。
- Create `web/admin-react/src/auth/AuthGate.tsx` — 未登录跳 `/login`。
- Create `web/admin-react/src/auth/useApi.ts` — 通用数据 hook。
- Create `web/admin-react/src/layout/AppShell.tsx`、`Sidebar.tsx`、`TopBar.tsx`、`PageHeader.tsx`。
- Create `web/admin-react/src/theme/ThemeProvider.tsx`、`themes.ts`、`useTheme.ts`。
- Create `web/admin-react/src/components/Modal.tsx`、`Toast.tsx`、`SearchSelect.tsx`、`KeyValueEditor.tsx`、`JsonEditor.tsx`、`TimeRangePicker.tsx`、`StatusBadge.tsx`、`DataTable.tsx`、`TokenUsageBar.tsx`、`QuotaDimension.tsx`、`JsonTree.tsx`、`SseStreamView.tsx`、`CopyButton.tsx`、`EmptyState.tsx`。
- Create `web/admin-react/src/features/login/LoginPage.tsx`。
- Create `web/admin-react/src/features/dashboard/DashboardPage.tsx`、`useDashboardData.ts`。
- Create `web/admin-react/src/features/channels/ChannelsPage.tsx`、`ChannelCard.tsx`、`ChannelDetail.tsx`、`useChannelForm.ts`、`useCooldownPolicy.ts`、`useLimitPolicy.ts`。
- Create `web/admin-react/src/features/accounts/AccountsPage.tsx`、`AccountList.tsx`、`AccountEdit.tsx`、`useAccountList.ts`。
- Create `web/admin-react/src/features/models/ModelRoutingPage.tsx`、`ModelGroupsPage.tsx`、`ModelMetadataPage.tsx`、`RouteDetailModal.tsx`、`GroupDetailModal.tsx`、`useModelRoutes.ts`、`useModelGroups.ts`。
- Create `web/admin-react/src/features/api-keys/ApiKeysPage.tsx`、`ApiKeyEditModal.tsx`、`useApiKeys.ts`。
- Create `web/admin-react/src/features/request-logs/RequestLogsPage.tsx`、`RequestLogDetail.tsx`、`useRequestLogs.ts`、`logFormatters.ts`。
- Create `web/admin-react/src/features/config/ConfigPage.tsx`、`ProxiesPage.tsx`、`AdminSettingsPage.tsx`。
- Create `web/admin-react/src/features/balance-methods/BalanceMethodsPage.tsx`、`BalanceMethodForm.tsx`、`BalanceMethodTest.tsx`。
- Create `web/admin-react/src/features/custom-providers/CustomChannelCreatePage.tsx`。
- Create `web/admin-react/src/features/test-model/TestModelPage.tsx`。
- Create `web/admin-react/src/features/usage-guide/UsageGuidePage.tsx`。
- Create `web/admin-react/src/features/docs/DocsPage.tsx`（项目文档 tab，对应 `/admin/project-docs`）。
- Create `web/admin-react/src/utils/format.ts`、`requestPath.ts`、`debounce.ts`、`cooldownPolicy.ts`、`httpQuery.ts`。
- Create `web/admin-react/src/utils/__tests__/format.test.ts`、`requestPath.test.ts`、`debounce.test.ts`、`cooldownPolicy.test.ts`、`httpQuery.test.ts`。
- Create `web/admin-react/src/api/__tests__/client.test.ts`。
- Create `web/admin-react/src/auth/__tests__/token.test.ts`。
- Create `web/admin-react/src/features/request-logs/__tests__/logFormatters.test.ts`。
- Create `web/admin-react/src/features/channels/__tests__/useCooldownPolicy.test.ts`。
- Create `web/admin-react/vitest.config.ts` — jsdom 环境、path alias、setup 文件。
- Create `web/admin-react/vitest.setup.ts` — BroadcastChannel mock、localStorage mock。
- Modify `admin.py:649-664` — 新增 `/admin/v2` 与 `/admin/v2/{path:path}` 路由返回 `static/admin-react/index.html`。
- Modify `.gitignore` — 追加 `web/admin-react/node_modules/`、`web/admin-react/dist/`。
- Modify `docs/superpowers/specs/2026-06-01-react-admin-rewrite-design.md` — 不改；本计划链接它。

**测试原则**：vitest 跑纯函数与 hooks（happy-dom/jsdom），UI 走手工验收（`pnpm dev` + 浏览器走查）。本计划**不**为 Antd 组件写组件级测试。

---

### Task 1: 初始化 Vite + React + TS 工程

**Files:**
- Create: `web/admin-react/package.json`
- Create: `web/admin-react/tsconfig.json`
- Create: `web/admin-react/tsconfig.node.json`
- Create: `web/admin-react/vite.config.ts`
- Create: `web/admin-react/index.html`
- Create: `.gitignore`（追加 node_modules/dist）

- [ ] **Step 1: 写工程清单 `web/admin-react/package.json`**

写明以下内容（pnpm 工程，scripts: dev/build/preview/test）：

```json
{
  "name": "model-api-admin-react",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "antd": "^5.20.0",
    "axios": "^1.7.7",
    "dayjs": "^1.11.13",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-hook-form": "^7.53.0",
    "react-router-dom": "^6.26.2",
    "zod": "^3.23.8"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.5.0",
    "@testing-library/react": "^16.0.1",
    "@testing-library/user-event": "^14.5.2",
    "@types/react": "^18.3.5",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "happy-dom": "^15.7.4",
    "typescript": "^5.5.4",
    "vite": "^5.4.6",
    "vitest": "^2.1.1"
  }
}
```

- [ ] **Step 2: 写 TS 配置**

`web/admin-react/tsconfig.json`：

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noImplicitAny": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "allowSyntheticDefaultImports": true,
    "useDefineForClassFields": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["src/*"]
    },
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

`web/admin-react/tsconfig.node.json`：

```json
{
  "compilerOptions": {
    "composite": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["vite.config.ts", "vitest.config.ts"]
}
```

- [ ] **Step 3: 写 Vite 配置**

`web/admin-react/vite.config.ts`：

```ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  base: './',
  build: {
    outDir: path.resolve(__dirname, '../static/admin-react'),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/admin': 'http://127.0.0.1:8000',
      '/v1': 'http://127.0.0.1:8000',
    },
  },
});
```

- [ ] **Step 4: 写入口 HTML**

`web/admin-react/index.html`：

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Model API Admin</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 5: 更新 `.gitignore`**

在文件末尾追加：

```
web/admin-react/node_modules/
web/admin-react/dist/
```

- [ ] **Step 6: 跑 pnpm install 验证工程能起**

Run:
```bash
cd web/admin-react && pnpm install
```

Expected: 安装成功，无 ERR_PNPM_PEER_DEP_ISSUES 阻断。

- [ ] **Step 7: 提交**

```bash
git add web/admin-react admin.py docs/superpowers/specs/2026-06-01-react-admin-rewrite-design.md .gitignore 2>/dev/null
git add web/admin-react/package.json web/admin-react/tsconfig.json web/admin-react/tsconfig.node.json web/admin-react/vite.config.ts web/admin-react/index.html .gitignore
git commit -m "feat: scaffold react admin vite project"
```

Note: admin.py 与 spec 本任务暂不动，本任务只 init 工程。

---

### Task 2: vitest + 工具纯函数 + 第一个测试

**Files:**
- Create: `web/admin-react/vitest.config.ts`
- Create: `web/admin-react/vitest.setup.ts`
- Create: `web/admin-react/src/utils/format.ts`
- Create: `web/admin-react/src/utils/__tests__/format.test.ts`

- [ ] **Step 1: 写 vitest 配置**

`web/admin-react/vitest.config.ts`：

```ts
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  test: {
    globals: true,
    environment: 'happy-dom',
    setupFiles: ['./vitest.setup.ts'],
  },
});
```

- [ ] **Step 2: 写 vitest setup**

`web/admin-react/vitest.setup.ts`：

```ts
import '@testing-library/jest-dom/vitest';

if (typeof globalThis.BroadcastChannel === 'undefined') {
  class MockBC {
    onmessage: ((ev: MessageEvent) => void) | null = null;
    listeners = new Set<(ev: MessageEvent) => void>();
    postMessage(data: unknown) {
      // no-op for tests
    }
    addEventListener(_: string, fn: (ev: MessageEvent) => void) {
      this.listeners.add(fn);
    }
    removeEventListener(_: string, fn: (ev: MessageEvent) => void) {
      this.listeners.delete(fn);
    }
    close() {}
  }
  (globalThis as unknown as { BroadcastChannel: typeof MockBC }).BroadcastChannel = MockBC as unknown as typeof BroadcastChannel;
}
```

- [ ] **Step 3: 写失败测试 `format.test.ts`**

`web/admin-react/src/utils/__tests__/format.test.ts`：

```ts
import { describe, it, expect } from 'vitest';
import { formatCount, formatTokenLimit, formatCooldownRemaining, formatPercent, formatDuration } from '../format';

describe('formatCount', () => {
  it('formats small numbers as-is', () => {
    expect(formatCount(0)).toBe('0');
    expect(formatCount(123)).toBe('123');
  });
  it('abbreviates thousands to k', () => {
    expect(formatCount(1500)).toBe('1.5k');
    expect(formatCount(12000)).toBe('12k');
  });
  it('abbreviates millions to m', () => {
    expect(formatCount(2_300_000)).toBe('2.3m');
  });
  it('handles null/undefined as dash', () => {
    expect(formatCount(null)).toBe('-');
    expect(formatCount(undefined)).toBe('-');
  });
});

describe('formatTokenLimit', () => {
  it('returns dash for 0', () => {
    expect(formatTokenLimit(0)).toBe('-');
  });
  it('formats positive numbers with locale', () => {
    expect(formatTokenLimit(1234567)).toBe('1,234,567');
  });
});

describe('formatCooldownRemaining', () => {
  it('returns dash for null', () => {
    expect(formatCooldownRemaining(null)).toBe('-');
  });
  it('returns dash for negative or zero', () => {
    expect(formatCooldownRemaining(0)).toBe('-');
    expect(formatCooldownRemaining(-1)).toBe('-');
  });
  it('returns "当天" for -1 sentinel', () => {
    expect(formatCooldownRemaining(-1, { sentinel: -1, todayLabel: '当天' })).toBe('当天');
  });
  it('formats seconds < 60 as Ns', () => {
    expect(formatCooldownRemaining(45)).toBe('45s');
  });
  it('formats minutes when >= 60', () => {
    expect(formatCooldownRemaining(120)).toBe('2m');
    expect(formatCooldownRemaining(3600)).toBe('60m');
  });
});

describe('formatPercent', () => {
  it('rounds to one decimal by default', () => {
    expect(formatPercent(0.123)).toBe('12.3%');
  });
  it('returns 0% for non-finite', () => {
    expect(formatPercent(NaN)).toBe('0%');
    expect(formatPercent(Infinity)).toBe('0%');
  });
});

describe('formatDuration', () => {
  it('formats milliseconds under 1000 as Nms', () => {
    expect(formatDuration(250)).toBe('250ms');
  });
  it('formats seconds when >= 1000', () => {
    expect(formatDuration(1500)).toBe('1.5s');
    expect(formatDuration(60_000)).toBe('60.0s');
  });
});
```

- [ ] **Step 4: 跑测试确认失败**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/format.test.ts`
Expected: 失败，错误 `Cannot find module '../format'`。

- [ ] **Step 5: 实现 `format.ts`**

`web/admin-react/src/utils/format.ts`：

```ts
export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '-';
  const abs = Math.abs(n);
  if (abs < 1000) return String(n);
  if (abs < 1_000_000) return `${(n / 1000).toFixed(abs < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}m`;
}

export function formatTokenLimit(n: number | null | undefined): string {
  if (n === null || n === undefined || !n) return '-';
  return n.toLocaleString('en-US');
}

export function formatCooldownRemaining(
  seconds: number | null | undefined,
  opts: { sentinel?: number; todayLabel?: string } = {}
): string {
  const { sentinel = -1, todayLabel = '当天' } = opts;
  if (seconds === null || seconds === undefined) return '-';
  if (seconds === sentinel) return todayLabel;
  if (seconds <= 0) return '-';
  if (seconds < 60) return `${seconds}s`;
  return `${Math.round(seconds / 60)}m`;
}

export function formatPercent(value: number, decimals = 1): string {
  if (!Number.isFinite(value)) return '0%';
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/format.test.ts`
Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add web/admin-react/vitest.config.ts web/admin-react/vitest.setup.ts web/admin-react/src/utils
git commit -m "test: add format utility unit tests with vitest"
```

---
---

### Task 3: requestPath 工具 + 测试

**Files:**
- Create: `web/admin-react/src/utils/requestPath.ts`
- Create: `web/admin-react/src/utils/__tests__/requestPath.test.ts`

- [ ] **Step 1: 写失败测试**

`web/admin-react/src/utils/__tests__/requestPath.test.ts`：

```ts
import { describe, it, expect } from 'vitest';
import { parseRetryPath, formatRetryPathSummary } from '../requestPath';

describe('parseRetryPath', () => {
  it('returns empty array for null/undefined/empty', () => {
    expect(parseRetryPath(null)).toEqual([]);
    expect(parseRetryPath(undefined)).toEqual([]);
    expect(parseRetryPath('')).toEqual([]);
  });
  it('parses JSON array of integers', () => {
    expect(parseRetryPath('[12, 34, 56]')).toEqual([12, 34, 56]);
  });
  it('parses JSON array of objects with id', () => {
    expect(parseRetryPath(JSON.stringify([{ id: 1 }, { id: 2 }]))).toEqual([1, 2]);
  });
  it('falls back to comma-separated integers on parse failure', () => {
    expect(parseRetryPath('12,34,56')).toEqual([12, 34, 56]);
  });
  it('returns empty array for garbage input', () => {
    expect(parseRetryPath('not a list')).toEqual([]);
  });
});

describe('formatRetryPathSummary', () => {
  it('returns dash for empty', () => {
    expect(formatRetryPathSummary([])).toBe('-');
  });
  it('formats single entry with provider and status', () => {
    expect(formatRetryPathSummary([{ provider: 'openai', status: 200 }])).toBe('openai · 200');
  });
  it('joins multiple entries with arrow', () => {
    const summary = formatRetryPathSummary([
      { provider: 'openai', status: 500 },
      { provider: 'anthropic', status: 200 },
    ]);
    expect(summary).toBe('openai · 500 → anthropic · 200');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/requestPath.test.ts`
Expected: 失败 `Cannot find module '../requestPath'`。

- [ ] **Step 3: 实现 `requestPath.ts`**

```ts
export interface RetryPathEntry {
  id?: number;
  provider?: string;
  account?: string;
  model?: string;
  status?: number | null;
  duration_ms?: number;
  request_id?: string;
}

export function parseRetryPath(raw: string | null | undefined): number[] {
  if (!raw) return [];
  const text = raw.trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed
        .map((item) => (typeof item === 'number' ? item : item?.id))
        .filter((id): id is number => typeof id === 'number');
    }
  } catch {
    // fall through
  }
  const matches = text.match(/\d+/g);
  return matches ? matches.map(Number) : [];
}

export function formatRetryPathSummary(entries: RetryPathEntry[]): string {
  if (!entries.length) return '-';
  return entries
    .map((e) => `${e.provider ?? '?'} · ${e.status ?? '-'}`)
    .join(' → ');
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/requestPath.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add web/admin-react/src/utils/requestPath.ts web/admin-react/src/utils/__tests__/requestPath.test.ts
git commit -m "feat(utils): add request path parser and summary"
```

---

### Task 4: debounce + httpQuery 工具 + 测试

**Files:**
- Create: `web/admin-react/src/utils/debounce.ts`
- Create: `web/admin-react/src/utils/httpQuery.ts`
- Create: `web/admin-react/src/utils/__tests__/debounce.test.ts`
- Create: `web/admin-react/src/utils/__tests__/httpQuery.test.ts`

- [ ] **Step 1: 写失败测试 `debounce.test.ts`**

```ts
import { describe, it, expect, vi } from 'vitest';
import { debounce } from '../debounce';

describe('debounce', () => {
  it('delays invocation until wait elapses', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const d = debounce(fn, 200);
    d('a');
    d('b');
    d('c');
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(199);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith('c');
    vi.useRealTimers();
  });
});
```

- [ ] **Step 2: 写失败测试 `httpQuery.test.ts`**

```ts
import { describe, it, expect } from 'vitest';
import { buildQuery, parseQueryNumber } from '../httpQuery';

describe('buildQuery', () => {
  it('omits empty/null/undefined', () => {
    expect(buildQuery({ a: 'x', b: '', c: null, d: undefined })).toBe('a=x');
  });
  it('joins multiple values with &', () => {
    expect(buildQuery({ a: 1, b: 2 })).toBe('a=1&b=2');
  });
  it('encodes values', () => {
    expect(buildQuery({ q: 'a b/c' })).toBe('q=a%20b%2Fc');
  });
});

describe('parseQueryNumber', () => {
  it('returns null for null/undefined/empty', () => {
    expect(parseQueryNumber(null)).toBeNull();
    expect(parseQueryNumber('')).toBeNull();
  });
  it('parses valid number', () => {
    expect(parseQueryNumber('42')).toBe(42);
  });
  it('returns null for non-numeric', () => {
    expect(parseQueryNumber('abc')).toBeNull();
  });
});
```

- [ ] **Step 3: 跑测试确认都失败**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/debounce.test.ts src/utils/__tests__/httpQuery.test.ts`
Expected: 两个文件都报 module not found。

- [ ] **Step 4: 实现 `debounce.ts`**

```ts
export function debounce<TArgs extends unknown[]>(
  fn: (...args: TArgs) => void,
  wait: number
): (...args: TArgs) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: TArgs) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, wait);
  };
}
```

- [ ] **Step 5: 实现 `httpQuery.ts`**

```ts
export function buildQuery(params: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.join('&');
}

export function parseQueryNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
```

- [ ] **Step 6: 跑测试确认全部 PASS**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/debounce.test.ts src/utils/__tests__/httpQuery.test.ts`
Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add web/admin-react/src/utils/debounce.ts web/admin-react/src/utils/httpQuery.ts web/admin-react/src/utils/__tests__/debounce.test.ts web/admin-react/src/utils/__tests__/httpQuery.test.ts
git commit -m "feat(utils): add debounce and http query helpers"
```

---

### Task 5: 冷却策略纯函数 + 测试

**Files:**
- Create: `web/admin-react/src/utils/cooldownPolicy.ts`
- Create: `web/admin-react/src/utils/__tests__/cooldownPolicy.test.ts`

- [ ] **Step 1: 写失败测试**

```ts
import { describe, it, expect } from 'vitest';
import { collectCooldownPolicy, parseCooldownCell, summarizeCooldownPolicy } from '../cooldownPolicy';

describe('parseCooldownCell', () => {
  it('returns error sentinel for empty/whitespace/error', () => {
    expect(parseCooldownCell('')).toEqual({ codes: 'error' });
    expect(parseCooldownCell('error')).toEqual({ codes: 'error' });
  });
  it('parses comma-separated numeric codes', () => {
    expect(parseCooldownCell('429,500')).toEqual({ codes: [429, 500] });
  });
  it('parses single code', () => {
    expect(parseCooldownCell('503')).toEqual({ codes: [503] });
  });
});

describe('collectCooldownPolicy', () => {
  it('always includes error entry', () => {
    const out = collectCooldownPolicy([]);
    expect(out).toEqual([{ codes: 'error', cooldown: 60 }]);
  });
  it('keeps only valid numeric codes', () => {
    const out = collectCooldownPolicy([
      { codes: '429,500', cooldown: 120 },
    ]);
    expect(out).toEqual([
      { codes: [429, 500], cooldown: 120 },
      { codes: 'error', cooldown: 60 },
    ]);
  });
});

describe('summarizeCooldownPolicy', () => {
  it('returns "默认" when empty', () => {
    expect(summarizeCooldownPolicy([])).toBe('默认');
  });
  it('joins first three entries with bullet', () => {
    const summary = summarizeCooldownPolicy([
      { codes: [429], cooldown: 60 },
      { codes: [500, 502], cooldown: 30 },
    ]);
    expect(summary).toContain('429');
    expect(summary).toContain('500,502');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/cooldownPolicy.test.ts`
Expected: module not found。

- [ ] **Step 3: 实现 `cooldownPolicy.ts`**

```ts
export interface CooldownEntry {
  codes: number[] | 'error';
  cooldown: number;
}

export function parseCooldownCell(raw: string): { codes: number[] | 'error' } {
  const text = raw.trim();
  if (!text || text.toLowerCase() === 'error') {
    return { codes: 'error' };
  }
  const codes = text
    .split(',')
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n));
  return { codes: codes.length ? codes : 'error' };
}

export function collectCooldownPolicy(
  rows: Array<{ codes: string; cooldown: number | string }>
): CooldownEntry[] {
  const entries: CooldownEntry[] = rows
    .map((row) => {
      const parsed = parseCooldownCell(row.codes);
      const cooldown = Number(row.cooldown) || 0;
      return { codes: parsed.codes, cooldown };
    });
  entries.push({ codes: 'error', cooldown: 60 });
  return entries;
}

export function summarizeCooldownPolicy(entries: CooldownEntry[]): string {
  if (!entries.length) return '默认';
  return entries
    .slice(0, 3)
    .map((e) => (Array.isArray(e.codes) ? e.codes.join(',') : 'error'))
    .join(' · ');
}
```

- [ ] **Step 4: 跑测试确认 PASS**

Run: `cd web/admin-react && pnpm test src/utils/__tests__/cooldownPolicy.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add web/admin-react/src/utils/cooldownPolicy.ts web/admin-react/src/utils/__tests__/cooldownPolicy.test.ts
git commit -m "feat(utils): add cooldown policy parser and summarizer"
```

---

### Task 6: axios client + token 存储 + 测试

**Files:**
- Create: `web/admin-react/src/api/client.ts`
- Create: `web/admin-react/src/auth/token.ts`
- Create: `web/admin-react/src/api/__tests__/client.test.ts`
- Create: `web/admin-react/src/auth/__tests__/token.test.ts`

- [ ] **Step 1: 写失败测试 `client.test.ts`**

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AxiosHeaders } from 'axios';
import { createApiClient } from '../client';

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe('api client interceptors', () => {
  it('attaches Authorization header from localStorage', () => {
    localStorage.setItem('adminToken', 'tok-123');
    const client = createApiClient({ onUnauthorized: () => {} });
    const cfg = { headers: new AxiosHeaders() } as any;
    client.interceptors.request.handlers[0].fulfilled(cfg);
    expect(cfg.headers.get('Authorization')).toBe('Bearer tok-123');
  });

  it('clears token and invokes onUnauthorized on 401', () => {
    localStorage.setItem('adminToken', 'tok-123');
    const onUnauth = vi.fn();
    const client = createApiClient({ onUnauthorized: onUnauth });
    const error = { response: { status: 401 } } as any;
    return client.interceptors.response.handlers[0].rejected(error).then(
      () => { throw new Error('should reject'); },
      (e: unknown) => {
        expect(onUnauth).toHaveBeenCalled();
        expect(localStorage.getItem('adminToken')).toBeNull();
        expect(e).toBe(error);
      }
    );
  });
});
```

- [ ] **Step 2: 写失败测试 `token.test.ts`**

```ts
import { describe, it, expect, beforeEach } from 'vitest';
import { getStoredToken, persistToken, clearToken } from '../token';

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe('token storage', () => {
  it('returns empty string when nothing stored', () => {
    expect(getStoredToken()).toBe('');
  });
  it('persists to localStorage when remember=true', () => {
    persistToken('abc', true);
    expect(localStorage.getItem('adminToken')).toBe('abc');
    expect(localStorage.getItem('adminRemember')).toBe('1');
    expect(sessionStorage.getItem('adminToken')).toBeNull();
  });
  it('persists to sessionStorage when remember=false', () => {
    persistToken('abc', false);
    expect(sessionStorage.getItem('adminToken')).toBe('abc');
    expect(localStorage.getItem('adminToken')).toBeNull();
  });
  it('clears from both storages', () => {
    persistToken('abc', true);
    clearToken();
    expect(getStoredToken()).toBe('');
  });
});
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd web/admin-react && pnpm test src/api/__tests__/client.test.ts src/auth/__tests__/token.test.ts`
Expected: 两个都 module not found。

- [ ] **Step 4: 实现 `token.ts`**

```ts
const TOKEN_KEY = 'adminToken';
const REMEMBER_KEY = 'adminRemember';
const CHANNEL_NAME = 'admin-auth';

let channel: BroadcastChannel | null = null;
function getChannel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === 'undefined') return null;
  if (!channel) channel = new BroadcastChannel(CHANNEL_NAME);
  return channel;
}

export function getStoredToken(): string {
  return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || '';
}

export function persistToken(token: string, remember: boolean): void {
  if (remember) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(REMEMBER_KEY, '1');
    sessionStorage.removeItem(TOKEN_KEY);
  } else {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REMEMBER_KEY);
    sessionStorage.setItem(TOKEN_KEY, token);
  }
  getChannel()?.postMessage({ type: 'login', token, remember });
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REMEMBER_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  getChannel()?.postMessage({ type: 'logout' });
}

export function requestTokenFromPeers(timeoutMs = 300): Promise<{ token: string; remember: boolean } | null> {
  return new Promise((resolve) => {
    const ch = getChannel();
    if (!ch) return resolve(null);
    let done = false;
    const finish = (v: { token: string; remember: boolean } | null) => {
      if (done) return;
      done = true;
      ch.removeEventListener('message', handler);
      resolve(v);
    };
    const handler = (ev: MessageEvent) => {
      const data = ev.data as { type?: string; token?: string; remember?: boolean } | null;
      if (data?.type === 'token-response' && data.token) {
        finish({ token: data.token, remember: !!data.remember });
      }
    };
    ch.addEventListener('message', handler);
    ch.postMessage({ type: 'token-request' });
    setTimeout(() => finish(null), timeoutMs);
  });
}

export function onPeerAuthMessage(handler: (data: { type: string; token?: string; remember?: boolean }) => void): () => void {
  const ch = getChannel();
  if (!ch) return () => {};
  const fn = (ev: MessageEvent) => handler(ev.data as { type: string; token?: string; remember?: boolean });
  ch.addEventListener('message', fn);
  return () => ch.removeEventListener('message', fn);
}
```

- [ ] **Step 5: 实现 `client.ts`**

```ts
import axios, { AxiosError, AxiosHeaders, AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { clearToken, getStoredToken } from '../auth/token';

export interface ApiClientOptions {
  onUnauthorized: () => void;
  onError?: (message: string) => void;
}

export function createApiClient(opts: ApiClientOptions): AxiosInstance {
  const instance = axios.create({ baseURL: '' });

  instance.interceptors.request.use((config: InternalAxiosRequestConfig) => {
    const token = getStoredToken();
    if (token) {
      if (!config.headers) config.headers = new AxiosHeaders();
      (config.headers as AxiosHeaders).set('Authorization', `Bearer ${token}`);
    }
    return config;
  });

  instance.interceptors.response.use(
    (r) => r,
    (err: AxiosError) => {
      if (err.response?.status === 401) {
        clearToken();
        opts.onUnauthorized();
      } else {
        const detail = (err.response?.data as { detail?: string; message?: string } | undefined)?.detail
          || (err.response?.data as { message?: string } | undefined)?.message
          || err.message;
        opts.onError?.(String(detail));
      }
      return Promise.reject(err);
    }
  );

  return instance;
}

export const api = createApiClient({
  onUnauthorized: () => {
    if (typeof window !== 'undefined' && window.location.hash !== '#/login') {
      window.location.hash = '#/login';
    }
  },
  onError: (msg) => {
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('app:toast', { detail: { type: 'error', message: msg } }));
    }
  },
});
```

- [ ] **Step 6: 跑测试确认 PASS**

Run: `cd web/admin-react && pnpm test src/api/__tests__/client.test.ts src/auth/__tests__/token.test.ts`
Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add web/admin-react/src/api/client.ts web/admin-react/src/auth/token.ts web/admin-react/src/api/__tests__/client.test.ts web/admin-react/src/auth/__tests__/token.test.ts
git commit -m "feat(api): add axios client with token interceptor and storage"
```

---

### Task 7: API 封装（auth、dashboard、providers、accounts、keys、proxies、customProviders、balance、config、modelMetadata、docs、logs、testModel）

**Files:**
- Create: `web/admin-react/src/api/auth.ts`
- Create: `web/admin-react/src/api/dashboard.ts`
- Create: `web/admin-react/src/api/providers.ts`
- Create: `web/admin-react/src/api/accounts.ts`
- Create: `web/admin-react/src/api/keys.ts`
- Create: `web/admin-react/src/api/proxies.ts`
- Create: `web/admin-react/src/api/customProviders.ts`
- Create: `web/admin-react/src/api/balance.ts`
- Create: `web/admin-react/src/api/config.ts`
- Create: `web/admin-react/src/api/modelMetadata.ts`
- Create: `web/admin-react/src/api/docs.ts`
- Create: `web/admin-react/src/api/logs.ts`
- Create: `web/admin-react/src/api/testModel.ts`

- [ ] **Step 1: auth.ts**

```ts
import { api } from './client';
export async function login(password: string) {
  const r = await api.post<{ token: string }>('/admin/login', { password });
  return r.data;
}
export async function logout() {
  await api.post('/admin/logout');
}
export async function checkAuth(token: string) {
  const r = await api.get<{ ok: boolean }>('/admin/check-auth', { headers: { Authorization: `Bearer ${token}` } });
  return r.data;
}
export async function changePassword(oldPassword: string, newPassword: string) {
  await api.post('/admin/change-password', { old_password: oldPassword, new_password: newPassword });
}
```

- [ ] **Step 2: dashboard.ts**

```ts
import { api } from './client';
import { buildQuery } from '../utils/httpQuery';

export async function getDashboardStats(params: { start?: string; end?: string; grain?: string; provider?: string; model?: string }) {
  const qs = buildQuery(params as Record<string, unknown>);
  const r = await api.get(`/admin/dashboard-stats${qs ? `?${qs}` : ''}`);
  return r.data;
}
export async function getRecentLogs(limit = 20) {
  const r = await api.get(`/admin/recent-logs?limit=${limit}`);
  return r.data;
}
export async function getStats(opts?: { includeTokenStats?: boolean }) {
  const qs = opts?.includeTokenStats ? '?include_token_stats=true' : '';
  const r = await api.get(`/admin/stats${qs}`);
  return r.data;
}
```

- [ ] **Step 3: providers.ts**

```ts
import { api } from './client';

export async function listProviders() { return (await api.get('/admin/providers')).data; }
export async function getProviderBase(name: string) { return (await api.get(`/admin/providers/${encodeURIComponent(name)}/base`)).data; }
export async function getProviderAccounts(name: string) { return (await api.get(`/admin/providers/${encodeURIComponent(name)}/accounts`)).data; }
export async function getProviderModels(name: string) { return (await api.get(`/admin/providers/${encodeURIComponent(name)}/models`)).data; }
export async function getProviderConfig(name: string) { return (await api.get(`/admin/config/providers/${encodeURIComponent(name)}`)).data; }
export async function putProviderCustom(name: string, data: unknown) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/custom`, data)).data; }
export async function setProviderEnabled(name: string, enabled: boolean) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/enabled?enabled=${enabled}`)).data; }
export async function setProviderClearConv(name: string, enabled: boolean, maxAgeHours: number) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/clear-conversation?enabled=${enabled}&max_age_hours=${maxAgeHours}`)).data; }
export async function putProviderModels(name: string, models: unknown) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/models`, { models })).data; }
export async function postProviderModel(name: string, upstreamModelId: string, modelId?: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/models`, { upstream_model_id: upstreamModelId, model_id: modelId || upstreamModelId })).data; }
export async function deleteProviderModel(name: string, upstreamId: string) { return (await api.delete(`/admin/providers/${encodeURIComponent(name)}/models/${encodeURIComponent(upstreamId)}`)).data; }
export async function healthCheck(name: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/health-check`)).data; }
export async function refreshModels(name: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/refresh-models`)).data; }
export async function deleteProvider(name: string) { return (await api.delete(`/admin/providers/${encodeURIComponent(name)}`)).data; }
export async function addAccount(name: string, data: unknown) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/accounts/add`, data)).data; }
export async function updateAccount(name: string, username: string, data: unknown) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}`, data)).data; }
export async function deleteAccount(name: string, username: string) { return (await api.delete(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}`)).data; }
export async function switchAccount(name: string, username: string) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}/switch`)).data; }
export async function testAccount(name: string, body: unknown) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/accounts/test`, body)).data; }
export async function checkAccounts(name: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/check-accounts`)).data; }
export async function getDuplicateTokens(name: string) { return (await api.get(`/admin/providers/${encodeURIComponent(name)}/accounts/duplicate-tokens`)).data; }
export async function getAccountBalance(name: string, username: string) { return (await api.get(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}/balance`)).data; }
export async function checkAccountBalance(name: string, username: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}/check-balance`)).data; }
export async function clearAccountBalanceCache(name: string, username: string) { return (await api.delete(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}/balance-cache`)).data; }
export async function initAccount(name: string, username: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}/init`)).data; }
export async function clearAccountCooldown(name: string, username: string) { return (await api.post(`/admin/providers/${encodeURIComponent(name)}/accounts/${encodeURIComponent(username)}/clear-cooldown`)).data; }
```

- [ ] **Step 4: accounts.ts**

```ts
import { api } from './client';
export async function getDailyUsage(username: string, provider = '') {
  const qp = provider ? `?provider=${encodeURIComponent(provider)}` : '';
  return (await api.get(`/admin/accounts/${encodeURIComponent(username)}/daily-usage${qp}`)).data;
}
```

- [ ] **Step 5: keys.ts**

```ts
import { api } from './client';
export async function listApiKeys() { return (await api.get('/admin/config/api-keys')).data; }
export async function setApiKeysEnabled(enabled: boolean) { return (await api.put(`/admin/config/api-keys/enabled?enabled=${enabled}`)).data; }
export async function setApiKeyDisabled(keyId: string, disabled: boolean) { return (await api.put(`/admin/config/api-keys/${keyId}/disable?disabled=${disabled}`)).data; }
export async function addApiKey(data: unknown) { return (await api.put('/admin/config/api-keys/add', data)).data; }
export async function updateApiKey(keyId: string, data: unknown) { return (await api.put(`/admin/config/api-keys/${keyId}`, data)).data; }
export async function deleteApiKey(keyId: string) { return (await api.delete(`/admin/config/api-keys/${keyId}`)).data; }
export async function getApiKeyUsage() { return (await api.get('/admin/config/api-keys/usage')).data; }
```

- [ ] **Step 6: proxies.ts**

```ts
import { api } from './client';
export async function getProxies() { return (await api.get('/admin/config/proxies')).data; }
export async function putProxies(data: unknown) { return (await api.put('/admin/config/proxies', data)).data; }
```

- [ ] **Step 7: customProviders.ts**

```ts
import { api } from './client';
export async function listCustomProviders() { return (await api.get('/admin/custom-providers')).data; }
export async function createCustomProvider(data: unknown) { return (await api.post('/admin/custom-providers', data)).data; }
export async function fetchModels(body: unknown) { return (await api.post('/admin/fetch-models', body)).data; }
```

- [ ] **Step 8: balance.ts**

```ts
import { api } from './client';
export async function listBalanceMethods() { return (await api.get('/admin/balance-methods')).data; }
export async function createBalanceMethod(data: unknown) { return (await api.post('/admin/balance-methods', data)).data; }
export async function updateBalanceMethod(id: number | string, data: unknown) { return (await api.put(`/admin/balance-methods/${id}`, data)).data; }
export async function deleteBalanceMethod(id: number | string) { return (await api.delete(`/admin/balance-methods/${id}`)).data; }
export async function testBalanceMethod(id: number | string, body: { provider_name: string; username?: string }) { return (await api.post(`/admin/balance-methods/${id}/test`, body)).data; }
export async function getLimitPolicy(name: string) { return (await api.get(`/admin/providers/${encodeURIComponent(name)}/limit-policy`)).data; }
export async function putLimitPolicy(name: string, data: unknown) { return (await api.put(`/admin/providers/${encodeURIComponent(name)}/limit-policy`, data)).data; }
```

- [ ] **Step 9: config.ts**

```ts
import { api } from './client';
export async function getMainConfig() { return (await api.get('/admin/config/main')).data; }
export async function putMainConfig(data: unknown) { return (await api.put('/admin/config/main', data)).data; }
export async function getAdminConfig() { return (await api.get('/admin/config/admin')).data; }
export async function putSessionDuration(hours: number) { return (await api.put(`/admin/config/admin/session?duration_hours=${hours}`)).data; }
export async function putServer(host: string, port: number) { return (await api.put(`/admin/config/main/server?host=${encodeURIComponent(host)}&port=${port}`)).data; }
export async function putLogging(level: string) { return (await api.put(`/admin/config/main/logging?level=${encodeURIComponent(level)}`)).data; }
export async function putSystem(debug: boolean) { return (await api.put(`/admin/config/main/system?debug=${debug}`)).data; }
export async function putPostgres(data: { host: string; port: number; user: string; password: string; database: string }) {
  return (await api.put(`/admin/config/main/postgres?host=${encodeURIComponent(data.host)}&port=${data.port}&user=${encodeURIComponent(data.user)}&password=${encodeURIComponent(data.password)}&database=${encodeURIComponent(data.database)}`)).data;
}
export async function putModelRefresh(enabled: boolean, intervalMinutes: number) { return (await api.put(`/admin/config/main/model-refresh?enabled=${enabled}&interval_minutes=${intervalMinutes}`)).data; }
export async function putMessageDelete(enabled: boolean, intervalMinutes: number) { return (await api.put(`/admin/config/main/message-delete?enabled=${enabled}&interval_minutes=${intervalMinutes}`)).data; }
export async function putLogRetention(maxEntries: number) { return (await api.put(`/admin/config/main/log-retention?max_entries=${maxEntries}`)).data; }
export async function putRedis(data: unknown) { return (await api.put('/admin/config/main/redis', data)).data; }
export async function putRetry(maxRetries: number) { return (await api.put(`/admin/config/main/retry?max_retries=${maxRetries}`)).data; }
export async function putStream(data: unknown) { return (await api.put('/admin/config/main/stream', data)).data; }
export async function putRateLimit(data: unknown) { return (await api.put('/admin/config/main/rate-limit', data)).data; }
export async function getTokenizerRules() { return (await api.get('/admin/config/main/tokenizer-rules')).data; }
export async function putTokenizerRules(data: unknown) { return (await api.put('/admin/config/main/tokenizer-rules', data)).data; }
```

- [ ] **Step 10: modelMetadata.ts**

```ts
import { api } from './client';
export async function listModelMetadata() { return (await api.get('/admin/model-metadata')).data; }
export async function putModelMetadata(id: string, data: unknown) { return (await api.put(`/admin/model-metadata/${encodeURIComponent(id)}`, data)).data; }
export async function deleteModelMetadata(id: string) { return (await api.delete(`/admin/model-metadata/${encodeURIComponent(id)}`)).data; }
export async function putDefaultModelMetadata(data: unknown) { return (await api.put('/admin/model-metadata/default', data)).data; }
export async function fetchOpenRouter() { return (await api.get('/admin/model-metadata/openrouter/fetch')).data; }
export async function syncOpenRouter(ids: string[]) { return (await api.post('/admin/model-metadata/openrouter/sync', { model_ids: ids })).data; }
export async function fetchModelsDev() { return (await api.get('/admin/model-metadata/modelsdev/fetch')).data; }
export async function syncModelsDev(ids: string[]) { return (await api.post('/admin/model-metadata/modelsdev/sync', { model_ids: ids })).data; }
```

- [ ] **Step 11: docs.ts**

```ts
import { api } from './client';
export async function listDocs() { return (await api.get('/project-docs')).data; }
export async function getDoc(path: string) { return (await api.get(`/project-docs/raw/${path}`)).data; }
```

- [ ] **Step 12: logs.ts**

```ts
import { api } from './client';
import { buildQuery } from '../utils/httpQuery';

export async function listRequestLogs(params: Record<string, unknown> & { page?: number; page_size?: number }) {
  const qs = buildQuery(params as Record<string, unknown>);
  return (await api.get(`/admin/request-logs${qs ? `?${qs}` : ''}`)).data;
}
export async function getRequestLogDetail(id: string | number) {
  return (await api.get(`/admin/request-logs/${id}`)).data;
}
```

- [ ] **Step 13: testModel.ts**

```ts
import { api } from './client';
export async function listV1Models(token: string) {
  return (await api.get('/v1/models', { headers: { Authorization: `Bearer ${token}` } })).data;
}
export async function chatCompletions(token: string, body: unknown) {
  return (await api.post('/v1/chat/completions', body, { headers: { Authorization: `Bearer ${token}` } })).data;
}
```

- [ ] **Step 14: 提交**

```bash
git add web/admin-react/src/api
git commit -m "feat(api): add admin api client modules for all endpoints"
```

---

### Task 8: models API（路由 + 模型组）+ useApi hook + AuthGate

**Files:**
- Create: `web/admin-react/src/api/models.ts`
- Create: `web/admin-react/src/auth/useApi.ts`
- Create: `web/admin-react/src/auth/AuthGate.tsx`

- [ ] **Step 1: models.ts**

```ts
import { api } from './client';
export async function getModelRouting() { return (await api.get('/admin/model-routing')).data; }
export async function putModelRouting(data: unknown) { return (await api.put('/admin/model-routing', data)).data; }
export async function createModelRoute(data: unknown) { return (await api.post('/admin/model-routes', data)).data; }
export async function updateModelRoute(id: string, data: unknown) { return (await api.put(`/admin/model-routes/${encodeURIComponent(id)}`, data)).data; }
export async function deleteModelRoute(id: string) { return (await api.delete(`/admin/model-routes/${encodeURIComponent(id)}`)).data; }
export async function createModelGroup(data: unknown) { return (await api.post('/admin/model-groups', data)).data; }
export async function updateModelGroup(name: string, data: unknown) { return (await api.put(`/admin/model-groups/${encodeURIComponent(name)}`, data)).data; }
export async function deleteModelGroup(name: string) { return (await api.delete(`/admin/model-groups/${encodeURIComponent(name)}`)).data; }
```

- [ ] **Step 2: useApi.ts**

```ts
import { useCallback, useEffect, useState } from 'react';

export interface UseApiResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refetch: () => Promise<void>;
}

export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = []): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fn();
      setData(r);
    } catch (e: unknown) {
      const message = (e as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail
        || (e as { message?: string })?.message
        || '请求失败';
      setError(String(message));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => { void run(); }, [run]);

  return { data, error, loading, refetch: run };
}
```

- [ ] **Step 3: AuthGate.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { getStoredToken, onPeerAuthMessage, persistToken, requestTokenFromPeers } from './token';

export function AuthGate({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const [token, setToken] = useState<string | null>(getStoredToken() || null);
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    if (token) { setResolved(true); return; }
    let mounted = true;
    requestTokenFromPeers().then((peer) => {
      if (!mounted) return;
      if (peer) {
        persistToken(peer.token, peer.remember);
        setToken(peer.token);
      }
      setResolved(true);
    });
    const off = onPeerAuthMessage((data) => {
      if (data.type === 'login' && data.token) setToken(data.token);
      if (data.type === 'logout') setToken(null);
    });
    return () => { mounted = false; off(); };
  }, [token]);

  if (!resolved) return null;
  if (!token) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}
```

- [ ] **Step 4: 提交**

```bash
git add web/admin-react/src/api/models.ts web/admin-react/src/auth/useApi.ts web/admin-react/src/auth/AuthGate.tsx
git commit -m "feat(api): add model routing/groups api and useApi/AuthGate"
```

---

### Task 9: 主题系统（4 套）+ Toast + Layout

**Files:**
- Create: `web/admin-react/src/theme/themes.ts`
- Create: `web/admin-react/src/theme/ThemeProvider.tsx`
- Create: `web/admin-react/src/theme/useTheme.ts`
- Create: `web/admin-react/src/components/Toast.tsx`
- Create: `web/admin-react/src/layout/AppShell.tsx`
- Create: `web/admin-react/src/layout/Sidebar.tsx`
- Create: `web/admin-react/src/layout/TopBar.tsx`
- Create: `web/admin-react/src/layout/PageHeader.tsx`

- [ ] **Step 1: themes.ts**

```ts
import type { ThemeConfig } from 'antd';

export interface AppTheme {
  key: 'midnight' | 'slate' | 'emerald' | 'light';
  label: string;
  antd: ThemeConfig;
  cssVars: Record<string, string>;
}

const baseAntd: ThemeConfig = {
  token: { fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif', borderRadius: 8 },
};

const darkBase: Record<string, string> = {
  '--bg': '#0b1220',
  '--bg2': '#0f172a',
  '--bg3': '#111827',
  '--surface': '#1e293b',
  '--surface2': '#0b1220',
  '--text': '#cbd5e1',
  '--textH': '#f1f5f9',
  '--text2': '#94a3b8',
  '--border': '#1e293b',
  '--blue': '#60a5fa',
  '--tint': 'rgba(96,165,250,.10)',
  '--hover': 'rgba(255,255,255,.04)',
  '--red': '#f87171',
  '--green': '#34d399',
  '--yellow': '#facc15',
  '--fontM': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  '--radius': '6px',
  '--radiusL': '10px',
};

const lightBase: Record<string, string> = {
  '--bg': '#f5f7fb',
  '--bg2': '#ffffff',
  '--bg3': '#eef1f6',
  '--surface': '#ffffff',
  '--surface2': '#f5f7fb',
  '--text': '#1f2937',
  '--textH': '#0f172a',
  '--text2': '#64748b',
  '--border': '#e2e8f0',
  '--blue': '#2563eb',
  '--tint': 'rgba(37,99,235,.10)',
  '--hover': 'rgba(15,23,42,.04)',
  '--red': '#dc2626',
  '--green': '#059669',
  '--yellow': '#d97706',
  '--fontM': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  '--radius': '6px',
  '--radiusL': '10px',
};

export const themes: AppTheme[] = [
  {
    key: 'midnight',
    label: '深夜黑',
    antd: { ...baseAntd, algorithm: undefined, token: { ...baseAntd.token, colorPrimary: '#60a5fa', colorBgBase: '#0b1220', colorTextBase: '#cbd5e1' } },
    cssVars: darkBase,
  },
  {
    key: 'slate',
    label: '蓝灰深色',
    antd: { ...baseAntd, token: { ...baseAntd.token, colorPrimary: '#7dd3fc', colorBgBase: '#0f172a', colorTextBase: '#e2e8f0' } },
    cssVars: { ...darkBase, '--bg': '#0f172a', '--bg2': '#111827', '--bg3': '#0b1220', '--blue': '#7dd3fc' },
  },
  {
    key: 'emerald',
    label: '墨绿控制台',
    antd: { ...baseAntd, token: { ...baseAntd.token, colorPrimary: '#34d399', colorBgBase: '#0a1f1c', colorTextBase: '#d1fae5' } },
    cssVars: { ...darkBase, '--bg': '#0a1f1c', '--bg2': '#0d2924', '--bg3': '#0a1f1c', '--surface': '#10332c', '--blue': '#34d399', '--tint': 'rgba(52,211,153,.10)' },
  },
  {
    key: 'light',
    label: '明亮浅色',
    antd: { ...baseAntd, token: { ...baseAntd.token, colorPrimary: '#2563eb', colorBgBase: '#ffffff', colorTextBase: '#1f2937' } },
    cssVars: lightBase,
  },
];

export function getTheme(key: string): AppTheme {
  return themes.find((t) => t.key === key) ?? themes[0];
}
```

- [ ] **Step 2: useTheme.ts**

```ts
import { useEffect, useState } from 'react';
import { getTheme, themes, type AppTheme } from './themes';

const KEY = 'adminTheme';

export function useTheme(): { theme: AppTheme; setTheme: (k: string) => void; themes: AppTheme[] } {
  const [theme, setThemeState] = useState<AppTheme>(() => {
    const saved = typeof localStorage !== 'undefined' ? localStorage.getItem(KEY) : null;
    return getTheme(saved || 'midnight');
  });

  useEffect(() => {
    const root = document.documentElement;
    Object.entries(theme.cssVars).forEach(([k, v]) => root.style.setProperty(k, v));
    document.body.dataset.theme = theme.key;
    localStorage.setItem(KEY, theme.key);
  }, [theme]);

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY && e.newValue) setThemeState(getTheme(e.newValue));
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  return { theme, setTheme: (k: string) => setThemeState(getTheme(k)), themes };
}
```

- [ ] **Step 3: ThemeProvider.tsx**

```tsx
import { ConfigProvider } from 'antd';
import { useTheme } from './useTheme';

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const { theme } = useTheme();
  return <ConfigProvider theme={theme.antd}>{children}</ConfigProvider>;
}
```

- [ ] **Step 4: Toast.tsx**

```tsx
import { useEffect, useState } from 'react';

interface Toast { id: number; type: 'success' | 'error' | 'info'; message: string; }

let nextId = 1;

export function showToast(message: string, type: Toast['type'] = 'info') {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('app:toast', { detail: { id: nextId++, type, message } }));
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  useEffect(() => {
    const onToast = (e: Event) => {
      const detail = (e as CustomEvent).detail as Toast;
      setToasts((prev) => [...prev, detail]);
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== detail.id));
      }, 3000);
    };
    window.addEventListener('app:toast', onToast);
    return () => window.removeEventListener('app:toast', onToast);
  }, []);

  return (
    <div style={{ position: 'fixed', right: 20, top: 20, zIndex: 1100, display: 'flex', flexDirection: 'column', gap: 8 }}>
      {toasts.map((t) => (
        <div
          key={t.id}
          style={{
            padding: '10px 14px',
            borderRadius: 8,
            background: t.type === 'error' ? 'var(--red)' : t.type === 'success' ? 'var(--green)' : 'var(--surface)',
            color: 'var(--textH)',
            boxShadow: '0 6px 20px rgba(0,0,0,.25)',
            minWidth: 220,
            fontSize: 13,
          }}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Sidebar.tsx**

```tsx
import { Link, useLocation } from 'react-router-dom';

const items = [
  { path: '/', label: '仪表盘' },
  { path: '/config', label: '主配置' },
  { path: '/api-keys', label: 'API Keys' },
  { path: '/proxies', label: '代理池' },
  { path: '/channels', label: '渠道' },
  { path: '/custom-channel-create', label: '创建自定义渠道' },
  { path: '/balance-methods', label: '余额方式' },
  { path: '/models', label: '模型路由/分组' },
  { path: '/model-metadata', label: '模型广场' },
  { path: '/request-logs', label: '请求日志' },
  { path: '/data-dashboard', label: '数据看板' },
  { path: '/test-model', label: '测试模型' },
  { path: '/usage-guide', label: '使用方法' },
];

export function Sidebar() {
  const location = useLocation();
  return (
    <div style={{ width: 220, background: 'var(--bg2)', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '14px 16px', fontWeight: 700, color: 'var(--textH)' }}>
        <span style={{ display: 'inline-block', width: 8, height: 8, background: 'var(--blue)', borderRadius: 999, marginRight: 8 }} />
        Model API Admin
      </div>
      <nav style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
        {items.map((it) => {
          const active = location.pathname === it.path;
          return (
            <Link
              key={it.path}
              to={it.path}
              style={{
                display: 'block',
                padding: '9px 10px',
                fontSize: 13,
                color: active ? 'var(--blue)' : 'var(--text)',
                background: active ? 'var(--tint)' : 'transparent',
                borderLeft: `3px solid ${active ? 'var(--blue)' : 'transparent'}`,
                borderRadius: 8,
                textDecoration: 'none',
                fontWeight: active ? 700 : 400,
              }}
            >
              {it.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
```

- [ ] **Step 6: TopBar.tsx**

```tsx
import { Dropdown } from 'antd';
import { useTheme } from '../theme/useTheme';
import { clearToken } from '../auth/token';
import { logout } from '../api/auth';

export function TopBar() {
  const { theme, setTheme, themes } = useTheme();
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '10px 16px', borderBottom: '1px solid var(--border)', background: 'var(--bg2)' }}>
      <Dropdown
        menu={{
          items: themes.map((t) => ({ key: t.key, label: t.label })),
          onClick: ({ key }) => setTheme(String(key)),
        }}
      >
        <button style={{ background: 'transparent', border: '1px solid var(--border)', color: 'var(--textH)', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }}>主题: {theme.label}</button>
      </Dropdown>
      <button
        onClick={async () => { try { await logout(); } catch {} clearToken(); window.location.hash = '#/login'; }}
        style={{ background: 'transparent', border: '1px solid var(--border)', color: 'var(--textH)', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }}
      >
        登出
      </button>
    </div>
  );
}
```

- [ ] **Step 7: PageHeader.tsx**

```tsx
export function PageHeader({ title, sub }: { title: string; sub?: string }) {
  return (
    <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'baseline', gap: 12, borderBottom: '1px solid var(--border)', background: 'var(--bg2)' }}>
      <h1 style={{ fontSize: 18, margin: 0, color: 'var(--textH)' }}>{title}</h1>
      {sub && <span style={{ color: 'var(--text2)', fontSize: 12 }}>{sub}</span>}
    </div>
  );
}
```

- [ ] **Step 8: AppShell.tsx**

```tsx
import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { ToastContainer } from '../components/Toast';

export function AppShell() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gridTemplateRows: 'auto 1fr', height: '100vh' }}>
      <div style={{ gridRow: '1 / 3' }}><Sidebar /></div>
      <div style={{ gridColumn: 2 }}><TopBar /></div>
      <main style={{ gridColumn: 2, overflow: 'auto', background: 'var(--bg)' }}>
        <Outlet />
      </main>
      <ToastContainer />
    </div>
  );
}
```

- [ ] **Step 9: 提交**

```bash
git add web/admin-react/src/theme web/admin-react/src/components/Toast.tsx web/admin-react/src/layout
git commit -m "feat(shell): add 4 themes, toast container, sidebar/topbar layout"
```

---

### Task 10: 通用组件（Modal、StatusBadge、DataTable、JsonTree、KeyValueEditor、JsonEditor、TimeRangePicker、SearchSelect、TokenUsageBar、EmptyState、CopyButton）

**Files:**
- Create: `web/admin-react/src/components/Modal.tsx`
- Create: `web/admin-react/src/components/StatusBadge.tsx`
- Create: `web/admin-react/src/components/DataTable.tsx`
- Create: `web/admin-react/src/components/JsonTree.tsx`
- Create: `web/admin-react/src/components/KeyValueEditor.tsx`
- Create: `web/admin-react/src/components/JsonEditor.tsx`
- Create: `web/admin-react/src/components/TimeRangePicker.tsx`
- Create: `web/admin-react/src/components/SearchSelect.tsx`
- Create: `web/admin-react/src/components/TokenUsageBar.tsx`
- Create: `web/admin-react/src/components/EmptyState.tsx`
- Create: `web/admin-react/src/components/CopyButton.tsx`
- Create: `web/admin-react/src/components/SseStreamView.tsx`
- Create: `web/admin-react/src/components/QuotaDimension.tsx`

- [ ] **Step 1: Modal.tsx**

```tsx
import { Modal as AntdModal } from 'antd';
import { useEffect, useState } from 'react';

interface AppModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  onSave?: () => Promise<void> | void;
  saveLabel?: string;
  saveDisabled?: boolean;
  children: React.ReactNode;
  width?: number;
}

export function AppModal({ open, title, onClose, onSave, saveLabel = '保存', saveDisabled, children, width = 640 }: AppModalProps) {
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (!open) setSaving(false); }, [open]);
  return (
    <AntdModal
      open={open}
      title={title}
      onCancel={onClose}
      width={width}
      okText={saveLabel}
      cancelText="取消"
      okButtonProps={{ disabled: saveDisabled, loading: saving }}
      onOk={async () => {
        if (!onSave) { onClose(); return; }
        setSaving(true);
        try { await onSave(); onClose(); } finally { setSaving(false); }
      }}
    >
      {children}
    </AntdModal>
  );
}
```

- [ ] **Step 2: StatusBadge.tsx**

```tsx
export function StatusBadge({ status, success }: { status?: number | null; success?: boolean }) {
  const isSuccess = success ?? (typeof status === 'number' && status >= 200 && status < 300);
  const color = isSuccess ? 'var(--green)' : status === undefined || status === null ? 'var(--text2)' : 'var(--red)';
  return (
    <span style={{ display: 'inline-block', padding: '2px 8px', fontSize: 11, borderRadius: 4, color, background: `color-mix(in srgb, ${color} 15%, transparent)`, fontWeight: 600 }}>
      {status ?? (success ? 'OK' : '-')}
    </span>
  );
}
```

- [ ] **Step 3: DataTable.tsx**

```tsx
import { Empty, Spin } from 'antd';

export interface Column<T> {
  key: string;
  title: React.ReactNode;
  width?: number | string;
  render: (row: T) => React.ReactNode;
}

export function DataTable<T>({ columns, data, loading, rowKey }: { columns: Column<T>[]; data: T[]; loading?: boolean; rowKey: (row: T) => string }) {
  if (loading) return <div style={{ padding: 40, textAlign: 'center' }}><Spin /></div>;
  if (!data.length) return <Empty />;
  return (
    <div style={{ overflow: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'separate', borderSpacing: 0, fontSize: 13 }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} style={{ padding: '10px 12px', textAlign: 'left', background: 'var(--surface2)', color: 'var(--text2)', fontSize: 12, fontWeight: 700, borderBottom: '1px solid var(--border)', width: c.width }}>
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={rowKey(row)} style={{ borderBottom: '1px solid var(--border)' }}>
              {columns.map((c) => (
                <td key={c.key} style={{ padding: '10px 12px', color: 'var(--text)' }}>{c.render(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 4: JsonTree.tsx**

```tsx
import { useState } from 'react';
import { CopyButton } from './CopyButton';

export function JsonTree({ data, name = '' }: { data: unknown; name?: string }) {
  return (
    <div style={{ fontFamily: 'var(--fontM)', fontSize: 12 }}>
      <Node name={name} value={data} depth={0} />
    </div>
  );
}

function Node({ name, value, depth }: { name: string; value: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  if (value === null || value === undefined) return <Row name={name} value={String(value)} depth={depth} />;
  if (typeof value !== 'object') return <Row name={name} value={JSON.stringify(value)} depth={depth} />;
  const isArray = Array.isArray(value);
  const entries = isArray ? value.map((v, i) => [String(i), v] as const) : Object.entries(value as Record<string, unknown>);
  return (
    <div style={{ paddingLeft: depth === 0 ? 0 : 14 }}>
      <div style={{ cursor: 'pointer' }} onClick={() => setOpen((o) => !o)}>
        {open ? '▼' : '▶'} <span style={{ color: 'var(--blue)' }}>{name || (isArray ? '[]' : '{}')}</span> <span style={{ color: 'var(--text2)' }}>{isArray ? `[${entries.length}]` : `{${entries.length}}`}</span>
        <CopyButton text={JSON.stringify(value, null, 2)} />
      </div>
      {open && <div>{entries.map(([k, v]) => <Node key={k} name={isArray ? `[${k}]` : k} value={v} depth={depth + 1} />)}</div>}
    </div>
  );
}

function Row({ name, value, depth }: { name: string; value: string; depth: number }) {
  return (
    <div style={{ paddingLeft: depth === 0 ? 0 : 14 }}>
      <span style={{ color: 'var(--text2)' }}>{name}:</span> <span>{value}</span>
    </div>
  );
}
```

- [ ] **Step 5: KeyValueEditor.tsx**

```tsx
import { Button, Input } from 'antd';

export interface KV { key: string; value: string; }

export function KeyValueEditor({ value, onChange, keyPlaceholder = 'key', valuePlaceholder = 'value' }: { value: KV[]; onChange: (v: KV[]) => void; keyPlaceholder?: string; valuePlaceholder?: string }) {
  const update = (i: number, patch: Partial<KV>) => {
    const next = value.map((row, idx) => (idx === i ? { ...row, ...patch } : row));
    onChange(next);
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {value.map((row, i) => (
        <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 32px', gap: 6 }}>
          <Input value={row.key} placeholder={keyPlaceholder} onChange={(e) => update(i, { key: e.target.value })} />
          <Input value={row.value} placeholder={valuePlaceholder} onChange={(e) => update(i, { value: e.target.value })} />
          <Button onClick={() => onChange(value.filter((_, idx) => idx !== i))} danger>×</Button>
        </div>
      ))}
      <Button onClick={() => onChange([...value, { key: '', value: '' }])}>+ 添加</Button>
    </div>
  );
}
```

- [ ] **Step 6: JsonEditor.tsx**

```tsx
import { Input } from 'antd';
import { useMemo } from 'react';

export function JsonEditor({ value, onChange, rows = 8 }: { value: unknown; onChange: (v: unknown) => void; rows?: number }) {
  const text = useMemo(() => JSON.stringify(value ?? {}, null, 2), [value]);
  return (
    <Input.TextArea
      rows={rows}
      value={text}
      onChange={(e) => {
        try { onChange(JSON.parse(e.target.value)); } catch { /* keep invalid until valid */ }
      }}
      style={{ fontFamily: 'var(--fontM)', fontSize: 12 }}
    />
  );
}
```

- [ ] **Step 7: TimeRangePicker.tsx**

```tsx
import { DatePicker } from 'antd';
import dayjs, { Dayjs } from 'dayjs';

const { RangePicker } = DatePicker;

export interface TimeRangeValue { start: string; end: string; }

export function TimeRangePicker({ value, onChange }: { value: TimeRangeValue; onChange: (v: TimeRangeValue) => void }) {
  return (
    <RangePicker
      showTime={{ format: 'HH:mm' }}
      format="YYYY-MM-DD HH:mm"
      value={[value.start ? dayjs(value.start) : null, value.end ? dayjs(value.end) : null]}
      onChange={(v) => {
        const [s, e] = v as [Dayjs | null, Dayjs | null];
        onChange({ start: s ? s.toISOString() : '', end: e ? e.toISOString() : '' });
      }}
    />
  );
}
```

- [ ] **Step 8: SearchSelect.tsx**

```tsx
import { Input, Tag } from 'antd';
import { useMemo, useState } from 'react';

export interface SearchSelectOption { value: string; label: string; }

export function SearchSelect({ value, options, onChange, mode = 'single', placeholder = '' }: { value: string | string[]; options: SearchSelectOption[]; onChange: (v: string | string[]) => void; mode?: 'single' | 'tags'; placeholder?: string }) {
  const [q, setQ] = useState('');
  const filtered = useMemo(() => options.filter((o) => o.label.toLowerCase().includes(q.toLowerCase()) || o.value.toLowerCase().includes(q.toLowerCase())), [options, q]);
  const selected = Array.isArray(value) ? value : value ? [value] : [];

  const toggle = (v: string) => {
    if (mode === 'tags') {
      onChange(selected.includes(v) ? selected.filter((x) => x !== v) : [...selected, v]);
    } else {
      onChange(v);
    }
  };

  return (
    <div style={{ position: 'relative' }}>
      {mode === 'tags' && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: 4, border: '1px solid var(--border)', borderRadius: 6, minHeight: 32, marginBottom: 6 }}>
          {selected.map((v) => {
            const opt = options.find((o) => o.value === v);
            return (
              <Tag key={v} closable onClose={() => toggle(v)}>{opt?.label ?? v}</Tag>
            );
          })}
        </div>
      )}
      <Input.Search value={q} placeholder={placeholder} onChange={(e) => setQ(e.target.value)} />
      <div style={{ marginTop: 6, maxHeight: 200, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 6 }}>
        {filtered.map((o) => (
          <div key={o.value} onClick={() => toggle(o.value)} style={{ padding: '6px 10px', cursor: 'pointer', background: selected.includes(o.value) ? 'var(--tint)' : 'transparent', color: 'var(--text)', fontSize: 13 }}>
            {o.label}
          </div>
        ))}
        {!filtered.length && <div style={{ padding: 10, color: 'var(--text2)', fontSize: 12 }}>无匹配</div>}
      </div>
    </div>
  );
}
```

- [ ] **Step 9: TokenUsageBar.tsx**

```tsx
import { formatPercent, formatTokenLimit } from '../utils/format';

export function TokenUsageBar({ used, limit }: { used: number; limit: number }) {
  const pct = limit > 0 ? used / limit : 0;
  const color = pct > 0.9 ? 'var(--red)' : pct > 0.7 ? 'var(--yellow)' : 'var(--blue)';
  return (
    <div>
      <div style={{ height: 6, background: 'var(--bg3)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${Math.min(100, pct * 100)}%`, height: '100%', background: color, transition: 'width .3s' }} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text2)', marginTop: 2 }}>{formatTokenLimit(used)} / {formatTokenLimit(limit)} · {formatPercent(pct)}</div>
    </div>
  );
}
```

- [ ] **Step 10: EmptyState.tsx**

```tsx
import { Empty } from 'antd';
export function EmptyState({ tip = '暂无数据' }: { tip?: string }) {
  return <Empty description={tip} />;
}
```

- [ ] **Step 11: CopyButton.tsx**

```tsx
import { message } from 'antd';
export function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation();
        try { await navigator.clipboard.writeText(text); message.success('已复制'); } catch { message.error('复制失败'); }
      }}
      style={{ marginLeft: 8, fontSize: 11, background: 'transparent', border: '1px solid var(--border)', color: 'var(--text2)', padding: '1px 6px', borderRadius: 4, cursor: 'pointer' }}
    >{label}</button>
  );
}
```

- [ ] **Step 12: SseStreamView.tsx**

```tsx
export function SseStreamView({ events }: { events: Array<{ event?: string; data: string }> }) {
  return (
    <pre style={{ background: 'var(--bg)', padding: 10, borderRadius: 6, maxHeight: 320, overflow: 'auto', fontSize: 12, fontFamily: 'var(--fontM)' }}>
      {events.map((e, i) => (
        <div key={i} style={{ borderBottom: '1px solid var(--border)', padding: '2px 0' }}>
          {e.event && <span style={{ color: 'var(--blue)' }}>event: {e.event}{'\n'}</span>}
          <span style={{ color: 'var(--text)' }}>data: {e.data}</span>
        </div>
      ))}
    </pre>
  );
}
```

- [ ] **Step 13: QuotaDimension.tsx**

```tsx
import { Tag } from 'antd';

export function QuotaDimension({ items }: { items: Array<{ label: string; value: string; remaining?: number }> }) {
  if (!items.length) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
      {items.map((it) => {
        const color = it.remaining === undefined ? 'var(--text2)' : it.remaining <= 0 ? 'var(--red)' : it.remaining < 5 ? 'var(--yellow)' : 'var(--green)';
        return (
          <div key={it.label} style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ color: 'var(--text2)' }}>{it.label}</span>
            <span>
              {it.value}
              {it.remaining !== undefined && <Tag color={color} style={{ marginLeft: 6 }}>剩 {it.remaining}</Tag>}
            </span>
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 14: 提交**

```bash
git add web/admin-react/src/components
git commit -m "feat(ui): add shared modal, table, json tree, time range, search select components"
```

---

### Task 11: 路由表 + main.tsx + App.tsx

**Files:**
- Create: `web/admin-react/src/router.tsx`
- Create: `web/admin-react/src/App.tsx`
- Create: `web/admin-react/src/main.tsx`

- [ ] **Step 1: router.tsx**

```tsx
import { createHashRouter, Navigate } from 'react-router-dom';
import { AppShell } from './layout/AppShell';
import { AuthGate } from './auth/AuthGate';
import { LoginPage } from './features/login/LoginPage';
import { DashboardPage } from './features/dashboard/DashboardPage';
import { ConfigPage } from './features/config/ConfigPage';
import { ApiKeysPage } from './features/api-keys/ApiKeysPage';
import { ProxiesPage } from './features/config/ProxiesPage';
import { ChannelsPage } from './features/channels/ChannelsPage';
import { ChannelDetail } from './features/channels/ChannelDetail';
import { CustomChannelCreatePage } from './features/custom-providers/CustomChannelCreatePage';
import { BalanceMethodsPage } from './features/balance-methods/BalanceMethodsPage';
import { ModelRoutingPage } from './features/models/ModelRoutingPage';
import { ModelMetadataPage } from './features/models/ModelMetadataPage';
import { RequestLogsPage } from './features/request-logs/RequestLogsPage';
import { RequestLogDetail } from './features/request-logs/RequestLogDetail';
import { DataDashboardPage } from './features/dashboard/DataDashboardPage';
import { TestModelPage } from './features/test-model/TestModelPage';
import { UsageGuidePage } from './features/usage-guide/UsageGuidePage';
import { DocsPage } from './features/docs/DocsPage';
import { AdminSettingsPage } from './features/config/AdminSettingsPage';

export const router = createHashRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: <AuthGate><AppShell /></AuthGate>,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'config', element: <ConfigPage /> },
      { path: 'admin-settings', element: <AdminSettingsPage /> },
      { path: 'api-keys', element: <ApiKeysPage /> },
      { path: 'proxies', element: <ProxiesPage /> },
      { path: 'channels', element: <ChannelsPage /> },
      { path: 'channels/:name', element: <ChannelDetail /> },
      { path: 'custom-channel-create', element: <CustomChannelCreatePage /> },
      { path: 'balance-methods', element: <BalanceMethodsPage /> },
      { path: 'models', element: <ModelRoutingPage /> },
      { path: 'model-metadata', element: <ModelMetadataPage /> },
      { path: 'request-logs', element: <RequestLogsPage /> },
      { path: 'request-logs/:id', element: <RequestLogDetail /> },
      { path: 'data-dashboard', element: <DataDashboardPage /> },
      { path: 'test-model', element: <TestModelPage /> },
      { path: 'usage-guide', element: <UsageGuidePage /> },
      { path: 'docs', element: <DocsPage /> },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
]);
```

- [ ] **Step 2: App.tsx**

```tsx
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { ThemeProvider } from './theme/ThemeProvider';
import { App as AntdApp } from 'antd';

export function App() {
  return (
    <AntdApp>
      <ThemeProvider>
        <RouterProvider router={router} />
      </ThemeProvider>
    </AntdApp>
  );
}
```

- [ ] **Step 3: main.tsx**

```tsx
import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

- [ ] **Step 4: 写 `src/styles.css`**

`web/admin-react/src/styles.css`：

```css
* { box-sizing: border-box; }
html, body, #root { margin: 0; padding: 0; height: 100%; }
body { background: var(--bg, #0b1220); color: var(--text, #cbd5e1); font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
button { font-family: inherit; }
a { color: inherit; }
input, select, textarea { font-family: inherit; color: var(--text); }
```

- [ ] **Step 5: 提交**

```bash
git add web/admin-react/src/router.tsx web/admin-react/src/App.tsx web/admin-react/src/main.tsx web/admin-react/src/styles.css
git commit -m "feat(shell): add router, App entrypoint and global styles"
```

---

### Task 12: 后端 1 行路由 + build 配置 + dev 验证

**Files:**
- Modify: `admin.py:649-664`
- Create: `web/admin-react/src/features/login/LoginPage.tsx`（占位，Task 13 实装）

- [ ] **Step 1: 写占位 LoginPage**

```tsx
export function LoginPage() {
  return <div style={{ padding: 40 }}>登录页（Task 13 实装）</div>;
}
```

- [ ] **Step 2: 修改 admin.py**

在 admin.py 现有 `@router.get("/provider/{name}")` 之后（line 664 之后）插入：

```python
@router.get("/v2", include_in_schema=False)
@router.get("/v2/{path:path}", include_in_schema=False)
async def admin_v2(path: str = ""):
    return FileResponse(_ADMIN_PAGE_FILE.parent / "admin-react" / "index.html")
```

- [ ] **Step 3: 跑构建确认产物落到 `static/admin-react/`**

Run: `cd web/admin-react && pnpm build`
Expected: 产物在 `static/admin-react/index.html` 与 `static/admin-react/assets/`；vite 报 `outDir` 路径成功解析。

- [ ] **Step 4: 起 dev 跑端到端**

- 启动后端：另开终端跑 `python main.py`。
- 启动前端：当前终端跑 `cd web/admin-react && pnpm dev`。
- 浏览器打开 `http://localhost:5173/admin/v2`。
- 应该看到 404（因为没真实页面），路由走到 LoginPage 占位文本。
- 浏览器打开 `http://127.0.0.1:8000/admin/v2`：
- 应该看到 React 打包的 `index.html`。
- 打开 DevTools，JS/CSS 资源能正常加载。

- [ ] **Step 5: 提交**

```bash
git add admin.py web/admin-react/src/features/login/LoginPage.tsx
git commit -m "feat(backend): expose /admin/v2 serving the react build, login placeholder"
```

---

### Task 13: LoginPage + Login 流程 + 修改密码

**Files:**
- Create: `web/admin-react/src/features/login/LoginPage.tsx`

- [ ] **Step 1: 实现 LoginPage**

```tsx
import { Button, Checkbox, Input } from 'antd';
import { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { login, changePassword } from '../../api/auth';
import { persistToken } from '../../auth/token';
import { showToast } from '../../components/Toast';

export function LoginPage() {
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [changePwd, setChangePwd] = useState(false);
  const [oldPwd, setOldPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [newPwd2, setNewPwd2] = useState('');
  const nav = useNavigate();
  const loc = useLocation();
  const from = (loc.state as { from?: string } | null)?.from || '/';

  const onLogin = async () => {
    setErr('');
    if (!password) { setErr('请输入密码'); return; }
    setLoading(true);
    try {
      const { token } = await login(password);
      persistToken(token, remember);
      nav(from, { replace: true });
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '登录失败';
      setErr(detail);
    } finally { setLoading(false); }
  };

  const onChangePwd = async () => {
    if (!oldPwd) { showToast('请输入旧密码', 'error'); return; }
    if (newPwd.length < 4) { showToast('新密码至少 4 位', 'error'); return; }
    if (newPwd !== newPwd2) { showToast('两次新密码不一致', 'error'); return; }
    try {
      await changePassword(oldPwd, newPwd);
      showToast('密码修改成功', 'success');
      setChangePwd(false);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '修改失败';
      showToast(detail, 'error');
    }
  };

  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: 'var(--bg)' }}>
      <div style={{ width: 360, padding: 28, background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 12 }}>
        <h1 style={{ fontSize: 18, color: 'var(--textH)', margin: '0 0 16px' }}>Model API Admin</h1>
        {!changePwd ? (
          <>
            <Input.Password
              placeholder="请输入管理密码"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onPressEnter={onLogin}
              autoFocus
            />
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--text2)', margin: '10px 0 14px' }}>
              <Checkbox checked={remember} onChange={(e) => setRemember(e.target.checked)} /> 记住我
            </label>
            <Button type="primary" block loading={loading} onClick={onLogin}>登录</Button>
            <Button type="link" onClick={() => setChangePwd(true)} style={{ padding: 0, marginTop: 4 }}>修改密码</Button>
            {err && <div style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>{err}</div>}
          </>
        ) : (
          <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <Input.Password placeholder="旧密码" value={oldPwd} onChange={(e) => setOldPwd(e.target.value)} />
              <Input.Password placeholder="新密码（至少 4 位）" value={newPwd} onChange={(e) => setNewPwd(e.target.value)} />
              <Input.Password placeholder="再次输入" value={newPwd2} onChange={(e) => setNewPwd2(e.target.value)} />
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
              <Button onClick={() => setChangePwd(false)} block>取消</Button>
              <Button type="primary" onClick={onChangePwd} block>提交</Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 手工验收**

Run: `cd web/admin-react && pnpm dev`。
- 打开 `http://127.0.0.1:8000/admin/v2`。
- 输入错误密码，看到红色错误。
- 输入正确密码，进入 dashboard 占位页。
- 刷新页面，token 持久化生效（记住我勾选后），session 模式刷新会跳回登录。

- [ ] **Step 3: 提交**

```bash
git add web/admin-react/src/features/login/LoginPage.tsx
git commit -m "feat(login): implement login page with remember and change password"
```

---

### Task 14: DashboardPage（仪表盘）

**Files:**
- Create: `web/admin-react/src/features/dashboard/DashboardPage.tsx`
- Create: `web/admin-react/src/features/dashboard/DataDashboardPage.tsx`

- [ ] **Step 1: DashboardPage**

```tsx
import { useEffect, useState } from 'react';
import { Card, Col, Row, Spin, Tag } from 'antd';
import { Link } from 'react-router-dom';
import { PageHeader } from '../../layout/PageHeader';
import { getStats, getRecentLogs } from '../../api/dashboard';
import { listProviders } from '../../api/providers';
import { formatCount, formatTokenLimit } from '../../utils/format';
import { StatusBadge } from '../../components/StatusBadge';

export function DashboardPage() {
  const [stats, setStats] = useState<any>(null);
  const [logs, setLogs] = useState<any[]>([]);
  const [providers, setProviders] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    (async () => {
      try {
        const [s, l, p] = await Promise.all([getStats({ includeTokenStats: true }), getRecentLogs(20), listProviders()]);
        setStats(s); setLogs(l?.logs || l || []); setProviders(p?.providers || p || []);
      } finally { setLoading(false); }
    })();
  }, []);
  if (loading) return <Spin />;
  return (
    <>
      <PageHeader title="仪表盘" sub="服务概览 & 渠道状态" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Row gutter={16}>
          <Col span={6}><Card><div style={{ color: 'var(--text2)' }}>渠道数</div><div style={{ fontSize: 24, color: 'var(--textH)' }}>{formatCount(stats?.channel_count ?? providers.length)}</div></Card></Col>
          <Col span={6}><Card><div style={{ color: 'var(--text2)' }}>今日请求</div><div style={{ fontSize: 24, color: 'var(--textH)' }}>{formatCount(stats?.today_requests ?? stats?.requests)}</div></Card></Col>
          <Col span={6}><Card><div style={{ color: 'var(--text2)' }}>Token 用量</div><div style={{ fontSize: 24, color: 'var(--textH)' }}>{formatTokenLimit(stats?.token_total)}</div></Card></Col>
          <Col span={6}><Card><div style={{ color: 'var(--text2)' }}>活跃账号</div><div style={{ fontSize: 24, color: 'var(--textH)' }}>{formatCount(stats?.active_accounts)}</div></Card></Col>
        </Row>
        <Card title="服务配置概览">
          <pre style={{ margin: 0, fontSize: 12, color: 'var(--text)', maxHeight: 200, overflow: 'auto', fontFamily: 'var(--fontM)' }}>{JSON.stringify(stats?.config_summary || stats, null, 2)}</pre>
        </Card>
        <Card title={<Link to="/channels">查看全部渠道 →</Link>}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 12 }}>
            {providers.slice(0, 8).map((p) => (
              <Link key={p.name} to={`/channels/${encodeURIComponent(p.name)}`} style={{ textDecoration: 'none' }}>
                <Card size="small">
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: 'var(--textH)' }}>{p.name}</span>
                    <Tag color={p.enabled ? 'green' : 'default'}>{p.enabled ? '启用' : '关闭'}</Tag>
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--text2)', marginTop: 4 }}>{p.type || p.custom_channel ? '自定义' : '内置'}</div>
                </Card>
              </Link>
            ))}
          </div>
        </Card>
        <Card title="最近请求日志" extra={<Link to="/request-logs">查看全部 →</Link>}>
          <table style={{ width: '100%', fontSize: 12 }}>
            <thead><tr style={{ color: 'var(--text2)' }}><th style={{ textAlign: 'left' }}>时间</th><th>渠道/账号</th><th>模型</th><th>用时</th><th>结果</th></tr></thead>
            <tbody>
              {logs.slice(0, 10).map((l) => (
                <tr key={l.id} style={{ borderTop: '1px solid var(--border)' }}>
                  <td style={{ padding: '6px 0', color: 'var(--text)' }}>{l.created_at}</td>
                  <td style={{ textAlign: 'center' }}>{l.provider}/{l.account}</td>
                  <td style={{ textAlign: 'center' }}>{l.model}</td>
                  <td style={{ textAlign: 'center' }}>{l.duration_ms}ms</td>
                  <td style={{ textAlign: 'center' }}><StatusBadge status={l.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </>
  );
}
```

- [ ] **Step 2: DataDashboardPage**

```tsx
import { useEffect, useState } from 'react';
import { Card, Select, Spin } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { getDashboardStats } from '../../api/dashboard';
import { TimeRangePicker, type TimeRangeValue } from '../../components/TimeRangePicker';

export function DataDashboardPage() {
  const [range, setRange] = useState<TimeRangeValue>({ start: '', end: '' });
  const [grain, setGrain] = useState('hour');
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const fetchData = async () => {
    if (!range.start || !range.end) return;
    setLoading(true);
    try { setData(await getDashboardStats({ start: range.start, end: range.end, grain })); }
    finally { setLoading(false); }
  };
  useEffect(() => { fetchData(); }, [range.start, range.end, grain]);
  return (
    <>
      <PageHeader title="数据看板" sub="调用与消耗分布" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            <TimeRangePicker value={range} onChange={setRange} />
            <Select value={grain} onChange={setGrain} style={{ width: 120 }} options={[{ value: 'hour', label: '小时' }, { value: 'day', label: '天' }]} />
          </div>
        </Card>
        {loading && <Spin />}
        {data && <Card title="调用分布"><pre style={{ margin: 0, fontSize: 12, color: 'var(--text)', fontFamily: 'var(--fontM)' }}>{JSON.stringify(data, null, 2)}</pre></Card>}
      </div>
    </>
  );
}
```

- [ ] **Step 3: 手工验收**

- 打开 `/`，确认 dashboard 4 个统计卡片、最近日志、渠道卡片网格渲染。
- 打开 `/#/data-dashboard`，选时间区间，确认能拉数据。

- [ ] **Step 4: 提交**

```bash
git add web/admin-react/src/features/dashboard
git commit -m "feat(dashboard): implement dashboard and data dashboard pages"
```

---

### Task 15: ChannelsPage（渠道卡片 + 筛选 + 添加）+ ChannelDetail（基础 + 限制策略 + 模型表）

**Files:**
- Create: `web/admin-react/src/features/channels/ChannelsPage.tsx`
- Create: `web/admin-react/src/features/channels/ChannelCard.tsx`
- Create: `web/admin-react/src/features/channels/ChannelDetail.tsx`
- Create: `web/admin-react/src/features/channels/useCooldownPolicy.ts`
- Create: `web/admin-react/src/features/channels/useLimitPolicy.ts`
- Create: `web/admin-react/src/features/channels/__tests__/useCooldownPolicy.test.ts`

- [ ] **Step 1: 写 useCooldownPolicy 测试**

```ts
import { describe, it, expect } from 'vitest';
import { collectCooldownPolicy, parseCooldownCell } from '../../../utils/cooldownPolicy';
import { rowsToPolicy, policyToRows } from '../useCooldownPolicy';

describe('rowsToPolicy / policyToRows', () => {
  it('converts form rows into policy entries', () => {
    const policy = rowsToPolicy([
      { codes: '429,500', cooldown: 60 },
    ]);
    expect(policy).toEqual([
      { codes: [429, 500], cooldown: 60 },
      { codes: 'error', cooldown: 60 },
    ]);
  });
  it('round-trips back to rows', () => {
    const rows = policyToRows([{ codes: [429, 500], cooldown: 60 }, { codes: 'error', cooldown: 60 }]);
    expect(rows[0].codes).toBe('429,500');
    expect(rows[1].codes).toBe('error');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web/admin-react && pnpm test src/features/channels/__tests__/useCooldownPolicy.test.ts`
Expected: module not found。

- [ ] **Step 3: 实现 useCooldownPolicy.ts**

```ts
import { collectCooldownPolicy, parseCooldownCell, type CooldownEntry } from '../../utils/cooldownPolicy';

export interface CooldownRow { codes: string; cooldown: number; }

export function rowsToPolicy(rows: CooldownRow[]): CooldownEntry[] {
  return collectCooldownPolicy(rows);
}

export function policyToRows(entries: CooldownEntry[]): CooldownRow[] {
  return entries.map((e) => ({
    codes: Array.isArray(e.codes) ? e.codes.join(',') : 'error',
    cooldown: e.cooldown,
  }));
}
```

- [ ] **Step 4: 跑测试确认 PASS**

Run: `cd web/admin-react && pnpm test src/features/channels/__tests__/useCooldownPolicy.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: useLimitPolicy.ts**

```ts
import { useState } from 'react';
import { getLimitPolicy, putLimitPolicy } from '../../api/balance';
import { showToast } from '../../components/Toast';

export function useLimitPolicy(providerName: string) {
  const [policy, setPolicy] = useState<any>(null);
  const [methods, setMethods] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const [p, m] = await Promise.all([
        getLimitPolicy(providerName).catch(() => ({})),
        import('../../api/balance').then(({ listBalanceMethods }) => listBalanceMethods()).catch(() => []),
      ]);
      setPolicy(p || {}); setMethods(m || []);
    } finally { setLoading(false); }
  };

  const save = async (data: unknown) => {
    try {
      await putLimitPolicy(providerName, data);
      showToast('限制策略已保存', 'success');
      await load();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '保存失败';
      showToast(detail, 'error');
    }
  };

  return { policy, methods, loading, load, save };
}
```

- [ ] **Step 6: ChannelCard.tsx**

```tsx
import { Card, Tag, Switch } from 'antd';
import { Link } from 'react-router-dom';
import { setProviderEnabled } from '../../api/providers';

export function ChannelCard({ provider, onChanged }: { provider: any; onChanged?: () => void }) {
  const enabled = provider.enabled !== false;
  return (
    <Card size="small" style={{ height: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Link to={`/channels/${encodeURIComponent(provider.name)}`} style={{ color: 'var(--textH)', fontWeight: 600 }}>{provider.name}</Link>
        <Switch checked={enabled} onChange={async (v) => { await setProviderEnabled(provider.name, v); onChanged?.(); }} />
      </div>
      <div style={{ fontSize: 12, color: 'var(--text2)', marginTop: 6 }}>
        <Tag color={enabled ? 'green' : 'default'}>{enabled ? '启用' : '关闭'}</Tag>
        {provider.custom_channel ? <Tag>自定义</Tag> : <Tag color="blue">内置</Tag>}
      </div>
      <div style={{ marginTop: 8, display: 'flex', gap: 6 }}>
        <Link to={`/channels/${encodeURIComponent(provider.name)}`}><Tag color="blue">管理</Tag></Link>
      </div>
    </Card>
  );
}
```

- [ ] **Step 7: ChannelsPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Input, Select, Space, Spin } from 'antd';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../../layout/PageHeader';
import { listProviders } from '../../api/providers';
import { ChannelCard } from './ChannelCard';

export function ChannelsPage() {
  const [providers, setProviders] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');
  const [status, setStatus] = useState<'all' | 'enabled' | 'disabled'>('all');
  const [type, setType] = useState<'all' | 'custom' | 'builtin'>('all');
  const [sortBy, setSortBy] = useState('name');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [hideDisabled, setHideDisabled] = useState(false);
  const nav = useNavigate();

  const load = async () => {
    setLoading(true);
    try { const r = await listProviders(); setProviders(r?.providers || r || []); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const filtered = providers
    .filter((p) => !q || p.name.toLowerCase().includes(q.toLowerCase()))
    .filter((p) => status === 'all' || (status === 'enabled' ? p.enabled !== false : p.enabled === false))
    .filter((p) => type === 'all' || (type === 'custom' ? !!p.custom_channel : !p.custom_channel))
    .filter((p) => !hideDisabled || p.enabled !== false)
    .sort((a, b) => {
      const av = (a[sortBy] ?? a.name) as string;
      const bv = (b[sortBy] ?? b.name) as string;
      return sortDir === 'asc' ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
    });

  return (
    <>
      <PageHeader title="渠道" sub="渠道列表、状态与管理" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Space wrap>
          <Input placeholder="按名字/备注筛选" value={q} onChange={(e) => setQ(e.target.value)} allowClear style={{ width: 220 }} />
          <Select value={status} onChange={setStatus} style={{ width: 120 }} options={[{ value: 'all', label: '全部' }, { value: 'enabled', label: '启用' }, { value: 'disabled', label: '关闭' }]} />
          <Select value={type} onChange={setType} style={{ width: 120 }} options={[{ value: 'all', label: '全部' }, { value: 'custom', label: '自定义' }, { value: 'builtin', label: '内置' }]} />
          <Select value={sortBy} onChange={setSortBy} style={{ width: 120 }} options={[{ value: 'name', label: '按名字' }, { value: 'type', label: '按类型' }]} />
          <Select value={sortDir} onChange={setSortDir as any} style={{ width: 100 }} options={[{ value: 'asc', label: '升序' }, { value: 'desc', label: '降序' }]} />
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text2)', fontSize: 12 }}><input type="checkbox" checked={hideDisabled} onChange={(e) => setHideDisabled(e.target.checked)} /> 隐藏关闭</label>
          <Button type="primary" onClick={() => nav('/custom-channel-create')}>+ 添加自定义渠道</Button>
        </Space>
        {loading ? <Spin /> : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14 }}>
            {filtered.map((p) => <ChannelCard key={p.name} provider={p} onChanged={load} />)}
          </div>
        )}
      </div>
    </>
  );
}
```

- [ ] **Step 8: ChannelDetail.tsx（实现基础信息 + 限制策略 + 模型表 + 账号列表）**

```tsx
import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { Button, Card, Col, Input, InputNumber, Row, Select, Space, Spin, Switch, Table, Tag } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import {
  getProviderBase, getProviderConfig, getProviderModels, putProviderCustom,
  addAccount, updateAccount, deleteAccount, switchAccount, setProviderClearConv,
  postProviderModel, deleteProviderModel, refreshModels, healthCheck,
} from '../../api/providers';
import { showToast } from '../../components/Toast';
import { useLimitPolicy } from './useLimitPolicy';
import { rowsToPolicy, policyToRows, type CooldownRow } from './useCooldownPolicy';
import { AppModal } from '../../components/Modal';

export function ChannelDetail() {
  const { name = '' } = useParams();
  const decoded = decodeURIComponent(name);
  const [base, setBase] = useState<any>(null);
  const [cfg, setCfg] = useState<any>(null);
  const [models, setModels] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [showClearConv, setShowClearConv] = useState(false);
  const [clearConvEnabled, setClearConvEnabled] = useState(false);
  const [clearConvAge, setClearConvAge] = useState(2);
  const policyHook = useLimitPolicy(decoded);

  const load = async () => {
    setLoading(true);
    try {
      const [b, c, m] = await Promise.all([getProviderBase(decoded), getProviderConfig(decoded).catch(() => ({})), getProviderModels(decoded).catch(() => ({ models: [] }))]);
      setBase(b); setCfg(c || {}); setModels(m?.models || []);
      setClearConvEnabled(!!c?.clear_conversation?.enabled);
      setClearConvAge(c?.clear_conversation?.max_age_hours || 2);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); policyHook.load(); }, [decoded]);

  const save = async () => {
    try { await putProviderCustom(decoded, cfg); showToast('已保存', 'success'); await load(); }
    catch (e: any) { showToast(e?.response?.data?.detail || '保存失败', 'error'); }
  };

  const onSaveClearConv = async () => {
    try { await setProviderClearConv(decoded, clearConvEnabled, clearConvAge); showToast('已保存', 'success'); setShowClearConv(false); }
    catch (e: any) { showToast(e?.response?.data?.detail || '保存失败', 'error'); }
  };

  if (loading || !base) return <Spin />;

  return (
    <>
      <PageHeader title={`渠道：${decoded}`} sub={base.type} />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card title="基础配置" extra={<Button type="primary" onClick={save}>保存</Button>}>
          <Row gutter={16}>
            <Col span={8}><div>账号优先级</div><InputNumber value={cfg.account_priority ?? 0} onChange={(v) => setCfg({ ...cfg, account_priority: v || 0 })} style={{ width: '100%' }} /></Col>
            <Col span={8}><div>账号权重</div><InputNumber value={cfg.account_weight ?? 0} onChange={(v) => setCfg({ ...cfg, account_weight: v || 0 })} style={{ width: '100%' }} /></Col>
            <Col span={8}><div>清空会话</div><Switch checked={clearConvEnabled} onChange={(v) => { setClearConvEnabled(v); setShowClearConv(true); }} /></Col>
          </Row>
        </Card>

        <Card title="限制策略" extra={<Button onClick={() => setShowClearConv(true)}>编辑</Button>}>
          <pre style={{ margin: 0, fontSize: 12, fontFamily: 'var(--fontM)' }}>{JSON.stringify(policyHook.policy || {}, null, 2)}</pre>
        </Card>

        <Card title="模型表" extra={<Space><Button onClick={async () => { await refreshModels(decoded); showToast('已触发刷新', 'success'); }}>从上游同步</Button><Button danger onClick={async () => { if (confirm('确认清空？')) { await deleteProviderModel(decoded, ''); load(); } }}>清空</Button><Button type="primary" onClick={async () => { await putProviderCustom(decoded, { ...cfg, models }); showToast('已保存', 'success'); }}>保存</Button></Space>}>
          <Table
            dataSource={models}
            rowKey={(r) => r.upstream_model_id || r.model_id}
            size="small"
            pagination={false}
            columns={[
              { title: '上游模型 ID', dataIndex: 'upstream_model_id' },
              { title: '对外模型 ID', dataIndex: 'model_id', render: (v, r) => <Input defaultValue={r.model_id || r.upstream_model_id} onBlur={async (e) => { await postProviderModel(decoded, r.upstream_model_id, e.target.value); load(); }} /> },
              { title: '操作', render: (_, r) => <Button danger size="small" onClick={async () => { await deleteProviderModel(decoded, r.upstream_model_id); load(); }}>删除</Button> },
            ]}
          />
        </Card>

        <Card title="账号" extra={<Link to={`/channels/${encodeURIComponent(decoded)}/accounts`}><Button>账号管理</Button></Link>}>
          <Tag color="blue">账号数：{base.account_count ?? 0}</Tag>
        </Card>
      </div>

      <AppModal
        open={showClearConv}
        title="清空会话"
        onClose={() => setShowClearConv(false)}
        onSave={onSaveClearConv}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div><Switch checked={clearConvEnabled} onChange={setClearConvEnabled} /> 启用</div>
          <div>最大保留时间（小时）<InputNumber value={clearConvAge} onChange={(v) => setClearConvAge(v || 0)} min={0} /></div>
        </Space>
      </AppModal>
    </>
  );
}
```

- [ ] **Step 9: 手工验收**

- 打开 `/#/channels`，确认 4 套筛选生效、添加自定义渠道跳转。
- 进入任一内置渠道详情，确认基础配置可编辑、限制策略/模型表可展示。
- 切换渠道启用/关闭状态，确认 401 时弹回登录。

- [ ] **Step 10: 提交**

```bash
git add web/admin-react/src/features/channels
git commit -m "feat(channels): implement channels list and detail with policy/model/account sections"
```

---

### Task 16: AccountsPage + AccountList + AccountEdit + useAccountList

**Files:**
- Create: `web/admin-react/src/features/accounts/AccountsPage.tsx`
- Create: `web/admin-react/src/features/accounts/AccountList.tsx`
- Create: `web/admin-react/src/features/accounts/AccountEdit.tsx`
- Create: `web/admin-react/src/features/accounts/useAccountList.ts`

- [ ] **Step 1: useAccountList.ts**

```ts
import { useState } from 'react';
import { getProviderAccounts, switchAccount, deleteAccount, initAccount, clearAccountCooldown, checkAccountBalance, clearAccountBalanceCache, getAccountBalance } from '../../api/providers';
import { showToast } from '../../components/Toast';

export function useAccountList(providerName: string) {
  const [accounts, setAccounts] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try { const r = await getProviderAccounts(providerName); setAccounts(r?.accounts || r || []); }
    finally { setLoading(false); }
  };

  const onSwitch = async (u: string) => { await switchAccount(providerName, u); showToast('已切换', 'success'); await load(); };
  const onDelete = async (u: string) => { if (!confirm(`确认删除 ${u}？`)) return; await deleteAccount(providerName, u); showToast('已删除', 'success'); await load(); };
  const onInit = async (u: string) => { await initAccount(providerName, u); showToast('已初始化', 'success'); await load(); };
  const onClearCooldown = async (u: string) => { await clearAccountCooldown(providerName, u); showToast('已清冷却', 'success'); await load(); };
  const onCheckBalance = async (u: string) => { await checkAccountBalance(providerName, u); showToast('已检查', 'success'); };
  const onClearBalance = async (u: string) => { await clearAccountBalanceCache(providerName, u); showToast('已清缓存', 'success'); };

  return { accounts, loading, load, onSwitch, onDelete, onInit, onClearCooldown, onCheckBalance, onClearBalance };
}
```

- [ ] **Step 2: AccountList.tsx**

```tsx
import { Button, Input, Select, Space, Spin, Switch, Table, Tag } from 'antd';
import { useState } from 'react';
import { useAccountList } from './useAccountList';
import { getAccountBalance } from '../../api/providers';
import { TokenUsageBar } from '../../components/TokenUsageBar';
import { QuotaDimension } from '../../components/QuotaDimension';
import { formatCount, formatTokenLimit, formatCooldownRemaining } from '../../utils/format';

export function AccountList({ providerName }: { providerName: string }) {
  const hook = useAccountList(providerName);
  const [filter, setFilter] = useState<'all' | 'enabled' | 'disabled' | 'cooldown'>('all');
  const [search, setSearch] = useState('');
  const [balances, setBalances] = useState<Record<string, any>>({});

  if (hook.loading) return <Spin />;

  const rows = hook.accounts
    .filter((a) => !search || (a.username || '').toLowerCase().includes(search.toLowerCase()))
    .filter((a) => {
      if (filter === 'enabled') return a.disabled !== true;
      if (filter === 'disabled') return a.disabled === true;
      if (filter === 'cooldown') return a.cooldown_remaining && a.cooldown_remaining > 0;
      return true;
    });

  const fetchBalance = async (u: string) => {
    try { const b = await getAccountBalance(providerName, u); setBalances((prev) => ({ ...prev, [u]: b })); }
    catch { showToastError('获取余额失败'); }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Space>
        <Input.Search placeholder="按账号筛选" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 220 }} />
        <Select value={filter} onChange={setFilter} style={{ width: 140 }} options={[{ value: 'all', label: '全部' }, { value: 'enabled', label: '启用' }, { value: 'disabled', label: '关闭' }, { value: 'cooldown', label: '冷却中' }]} />
      </Space>
      <Table
        dataSource={rows}
        rowKey={(r) => r.username}
        size="small"
        pagination={{ pageSize: 20 }}
        columns={[
          { title: '账号', dataIndex: 'username' },
          { title: '状态', render: (_, r) => <Switch checked={r.disabled !== true} onChange={async (v) => { if (v) await hook.onInit(r.username); else await hook.onClearCooldown(r.username); }} /> },
          { title: '冷却', render: (_, r) => <Tag color={r.cooldown_remaining > 0 ? 'red' : 'green'}>{formatCooldownRemaining(r.cooldown_remaining)}</Tag> },
          { title: '用量', render: (_, r) => r.usage ? <TokenUsageBar used={r.usage.used || 0} limit={r.usage.limit || 0} /> : '-' },
          { title: '配额', render: (_, r) => r.quota ? <QuotaDimension items={r.quota} /> : '-' },
          { title: '操作', render: (_, r) => (
            <Space>
              <Button size="small" onClick={() => fetchBalance(r.username)}>查余额</Button>
              <Button size="small" onClick={() => hook.onSwitch(r.username)}>切换</Button>
              <Button size="small" danger onClick={() => hook.onDelete(r.username)}>删除</Button>
            </Space>
          ) },
        ]}
      />
      {Object.entries(balances).map(([u, b]) => (
        <pre key={u} style={{ background: 'var(--bg)', padding: 10, fontSize: 11, fontFamily: 'var(--fontM)' }}>{u}: {JSON.stringify(b, null, 2)}</pre>
      ))}
    </Space>
  );
}

function showToastError(msg: string) {
  import('../../components/Toast').then(({ showToast }) => showToast(msg, 'error'));
}
```

- [ ] **Step 3: AccountEdit.tsx（账号添加/编辑）**

```tsx
import { AppModal } from '../../components/Modal';
import { Form, Input, Switch, Select, InputNumber } from 'antd';
import { addAccount, updateAccount } from '../../api/providers';
import { showToast } from '../../components/Toast';

export function AccountEdit({ providerName, username, account, onClose, onSaved }: { providerName: string; username?: string; account?: any; onClose: () => void; onSaved: () => void }) {
  const [form] = Form.useForm();
  const onSave = async () => {
    const v = await form.validateFields();
    try {
      if (username) await updateAccount(providerName, username, v);
      else await addAccount(providerName, v);
      showToast('已保存', 'success');
      onSaved(); onClose();
    } catch (e: any) { showToast(e?.response?.data?.detail || '保存失败', 'error'); throw e; }
  };
  return (
    <AppModal open title={username ? `编辑 ${username}` : '新增账号'} onClose={onClose} onSave={onSave} width={520}>
      <Form form={form} layout="vertical" initialValues={account || { disabled: false, weight: 1 }}>
        <Form.Item name="username" label="账号" rules={[{ required: !username }]}><Input disabled={!!username} /></Form.Item>
        <Form.Item name="password" label="密码/Token"><Input.Password /></Form.Item>
        <Form.Item name="disabled" label="禁用" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="weight" label="权重"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
      </Form>
    </AppModal>
  );
}
```

- [ ] **Step 4: AccountsPage.tsx**

```tsx
import { useState } from 'react';
import { PageHeader } from '../../layout/PageHeader';
import { Select, Space } from 'antd';
import { useApi } from '../../auth/useApi';
import { listProviders } from '../../api/providers';
import { AccountList } from './AccountList';

export function AccountsPage() {
  const { data: providers } = useApi<any[]>(() => listProviders().then((r) => r?.providers || r || []), []);
  const [name, setName] = useState<string>('');
  const options = (providers || []).map((p) => ({ value: p.name, label: p.name }));
  return (
    <>
      <PageHeader title="账号" sub="按渠道分组管理" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Space>
          <span style={{ color: 'var(--text2)' }}>渠道：</span>
          <Select options={options} value={name} onChange={setName} style={{ width: 240 }} placeholder="选择渠道" allowClear />
        </Space>
        {name && <AccountList providerName={name} />}
      </div>
    </>
  );
}
```

- [ ] **Step 5: 手工验收**

- 打开 `/#/accounts`，选择渠道，确认账号表加载、过滤/搜索生效、删除/初始化按钮触发。
- 余额查询 → 弹错误（如果后端异常）或显示 JSON。

- [ ] **Step 6: 提交**

```bash
git add web/admin-react/src/features/accounts
git commit -m "feat(accounts): implement accounts list and edit with filter and balance"
```

---

### Task 17: BalanceMethodsPage（余额方式列表 + 编辑器 + 测试）

**Files:**
- Create: `web/admin-react/src/features/balance-methods/BalanceMethodsPage.tsx`
- Create: `web/admin-react/src/features/balance-methods/BalanceMethodForm.tsx`
- Create: `web/admin-react/src/features/balance-methods/BalanceMethodTest.tsx`

- [ ] **Step 1: BalanceMethodForm.tsx**

```tsx
import { Form, Input, Select, Switch, InputNumber } from 'antd';
import { useEffect, useState } from 'react';

export function BalanceMethodForm({ method, onChange }: { method?: any; onChange: (v: any) => void }) {
  const [form] = Form.useForm();
  useEffect(() => { form.setFieldsValue(method || { type: 'headers', enabled: true, method: 'GET', timeout_seconds: 20 }); form.validateFields().then(() => onChange(form.getFieldsValue(true))).catch(() => {}); }, [method]);
  return (
    <Form form={form} layout="vertical" onValuesChange={() => onChange(form.getFieldsValue(true))}>
      <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
      <Form.Item name="type" label="类型"><Select options={[{ value: 'headers', label: 'Headers 映射' }, { value: 'json_path', label: 'JSON Path' }, { value: 'usage', label: '/v1/usage' }, { value: 'subscription', label: 'Billing Subscription' }]} /></Form.Item>
      <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      <Form.Item name="method" label="请求方法"><Select options={[{ value: 'GET', label: 'GET' }, { value: 'POST', label: 'POST' }]} /></Form.Item>
      <Form.Item name="url" label="URL"><Input placeholder="{{base_url}}/v1/usage" /></Form.Item>
      <Form.Item name="timeout_seconds" label="超时（秒）"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
      <Form.Item name="headers" label="请求 Headers (JSON)"><Input.TextArea rows={4} /></Form.Item>
      <Form.Item name="body" label="请求 Body (JSON)"><Input.TextArea rows={4} /></Form.Item>
      <Form.Item name="parser" label="Parser (JSON)"><Input.TextArea rows={5} /></Form.Item>
      <Form.Item name="result_mapping" label="Result Mapping (JSON)"><Input.TextArea rows={5} /></Form.Item>
    </Form>
  );
}
```

- [ ] **Step 2: BalanceMethodTest.tsx**

```tsx
import { useState } from 'react';
import { Button, Input, Select, Space } from 'antd';
import { useApi } from '../../auth/useApi';
import { listProviders } from '../../api/providers';
import { testBalanceMethod } from '../../api/balance';
import { showToast } from '../../components/Toast';

export function BalanceMethodTest({ methodId }: { methodId: number | string }) {
  const { data: providers } = useApi<any[]>(() => listProviders().then((r) => r?.providers || r || []), []);
  const [provider, setProvider] = useState<string>('');
  const [username, setUsername] = useState('');
  const [result, setResult] = useState<string>('');
  const [loading, setLoading] = useState(false);

  const run = async () => {
    if (!provider) { showToast('请选择渠道', 'error'); return; }
    setLoading(true);
    try { const r = await testBalanceMethod(methodId, { provider_name: provider, username }); setResult(JSON.stringify(r, null, 2)); }
    catch (e: any) { setResult(e?.response?.data?.detail || e?.message || '失败'); }
    finally { setLoading(false); }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Select options={(providers || []).map((p) => ({ value: p.name, label: p.name }))} value={provider} onChange={setProvider} style={{ width: 220 }} placeholder="选择渠道" />
        <Input placeholder="账号（可空）" value={username} onChange={(e) => setUsername(e.target.value)} style={{ width: 200 }} />
        <Button type="primary" loading={loading} onClick={run}>测试</Button>
      </Space>
      <pre style={{ background: 'var(--bg)', padding: 10, borderRadius: 6, maxHeight: 280, overflow: 'auto', fontSize: 12, fontFamily: 'var(--fontM)' }}>{result || '点击测试查看结果'}</pre>
    </Space>
  );
}
```

- [ ] **Step 3: BalanceMethodsPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, Space, Table, Tag } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { listBalanceMethods, createBalanceMethod, updateBalanceMethod, deleteBalanceMethod } from '../../api/balance';
import { AppModal } from '../../components/Modal';
import { BalanceMethodForm } from './BalanceMethodForm';
import { BalanceMethodTest } from './BalanceMethodTest';
import { showToast } from '../../components/Toast';

export function BalanceMethodsPage() {
  const [methods, setMethods] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [edit, setEdit] = useState<{ open: boolean; method?: any }>({ open: false });
  const [formValue, setFormValue] = useState<any>({});
  const [testId, setTestId] = useState<number | string | null>(null);

  const load = async () => { setLoading(true); try { const r = await listBalanceMethods(); setMethods(r || []); } finally { setLoading(false); } };
  useEffect(() => { load(); }, []);

  const onSave = async () => {
    try {
      const payload = {
        ...formValue,
        headers: parseJson(formValue.headers),
        body: parseJson(formValue.body),
        parser: parseJson(formValue.parser),
        result_mapping: parseJson(formValue.result_mapping),
      };
      if (edit.method?.id) await updateBalanceMethod(edit.method.id, payload);
      else await createBalanceMethod(payload);
      showToast('已保存', 'success');
      setEdit({ open: false }); load();
    } catch (e: any) { showToast(e?.response?.data?.detail || '保存失败', 'error'); throw e; }
  };

  return (
    <>
      <PageHeader title="余额方式" sub="余额查询方式与解析规则" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card extra={<Button type="primary" onClick={() => { setFormValue({ type: 'headers', method: 'GET', enabled: true, timeout_seconds: 20 }); setEdit({ open: true }); }}>+ 新增</Button>}>
          <Table
            loading={loading}
            dataSource={methods}
            rowKey="id"
            size="small"
            columns={[
              { title: '名称', dataIndex: 'name' },
              { title: '类型', dataIndex: 'type' },
              { title: 'URL', dataIndex: 'url' },
              { title: '状态', render: (_, m) => <Tag color={m.enabled ? 'green' : 'default'}>{m.enabled ? '启用' : '关闭'}</Tag> },
              { title: '操作', render: (_, m) => (
                <Space>
                  {m.builtin ? <Tag>内置</Tag> : <Button size="small" onClick={() => { setFormValue(m); setEdit({ open: true, method: m }); }}>编辑</Button>}
                  <Button size="small" onClick={() => setTestId(m.id)}>测试</Button>
                  {!m.builtin && <Button size="small" danger onClick={async () => { if (confirm('确认删除？')) { await deleteBalanceMethod(m.id); load(); } }}>删除</Button>}
                </Space>
              ) },
            ]}
          />
        </Card>
        {testId && <Card title="测试" extra={<Button onClick={() => setTestId(null)}>关闭</Button>}><BalanceMethodTest methodId={testId} /></Card>}
      </div>
      <AppModal open={edit.open} title={edit.method ? '编辑余额方式' : '新增余额方式'} onClose={() => setEdit({ open: false })} onSave={onSave} width={720}>
        <BalanceMethodForm method={formValue} onChange={setFormValue} />
      </AppModal>
    </>
  );
}

function parseJson(s: string | undefined): any {
  if (!s) return undefined;
  try { return JSON.parse(s); } catch { return undefined; }
}
```

- [ ] **Step 4: 手工验收**

- 打开 `/#/balance-methods`，确认列表、编辑/测试弹窗行为正常。
- 内置项不可编辑只能测试；自定义项可删。

- [ ] **Step 5: 提交**

```bash
git add web/admin-react/src/features/balance-methods
git commit -m "feat(balance): implement balance methods page with form and test runner"
```

---

### Task 18: ModelRoutingPage（路由 + 模型组 + 元数据）

**Files:**
- Create: `web/admin-react/src/features/models/ModelRoutingPage.tsx`
- Create: `web/admin-react/src/features/models/ModelGroupsPage.tsx`
- Create: `web/admin-react/src/features/models/ModelMetadataPage.tsx`
- Create: `web/admin-react/src/features/models/RouteDetailModal.tsx`
- Create: `web/admin-react/src/features/models/GroupDetailModal.tsx`
- Create: `web/admin-react/src/features/models/useModelRoutes.ts`
- Create: `web/admin-react/src/features/models/useModelGroups.ts`

- [ ] **Step 1: useModelRoutes.ts**

```ts
import { useState } from 'react';
import { getModelRouting, createModelRoute, updateModelRoute, deleteModelRoute, putModelRouting } from '../../api/models';
import { showToast } from '../../components/Toast';

export function useModelRoutes() {
  const [routing, setRouting] = useState<{ routes: any[] }>({ routes: [] });
  const [loading, setLoading] = useState(true);
  const load = async () => { setLoading(true); try { const r = await getModelRouting(); setRouting({ routes: r?.routes || [] }); } finally { setLoading(false); } };
  const saveAll = async (routes: any[]) => { await putModelRouting({ model_routes: { routes }, model_groups: { groups: routing.groups || {} } }); showToast('已保存', 'success'); await load(); };
  const upsert = async (route: any) => { if (route.id) await updateModelRoute(route.id, route); else await createModelRoute(route); showToast('已保存', 'success'); await load(); };
  const remove = async (id: string) => { await deleteModelRoute(id); showToast('已删除', 'success'); await load(); };
  return { routing, loading, load, saveAll, upsert, remove };
}
```

- [ ] **Step 2: useModelGroups.ts**

```ts
import { useState } from 'react';
import { getModelRouting, createModelGroup, updateModelGroup, deleteModelGroup, putModelRouting } from '../../api/models';
import { showToast } from '../../components/Toast';

export function useModelGroups() {
  const [groups, setGroups] = useState<Record<string, any>>({});
  const load = async () => { const r = await getModelRouting(); setGroups(r?.groups || {}); };
  const saveAll = async (g: Record<string, any>) => { await putModelRouting({ model_routes: { routes: [] }, model_groups: { groups: g } }); showToast('已保存', 'success'); await load(); };
  const upsert = async (name: string, g: any) => { if (name) await updateModelGroup(name, g); else await createModelGroup(g); showToast('已保存', 'success'); await load(); };
  const remove = async (name: string) => { await deleteModelGroup(name); showToast('已删除', 'success'); await load(); };
  return { groups, load, saveAll, upsert, remove };
}
```

- [ ] **Step 3: RouteDetailModal.tsx**

```tsx
import { AppModal } from '../../components/Modal';
import { Form, Input, Select, Switch, Table, Button, Space } from 'antd';
import { useState } from 'react';
import { useApi } from '../../auth/useApi';
import { listProviders } from '../../api/providers';

export function RouteDetailModal({ route, onClose, onSave }: { route?: any; onClose: () => void; onSave: (r: any) => void }) {
  const { data: providers } = useApi<any[]>(() => listProviders().then((r) => r?.providers || r || []), []);
  const [form] = Form.useForm();
  const [entries, setEntries] = useState<any[]>(route?.channels || []);

  return (
    <AppModal open title={route?.id ? '编辑路由' : '新增路由'} onClose={onClose} onSave={async () => { const v = await form.validateFields(); onSave({ ...v, channels: entries, id: route?.id }); }} width={760}>
      <Form form={form} layout="vertical" initialValues={route || { enabled: true }}>
        <Form.Item name="model" label="请求模型" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
      <div style={{ marginTop: 12 }}>
        <div style={{ marginBottom: 6, color: 'var(--text2)' }}>渠道列表</div>
        <Table
          dataSource={entries}
          rowKey={(_, i) => String(i)}
          size="small"
          pagination={false}
          columns={[
            { title: '排序', render: (_, r, i) => <InputNumber value={r.order ?? i} onChange={(v) => { const next = [...entries]; next[i] = { ...next[i], order: v || 0 }; setEntries(next); }} /> },
            { title: '启用', render: (_, r, i) => <Switch checked={r.enabled !== false} onChange={(v) => { const next = [...entries]; next[i] = { ...next[i], enabled: v }; setEntries(next); }} /> },
            { title: '渠道', render: (_, r, i) => <Select value={r.provider} onChange={(v) => { const next = [...entries]; next[i] = { ...next[i], provider: v }; setEntries(next); }} options={(providers || []).map((p) => ({ value: p.name, label: p.name }))} style={{ width: 200 }} /> },
            { title: '账号数', render: (_, r, i) => <InputNumber value={r.account_count ?? 0} onChange={(v) => { const next = [...entries]; next[i] = { ...next[i], account_count: v || 0 }; setEntries(next); }} /> },
            { title: '操作', render: (_, _r, i) => <Button size="small" danger onClick={() => setEntries(entries.filter((_, idx) => idx !== i))}>删除</Button> },
          ]}
        />
        <Button onClick={() => setEntries([...entries, { order: entries.length, enabled: true, account_count: 0 }])} style={{ marginTop: 8 }}>+ 增加渠道</Button>
      </div>
    </AppModal>
  );
}

import { InputNumber } from 'antd';
```

- [ ] **Step 4: GroupDetailModal.tsx**

```tsx
import { AppModal } from '../../components/Modal';
import { Form, Input, Select, Switch, Table, Button, InputNumber } from 'antd';
import { useState } from 'react';
import { useApi } from '../../auth/useApi';
import { listProviders } from '../../api/providers';

export function GroupDetailModal({ name, group, onClose, onSave }: { name?: string; group?: any; onClose: () => void; onSave: (n: string, g: any) => void }) {
  const { data: providers } = useApi<any[]>(() => listProviders().then((r) => r?.providers || r || []), []);
  const [form] = Form.useForm();
  const [models, setModels] = useState<any[]>(group?.models || []);
  const [whitelist, setWhitelist] = useState<string[]>(group?.whitelist || []);
  const [blacklist, setBlacklist] = useState<string[]>(group?.blacklist || []);
  const [newModel, setNewModel] = useState('');

  return (
    <AppModal open title={name ? `编辑模型组 ${name}` : '新增模型组'} onClose={onClose} onSave={async () => { const v = await form.validateFields(); onSave(name || v.name, { ...v, models, whitelist, blacklist }); }} width={760}>
      <Form form={form} layout="vertical" initialValues={group || { enabled: true }}>
        <Form.Item name="name" label="组名" rules={[{ required: true }]}><Input disabled={!!name} /></Form.Item>
        <Form.Item name="remark" label="备注名"><Input placeholder="首页/客户端展示名" /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
      <div style={{ marginTop: 12 }}>
        <div style={{ marginBottom: 6, color: 'var(--text2)' }}>组内模型</div>
        <Table
          dataSource={models}
          rowKey={(_, i) => String(i)}
          size="small"
          pagination={false}
          columns={[
            { title: '排序', render: (_, _r, i) => <InputNumber value={i} disabled /> },
            { title: '模型', render: (_, r, i) => <Input value={r.model || ''} onChange={(e) => { const next = [...models]; next[i] = { ...next[i], model: e.target.value }; setModels(next); }} /> },
            { title: '操作', render: (_, _r, i) => <Button size="small" danger onClick={() => setModels(models.filter((_, idx) => idx !== i))}>删除</Button> },
          ]}
        />
        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
          <Input value={newModel} onChange={(e) => setNewModel(e.target.value)} placeholder="新增模型" />
          <Button onClick={() => { if (newModel) { setModels([...models, { model: newModel }]); setNewModel(''); } }}>添加</Button>
        </div>
      </div>
      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 6, color: 'var(--text2)' }}>渠道白名单</div>
        <Select mode="multiple" value={whitelist} onChange={setWhitelist} options={(providers || []).map((p) => ({ value: p.name, label: p.name }))} style={{ width: '100%' }} />
        <div style={{ marginBottom: 6, color: 'var(--text2)', marginTop: 12 }}>渠道黑名单</div>
        <Select mode="multiple" value={blacklist} onChange={setBlacklist} options={(providers || []).map((p) => ({ value: p.name, label: p.name }))} style={{ width: '100%' }} />
      </div>
    </AppModal>
  );
}
```

- [ ] **Step 5: ModelRoutingPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Card, Button, Space, Switch, Table, Tag } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { useModelRoutes } from './useModelRoutes';
import { useModelGroups } from './useModelGroups';
import { RouteDetailModal } from './RouteDetailModal';
import { GroupDetailModal } from './GroupDetailModal';

export function ModelRoutingPage() {
  const routes = useModelRoutes();
  const groups = useModelGroups();
  const [editRoute, setEditRoute] = useState<any | undefined>(undefined);
  const [editGroup, setEditGroup] = useState<{ name?: string; group?: any } | undefined>(undefined);

  useEffect(() => { routes.load(); groups.load(); }, []);

  return (
    <>
      <PageHeader title="模型路由 / 分组" sub="模型专用线路、API Key 线路与模型组" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card title={<Space>模型专用线路 <Button size="small" onClick={() => setEditRoute({})}>+ 增加</Button></Space>}>
          <Table
            loading={routes.loading}
            dataSource={routes.routing.routes}
            rowKey={(r) => r.id || r.model}
            size="small"
            columns={[
              { title: '请求模型', dataIndex: 'model' },
              { title: '启用', render: (_, r) => <Switch checked={r.enabled !== false} onChange={async (v) => { await routes.upsert({ ...r, enabled: v }); }} /> },
              { title: '渠道数', render: (_, r) => (r.channels || []).length },
              { title: '操作', render: (_, r) => <Space><Button size="small" onClick={() => setEditRoute(r)}>编辑</Button><Button size="small" danger onClick={async () => { if (confirm('确认删除？')) await routes.remove(r.id); }}>删除</Button></Space> },
            ]}
          />
        </Card>
        <Card title={<Space>模型组 <Button size="small" onClick={() => setEditGroup({})}>+ 增加</Button></Space>}>
          <Table
            dataSource={Object.entries(groups.groups).map(([name, g]) => ({ name, ...(g as any) }))}
            rowKey="name"
            size="small"
            columns={[
              { title: '组名', dataIndex: 'name' },
              { title: '备注', dataIndex: 'remark' },
              { title: '启用', render: (_, r) => <Switch checked={r.enabled !== false} onChange={async (v) => { await groups.upsert(r.name, { ...r, enabled: v }); }} /> },
              { title: '模型数', render: (_, r) => (r.models || []).length },
              { title: '操作', render: (_, r) => <Space><Button size="small" onClick={() => setEditGroup({ name: r.name, group: r })}>编辑</Button><Button size="small" danger onClick={async () => { if (confirm('确认删除？')) await groups.remove(r.name); }}>删除</Button></Space> },
            ]}
          />
        </Card>
      </div>
      {editRoute !== undefined && <RouteDetailModal route={editRoute.id ? editRoute : undefined} onClose={() => setEditRoute(undefined)} onSave={async (r) => { await routes.upsert(r); setEditRoute(undefined); }} />}
      {editGroup !== undefined && <GroupDetailModal name={editGroup.name} group={editGroup.group} onClose={() => setEditGroup(undefined)} onSave={async (n, g) => { await groups.upsert(n, g); setEditGroup(undefined); }} />}
    </>
  );
}
```

- [ ] **Step 6: ModelMetadataPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Card, Button, Form, Input, Modal, Select, Space, Table, Tag, Switch, message } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { listModelMetadata, putModelMetadata, deleteModelMetadata, fetchOpenRouter, syncOpenRouter, fetchModelsDev, syncModelsDev, putDefaultModelMetadata } from '../../api/modelMetadata';
import { listProviders } from '../../api/providers';
import { showToast } from '../../components/Toast';

export function ModelMetadataPage() {
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<any | null>(null);
  const [defaultOpen, setDefaultOpen] = useState(false);
  const [defaultVal, setDefaultVal] = useState<any>({});
  const [pickProvider, setPickProvider] = useState<{ open: boolean; items: any[] }>({ open: false, items: [] });
  const [providers, setProviders] = useState<any[]>([]);

  const load = async () => { setLoading(true); try { const r = await listModelMetadata(); setData(r?.models || r || []); } finally { setLoading(false); } };
  useEffect(() => { load(); listProviders().then((r) => setProviders(r?.providers || r || [])); }, []);

  const rows = data.filter((d) => !search || d.model_id?.toLowerCase().includes(search.toLowerCase()));

  return (
    <>
      <PageHeader title="模型广场" sub="模型可用性、渠道映射与元数据管理" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card>
          <Space wrap>
            <Input.Search placeholder="按 model_id 搜索" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 240 }} />
            <Button onClick={async () => { try { const r = await fetchOpenRouter(); setPickProvider({ open: true, items: r?.data || r || [] }); } catch { showToast('拉取失败', 'error'); } }}>从 OpenRouter 拉取</Button>
            <Button onClick={async () => { try { const r = await fetchModelsDev(); setPickProvider({ open: true, items: r?.data || r || [] }); } catch { showToast('拉取失败', 'error'); } }}>从 models.dev 拉取</Button>
            <Button onClick={() => setDefaultOpen(true)}>默认元数据</Button>
            <Button type="primary" onClick={() => setEditing({ model_id: '' })}>+ 新增</Button>
          </Space>
        </Card>
        <Card>
          <Table
            loading={loading}
            dataSource={rows}
            rowKey="model_id"
            size="small"
            columns={[
              { title: 'model_id', dataIndex: 'model_id' },
              { title: '显示名', dataIndex: 'name' },
              { title: '上下文', dataIndex: 'context_length', render: (v) => v || '-' },
              { title: '可发现', render: (_, r) => <Tag color={r.listed !== false ? 'green' : 'default'}>{r.listed !== false ? '是' : '否'}</Tag> },
              { title: '操作', render: (_, r) => <Space><Button size="small" onClick={() => setEditing(r)}>编辑</Button><Button size="small" danger onClick={async () => { if (confirm('确认删除？')) { await deleteModelMetadata(r.model_id); load(); } }}>删除</Button></Space> },
            ]}
          />
        </Card>
      </div>
      {editing && <MetadataEditor entry={editing} providers={providers} onClose={() => setEditing(null)} onSave={async (m) => { await putModelMetadata(m.model_id, m); setEditing(null); load(); }} />}
      <Modal open={defaultOpen} title="默认元数据" onCancel={() => setDefaultOpen(false)} onOk={async () => { await putDefaultModelMetadata(defaultVal); setDefaultOpen(false); showToast('已保存', 'success'); }} width={520}>
        <Form layout="vertical" onValuesChange={(_, v) => setDefaultVal(v)}>
          <Form.Item name="context_length" label="默认上下文"><Input type="number" /></Form.Item>
          <Form.Item name="supports_vision" label="视觉"><Switch /></Form.Item>
          <Form.Item name="supports_tools" label="工具调用"><Switch /></Form.Item>
        </Form>
      </Modal>
      <Modal open={pickProvider.open} title="选择要导入的模型" onCancel={() => setPickProvider({ open: false, items: [] })} onOk={async () => { await syncOpenRouter(pickProvider.items.filter((i) => i._selected).map((i) => i.id)); setPickProvider({ open: false, items: [] }); load(); }} width={760}>
        <Table dataSource={pickProvider.items} rowKey="id" size="small" columns={[
          { title: '选', render: (_, r) => <Switch checked={r._selected} onChange={(v) => { const next = pickProvider.items.map((x) => x.id === r.id ? { ...x, _selected: v } : x); setPickProvider({ open: true, items: next }); }} /> },
          { title: 'id', dataIndex: 'id' },
          { title: 'name', dataIndex: 'name' },
          { title: 'context', dataIndex: 'context_length' },
        ]} />
      </Modal>
    </>
  );
}

function MetadataEditor({ entry, providers, onClose, onSave }: { entry: any; providers: any[]; onClose: () => void; onSave: (m: any) => void }) {
  const [form] = Form.useForm();
  return (
    <Modal open title="编辑元数据" onCancel={onClose} onOk={async () => { const v = await form.validateFields(); onSave({ ...entry, ...v }); }} width={640}>
      <Form form={form} layout="vertical" initialValues={entry}>
        <Form.Item name="model_id" label="model_id" rules={[{ required: true }]}><Input disabled={!!entry.model_id} /></Form.Item>
        <Form.Item name="name" label="显示名"><Input /></Form.Item>
        <Form.Item name="context_length" label="上下文长度"><Input type="number" /></Form.Item>
        <Form.Item name="supports_vision" label="视觉" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="supports_tools" label="工具调用" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="providers" label="可见渠道"><Select mode="multiple" options={providers.map((p) => ({ value: p.name, label: p.name }))} /></Form.Item>
      </Form>
    </Modal>
  );
}
```

- [ ] **Step 7: 手工验收**

- 打开 `/#/models`，确认路由卡片、模型组卡片可编辑/删除/启用。
- 打开 `/#/model-metadata`，确认能搜索/编辑/删除，OpenRouter 拉取弹窗能选要导入的。

- [ ] **Step 8: 提交**

```bash
git add web/admin-react/src/features/models
git commit -m "feat(models): implement model routing, groups and metadata pages"
```

---

### Task 19: ApiKeysPage + ApiKeyEditModal + useApiKeys

**Files:**
- Create: `web/admin-react/src/features/api-keys/ApiKeysPage.tsx`
- Create: `web/admin-react/src/features/api-keys/ApiKeyEditModal.tsx`
- Create: `web/admin-react/src/features/api-keys/useApiKeys.ts`

- [ ] **Step 1: useApiKeys.ts**

```ts
import { useState } from 'react';
import { listApiKeys, setApiKeysEnabled, setApiKeyDisabled, addApiKey, updateApiKey, deleteApiKey, getApiKeyUsage } from '../../api/keys';
import { showToast } from '../../components/Toast';

export function useApiKeys() {
  const [keys, setKeys] = useState<any>({ enabled: true, keys: [] });
  const [usage, setUsage] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const load = async () => { setLoading(true); try { const [k, u] = await Promise.all([listApiKeys(), getApiKeyUsage()]); setKeys(k); setUsage(u); } finally { setLoading(false); } };
  const toggleAll = async (v: boolean) => { await setApiKeysEnabled(v); showToast('已更新', 'success'); await load(); };
  const toggle = async (id: string, disabled: boolean) => { await setApiKeyDisabled(id, disabled); showToast('已更新', 'success'); await load(); };
  const add = async (data: any) => { await addApiKey(data); showToast('已添加', 'success'); await load(); };
  const update = async (id: string, data: any) => { await updateApiKey(id, data); showToast('已更新', 'success'); await load(); };
  const remove = async (id: string) => { if (!confirm('确认删除？')) return; await deleteApiKey(id); showToast('已删除', 'success'); await load(); };
  return { keys, usage, loading, load, toggleAll, toggle, add, update, remove };
}
```

- [ ] **Step 2: ApiKeyEditModal.tsx**

```tsx
import { AppModal } from '../../components/Modal';
import { Form, Input, Select, InputNumber, Switch, Space, Divider } from 'antd';
import { JsonEditor } from '../../components/JsonEditor';

export function ApiKeyEditModal({ apiKey, onClose, onSave }: { apiKey?: any; onClose: () => void; onSave: (k: any) => void }) {
  const [form] = Form.useForm();
  return (
    <AppModal open title={apiKey?.id ? `编辑 ${apiKey.id}` : '新增 API Key'} onClose={onClose} onSave={async () => { const v = await form.validateFields(); onSave({ ...apiKey, ...v }); }} width={680}>
      <Form form={form} layout="vertical" initialValues={apiKey || { rate_limit: {} }}>
        <Form.Item name="id" label="Key 别名" rules={[{ required: true }]}><Input disabled={!!apiKey?.id} /></Form.Item>
        <Form.Item name="key" label="Key 值"><Input.Password placeholder="新增时填写" /></Form.Item>
        <Divider>RPM / TPM 限制</Divider>
        <Space>
          <Form.Item name={['rate_limit', 'rpm']} label="RPM"><InputNumber min={0} /></Form.Item>
          <Form.Item name={['rate_limit', 'tpm']} label="TPM"><InputNumber min={0} /></Form.Item>
        </Space>
        <Divider>渠道白/黑名单</Divider>
        <Form.Item name="whitelist" label="白名单（空=不限）"><Select mode="tags" tokenSeparators={[',']} /></Form.Item>
        <Form.Item name="blacklist" label="黑名单"><Select mode="tags" tokenSeparators={[',']} /></Form.Item>
      </Form>
    </AppModal>
  );
}
```

- [ ] **Step 3: ApiKeysPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, Space, Switch, Table, Tag, Input, Select } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { useApiKeys } from './useApiKeys';
import { ApiKeyEditModal } from './ApiKeyEditModal';
import { TokenUsageBar } from '../../components/TokenUsageBar';

export function ApiKeysPage() {
  const hook = useApiKeys();
  const [edit, setEdit] = useState<any | null>(null);
  useEffect(() => { hook.load(); }, []);

  return (
    <>
      <PageHeader title="API Keys" sub="密钥管理" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card extra={<Switch checked={hook.keys.enabled !== false} onChange={hook.toggleAll} />}>
          <Table
            loading={hook.loading}
            dataSource={hook.keys.keys || []}
            rowKey="id"
            size="small"
            columns={[
              { title: '别名', dataIndex: 'id' },
              { title: '状态', render: (_, r) => <Switch checked={r.disabled !== true} onChange={(v) => hook.toggle(r.id, !v)} /> },
              { title: 'RPM/TPM', render: (_, r) => `${r.rate_limit?.rpm || 0} / ${r.rate_limit?.tpm || 0}` },
              { title: '用量', render: (_, r) => hook.usage?.keys?.[r.id] ? <TokenUsageBar used={hook.usage.keys[r.id].used || 0} limit={hook.usage.keys[r.id].limit || 0} /> : '-' },
              { title: '白/黑名单', render: (_, r) => <Space>{(r.whitelist || []).map((x: string) => <Tag key={x} color="blue">{x}</Tag>)}</Space> },
              { title: '操作', render: (_, r) => <Space><Button size="small" onClick={() => setEdit(r)}>编辑</Button><Button size="small" danger onClick={() => hook.remove(r.id)}>删除</Button></Space> },
            ]}
          />
        </Card>
      </div>
      {edit !== null && <ApiKeyEditModal apiKey={edit.id ? edit : undefined} onClose={() => setEdit(null)} onSave={async (k) => { if (k.id) await hook.update(k.id, k); else await hook.add(k); setEdit(null); }} />}
    </>
  );
}
```

- [ ] **Step 4: 手工验收**

- 打开 `/#/api-keys`，确认列表渲染、添加/编辑/删除、整体启用开关生效。

- [ ] **Step 5: 提交**

```bash
git add web/admin-react/src/features/api-keys
git commit -m "feat(api-keys): implement api keys list and edit modal"
```

---

### Task 20: RequestLogsPage + RequestLogDetail + logFormatters 测试

**Files:**
- Create: `web/admin-react/src/features/request-logs/RequestLogsPage.tsx`
- Create: `web/admin-react/src/features/request-logs/RequestLogDetail.tsx`
- Create: `web/admin-react/src/features/request-logs/useRequestLogs.ts`
- Create: `web/admin-react/src/features/request-logs/logFormatters.ts`
- Create: `web/admin-react/src/features/request-logs/__tests__/logFormatters.test.ts`

- [ ] **Step 1: 写 logFormatters 测试**

```ts
import { describe, it, expect } from 'vitest';
import { formatLogError, compactLogContent, contentPreview, parseSseDataEvents } from '../logFormatters';

describe('formatLogError', () => {
  it('returns empty string for null/empty', () => {
    expect(formatLogError(null)).toBe('');
    expect(formatLogError('')).toBe('');
  });
  it('extracts message from JSON-like string', () => {
    expect(formatLogError('{"message":"x"}')).toContain('x');
  });
  it('returns string for plain text', () => {
    expect(formatLogError('boom')).toBe('boom');
  });
});

describe('compactLogContent', () => {
  it('truncates long content with marker', () => {
    const long = 'a'.repeat(500);
    const out = compactLogContent(long, 100);
    expect(out.length).toBeLessThanOrEqual(120);
    expect(out).toContain('…');
  });
});

describe('contentPreview', () => {
  it('returns preview for long content', () => {
    const out = contentPreview('x'.repeat(200), 50);
    expect(out).toContain('…');
  });
});

describe('parseSseDataEvents', () => {
  it('parses data: lines', () => {
    const events = parseSseDataEvents('data: hello\n\ndata: world\n\n');
    expect(events.map((e) => e.data)).toEqual(['hello', 'world']);
  });
  it('handles [DONE] marker', () => {
    const events = parseSseDataEvents('data: hi\n\ndata: [DONE]\n\n');
    expect(events.at(-1)?.data).toBe('[DONE]');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web/admin-react && pnpm test src/features/request-logs/__tests__/logFormatters.test.ts`
Expected: module not found。

- [ ] **Step 3: 实现 logFormatters.ts**

```ts
export function formatLogError(raw: string | null | undefined): string {
  if (!raw) return '';
  try {
    const obj = JSON.parse(raw);
    if (obj?.message) return String(obj.message);
    if (obj?.error) return String(obj.error);
  } catch { /* not JSON */ }
  return String(raw);
}

export function compactLogContent(content: string, max = 200): string {
  if (!content) return '';
  if (content.length <= max) return content;
  return content.slice(0, max) + '…';
}

export function contentPreview(content: string, max = 120): string {
  return compactLogContent(content, max);
}

export interface SseEvent { event?: string; data: string; }

export function parseSseDataEvents(raw: string): SseEvent[] {
  const events: SseEvent[] = [];
  const blocks = raw.split(/\n\n+/);
  for (const block of blocks) {
    if (!block.trim()) continue;
    const lines = block.split('\n');
    const ev: SseEvent = { data: '' };
    const dataLines: string[] = [];
    for (const line of lines) {
      if (line.startsWith('event:')) ev.event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    ev.data = dataLines.join('\n');
    if (ev.data) events.push(ev);
  }
  return events;
}
```

- [ ] **Step 4: 跑测试确认 PASS**

Run: `cd web/admin-react && pnpm test src/features/request-logs/__tests__/logFormatters.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: useRequestLogs.ts**

```ts
import { useState } from 'react';
import { listRequestLogs, getRequestLogDetail } from '../../api/logs';
import { showToast } from '../../components/Toast';

export function useRequestLogs() {
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(true);
  const load = async (params: Record<string, unknown>) => { setLoading(true); try { setData(await listRequestLogs(params)); } finally { setLoading(false); } };
  return { data, loading, load };
}
```

- [ ] **Step 6: RequestLogsPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, Input, Select, Space, Spin, Table, Tag } from 'antd';
import { Link, useSearchParams } from 'react-router-dom';
import { PageHeader } from '../../layout/PageHeader';
import { useRequestLogs } from './useRequestLogs';
import { TimeRangePicker, type TimeRangeValue } from '../../components/TimeRangePicker';
import { StatusBadge } from '../../components/StatusBadge';
import { formatDuration, formatCount } from '../../utils/format';

export function RequestLogsPage() {
  const hook = useRequestLogs();
  const [sp, setSp] = useSearchParams();
  const [range, setRange] = useState<TimeRangeValue>({ start: sp.get('start') || '', end: sp.get('end') || '' });
  const [provider, setProvider] = useState(sp.get('provider') || '');
  const [model, setModel] = useState(sp.get('model') || '');
  const [success, setSuccess] = useState(sp.get('success') || '');
  const [page, setPage] = useState(Number(sp.get('page') || '1'));
  const [pageSize, setPageSize] = useState(Number(sp.get('page_size') || '20'));

  const fetchData = () => {
    const params: Record<string, unknown> = { page, page_size: pageSize };
    if (range.start) params.start = range.start;
    if (range.end) params.end = range.end;
    if (provider) params.provider = provider;
    if (model) params.model = model;
    if (success) params.success = success;
    hook.load(params);
    setSp(params as Record<string, string>);
  };
  useEffect(() => { fetchData(); }, [page, pageSize]);

  return (
    <>
      <PageHeader title="请求日志" sub="请求、响应、重试与异常详情" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card>
          <Space wrap>
            <TimeRangePicker value={range} onChange={setRange} />
            <Input placeholder="渠道" value={provider} onChange={(e) => setProvider(e.target.value)} style={{ width: 160 }} />
            <Input placeholder="模型" value={model} onChange={(e) => setModel(e.target.value)} style={{ width: 160 }} />
            <Select value={success} onChange={setSuccess} style={{ width: 120 }} allowClear placeholder="结果" options={[{ value: 'true', label: '成功' }, { value: 'false', label: '失败' }]} />
            <Button type="primary" onClick={fetchData}>查询</Button>
          </Space>
        </Card>
        <Card>
          {hook.loading ? <Spin /> : (
            <Table
              dataSource={hook.data.items || []}
              rowKey="id"
              size="small"
              pagination={{
                current: page,
                pageSize,
                total: hook.data.total || 0,
                onChange: (p, ps) => { setPage(p); setPageSize(ps); },
              }}
              columns={[
                { title: '时间', dataIndex: 'created_at', width: 160 },
                { title: '渠道/账号', render: (_, r) => `${r.provider}/${r.account || '-'}` },
                { title: 'API Key', dataIndex: 'api_key_name' },
                { title: '模型', dataIndex: 'model' },
                { title: '用时/首字', render: (_, r) => `${formatDuration(r.duration_ms || 0)} / ${r.ttft_ms ? formatDuration(r.ttft_ms) : '-'}` },
                { title: '输入/输出', render: (_, r) => `${formatCount(r.prompt_tokens)} / ${formatCount(r.completion_tokens)}` },
                { title: '结果', render: (_, r) => <StatusBadge status={r.status} success={r.success} /> },
                { title: '异常', dataIndex: 'error', render: (v) => v ? <Tag color="red">{String(v).slice(0, 60)}</Tag> : '-' },
                { title: '操作', render: (_, r) => <Link to={`/request-logs/${r.id}`}>详情</Link> },
              ]}
            />
          )}
        </Card>
      </div>
    </>
  );
}
```

- [ ] **Step 7: RequestLogDetail.tsx**

```tsx
import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { Card, Spin, Tag } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { getRequestLogDetail } from '../../api/logs';
import { StatusBadge } from '../../components/StatusBadge';
import { JsonTree } from '../../components/JsonTree';
import { CopyButton } from '../../components/CopyButton';
import { formatDuration, formatCount } from '../../utils/format';
import { parseRetryPath, formatRetryPathSummary } from '../../utils/requestPath';
import { formatLogError, parseSseDataEvents } from './logFormatters';

export function RequestLogDetail() {
  const { id = '' } = useParams();
  const [log, setLog] = useState<any>(null);
  useEffect(() => { getRequestLogDetail(id).then(setLog).catch(() => setLog(null)); }, [id]);
  if (!log) return <Spin />;
  const ids = parseRetryPath(log.retry_path);
  return (
    <>
      <PageHeader title={`请求日志 ${id}`} sub={log.request_id} extra={<Link to="/request-logs">返回</Link>} />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card title="概要">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8, fontSize: 13 }}>
            <div>渠道/账号：{log.provider}/{log.account || '-'}</div>
            <div>API Key：{log.api_key_name || '-'}</div>
            <div>模型：{log.model}</div>
            <div>用时/首字：{formatDuration(log.duration_ms || 0)} / {log.ttft_ms ? formatDuration(log.ttft_ms) : '-'}</div>
            <div>输入/输出：{formatCount(log.prompt_tokens)} / {formatCount(log.completion_tokens)}</div>
            <div>缓存读/写：{formatCount(log.cached_tokens)} / {formatCount(log.cache_creation_tokens)}</div>
            <div>结果：<StatusBadge status={log.status} success={log.success} /></div>
            <div>时间：{log.created_at}</div>
          </div>
        </Card>
        <Card title={`重试路径 (${ids.length})`}>
          <Tag color="blue">请求 ID：{log.request_id}</Tag>
          <div style={{ marginTop: 8 }}>{formatRetryPathSummary(log.retry_entries || [])}</div>
        </Card>
        {log.request_body && <Card title="请求" extra={<CopyButton text={JSON.stringify(log.request_body, null, 2)} />}>
          <JsonTree data={log.request_body} />
        </Card>}
        {log.response_body && <Card title="响应" extra={<CopyButton text={JSON.stringify(log.response_body, null, 2)} />}>
          <JsonTree data={log.response_body} />
        </Card>}
        {log.sse_events && <Card title="SSE 流">
          <pre style={{ fontSize: 12, fontFamily: 'var(--fontM)' }}>{parseSseDataEvents(log.sse_events).map((e, i) => <div key={i}>{e.data}</div>)}</pre>
        </Card>}
        {log.error && <Card title="异常"><pre style={{ color: 'var(--red)', fontSize: 12 }}>{formatLogError(log.error)}</pre></Card>}
      </div>
    </>
  );
}
```

- [ ] **Step 8: 手工验收**

- 打开 `/#/request-logs`，确认筛选、分页、跳详情生效。
- 详情页确认 4 段渲染（请求/响应/重试/异常），Token 数与缓存字段展示。

- [ ] **Step 9: 提交**

```bash
git add web/admin-react/src/features/request-logs
git commit -m "feat(logs): implement request logs list and detail with formatters"
```

---

### Task 21: ConfigPage + ProxiesPage + AdminSettingsPage

**Files:**
- Create: `web/admin-react/src/features/config/ConfigPage.tsx`
- Create: `web/admin-react/src/features/config/ProxiesPage.tsx`
- Create: `web/admin-react/src/features/config/AdminSettingsPage.tsx`

- [ ] **Step 1: ConfigPage.tsx（主配置 tabs）**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, Form, Input, InputNumber, Select, Space, Switch, Tabs, message } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { getMainConfig, putMainConfig, putServer, putLogging, putSystem, putPostgres, putModelRefresh, putMessageDelete, putLogRetention, putRedis, putRetry, putStream, putRateLimit, getTokenizerRules, putTokenizerRules } from '../../api/config';
import { showToast } from '../../components/Toast';

export function ConfigPage() {
  const [cfg, setCfg] = useState<any>(null);
  useEffect(() => { getMainConfig().then(setCfg).catch(() => setCfg({})); }, []);
  if (!cfg) return null;
  return (
    <>
      <PageHeader title="主配置" sub="config.json" />
      <div style={{ padding: 20 }}>
        <Tabs items={[
          { key: 'server', label: '服务', children: <ServerTab cfg={cfg} onSave={async (v) => { await putServer(v.host, v.port); showToast('已保存', 'success'); }} /> },
          { key: 'logging', label: '日志', children: <LoggingTab cfg={cfg} onSave={async (level) => { await putLogging(level); showToast('已保存', 'success'); }} /> },
          { key: 'system', label: '系统', children: <SystemTab cfg={cfg} onSave={async (debug) => { await putSystem(debug); showToast('已保存', 'success'); }} /> },
          { key: 'postgres', label: 'Postgres', children: <PostgresTab onSave={async (d) => { await putPostgres(d); showToast('已保存', 'success'); }} /> },
          { key: 'redis', label: 'Redis', children: <RedisTab cfg={cfg} onSave={async (d) => { await putRedis(d); showToast('已保存', 'success'); }} /> },
          { key: 'model-refresh', label: '模型刷新', children: <ModelRefreshTab cfg={cfg} onSave={async (enabled, interval) => { await putModelRefresh(enabled, interval); showToast('已保存', 'success'); }} /> },
          { key: 'message-delete', label: '消息清理', children: <MessageDeleteTab cfg={cfg} onSave={async (enabled, interval) => { await putMessageDelete(enabled, interval); showToast('已保存', 'success'); }} /> },
          { key: 'log-retention', label: '日志保留', children: <LogRetentionTab cfg={cfg} onSave={async (n) => { await putLogRetention(n); showToast('已保存', 'success'); }} /> },
          { key: 'retry', label: '重试', children: <RetryTab cfg={cfg} onSave={async (n) => { await putRetry(n); showToast('已保存', 'success'); }} /> },
          { key: 'stream', label: '流式', children: <StreamTab cfg={cfg} onSave={async (d) => { await putStream(d); showToast('已保存', 'success'); }} /> },
          { key: 'rate-limit', label: '限流', children: <RateLimitTab cfg={cfg} onSave={async (d) => { await putRateLimit(d); showToast('已保存', 'success'); }} /> },
          { key: 'tokenizer', label: 'Tokenizer 规则', children: <TokenizerTab onSave={async (d) => { await putTokenizerRules(d); showToast('已保存', 'success'); }} /> },
        ]} />
      </div>
    </>
  );
}

function ServerTab({ cfg, onSave }: { cfg: any; onSave: (v: { host: string; port: number }) => void }) {
  const [host, setHost] = useState(cfg.server?.host || '0.0.0.0');
  const [port, setPort] = useState(cfg.server?.port || 8000);
  return <Card extra={<Button type="primary" onClick={() => onSave({ host, port })}>保存</Button>}>
    <Space><div>Host</div><Input value={host} onChange={(e) => setHost(e.target.value)} /></Space>
    <Space style={{ marginTop: 8 }}><div>Port</div><InputNumber value={port} onChange={(v) => setPort(v || 0)} /></Space>
  </Card>;
}

function LoggingTab({ cfg, onSave }: { cfg: any; onSave: (level: string) => void }) {
  const [level, setLevel] = useState(cfg.logging?.level || 'INFO');
  return <Card extra={<Button type="primary" onClick={() => onSave(level)}>保存</Button>}>
    <Select value={level} onChange={setLevel} options={['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((v) => ({ value: v, label: v }))} />
  </Card>;
}

function SystemTab({ cfg, onSave }: { cfg: any; onSave: (debug: boolean) => void }) {
  const [debug, setDebug] = useState(!!cfg.system?.debug);
  return <Card extra={<Button type="primary" onClick={() => onSave(debug)}>保存</Button>}>
    <Switch checked={debug} onChange={setDebug} /> 调试模式
  </Card>;
}

function PostgresTab({ onSave }: { onSave: (d: { host: string; port: number; user: string; password: string; database: string }) => void }) {
  const [host, setHost] = useState(''); const [port, setPort] = useState(5432); const [user, setUser] = useState(''); const [password, setPassword] = useState(''); const [database, setDatabase] = useState('');
  return <Card extra={<Button type="primary" onClick={() => onSave({ host, port, user, password, database })}>保存</Button>}>
    <Space wrap>
      <Input placeholder="Host" value={host} onChange={(e) => setHost(e.target.value)} />
      <InputNumber placeholder="Port" value={port} onChange={(v) => setPort(v || 0)} />
      <Input placeholder="User" value={user} onChange={(e) => setUser(e.target.value)} />
      <Input.Password placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <Input placeholder="Database" value={database} onChange={(e) => setDatabase(e.target.value)} />
    </Space>
  </Card>;
}

function RedisTab({ cfg, onSave }: { cfg: any; onSave: (d: any) => void }) {
  const [text, setText] = useState(JSON.stringify(cfg.redis || {}, null, 2));
  return <Card extra={<Button type="primary" onClick={() => { try { onSave(JSON.parse(text)); } catch { message.error('JSON 解析失败'); } }}>保存</Button>}>
    <Input.TextArea rows={8} value={text} onChange={(e) => setText(e.target.value)} style={{ fontFamily: 'var(--fontM)', fontSize: 12 }} />
  </Card>;
}

function ModelRefreshTab({ cfg, onSave }: { cfg: any; onSave: (enabled: boolean, interval: number) => void }) {
  const [enabled, setEnabled] = useState(!!cfg.model_refresh?.enabled);
  const [interval, setInterval] = useState(cfg.model_refresh?.interval_minutes || 60);
  return <Card extra={<Button type="primary" onClick={() => onSave(enabled, interval)}>保存</Button>}>
    <Space><Switch checked={enabled} onChange={setEnabled} /> 启用</Space>
    <Space style={{ marginTop: 8 }}><div>间隔（分钟）</div><InputNumber value={interval} onChange={(v) => setInterval(v || 0)} /></Space>
  </Card>;
}

function MessageDeleteTab({ cfg, onSave }: { cfg: any; onSave: (enabled: boolean, interval: number) => void }) {
  const [enabled, setEnabled] = useState(!!cfg.message_delete?.enabled);
  const [interval, setInterval] = useState(cfg.message_delete?.interval_minutes || 60);
  return <Card extra={<Button type="primary" onClick={() => onSave(enabled, interval)}>保存</Button>}>
    <Space><Switch checked={enabled} onChange={setEnabled} /> 启用</Space>
    <Space style={{ marginTop: 8 }}><div>间隔（分钟）</div><InputNumber value={interval} onChange={(v) => setInterval(v || 0)} /></Space>
  </Card>;
}

function LogRetentionTab({ cfg, onSave }: { cfg: any; onSave: (n: number) => void }) {
  const [n, setN] = useState(cfg.log_retention?.max_entries || 10000);
  return <Card extra={<Button type="primary" onClick={() => onSave(n)}>保存</Button>}>
    <Space><div>最大保留</div><InputNumber value={n} onChange={(v) => setN(v || 0)} /></Space>
  </Card>;
}

function RetryTab({ cfg, onSave }: { cfg: any; onSave: (n: number) => void }) {
  const [n, setN] = useState(cfg.retry?.max_retries || 3);
  return <Card extra={<Button type="primary" onClick={() => onSave(n)}>保存</Button>}>
    <Space><div>最大重试</div><InputNumber value={n} onChange={(v) => setN(v || 0)} /></Space>
  </Card>;
}

function StreamTab({ cfg, onSave }: { cfg: any; onSave: (d: any) => void }) {
  const [text, setText] = useState(JSON.stringify(cfg.stream || {}, null, 2));
  return <Card extra={<Button type="primary" onClick={() => { try { onSave(JSON.parse(text)); } catch { message.error('JSON 解析失败'); } }}>保存</Button>}>
    <Input.TextArea rows={6} value={text} onChange={(e) => setText(e.target.value)} style={{ fontFamily: 'var(--fontM)', fontSize: 12 }} />
  </Card>;
}

function RateLimitTab({ cfg, onSave }: { cfg: any; onSave: (d: any) => void }) {
  const [text, setText] = useState(JSON.stringify(cfg.rate_limit || {}, null, 2));
  return <Card extra={<Button type="primary" onClick={() => { try { onSave(JSON.parse(text)); } catch { message.error('JSON 解析失败'); } }}>保存</Button>}>
    <Input.TextArea rows={8} value={text} onChange={(e) => setText(e.target.value)} style={{ fontFamily: 'var(--fontM)', fontSize: 12 }} />
  </Card>;
}

function TokenizerTab({ onSave }: { onSave: (d: any) => void }) {
  const [rules, setRules] = useState<any>(null);
  useEffect(() => { getTokenizerRules().then(setRules).catch(() => setRules({})); }, []);
  const [text, setText] = useState('');
  useEffect(() => { setText(JSON.stringify(rules || {}, null, 2)); }, [rules]);
  if (!rules) return null;
  return <Card extra={<Button type="primary" onClick={() => { try { onSave(JSON.parse(text)); } catch { message.error('JSON 解析失败'); } }}>保存</Button>}>
    <Input.TextArea rows={14} value={text} onChange={(e) => setText(e.target.value)} style={{ fontFamily: 'var(--fontM)', fontSize: 12 }} />
  </Card>;
}
```

- [ ] **Step 2: ProxiesPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, Space, Table, Tag } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { getProxies, putProxies } from '../../api/proxies';
import { AppModal } from '../../components/Modal';
import { showToast } from '../../components/Toast';

export function ProxiesPage() {
  const [data, setData] = useState<any>({ proxies: [] });
  const [editing, setEditing] = useState<{ open: boolean; value?: any; index?: number }>({ open: false });
  const load = async () => { setData(await getProxies().catch(() => ({ proxies: [] }))); };
  useEffect(() => { load(); }, []);
  const onSave = async (v: any) => {
    const next = { ...data, proxies: data.proxies || [] };
    if (editing.index === undefined) next.proxies.push(v);
    else next.proxies[editing.index] = v;
    await putProxies(next);
    setEditing({ open: false });
    showToast('已保存', 'success');
    load();
  };
  return (
    <>
      <PageHeader title="代理池" sub="代理服务器管理" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card extra={<Button type="primary" onClick={() => setEditing({ open: true })}>+ 添加</Button>}>
          <Table
            dataSource={data.proxies || []}
            rowKey={(_, i) => String(i)}
            size="small"
            columns={[
              { title: 'URL', dataIndex: 'url' },
              { title: '状态', render: (_, r) => <Tag color={r.enabled !== false ? 'green' : 'default'}>{r.enabled !== false ? '启用' : '关闭'}</Tag> },
              { title: '操作', render: (_, _r, i) => <Space>
                <Button size="small" onClick={() => setEditing({ open: true, value: data.proxies[i], index: i })}>编辑</Button>
                <Button size="small" danger onClick={async () => { const next = { ...data, proxies: data.proxies.filter((_: any, idx: number) => idx !== i) }; await putProxies(next); load(); }}>删除</Button>
              </Space> },
            ]}
          />
        </Card>
      </div>
      <AppModal open={editing.open} title={editing.value ? '编辑代理' : '新增代理'} onClose={() => setEditing({ open: false })} onSave={async () => { await onSave(editing.value || { url: '', enabled: true }); }} width={520}>
        <ProxyForm initial={editing.value} onChange={(v) => setEditing({ ...editing, value: v })} />
      </AppModal>
    </>
  );
}

function ProxyForm({ initial, onChange }: { initial?: any; onChange: (v: any) => void }) {
  const [url, setUrl] = useState(initial?.url || '');
  const [enabled, setEnabled] = useState(initial?.enabled !== false);
  useEffect(() => { onChange({ url, enabled }); }, [url, enabled]);
  return <Space direction="vertical" style={{ width: '100%' }}><Input placeholder="http://user:pass@host:port" value={url} onChange={(e) => setUrl(e.target.value)} /><div><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> 启用</div></Space>;
}
```

- [ ] **Step 3: AdminSettingsPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, InputNumber, Space } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { getAdminConfig, putSessionDuration } from '../../api/config';
import { showToast } from '../../components/Toast';

export function AdminSettingsPage() {
  const [cfg, setCfg] = useState<any>(null);
  const [hours, setHours] = useState(12);
  useEffect(() => { getAdminConfig().then((c) => { setCfg(c); setHours(c?.session_duration_hours || 12); }).catch(() => setCfg({})); }, []);
  if (!cfg) return null;
  return (
    <>
      <PageHeader title="管理员设置" sub="session 与服务端配置" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card title="会话有效期" extra={<Button type="primary" onClick={async () => { await putSessionDuration(hours); showToast('已保存', 'success'); }}>保存</Button>}>
          <Space><div>小时</div><InputNumber min={1} value={hours} onChange={(v) => setHours(v || 0)} /></Space>
        </Card>
      </div>
    </>
  );
}
```

- [ ] **Step 4: 手工验收**

- 打开 `/#/config`，切 12 个 tab，确认每个保存按钮触发对应 PUT。
- 打开 `/#/proxies`，增删代理。
- 打开 `/#/admin-settings`，修改 session 时长。

- [ ] **Step 5: 提交**

```bash
git add web/admin-react/src/features/config
git commit -m "feat(config): implement main config, proxies and admin settings pages"
```

---

### Task 22: CustomChannelCreatePage + TestModelPage + UsageGuidePage + DocsPage

**Files:**
- Create: `web/admin-react/src/features/custom-providers/CustomChannelCreatePage.tsx`
- Create: `web/admin-react/src/features/test-model/TestModelPage.tsx`
- Create: `web/admin-react/src/features/usage-guide/UsageGuidePage.tsx`
- Create: `web/admin-react/src/features/docs/DocsPage.tsx`

- [ ] **Step 1: CustomChannelCreatePage.tsx**

```tsx
import { useState } from 'react';
import { Button, Card, Form, Input, InputNumber, Select, Space, Switch, Table, Tag } from 'antd';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../../layout/PageHeader';
import { createCustomProvider, fetchModels } from '../../api/customProviders';
import { showToast } from '../../components/Toast';

export function CustomChannelCreatePage() {
  const [form] = Form.useForm();
  const [models, setModels] = useState<any[]>([]);
  const [accounts, setAccounts] = useState<any[]>([{ username: '', password: '' }]);
  const [proxy, setProxy] = useState('');
  const [redir, setRedir] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const nav = useNavigate();

  const onSubmit = async () => {
    const v = await form.validateFields();
    try {
      await createCustomProvider({
        name: v.name,
        type: v.protocol,
        base_url: v.baseUrl,
        chat_path: v.chatPath || '/v1/chat/completions',
        models_path: v.modelsPath || '/v1/models',
        api_key: v.apiKey,
        proxy: proxy || undefined,
        accounts: accounts.filter((a) => a.username),
        redirects: redir,
        models,
      });
      showToast('已创建', 'success');
      nav(`/channels/${encodeURIComponent(v.name)}`);
    } catch (e: any) { showToast(e?.response?.data?.detail || '创建失败', 'error'); }
  };

  const onFetch = async () => {
    const v = await form.validateFields(['baseUrl', 'apiKey']);
    setLoading(true);
    try { const r = await fetchModels({ base_url: v.baseUrl, api_key: v.apiKey }); setModels(r?.data || r || []); }
    catch (e: any) { showToast(e?.response?.data?.detail || '拉取失败', 'error'); }
    finally { setLoading(false); }
  };

  return (
    <>
      <PageHeader title="创建自定义渠道" sub="创建后会作为独立渠道出现在列表中" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card>
          <Form form={form} layout="vertical" initialValues={{ protocol: 'openai', chatPath: '/v1/chat/completions', modelsPath: '/v1/models' }}>
            <Form.Item name="name" label="渠道名" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="protocol" label="协议"><Select options={[{ value: 'openai', label: 'OpenAI Chat' }, { value: 'anthropic', label: 'Anthropic' }, { value: 'responses', label: 'OpenAI Responses' }]} /></Form.Item>
            <Form.Item name="baseUrl" label="Base URL" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="apiKey" label="API Key"><Input.Password /></Form.Item>
            <Form.Item name="chatPath" label="聊天路径"><Input /></Form.Item>
            <Form.Item name="modelsPath" label="模型路径"><Input /></Form.Item>
            <Form.Item label="代理 URL"><Input value={proxy} onChange={(e) => setProxy(e.target.value)} /></Form.Item>
          </Form>
        </Card>
        <Card title="账号" extra={<Button onClick={() => setAccounts([...accounts, { username: '', password: '' }])}>+ 添加</Button>}>
          <Table
            dataSource={accounts}
            rowKey={(_, i) => String(i)}
            size="small"
            pagination={false}
            columns={[
              { title: '账号', render: (_, _r, i) => <Input value={accounts[i].username} onChange={(e) => { const next = [...accounts]; next[i] = { ...next[i], username: e.target.value }; setAccounts(next); }} /> },
              { title: '密码/Token', render: (_, _r, i) => <Input.Password value={accounts[i].password} onChange={(e) => { const next = [...accounts]; next[i] = { ...next[i], password: e.target.value }; setAccounts(next); }} /> },
              { title: '操作', render: (_, _r, i) => <Button size="small" danger onClick={() => setAccounts(accounts.filter((_, idx) => idx !== i))}>删除</Button> },
            ]}
          />
        </Card>
        <Card title="模型" extra={<Space><Button onClick={onFetch} loading={loading}>从上游拉取</Button><Button onClick={() => setModels([...models, { id: '' }])}>+ 添加</Button></Space>}>
          <Table
            dataSource={models}
            rowKey={(_, i) => String(i)}
            size="small"
            pagination={false}
            columns={[
              { title: '模型 ID', render: (_, r, i) => <Input value={r.id} onChange={(e) => { const next = [...models]; next[i] = { ...next[i], id: e.target.value }; setModels(next); }} /> },
              { title: '操作', render: (_, _r, i) => <Button size="small" danger onClick={() => setModels(models.filter((_, idx) => idx !== i))}>删除</Button> },
            ]}
          />
        </Card>
        <Button type="primary" size="large" onClick={onSubmit}>创建渠道</Button>
      </div>
    </>
  );
}
```

- [ ] **Step 2: TestModelPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Button, Card, Form, Input, Select, Space, Spin } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { listV1Models, chatCompletions } from '../../api/testModel';
import { getStoredToken } from '../../auth/token';
import { useApi } from '../../auth/useApi';
import { listApiKeys } from '../../api/keys';
import { showToast } from '../../components/Toast';

export function TestModelPage() {
  const token = getStoredToken();
  const { data: modelList } = useApi<any>(() => listV1Models(token).then((r) => r?.data || r || []), [token]);
  const { data: keys } = useApi<any>(() => listApiKeys().then((r) => r?.keys || r || []), []);
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [prompt, setPrompt] = useState('你好');
  const [stream, setStream] = useState(false);
  const [out, setOut] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { if (!apiKey && keys?.length) setApiKey(keys[0].key || ''); }, [keys]);

  const onRun = async () => {
    if (!model) { showToast('请选择模型', 'error'); return; }
    setBusy(true); setOut('');
    try {
      const data = await chatCompletions(apiKey, { model, messages: [{ role: 'user', content: prompt }], stream });
      setOut(JSON.stringify(data, null, 2));
    } catch (e: any) { setOut(e?.response?.data?.detail || e?.message || '失败'); }
    finally { setBusy(false); }
  };

  return (
    <>
      <PageHeader title="测试模型" sub="选择模型与测试类型进行调用测试" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card>
          <Space wrap>
            <Select placeholder="选择模型" value={model} onChange={setModel} style={{ width: 240 }} showSearch options={(modelList || []).map((m: any) => ({ value: m.id, label: m.id }))} />
            <Select placeholder="API Key" value={apiKey} onChange={setApiKey} style={{ width: 200 }} options={(keys || []).map((k: any) => ({ value: k.key, label: k.id }))} />
            <Input placeholder="输入提示" value={prompt} onChange={(e) => setPrompt(e.target.value)} style={{ width: 240 }} />
            <label><input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} /> 流式</label>
            <Button type="primary" loading={busy} onClick={onRun}>运行</Button>
          </Space>
        </Card>
        {busy && <Spin />}
        {out && <Card title="输出"><pre style={{ fontSize: 12, fontFamily: 'var(--fontM)' }}>{out}</pre></Card>}
      </div>
    </>
  );
}
```

- [ ] **Step 3: UsageGuidePage.tsx**

```tsx
import { Card } from 'antd';
import { PageHeader } from '../../layout/PageHeader';

const endpoints = [
  ['/v1/chat/completions', 'POST', 'OpenAI Chat Completions 兼容接口，支持非流式和流式。'],
  ['/v1/messages', 'POST', 'Anthropic Messages 兼容接口，支持 x-api-key 鉴权。'],
  ['/v1/responses', 'POST', 'OpenAI Responses 兼容接口。'],
  ['/v1/models', 'GET', '列出可用模型（含模型组）。'],
  ['/v1/dashboard/billing/usage', 'GET', '查询用量与计费。'],
  ['/v1/token/count', 'POST', '估算消息 token 数。'],
  ['/v1/images/generations', 'POST', '图片生成。'],
];

export function UsageGuidePage() {
  const curlChat = `curl -X POST \${location.origin}/v1/chat/completions \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '${JSON.stringify({ model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'hi' }] }, null, 2)}'`;
  return (
    <>
      <PageHeader title="使用方法" sub="接口、入参与可用模型" />
      <div style={{ padding: 20, display: 'grid', gap: 16 }}>
        <Card title="端点列表">
          <table style={{ width: '100%', fontSize: 13 }}>
            <thead><tr style={{ background: 'var(--bg3)', color: 'var(--text2)' }}><th style={{ padding: 10, textAlign: 'left' }}>接口</th><th style={{ padding: 10, width: 100 }}>方法</th><th style={{ padding: 10, textAlign: 'left' }}>说明</th></tr></thead>
            <tbody>
              {endpoints.map(([p, m, d]) => <tr key={p} style={{ borderTop: '1px solid var(--border)' }}><td style={{ padding: 10 }}>{p}</td><td style={{ padding: 10, textAlign: 'center' }}>{m}</td><td style={{ padding: 10, color: 'var(--text2)' }}>{d}</td></tr>)}
            </tbody>
          </table>
        </Card>
        <Card title="curl 示例：Chat Completions"><pre style={{ fontFamily: 'var(--fontM)', fontSize: 12 }}>{curlChat}</pre></Card>
      </div>
    </>
  );
}
```

- [ ] **Step 4: DocsPage.tsx**

```tsx
import { useEffect, useState } from 'react';
import { Card, Spin, Tree } from 'antd';
import { PageHeader } from '../../layout/PageHeader';
import { listDocs, getDoc } from '../../api/docs';

export function DocsPage() {
  const [tree, setTree] = useState<any[]>([]);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => { listDocs().then((d) => setTree(d?.tree || d || [])).finally(() => setLoading(false)); }, []);
  if (loading) return <Spin />;
  return (
    <>
      <PageHeader title="项目文档" sub="设计、计划、白皮书" />
      <div style={{ padding: 20, display: 'grid', gridTemplateColumns: '260px 1fr', gap: 16 }}>
        <Card>
          <Tree
            treeData={tree}
            onSelect={async (_keys, info: any) => {
              const node = info.node;
              if (node.isLeaf && node.path) {
                const c = await getDoc(node.path);
                setContent(typeof c === 'string' ? c : JSON.stringify(c, null, 2));
              }
            }}
          />
        </Card>
        <Card><pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'var(--fontM)', fontSize: 12 }}>{content || '选择左侧文档'}</pre></Card>
      </div>
    </>
  );
}
```

- [ ] **Step 5: 手工验收**

- `/#/custom-channel-create`：填表、添加账号、点击从上游拉取模型、创建跳转详情。
- `/#/test-model`：选模型、选 API Key、发请求、看到响应 JSON。
- `/#/usage-guide`：看到 7 行端点表和 curl 示例。
- `/#/docs`：左侧树点击、右侧渲染 markdown。

- [ ] **Step 6: 提交**

```bash
git add web/admin-react/src/features/custom-providers web/admin-react/src/features/test-model web/admin-react/src/features/usage-guide web/admin-react/src/features/docs
git commit -m "feat(pages): implement custom create, test model, usage guide and docs pages"
```

---

### Task 23: 端到端验收 + 修复 + 最终构建

**Files:**
- Modify: 任何文件（按需）

- [ ] **Step 1: 跑全部 vitest**

Run: `cd web/admin-react && pnpm test`
Expected: 所有用例 PASS。

- [ ] **Step 2: 跑生产构建**

Run: `cd web/admin-react && pnpm build`
Expected: 产物在 `static/admin-react/index.html`，vite 报 build 成功。

- [ ] **Step 3: 起后端 + 静态访问**

- 启动后端：`python main.py`。
- 浏览器打开 `http://127.0.0.1:8000/admin/v2`，看到登录页。
- 登录后逐个 Tab 走查：仪表盘、渠道、模型路由、API Key、请求日志、账号、余额方式、模型元数据、主配置、代理、自定义渠道创建、测试模型、使用方法、项目文档。
- 检查项：4 套主题切换、跨 tab token 同步、登录/登出、登录后回跳原 URL、表单校验、删除/编辑主流程、错误 toast、401 自动跳登录。

- [ ] **Step 4: 修复手工验收发现的问题**

按需修改、补 commit。

- [ ] **Step 5: 跑最终回归**

- 跑后端测试：`pytest tests/ -q`。
- 跑 vitest：`cd web/admin-react && pnpm test`。
- 跑构建：`cd web/admin-react && pnpm build`。
- 三者全部通过。

- [ ] **Step 6: 提交**

```bash
git add -u
git commit -m "fix: react admin e2e verification fixes"
```

如果无修复可跳过本步。

---

## Self-Review

- **Spec 覆盖**：本计划按设计文档（`docs/superpowers/specs/2026-06-01-react-admin-rewrite-design.md`）的每个 section 给出对应任务：
  - 工程脚手架 → Task 1
  - 工具层纯函数（TDD） → Tasks 2-5
  - API 客户端 + token 同步 → Task 6
  - API 封装（auth/dashboard/providers/accounts/keys/proxies/customProviders/balance/config/modelMetadata/docs/logs/testModel/models） → Tasks 7-8
  - 主题 + 布局 + Toast → Task 9
  - 通用组件 → Task 10
  - 路由 + main + App → Task 11
  - 后端 1 行路由 + 构建 + dev 验证 → Task 12
  - LoginPage + 改密 → Task 13
  - Dashboard + DataDashboard → Task 14
  - Channels + ChannelDetail + CooldownPolicy → Task 15
  - Accounts + AccountEdit → Task 16
  - BalanceMethods → Task 17
  - ModelRouting + ModelGroups + ModelMetadata → Task 18
  - ApiKeys → Task 19
  - RequestLogs + Detail → Task 20
  - Config + Proxies + AdminSettings → Task 21
  - CustomChannelCreate + TestModel + UsageGuide + Docs → Task 22
  - 端到端验收 → Task 23
- **占位扫描**：每个 Step 包含完整代码块，没有 TBD/TODO/"implement later"等占位。
- **类型一致**：
  - API 命名：所有 `api/*.ts` 导出函数名都与 Task 6 client 一致。
  - 工具函数命名：format / requestPath / debounce / httpQuery / cooldownPolicy 跨任务保持一致。
  - 主题命名：midnight / slate / emerald / light 与 themes.ts 中 key 一致。
  - token 存储键：adminToken / adminRemember / admin-auth（channel）跨任务一致。
- **手工验收覆盖**：每个 feature 任务都附"打开/看到/触发"清单，Task 23 做整体回归。
- **交付标准**：后端仅 admin.py 新增 1 个路由函数体；构建产物落 static/admin-react/；旧 /admin 入口零修改。
