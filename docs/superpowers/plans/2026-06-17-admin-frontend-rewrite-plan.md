# Admin Frontend Rewrite Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动后端业务逻辑、不替换现有 `static/admin.html` 的前提下，完成第一阶段 React + Vite + TypeScript 管理后台前端外壳。

**Architecture:** 新前端作为独立 Vite SPA 放在 `web/admin-frontend/`。开发模式由 Vite dev server 通过 proxy 调现有后端；生产模式只输出静态构建产物。第一阶段只实现 shell、登录、主题、API client、认证状态、路由保护、布局、导航和现有菜单项占位页；旧 `static/admin.html` 继续服务当前 `/admin`。

**Tech Stack:** React 18, Vite 5, TypeScript 5, React Router v6, Zustand, Axios, CSS Modules, npm scripts.

---

## 0. 已确认上下文

- 已批准设计：`D:\code\model-api\docs\superpowers\specs\2026-06-17-admin-frontend-rewrite-design.md`
- 旧登录/布局参考：`D:\code\model-api\static\admin.html:605`
- 旧主题变量参考：`D:\code\model-api\static\admin.html:10`
- 旧导航来源：`D:\code\model-api\static\admin.html:624`
- 当前后端管理页入口：`D:\code\model-api\admin.py:1023`
- 当前静态目录挂载：`D:\code\model-api\main.py:225`

阶段 1 明确不修改：`static/admin.html`、`admin.py`、`main.py`。是否把 `/admin` 切到新构建产物，必须后续单独审批。

### 现有导航清单

| key | 标题 | 路由 |
|---|---|---|
| `dashboard` | 仪表盘 | `/dashboard` |
| `main-config` | 全局配置 | `/main-config` |
| `api-keys` | API Keys | `/api-keys` |
| `proxy-pool` | 代理池 | `/proxy-pool` |
| `channels` | 渠道 | `/channels` |
| `relay-sites` | 中转站 | `/relay-sites` |
| `model-routing` | 模型路由/分组 | `/model-routing` |
| `model-metadata` | 模型广场 | `/model-metadata` |
| `request-logs` | 请求日志 | `/request-logs` |
| `data-dashboard` | 数据看板 | `/data-dashboard` |
| `test-model` | 测试模型 | `/test-model` |
| `usage-guide` | 使用方法 | `/usage-guide` |

---

## 1. File Structure Map

```text
web/admin-frontend/
├── index.html
├── package.json
├── tsconfig.json
├── tsconfig.node.json
├── vite.config.ts
├── src/
│   ├── main.tsx
│   ├── vite-env.d.ts
│   ├── app/
│   │   ├── App.tsx
│   │   └── router.tsx
│   ├── api/
│   │   ├── client.ts
│   │   └── auth.ts
│   ├── store/
│   │   ├── authStore.ts
│   │   └── themeStore.ts
│   ├── styles/
│   │   ├── variables.css
│   │   └── global.css
│   ├── routes/
│   │   └── navItems.ts
│   ├── components/
│   │   ├── Layout/
│   │   │   ├── Layout.tsx
│   │   │   ├── Layout.module.css
│   │   │   ├── Sidebar.tsx
│   │   │   ├── Sidebar.module.css
│   │   │   ├── Topbar.tsx
│   │   │   └── Topbar.module.css
│   │   └── UserMenu/
│   │       ├── UserMenu.tsx
│   │       └── UserMenu.module.css
│   ├── pages/
│   │   ├── Login/
│   │   │   ├── Login.tsx
│   │   │   └── Login.module.css
│   │   └── ShellPage/
│   │       ├── ShellPage.tsx
│   │       └── ShellPage.module.css
│   ├── scripts/
│   │   └── smoke.ts
│   └── types/
│       └── auth.ts
```

---

## 2. Implementation Tasks

### Task 1: 初始化 Vite + React + TypeScript 工程

**Files:**
- Create: `web/admin-frontend/index.html`
- Create: `web/admin-frontend/package.json`
- Create: `web/admin-frontend/src/main.tsx`
- Create: `web/admin-frontend/src/vite-env.d.ts`
- Create: `web/admin-frontend/src/app/App.tsx`

- [ ] **Step 1: 创建目录骨架**

Run:

```powershell
Test-Path -LiteralPath "D:\code\model-api"; if ($?) { New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\app" }
```

Expected:

