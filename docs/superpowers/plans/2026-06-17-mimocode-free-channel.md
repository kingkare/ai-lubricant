# MimoCode Free 渠道实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 model-api 项目中新增一个独立渠道 `mimocode-free`，对接 `https://api.xiaomimimo.com/api/free-ai/...`，支持 JWT bootstrap、Redis TTL 缓存、强制首条 system、仅 `mimo-auto` 模型、HTTP 状态码检查、流式 SSE 解析（含 `reasoning_content` / `content` / `usage`）。

**Architecture:** 继承 `BaseProvider` 实现 `MimoCodeFreeProvider`；认证逻辑使用设备指纹 + bootstrap 获取 JWT，并将 JWT 写入 Redis 缓存（TTL 从 JWT 的 `exp` 计算）；聊天请求复用 `BaseProvider.send_sse_request` 发送 OpenAI 兼容流式请求，并在 `_do_stream_chat` 中解析 SSE 返回 `thinking`/`content`/`usage`/`finish_reason`；注册入口、导出、默认配置分别修改 `main.py` 和 `providers/__init__.py`。

**Tech Stack:** Python 3.10+, FastAPI, aiohttp, coredis (Redis), loguru, pytest.

**设计文档:** `docs/superpowers/specs/2026-06-17-mimocode-free-channel-design.md`

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `providers/mimocode_free.py` | 创建 | `MimoCodeFreeProvider` 类 |
| `providers/__init__.py` | 修改 | 导出 `MimoCodeFreeProvider` |
| `main.py` | 修改 | 注册 provider 并添加默认配置 |
| `tests/test_mimocode_free_provider.py` | 创建 | 单元测试与集成测试 |

---

## Task 1: 创建 `MimoCodeFreeProvider` 类骨架与常量

**Files:**
- Create: `providers/mimocode_free.py`

- [ ] **Step 1: 编写最小类骨架测试**

```python
from providers.mimocode_free import MimoCodeFreeProvider

def test_provider_has_correct_constants():
    assert MimoCodeFreeProvider.PROVIDER_NAME == "mimocode-free"
    assert MimoCodeFreeProvider.BASE_URL == "https://api.xiaomimimo.com"
    assert MimoCodeFreeProvider.auto_update_models is False
    assert MimoCodeFreeProvider.SUPPORTED_MODELS == ["mimo-auto"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py::test_provider_has_correct_constants -v
```

Expected: `ModuleNotFoundError: No module named 'providers.mimocode_free'`

- [ ] **Step 3: 创建最小 Provider 类**

在 `providers/mimocode_free.py` 中写入：

```python
"""MiMoCode Free 模型提供商"""
import os
import time
import json
import base64
import hashlib
import platform
from typing import AsyncGenerator

import aiohttp
from fastapi import HTTPException
from loguru import logger

from providers.base import BaseProvider, make_insecure_connector
from rd import JdbcClient


class MimoCodeFreeProvider(BaseProvider):
    """MiMoCode Free 渠道

    对接 https://api.xiaomimimo.com/api/free-ai/...
    使用设备指纹 bootstrap 获取 JWT，JWT 写入 Redis 并设置 TTL = exp - now。
    """

    PROVIDER_NAME = "mimocode-free"
    BASE_URL = "https://api.xiaomimimo.com"
    DEFAULT_SYSTEM_PROMPT = (
        "You are MiMoCode, an interactive CLI tool that helps users with software engineering tasks."
    )
    SUPPORTED_MODELS = ["mimo-auto"]
    auto_update_models = False

    def __init__(self, username: str, password: str, proxy: str = None, device_id: str = None, **kwargs):
        super().__init__(username, password, proxy, **kwargs)
        self.device_id = device_id
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py::test_provider_has_correct_constants -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add providers/mimocode_free.py tests/test_mimocode_free_provider.py
git commit -m "feat(mimocode-free): add provider skeleton and constants"
```

---