```text
True
```

- [ ] **Step 2: 创建 `index.html`**

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Model API - 管理后台</title>
  </head>
  <body class="theme-slate">
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 3: 创建 `package.json`**

```json
{
  "name": "model-api-admin-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "tsc -b && vite build",
    "typecheck": "tsc -b --pretty false",
    "preview": "vite preview --host 0.0.0.0",
    "smoke": "tsx src/scripts/smoke.ts"
  },
  "dependencies": {
    "@vitejs/plugin-react": "^4.2.1",
    "axios": "^1.6.8",
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "react-router-dom": "^6.22.3",
    "zustand": "^4.5.2"
  },
  "devDependencies": {
    "@types/node": "^20.11.30",
    "@types/react": "^18.2.67",
    "@types/react-dom": "^18.2.22",
    "tsx": "^4.7.1",
    "typescript": "^5.4.2",
    "vite": "^5.1.6"
  }
}
```

- [ ] **Step 4: 创建入口文件**

`web/admin-frontend/src/vite-env.d.ts`:

```typescript
/// <reference types="vite/client" />
```

`web/admin-frontend/src/main.tsx`:

```typescript
import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import './styles/variables.css';
import './styles/global.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

`web/admin-frontend/src/app/App.tsx`:

```typescript
import { BrowserRouter } from 'react-router-dom';
import { AppRouter } from './router';

export function App() {
  return (
    <BrowserRouter>
      <AppRouter />
    </BrowserRouter>
  );
}
```

- [ ] **Step 5: 验证文件存在**

Run:

```powershell
Test-Path -LiteralPath "D:\code\model-api\web\admin-frontend\package.json"; Test-Path -LiteralPath "D:\code\model-api\web\admin-frontend\src\main.tsx"
```

Expected:

```text
True
True
```

### Task 2: 安装依赖

**Files:**
- Generated: `web/admin-frontend/package-lock.json`
- Generated: `web/admin-frontend/node_modules/`

- [ ] **Step 1: 执行安装**

Run in `D:\code\model-api\web\admin-frontend`:

```powershell
npm install
```

Expected output shape:

```text
added ... packages, and audited ... packages in ...s
```

If npm exits non-zero, stop and report the full npm error.

- [ ] **Step 2: 验证 lockfile**

Run:

```powershell
Test-Path -LiteralPath "D:\code\model-api\web\admin-frontend\package-lock.json"
```

Expected:

```text
True
```

### Task 3: 配置开发环境和构建

**Files:**
- Create: `web/admin-frontend/tsconfig.json`
- Create: `web/admin-frontend/tsconfig.node.json`
- Create: `web/admin-frontend/vite.config.ts`

- [ ] **Step 1: 创建 `tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2020"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

- [ ] **Step 2: 创建 `tsconfig.node.json`**

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "allowSyntheticDefaultImports": true,
    "strict": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 3: 创建 `vite.config.ts`**

```typescript
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/admin': { target: 'http://localhost:8000', changeOrigin: true },
      '/v1': { target: 'http://localhost:8000', changeOrigin: true },
      '/project-docs': { target: 'http://localhost:8000', changeOrigin: true }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true
  }
});
```

- [ ] **Step 4: 验证 TypeScript**

Run in `web/admin-frontend`:

```powershell
npm run typecheck
```

Expected:

```text
> model-api-admin-frontend@0.1.0 typecheck
> tsc -b --pretty false
```

Final expected exit code is `0` after all imported files are created.

### Task 4: 实现主题系统

**Files:**
- Create: `web/admin-frontend/src/styles/variables.css`
- Create: `web/admin-frontend/src/styles/global.css`
- Create: `web/admin-frontend/src/store/themeStore.ts`
- Modify: `web/admin-frontend/src/app/App.tsx`

- [ ] **Step 1: 创建 styles/store 目录**

```powershell
New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\styles", "D:\code\model-api\web\admin-frontend\src\store"
```

Expected exit code: `0`.

- [ ] **Step 2: 创建 `variables.css`**

Use variables from `static/admin.html:10`; include `:root`, `body.theme-midnight`, `body.theme-slate`, `body.theme-emerald`, and `body.theme-light`. Minimum required variables:

```css
:root {
  --bg: #0f172a;
  --bg2: #111827;
  --bg3: #1f2937;
  --border: #334155;
  --text: #cbd5e1;
  --text2: #94a3b8;
  --textH: #f8fafc;
  --blue: #60a5fa;
  --green: #34d399;
  --red: #fb7185;
  --yellow: #fbbf24;
  --radius: 7px;
  --radiusL: 12px;
  --font: system-ui, -apple-system, sans-serif;
  --fontM: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
  --surface: rgba(17, 24, 39, 0.92);
  --surface2: rgba(31, 41, 55, 0.82);
  --shadow: 0 18px 45px rgba(0, 0, 0, 0.24);
  --tint: rgba(96, 165, 250, 0.12);
  --hover: rgba(255, 255, 255, 0.06);
}
```

Add the four theme classes with the values copied from the legacy file, preserving color names and class names.

- [ ] **Step 3: 创建 `global.css`**

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body, #root { height: 100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.45;
  overflow: hidden;
}
a { color: var(--blue); text-decoration: none; }
button, input { font: inherit; }
```

- [ ] **Step 4: 创建 `themeStore.ts`**

```typescript
import { create } from 'zustand';

export type ThemeName = 'midnight' | 'slate' | 'emerald' | 'light';
const STORAGE_KEY = 'model-api-admin-theme';
const THEMES: ThemeName[] = ['midnight', 'slate', 'emerald', 'light'];

function isThemeName(value: string | null): value is ThemeName {
  return value === 'midnight' || value === 'slate' || value === 'emerald' || value === 'light';
}

function readInitialTheme(): ThemeName {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return isThemeName(stored) ? stored : 'slate';
}

function applyThemeClass(theme: ThemeName): void {
  document.body.classList.remove(...THEMES.map((name) => `theme-${name}`));
  document.body.classList.add(`theme-${theme}`);
}

interface ThemeState {
  theme: ThemeName;
  setTheme: (theme: ThemeName) => void;
  syncThemeToBody: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: readInitialTheme(),
  setTheme: (theme) => {
    window.localStorage.setItem(STORAGE_KEY, theme);
    applyThemeClass(theme);
    set({ theme });
  },
  syncThemeToBody: () => applyThemeClass(get().theme)
}));
```

- [ ] **Step 5: 在 `App.tsx` 同步主题**

```typescript
import { useEffect } from 'react';
import { BrowserRouter } from 'react-router-dom';
import { AppRouter } from './router';
import { useThemeStore } from '../store/themeStore';

export function App() {
  const syncThemeToBody = useThemeStore((state) => state.syncThemeToBody);

  useEffect(() => {
    syncThemeToBody();
  }, [syncThemeToBody]);

  return (
    <BrowserRouter>
      <AppRouter />
    </BrowserRouter>
  );
}
```

### Task 5: 实现 API client 和 auth store

**Files:**
- Create: `web/admin-frontend/src/types/auth.ts`
- Create: `web/admin-frontend/src/api/client.ts`
- Create: `web/admin-frontend/src/api/auth.ts`
- Create: `web/admin-frontend/src/store/authStore.ts`

- [ ] **Step 1: 创建目录**

```powershell
New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\api", "D:\code\model-api\web\admin-frontend\src\types"
```

Expected exit code: `0`.

- [ ] **Step 2: 定义认证类型**

```typescript
export interface LoginRequest { password: string; }
export interface LoginResponse { ok?: boolean; token?: string; message?: string; }
```

- [ ] **Step 3: 创建 Axios client**

```typescript
import axios from 'axios';

const AUTH_TOKEN_KEY = 'model-api-admin-token';
const SESSION_TOKEN_KEY = 'model-api-admin-session-token';

export function readAuthToken(): string | null {
  return window.localStorage.getItem(AUTH_TOKEN_KEY) ?? window.sessionStorage.getItem(SESSION_TOKEN_KEY);
}

export function writeAuthToken(token: string, remember: boolean): void {
  clearAuthToken();
  const storage = remember ? window.localStorage : window.sessionStorage;
  storage.setItem(remember ? AUTH_TOKEN_KEY : SESSION_TOKEN_KEY, token);
}

export function clearAuthToken(): void {
  window.localStorage.removeItem(AUTH_TOKEN_KEY);
  window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
}

export const apiClient = axios.create({ baseURL: '', headers: { 'Content-Type': 'application/json' } });