## Task 2: 实现设备指纹生成

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
def test_fingerprint_is_deterministic_for_same_username(monkeypatch):
    monkeypatch.setattr(platform, "node", lambda: "host")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("USERNAME", "alice")

    p1 = MimoCodeFreeProvider("alice", "")
    p2 = MimoCodeFreeProvider("alice", "")
    assert p1._generate_fingerprint() == p2._generate_fingerprint()


def test_fingerprint_changes_with_different_username(monkeypatch):
    monkeypatch.setattr(platform, "node", lambda: "host")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("USERNAME", "alice")
    p1 = MimoCodeFreeProvider("alice", "")

    monkeypatch.setenv("USERNAME", "bob")
    p2 = MimoCodeFreeProvider("bob", "")
    assert p1._generate_fingerprint() != p2._generate_fingerprint()


def test_device_id_overrides_fingerprint():
    provider = MimoCodeFreeProvider("alice", "", device_id="fixed-device")
    assert provider._generate_fingerprint() == "fixed-device"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: `AttributeError: 'MimoCodeFreeProvider' object has no attribute '_generate_fingerprint'`

- [ ] **Step 3: 实现 `_generate_fingerprint`**

在 `providers/mimocode_free.py` 中 `__init__` 后添加：

```python
    def _generate_fingerprint(self) -> str:
        if self.device_id:
            return self.device_id
        username = os.getenv("USERNAME", self.username or "unknown")
        fingerprint_input = (
            f"{platform.node()}|{platform.system()}|{platform.machine()}|unknown|{username}"
        )
        return hashlib.sha256(fingerprint_input.encode()).hexdigest()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): implement deterministic device fingerprint"
```

---

## Task 3: 实现 JWT 解析与 Redis 缓存

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock


def _build_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


@pytest.mark.asyncio
async def test_parse_jwt_exp(monkeypatch):
    now = int(time.time())
    provider = MimoCodeFreeProvider("alice", "")
    jwt = _build_jwt({"exp": now + 3600})
    assert provider._parse_jwt_exp(jwt) == now + 3600


def test_parse_jwt_exp_without_exp_raises():
    provider = MimoCodeFreeProvider("alice", "")
    jwt = _build_jwt({"sub": "test"})
    with pytest.raises(HTTPException) as exc:
        provider._parse_jwt_exp(jwt)
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_jwt_from_redis_skips_bootstrap(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev1")

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=b"cached_jwt")
    monkeypatch.setattr(JdbcClient, "redis", redis_mock)

    with patch.object(provider, "_bootstrap_jwt", new=AsyncMock()) as bootstrap:
        jwt = await provider._get_jwt()
    assert jwt == "cached_jwt"
    assert redis_mock.get.call_args[0][0] == "mimocode-free:jwt:dev1"
    bootstrap.assert_not_called()