apiClient.interceptors.request.use((config) => {
  const token = readAuthToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      clearAuthToken();
      window.dispatchEvent(new CustomEvent('admin-auth-expired'));
    }
    return Promise.reject(error);
  }
);
```

- [ ] **Step 4: 创建认证 API**

```typescript
import { apiClient } from './client';
import type { LoginRequest, LoginResponse } from '../types/auth';

export async function loginAdmin(payload: LoginRequest): Promise<LoginResponse> {
  const response = await apiClient.post<LoginResponse>('/admin/login', payload);
  return response.data;
}

export async function logoutAdmin(): Promise<void> {
  await Promise.resolve();
}
```

- [ ] **Step 5: 创建 `authStore.ts`**

```typescript
import { create } from 'zustand';
import { clearAuthToken, readAuthToken, writeAuthToken } from '../api/client';
import { loginAdmin } from '../api/auth';

interface AuthState {
  token: string | null;
  isAuthenticated: boolean;
  login: (password: string, remember: boolean) => Promise<void>;
  logout: () => void;
  markExpired: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: readAuthToken(),
  isAuthenticated: Boolean(readAuthToken()),
  login: async (password, remember) => {
    const trimmedPassword = password.trim();
    if (!trimmedPassword) throw new Error('请输入管理密码');
    const result = await loginAdmin({ password: trimmedPassword });
    if (result.ok === false) throw new Error(result.message || '登录失败');
    const token = result.token || 'session-authenticated';
    writeAuthToken(token, remember);
    set({ token, isAuthenticated: true });
  },
  logout: () => {
    clearAuthToken();
    set({ token: null, isAuthenticated: false });
  },
  markExpired: () => {
    clearAuthToken();
    set({ token: null, isAuthenticated: false });
  }
}));
```

### Task 6: 实现路由和导航

**Files:**
- Create: `web/admin-frontend/src/routes/navItems.ts`
- Create: `web/admin-frontend/src/app/router.tsx`
- Modify: `web/admin-frontend/src/app/App.tsx`

- [ ] **Step 1: 创建导航元数据**

```typescript
export interface NavItem { key: string; title: string; subtitle: string; path: string; }
export const navItems: NavItem[] = [
  { key: 'dashboard', title: '仪表盘', subtitle: '系统概览与运行状态', path: '/dashboard' },
  { key: 'main-config', title: '全局配置', subtitle: '服务级配置入口', path: '/main-config' },
  { key: 'api-keys', title: 'API Keys', subtitle: '密钥管理入口', path: '/api-keys' },
  { key: 'proxy-pool', title: '代理池', subtitle: '代理资源管理入口', path: '/proxy-pool' },
  { key: 'channels', title: '渠道', subtitle: '上游渠道管理入口', path: '/channels' },
  { key: 'relay-sites', title: '中转站', subtitle: '中转站配置入口', path: '/relay-sites' },
  { key: 'model-routing', title: '模型路由/分组', subtitle: '模型路由规则入口', path: '/model-routing' },
  { key: 'model-metadata', title: '模型广场', subtitle: '模型元数据入口', path: '/model-metadata' },
  { key: 'request-logs', title: '请求日志', subtitle: '请求观测入口', path: '/request-logs' },
  { key: 'data-dashboard', title: '数据看板', subtitle: '统计数据入口', path: '/data-dashboard' },
  { key: 'test-model', title: '测试模型', subtitle: '模型连通性测试入口', path: '/test-model' },
  { key: 'usage-guide', title: '使用方法', subtitle: '使用说明入口', path: '/usage-guide' }
];
export function findNavItemByPath(pathname: string): NavItem {
  return navItems.find((item) => item.path === pathname) ?? navItems[0];
}
```

- [ ] **Step 2: 创建 protected router**

```typescript
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { Layout } from '../components/Layout/Layout';
import { Login } from '../pages/Login/Login';
import { ShellPage } from '../pages/ShellPage/ShellPage';
import { navItems } from '../routes/navItems';
import { useAuthStore } from '../store/authStore';

function ProtectedRoute() {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const location = useLocation();
  return isAuthenticated ? <Outlet /> : <Navigate to="/login" replace state={{ from: location.pathname }} />;
}

function PublicOnlyRoute() {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  return isAuthenticated ? <Navigate to="/dashboard" replace /> : <Outlet />;
}

export function AppRouter() {
  return (
    <Routes>
      <Route element={<PublicOnlyRoute />}>
        <Route path="/login" element={<Login />} />
      </Route>
      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          {navItems.map((item) => (
            <Route key={item.key} path={item.path.slice(1)} element={<ShellPage title={item.title} subtitle={item.subtitle} pageKey={item.key} />} />
          ))}
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
```

- [ ] **Step 3: 在 `App.tsx` 监听认证过期事件**

Add `useAuthStore((state) => state.markExpired)` and register `window.addEventListener('admin-auth-expired', markExpired)` inside `useEffect`; cleanup with `removeEventListener`.

### Task 7: 实现登录页

**Files:**
- Create: `web/admin-frontend/src/pages/Login/Login.tsx`
- Create: `web/admin-frontend/src/pages/Login/Login.module.css`

- [ ] **Step 1: 创建登录目录**

```powershell
New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\pages\Login"
```

Expected exit code: `0`.

- [ ] **Step 2: 创建 `Login.tsx`**

```typescript
import { FormEvent, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';
import styles from './Login.module.css';

interface LocationState { from?: string; }

export function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const login = useAuthStore((state) => state.login);
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const from = (location.state as LocationState | null)?.from || '/dashboard';

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setIsSubmitting(true);
    try {
      await login(password, remember);
      navigate(from, { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '登录失败');
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className={styles.wrap}>
      <section className={styles.box} aria-labelledby="login-title">
        <h1 id="login-title">Model API</h1>
        <p className={styles.desc}>配置管理后台</p>
        <form onSubmit={handleSubmit}>
          <input className={styles.input} type="password" value={password} placeholder="请输入管理密码" autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} />
          <label className={styles.remember}><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />记住我</label>
          <button className={styles.button} type="submit" disabled={isSubmitting}>{isSubmitting ? '登录中...' : '登录'}</button>
        </form>
        <div className={styles.error} role="status" aria-live="polite">{error}</div>
      </section>
    </main>
  );
}
```

- [ ] **Step 3: 创建登录页样式**

```css
.wrap { display: flex; align-items: center; justify-content: center; height: 100vh; background: var(--bg); }
.box { width: 380px; padding: 36px 32px; background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radiusL); box-shadow: 0 8px 40px rgba(0,0,0,.5); }
.box h1 { margin-bottom: 4px; color: var(--textH); font-size: 20px; text-align: center; }
.desc { margin-bottom: 28px; color: var(--text2); font-size: 13px; text-align: center; }
.input { width: 100%; margin-bottom: 14px; padding: 10px 14px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--textH); }
.input:focus { border-color: var(--blue); outline: none; }
.remember { display: flex; align-items: center; gap: 6px; margin-bottom: 14px; color: var(--text2); font-size: 13px; }
.button { width: 100%; padding: 10px; border: 0; border-radius: var(--radius); background: var(--blue); color: #fff; font-weight: 600; cursor: pointer; }
.button:disabled { opacity: .5; cursor: default; }
.error { min-height: 20px; margin-top: 10px; color: var(--red); font-size: 13px; text-align: center; }
```

### Task 8: 实现主布局（Sidebar + Topbar + UserMenu）

**Files:**
- Create: `web/admin-frontend/src/components/Layout/Layout.tsx`
- Create: `web/admin-frontend/src/components/Layout/Sidebar.tsx`
- Create: `web/admin-frontend/src/components/Layout/Topbar.tsx`
- Create: `web/admin-frontend/src/components/UserMenu/UserMenu.tsx`
- Create: related `*.module.css`

- [ ] **Step 1: 创建组件目录**

```powershell
New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\components\Layout", "D:\code\model-api\web\admin-frontend\src\components\UserMenu"
```

Expected exit code: `0`.

- [ ] **Step 2: 创建 `Layout.tsx`**

```typescript
import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import styles from './Layout.module.css';

export function Layout() {
  return <div className={styles.layout}><Sidebar /><main className={styles.main}><Topbar /><section className={styles.content}><div className={styles.contentInner}><Outlet /></div></section></main></div>;
}
```

- [ ] **Step 3: 创建 `Sidebar.tsx`**

```typescript
import { NavLink } from 'react-router-dom';
import { navItems } from '../../routes/navItems';
import styles from './Sidebar.module.css';

export function Sidebar() {
  return <aside className={styles.sidebar}><div className={styles.brand}><span className={styles.dot} />Model API Admin</div><nav className={styles.nav}>{navItems.map((item) => <NavLink key={item.key} to={item.path} className={({ isActive }) => `${styles.navItem} ${isActive ? styles.active : ''}`}>{item.title}</NavLink>)}</nav><div className={styles.footer}>阶段 1 外壳</div></aside>;
}
```

- [ ] **Step 4: 创建 `Topbar.tsx`**

```typescript
import { useLocation } from 'react-router-dom';
import { findNavItemByPath } from '../../routes/navItems';
import { UserMenu } from '../UserMenu/UserMenu';
import styles from './Topbar.module.css';

export function Topbar() {
  const current = findNavItemByPath(useLocation().pathname);
  return <header className={styles.topbar}><div><h1>{current.title}</h1><p>{current.subtitle}</p></div><UserMenu /></header>;
}
```

- [ ] **Step 5: 创建 `UserMenu.tsx`**

```typescript
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { logoutAdmin } from '../../api/auth';
import { useAuthStore } from '../../store/authStore';
import { ThemeName, useThemeStore } from '../../store/themeStore';
import styles from './UserMenu.module.css';

const labels: Record<ThemeName, string> = { midnight: '深夜黑', slate: '蓝灰深色', emerald: '墨绿控制台', light: '明亮浅色' };

export function UserMenu() {
  const [open, setOpen] = useState(false);
  const setTheme = useThemeStore((state) => state.setTheme);
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();
  async function handleLogout() { await logoutAdmin(); logout(); navigate('/login', { replace: true }); }
  return <div className={styles.menu}><button type="button" onClick={() => setOpen(!open)}>管理员</button>{open ? <div className={styles.popover}>{Object.entries(labels).map(([key, label]) => <button key={key} type="button" onClick={() => setTheme(key as ThemeName)}>{label}</button>)}<button type="button" onClick={() => window.open('/project-docs', '_blank', 'noopener,noreferrer')}>项目文档</button><button type="button" onClick={handleLogout}>退出登录</button></div> : null}</div>;
}
```

- [ ] **Step 6: 创建 CSS Modules**

`Layout.module.css` must set `.layout` to `display:flex;height:100vh`, `.main` to flex column, `.content` to scroll internally.

`Sidebar.module.css` must set `.sidebar` width to `220px`, `.navItem.active` to `color:var(--blue);background:var(--tint);border-left-color:var(--blue)`.

`Topbar.module.css` must use `background:var(--surface)`, `border-bottom:1px solid var(--border)`, and `padding:13px 24px`.

`UserMenu.module.css` must use `position:relative` and place `.popover` absolute under or above the trigger with `background:var(--surface)` and `border:1px solid var(--border)`.

### Task 9: 创建所有现有侧边栏项的占位页面

**Files:**
- Create: `web/admin-frontend/src/pages/ShellPage/ShellPage.tsx`
- Create: `web/admin-frontend/src/pages/ShellPage/ShellPage.module.css`

- [ ] **Step 1: 创建目录**

```powershell
New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\pages\ShellPage"
```

Expected exit code: `0`.

- [ ] **Step 2: 创建 `ShellPage.tsx`**

```typescript
import styles from './ShellPage.module.css';

interface ShellPageProps { title: string; subtitle: string; pageKey: string; }

export function ShellPage({ title, subtitle, pageKey }: ShellPageProps) {
  return <section className={styles.page} data-page={pageKey}><h2>{title}</h2><p>{subtitle}</p><div className={styles.card}><strong>阶段 1 占位页</strong><span>该页面在后续迁移阶段替换为真实业务实现。</span></div></section>;
}
```

- [ ] **Step 3: 创建 `ShellPage.module.css`**

```css
.page { display: flex; flex-direction: column; gap: 16px; }
.page h2 { color: var(--textH); font-size: 22px; }
.page p { color: var(--text2); }
.card { display: flex; flex-direction: column; gap: 8px; padding: 20px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radiusL); }
```

- [ ] **Step 4: 验证 12 个 key 都通过 router 渲染 `ShellPage`**

Run:

```powershell
Select-String -Path "D:\code\model-api\web\admin-frontend\src\routes\navItems.ts" -Pattern "dashboard|main-config|api-keys|proxy-pool|channels|relay-sites|model-routing|model-metadata|request-logs|data-dashboard|test-model|usage-guide"
```

Expected: all 12 keys are present.

### Task 10: 添加 smoke 验证脚本与命令文档

**Files:**
- Create: `web/admin-frontend/src/scripts/smoke.ts`

- [ ] **Step 1: 创建 smoke 目录**

```powershell
New-Item -ItemType Directory -Force -Path "D:\code\model-api\web\admin-frontend\src\scripts"
```

Expected exit code: `0`.

- [ ] **Step 2: 创建 `smoke.ts`**

```typescript
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