@pytest.mark.asyncio
async def test_get_jwt_fetches_and_caches_when_missing(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev2")
    now = int(time.time())
    jwt = _build_jwt({"exp": now + 3600})

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.set = AsyncMock()
    monkeypatch.setattr(JdbcClient, "redis", redis_mock)

    with patch.object(provider, "_bootstrap_jwt", new=AsyncMock(return_value=jwt)) as bootstrap:
        result = await provider._get_jwt()

    assert result == jwt
    bootstrap.assert_called_once()
    redis_mock.set.assert_called_once()
    call_args = redis_mock.set.call_args
    assert call_args[0][0] == "mimocode-free:jwt:dev2"
    assert call_args[0][1] == jwt
    ttl = call_args.kwargs.get("ex", call_args[1].get("ex", 0))
    assert 3500 < ttl <= 3600
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: 多个 `AttributeError` / `Method not found`

- [ ] **Step 3: 实现 JWT 解析与 Redis 缓存方法**

在 `providers/mimocode_free.py` 中添加：

```python
    def _parse_jwt_exp(self, jwt: str) -> int:
        """解析 JWT payload 中的 exp 字段。"""
        try:
            parts = jwt.split(".")
            if len(parts) < 2:
                raise ValueError("JWT 格式错误")
            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            exp = payload.get("exp")
            if exp is None:
                raise ValueError("JWT 无 exp 字段")
            return int(exp)
        except Exception as e:
            logger.error(f"[MimoCodeFree] 解析 JWT exp 失败: {e}")
            raise HTTPException(status_code=502, detail=f"JWT 解析失败: {e}")

    def _jwt_redis_key(self) -> str:
        return f"mimocode-free:jwt:{self._generate_fingerprint()}"

    async def _get_jwt(self) -> str:
        """从 Redis 获取 JWT，没有则 bootstrap 并写入 Redis。"""
        redis = getattr(JdbcClient, "redis", None)
        if redis is not None:
            try:
                cached = await redis.get(self._jwt_redis_key())
                if cached:
                    jwt = cached.decode("utf-8") if isinstance(cached, bytes) else cached
                    logger.debug(f"[MimoCodeFree] 命中 Redis JWT: {self._jwt_redis_key()}")
                    return jwt
            except Exception as e:
                logger.warning(f"[MimoCodeFree] 读取 Redis JWT 失败: {e}")

        jwt = await self._bootstrap_jwt()
        ttl = max(self._parse_jwt_exp(jwt) - int(time.time()), 1)
        if redis is not None:
            try:
                await redis.set(self._jwt_redis_key(), jwt, ex=ttl)
                logger.debug(f"[MimoCodeFree] JWT 写入 Redis TTL={ttl}: {self._jwt_redis_key()}")
            except Exception as e:
                logger.warning(f"[MimoCodeFree] 写入 Redis JWT 失败: {e}")
        return jwt
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): add JWT parsing and Redis caching"
```

---

## Task 4: 实现 Bootstrap 获取 JWT

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
@pytest.mark.asyncio
async def test_bootstrap_jwt_success(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev3")
    jwt = _build_jwt({"exp": int(time.time()) + 3600})

    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value={"jwt": jwt})

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("aiohttp.ClientSession", return_value=session):
        result = await provider._bootstrap_jwt()
    assert result == jwt


@pytest.mark.asyncio
async def test_bootstrap_jwt_non_2xx_raises():
    provider = MimoCodeFreeProvider("alice", "", device_id="dev4")

    response = MagicMock()
    response.status = 429
    response.text = AsyncMock(return_value='{"error":"rate limited"}')

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(HTTPException) as exc:
            await provider._bootstrap_jwt()
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_bootstrap_jwt_missing_field_raises():
    provider = MimoCodeFreeProvider("alice", "", device_id="dev5")

    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value={})

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(HTTPException) as exc:
            await provider._bootstrap_jwt()
    assert exc.value.status_code == 502
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: `AttributeError: _bootstrap_jwt`

- [ ] **Step 3: 实现 `_bootstrap_jwt`**

在 `providers/mimocode_free.py` 中添加：

```python
    async def _bootstrap_jwt(self) -> str:
        """通过设备指纹 bootstrap 获取 JWT。"""
        url = f"{self.BASE_URL}/api/free-ai/bootstrap"
        fingerprint = self._generate_fingerprint()
        headers = {"Content-Type": "application/json"}
        data = {"client": fingerprint}

        async with aiohttp.ClientSession(connector=make_insecure_connector()) as session:
            async with session.post(url, json=data, headers=headers, proxy=self.proxy) as response:
                body = await response.text()
                if response.status >= 400:
                    logger.warning(f"[MimoCodeFree] bootstrap 失败 status={response.status}: {body[:500]}")
                    raise HTTPException(status_code=response.status, detail=body[:1000])
                try:
                    resp_json = json.loads(body)
                except json.JSONDecodeError as e:
                    raise HTTPException(status_code=502, detail=f"bootstrap 返回非 JSON: {body[:500]}") from e

                jwt = resp_json.get("jwt")
                if not jwt:
                    raise HTTPException(status_code=502, detail="bootstrap 未返回 jwt")
                return jwt
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): implement JWT bootstrap with status code check"
```

---

## Task 5: 实现 `init_auth` / `check_auth` / `is_init` / `fetch_upstream_model_list`

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
@pytest.mark.asyncio
async def test_init_auth_caches_jwt(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev6")
    jwt = _build_jwt({"exp": int(time.time()) + 3600})

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.set = AsyncMock()
    monkeypatch.setattr(JdbcClient, "redis", redis_mock)

    with patch.object(provider, "_bootstrap_jwt", new=AsyncMock(return_value=jwt)):
        assert await provider.init_auth() is True


def test_is_init_always_true():
    provider = MimoCodeFreeProvider("alice", "")
    assert provider.is_init() is True


@pytest.mark.asyncio
async def test_check_auth_returns_true():
    provider = MimoCodeFreeProvider("alice", "")
    assert await provider.check_auth() is True


@pytest.mark.asyncio
async def test_fetch_model_list():
    provider = MimoCodeFreeProvider("alice", "")
    models = await provider.fetch_upstream_model_list()
    assert len(models) == 1
    assert models[0]["id"] == "mimo-auto"
    assert models[0]["owned_by"] == "mimocode-free"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: `AttributeError` on `init_auth` / `check_auth` / `fetch_upstream_model_list`

- [ ] **Step 3: 实现认证与模型列表方法**

在 `providers/mimocode_free.py` 中添加：

```python
    def is_init(self) -> bool:
        return True

    async def init_auth(self, is_check: bool = False) -> bool:
        try:
            await self._get_jwt()
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[MimoCodeFree] init_auth 失败: {e}")
            raise HTTPException(status_code=502, detail=f"init_auth 失败: {e}")

    async def check_auth(self) -> bool:
        return True

    async def fetch_upstream_model_list(self) -> list[dict]:
        now = int(time.time())
        return [
            {
                "id": "mimo-auto",
                "name": "mimo-auto",
                "owned_by": "mimocode-free",
                "object": "model",
                "created": now,
            }
        ]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): add auth and model list methods"
```

---

## Task 6: 实现消息首条 system 强制插入

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
def test_ensure_system_message_inserts_when_first_not_system():
    provider = MimoCodeFreeProvider("alice", "")
    messages = [{"role": "user", "content": "hello"}]
    result = provider._ensure_system_message(messages)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == provider.DEFAULT_SYSTEM_PROMPT
    assert result[1:] == messages


def test_ensure_system_message_keeps_existing_system_at_first():
    provider = MimoCodeFreeProvider("alice", "")
    messages = [{"role": "system", "content": "custom"}, {"role": "user", "content": "hello"}]
    result = provider._ensure_system_message(messages)
    assert result == messages


def test_ensure_system_message_empty_list():
    provider = MimoCodeFreeProvider("alice", "")
    result = provider._ensure_system_message([])
    assert len(result) == 1
    assert result[0]["role"] == "system"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: `AttributeError: _ensure_system_message`

- [ ] **Step 3: 实现 `_ensure_system_message`**

在 `providers/mimocode_free.py` 中添加：

```python
    def _ensure_system_message(self, messages: list[dict]) -> list[dict]:
        if messages and messages[0].get("role") == "system":
            return messages
        return [{"role": "system", "content": self.DEFAULT_SYSTEM_PROMPT}, *messages]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): enforce first message as system"
```

---

## Task 7: 实现流式聊天与 SSE 解析

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
@pytest.mark.asyncio
async def test_stream_chat_parses_reasoning_and_content(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev7")
    jwt = _build_jwt({"exp": int(time.time()) + 3600})

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=jwt.encode())
    monkeypatch.setattr(JdbcClient, "redis", redis_mock)

    sse_events = [
        'data: {"id":"c1","choices":[{"delta":{"reasoning_content":"think"}}]}\n\n',
        'data: {"id":"c1","choices":[{"delta":{"content":"hello"}}]}\n\n',
        'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n',
    ]

    with patch.object(provider, "send_sse_request", new=AsyncMock(return_value=iter(sse_events))):
        chunks = []
        async for chunk in provider._do_stream_chat("mimo-auto", [{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert chunks[0] == {}
    assert chunks[1] == {"thinking": "think", "content": "", "tool_calls": []}
    assert chunks[2] == {"content": "hello", "thinking": "", "tool_calls": []}
    assert chunks[3].get("finish_reason") == "stop"
    assert chunks[4].get("usage") == {"prompt_tokens": 1, "completion_tokens": 2}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: `AttributeError: _do_stream_chat`

- [ ] **Step 3: 实现 `_do_stream_chat` 与辅助方法**

在 `providers/mimocode_free.py` 中添加：

```python
    def _build_chat_headers(self, jwt: str) -> dict:
        return {
            "Authorization": f"Bearer {jwt}",
            "X-Mimo-Source": "mimocode-cli-free",
            "x-session-affinity": f"ses_{os.urandom(12).hex()}",
            "Content-Type": "application/json",
        }

    def _build_chat_payload(self, messages: list[dict], **kwargs) -> dict:
        payload = {
            "model": "mimo-auto",
            "stream": True,
            "messages": self._ensure_system_message(messages),
        }
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]
        return payload

    async def _do_stream_chat(
        self,
        model_id: str,
        messages: list[dict],
        **kwargs
    ) -> AsyncGenerator[dict, None]:
        jwt = await self._get_jwt()
        url = f"{self.BASE_URL}/api/free-ai/openai/chat"
        headers = self._build_chat_headers(jwt)
        payload = self._build_chat_payload(messages, **kwargs)

        first = True
        async for event in self.send_sse_request(
            "POST",
            url,
            headers,
            json=payload,
            response_headers_callback=kwargs.get("response_headers_callback"),
            router_request_body_callback=kwargs.get("router_request_body_callback"),
            router_response_body_callback=kwargs.get("router_response_body_callback"),
        ):
            if first:
                yield {}
                first = False
            for line in event.splitlines():
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if not isinstance(chunk, dict):
                    continue

                # 处理空 choices + usage 的结束块
                usage = chunk.get("usage")
                choices = chunk.get("choices") or []
                if not choices and isinstance(usage, dict):
                    yield {"content": "", "thinking": "", "tool_calls": [], "usage": usage}
                    continue

                for choice in choices:
                    delta = choice.get("delta") or {}
                    finish_reason = choice.get("finish_reason")
                    reasoning = delta.get("reasoning_content")
                    content = delta.get("content")

                    if reasoning:
                        yield {"content": "", "thinking": reasoning, "tool_calls": []}
                    if content:
                        yield {"content": content, "thinking": "", "tool_calls": []}
                    if finish_reason:
                        yield {"content": "", "thinking": "", "tool_calls": [], "finish_reason": finish_reason}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): implement streaming chat and SSE parsing"
```

---

## Task 8: 实现非流式聊天

**Files:**
- Modify: `providers/mimocode_free.py`
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写测试**

```python
@pytest.mark.asyncio
async def test_non_stream_chat_aggregates(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev8")
    jwt = _build_jwt({"exp": int(time.time()) + 3600})

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=jwt.encode())
    monkeypatch.setattr(JdbcClient, "redis", redis_mock)

    sse_events = [
        'data: {"id":"c1","choices":[{"delta":{"reasoning_content":"think"}}]}\n\n',
        'data: {"id":"c1","choices":[{"delta":{"content":"hello world"}}]}\n\n',
        'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n',
    ]

    with patch.object(provider, "send_sse_request", new=AsyncMock(return_value=iter(sse_events))):
        response = await provider._do_non_stream_chat("mimo-auto", [{"role": "user", "content": "hi"}])

    assert response["model"] == "mimo-auto"
    assert response["choices"][0]["message"]["content"] == "hello world"
    assert response["choices"][0]["message"].get("reasoning_content") == "think"
    assert response["usage"]["completion_tokens"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: `AttributeError: _do_non_stream_chat`

- [ ] **Step 3: 实现 `_do_non_stream_chat`**

在 `providers/mimocode_free.py` 中添加：

```python
    async def _do_non_stream_chat(
        self,
        model_id: str,
        messages: list[dict],
        **kwargs
    ) -> dict:
        full_content = ""
        full_thinking = ""
        full_usage = None

        async for chunk in self._do_stream_chat(model_id, messages, **kwargs):
            if isinstance(chunk, dict):
                content = chunk.get("content") or ""
                thinking = chunk.get("thinking") or ""
                usage = chunk.get("usage")
                if content:
                    full_content += content
                if thinking:
                    full_thinking += thinking
                if usage:
                    full_usage = usage

        from message_utils import generate_completion_id
        completion_id = generate_completion_id()
        result = self.build_openai_response(
            completion_id,
            model_id,
            full_content,
            thinking=full_thinking,
        )
        if full_usage:
            result["usage"] = full_usage
        return result
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(mimocode-free): add non-stream chat aggregation"
```

---

## Task 9: 注册 Provider 并添加默认配置

**Files:**
- Modify: `providers/__init__.py`
- Modify: `main.py`

- [ ] **Step 1: 修改 `providers/__init__.py`**

```python
from providers.mimocode_free import MimoCodeFreeProvider
```

并更新 `__all__`：

```python
__all__ = [
    "BaseProvider", "QwenProvider", "XiaomiProvider",
    "EaiChatProvider", "JiekouProvider", "ZenmuxProvider",
    "PuterProvider", "GeminiProvider", "TabbitProvider",
    "CustomProvider", "KimiAiProvider", "EdgeOneAIProvider",
    "HhhlProvider", "MimoCodeFreeProvider",
]
```

- [ ] **Step 2: 修改 `main.py` 导入与注册**

在导入行添加：

```python
from providers import MimoCodeFreeProvider
```

在 `ModelClientPool.register_provider` 调用段添加：

```python
ModelClientPool.register_provider("mimocode-free", MimoCodeFreeProvider)
```

- [ ] **Step 3: 添加默认 provider 配置**

在 `main.py` 的 `provider_configs.setdefault(...)` 段附近添加：

```python
provider_configs.setdefault("mimocode-free", {
    "enabled": True,
    "type": "builtin",
    "remark": "MiMoCode Free (api.xiaomimimo.com)",
    "protocol": "openai",
    "base_url": "https://api.xiaomimimo.com",
})
```

- [ ] **Step 4: 编写测试**

```python
from providers import MimoCodeFreeProvider


def test_provider_exported():
    assert MimoCodeFreeProvider is not None
    assert MimoCodeFreeProvider.PROVIDER_NAME == "mimocode-free"
```

- [ ] **Step 5: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(mimocode-free): register provider in main.py and providers/__init__.py"
```

---

## Task 10: 集成测试与全链路验证

**Files:**
- Test: `tests/test_mimocode_free_provider.py`

- [ ] **Step 1: 编写端到端流式测试**

```python
@pytest.mark.asyncio
async def test_chat_stream_end_to_end(monkeypatch):
    provider = MimoCodeFreeProvider("alice", "", device_id="dev9")
    jwt = _build_jwt({"exp": int(time.time()) + 3600})

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=jwt.encode())
    monkeypatch.setattr(JdbcClient, "redis", redis_mock)

    sse_events = [
        'data: {"id":"c1","choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n',
        'data: {"id":"c1","choices":[{"delta":{"content":"answer"}}]}\n\n',
        'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ]

    with patch.object(provider, "send_sse_request", new=AsyncMock(return_value=iter(sse_events))):
        chunks = []
        async for chunk in provider.chat("mimo-auto", [{"role": "user", "content": "hi"}], stream=True):
            chunks.append(chunk)

    # 至少包含 role chunk + 1个内容 + finish + done
    assert any("role" in c for c in chunks if isinstance(c, str))
```

- [ ] **Step 2: 运行测试确认通过**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
```

Expected: PASS

- [ ] **Step 3: 运行全量测试**

```bash
python -m pytest tests/test_mimocode_free_provider.py -v
python -m pytest tests/ -x --ignore=tests/test_admin_*  # 快速回归，避免慢测试阻塞
```

Expected: 新增测试全部 PASS，核心回归测试无失败。

- [ ] **Step 4: Commit**

```bash
git commit -m "test(mimocode-free): add end-to-end stream integration test"
```

---

## Task 11: 补充文档与风险说明

**Files:**
- Create: `docs/providers/mimocode-free.md`
- Modify: `docs/providers/README.md`
- Modify: `docs/superpowers/specs/2026-06-17-mimocode-free-channel-design.md` 状态

- [ ] **Step 1: 创建渠道文档**

在 `docs/providers/mimocode-free.md` 中写入：

```markdown
# MimoCode Free 渠道

对应文件：`providers/mimocode_free.py`

## 定位

对接 `https://api.xiaomimimo.com` 的免费 AI 接口，使用设备指纹 bootstrap 获取 JWT。

## 认证和配置

账号配置字段：

- `username`：必填，用作设备指纹输入标识。
- `proxy`：可选。
- `device_id`：可选，覆盖自动生成的设备指纹。

JWT 缓存：

- 首次请求时 POST `/api/free-ai/bootstrap` 获取 JWT。
- 解析 JWT payload 的 `exp`，写入 Redis key `mimocode-free:jwt:{fingerprint}`，TTL = `exp - now`。
- 后续请求优先从 Redis 读取，过期后自动重新 bootstrap。

## 模型列表

固定返回 `mimo-auto`。

## 聊天链路

1. 检查 Redis 是否有 JWT，否则 bootstrap。
2. 确保 `messages` 第一条为 system，否则自动插入默认 system。
3. POST `/api/free-ai/openai/chat`，使用 OpenAI 兼容流式格式。
4. 解析 SSE，映射 `reasoning_content` -> `thinking`，`content` -> `content`，`usage` -> `usage`。

## 能力

- 支持流式和非流式。
- 支持 reasoning content。
- 仅支持 `mimo-auto` 模型。
- 检查 HTTP 状态码，非 2xx 抛错。
```

- [ ] **Step 2: 更新 `docs/providers/README.md`**

在内置渠道列表新增：

```markdown
- [MimoCode Free 渠道](./mimocode-free.md)
```

- [ ] **Step 3: 更新设计文档状态**

将 `docs/superpowers/specs/2026-06-17-mimocode-free-channel-design.md` 第一行状态改为：

```markdown
状态：已实施
```

- [ ] **Step 4: Commit**

```bash
git add docs/providers/mimocode-free.md docs/providers/README.md docs/superpowers/specs/2026-06-17-mimocode-free-channel-design.md
git commit -m "docs(mimocode-free): add provider documentation and update indexes"
```

---

## 计划自审

**Spec 覆盖检查：**

| 设计文档要求 | 对应任务 |
|---|---|
| 独立 provider `mimocode-free` | Task 1, 9 |
| 设备指纹生成 | Task 2 |
| JWT bootstrap + 状态码检查 | Task 4 |
| JWT exp 解析 + Redis TTL | Task 3 |
| 强制首条 system | Task 6 |
| 仅 `mimo-auto` | Task 1, 5 |
| 流式 SSE 解析 reasoning/content/usage | Task 7 |
| 非流式聚合 | Task 8 |
| 错误处理（HTTP 状态码） | Task 4, 7 |
| 注册入口 | Task 9 |
| 测试 | Task 2-10 |
| 文档 | Task 11 |

**Placeholder 检查：** 无 TBD、TODO、未定义函数或占位符。

**类型一致性：** 所有方法签名、Redis key、JWT 解析、payload 字段在文档与计划间一致。

---

## 执行交接

**Plan complete and saved to `docs/superpowers/plans/2026-06-17-mimocode-free-channel.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach would you like?