const requiredFiles = ['index.html', 'package.json', 'vite.config.ts', 'src/main.tsx', 'src/app/App.tsx', 'src/app/router.tsx', 'src/pages/Login/Login.tsx', 'src/components/Layout/Layout.tsx', 'src/components/UserMenu/UserMenu.tsx', 'src/pages/ShellPage/ShellPage.tsx'];
const missing = requiredFiles.filter((file) => !existsSync(resolve(process.cwd(), file)));
if (missing.length > 0) {
  console.error(`Missing required files: ${missing.join(', ')}`);
  process.exit(1);
}
console.log('Smoke check passed: core frontend shell files exist.');
```

- [ ] **Step 3: 运行 smoke**

Run in `web/admin-frontend`:

```powershell
npm run smoke
```

Expected:

```text
Smoke check passed: core frontend shell files exist.
```

- [ ] **Step 4: 验证 dev server**

Run in `web/admin-frontend`:

```powershell
npm run dev
```

Expected:

```text
VITE v5.x.x  ready in ... ms
  ➜  Local:   http://localhost:5173/
```

Open `/login`; expected: login box renders. Stop the server after confirmation.

- [ ] **Step 5: 验证 production build**

Run in `web/admin-frontend`:

```powershell
npm run build
```

Expected:

```text
vite v5.x.x building for production...
✓ built in ...ms
```

Expected artifact: `D:\code\model-api\web\admin-frontend\dist\index.html`.

---

## 3. Verification Checklist

- [ ] `npm install` succeeds and writes `web/admin-frontend/package-lock.json`.
- [ ] `npm run typecheck` exits `0`.
- [ ] `npm run smoke` prints `Smoke check passed: core frontend shell files exist.`
- [ ] `npm run dev` starts Vite at `http://localhost:5173/`.
- [ ] Browser smoke: `/login` renders, `/dashboard` redirects unauthenticated users to `/login`, authenticated users see the shell.
- [ ] `npm run build` emits `web/admin-frontend/dist/index.html` and `dist/assets/`.
- [ ] Sidebar shows all 12 legacy nav entries.
- [ ] Theme switching updates `body.theme-*` and persists to `localStorage`.
- [ ] `static/admin.html`, `admin.py`, and `main.py` remain untouched.

---

## 4. Self-Review

### 4.1 Spec coverage

- Vite + React + TypeScript 初始化：Task 1
- 安装依赖：Task 2
- TypeScript、Vite proxy、build 输出：Task 3
- 主题变量与主题状态：Task 4
- API client 与 auth store：Task 5
- Router 与 protected route：Task 6
- Login page：Task 7
- Layout、Sidebar、Topbar、UserMenu：Task 8
- 12 个旧导航项占位页：Task 9
- smoke、dev、build、typecheck 验证：Task 10 和 Verification Checklist

### 4.2 Placeholder scan

本计划避免使用空泛占位描述；保留的“占位页”是阶段 1 明确范围，不代表未定义实现。

### 4.3 Type consistency

- `ThemeName` 在 store 与 `UserMenu` 中一致：`midnight | slate | emerald | light`。
- `navItems.path` 与 router、Sidebar、Topbar 的使用一致。
- `useAuthStore` 暴露 `login`、`logout`、`markExpired`。
- `ShellPage` props 为 `title`、`subtitle`、`pageKey`。
- API client 只做认证头与 401 处理，不重写后端协议。

### 4.4 Scope boundaries

- 不迁移每个旧业务页面的完整功能。
- 不修改 `static/admin.html`。
- 不修改 `admin.py` 或 `main.py`。
- 不引入 SSR、Next.js、Nuxt。
- 不把 `/admin` 切换到新构建产物。

---

## 5. Execution Notes

执行顺序建议：Task 1 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9 → Task 2 → Task 10。依赖安装可在 Task 1 后立即执行；如果要先离线审阅文件结构，也可以在 Task 9 后再安装并统一验证。
