# Tokenizer Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable tokenizer estimation rules for context-window validation, with built-in defaults and a management entry on the model metadata / model market page.

**Architecture:** Context validation will resolve a tokenizer rule by matching the request model ID against configured regex rules, falling back to built-in defaults when no custom rule matches. Token estimation moves behind a small resolver/estimator interface in `main.py`, while admin APIs persist only user-defined rules in the main config store. The model market page exposes rule editing; built-in defaults are hardcoded and not stored.

**Tech Stack:** FastAPI, Python, optional `tiktoken`, existing `CONFIG_STORE`, existing `static/admin.html`, pytest.

---

## File Structure

- Modify `main.py`
  - Add tokenizer rule defaults.
  - Add tokenizer rule normalization and model matching.
  - Add token estimation by `chars` and optional `tiktoken`.
  - Wire `_validate_context_budget_for_parts()` through model-aware estimation.
- Modify `config.py`
  - Add `Config.get_tokenizer_rules()` returning custom rules from main config.
- Modify `admin.py`
  - Add `GET /admin/config/main/tokenizer-rules`.
  - Add `PUT /admin/config/main/tokenizer-rules`.
  - Validate regex and tokenizer fields.
- Modify `static/admin.html`
  - Add a Tokenizer Rules panel on the model metadata / model market page.
  - Show built-in default rules as read-only.
  - Allow editing custom rules as JSON textarea in the first implementation.
- Modify `tests/test_context_budget.py`
  - Add TDD tests for chars tokenizer, tiktoken fallback, custom regex rule priority, and built-in fallback.

---

### Task 1: Add tokenizer rule tests

**Files:**
- Modify: `tests/test_context_budget.py`

- [ ] **Step 1: Add failing tests**

Append these tests to `tests/test_context_budget.py`:

```python
def test_context_budget_uses_custom_chars_rule(monkeypatch):
    monkeypatch.setattr(
        main.config.Config,
        "get_tokenizer_rules",
        lambda: [{
            "name": "test chars 10",
            "enabled": True,
            "pattern": "^test-model$",
            "type": "chars",
            "chars_per_token": 10,
        }],
    )
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "x" * 100}],
        "max_tokens": 1,
    }

    main._validate_chat_request(body)


def test_context_budget_uses_builtin_chars_fallback(monkeypatch):
    monkeypatch.setattr(main.config.Config, "get_tokenizer_rules", lambda: [])
    assert main._estimate_request_part_tokens("unknown-model", "x" * 20) == 10


def test_tiktoken_rule_falls_back_to_chars_when_package_missing(monkeypatch):
    monkeypatch.setattr(main.config.Config, "get_tokenizer_rules", lambda: [{
        "name": "openai",
        "enabled": True,
        "pattern": "^gpt-",
        "type": "tiktoken",
        "encoding": "cl100k_base",
    }])
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("missing tiktoken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert main._estimate_request_part_tokens("gpt-test", "x" * 20) == 10


def test_custom_rule_takes_priority_over_builtin_openai_rule(monkeypatch):
    monkeypatch.setattr(main.config.Config, "get_tokenizer_rules", lambda: [{
        "name": "business gpt alias",
        "enabled": True,
        "pattern": "^gpt-5\\.5$",
        "type": "chars",
        "chars_per_token": 10,
    }])

    assert main._estimate_request_part_tokens("gpt-5.5", "x" * 100) == 10
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python -m pytest tests/test_context_budget.py -q
```

Expected: FAIL because `_estimate_request_part_tokens()` currently accepts only one argument and `Config.get_tokenizer_rules()` does not exist.

---

### Task 2: Add config accessor for custom tokenizer rules

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Implement accessor**

Add this method near other main-config accessors such as `stream_incomplete_error_enabled()`:

```python
    @classmethod
    def get_tokenizer_rules(cls) -> list[dict]:
        rules = cls._load().get("tokenizer", {}).get("rules", [])
        return rules if isinstance(rules, list) else []
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
python -m pytest tests/test_context_budget.py -q
```

Expected: still FAIL because `main._estimate_request_part_tokens()` has not been updated yet.

---

### Task 3: Implement tokenizer rule resolution and estimation

**Files:**
- Modify: `main.py:260-351`

- [ ] **Step 1: Add built-in default rules and helpers**

Replace the current `_estimate_request_part_tokens(value)` implementation with model-aware helpers:

```python
_BUILTIN_TOKENIZER_RULES = [
    {
        "name": "OpenAI-like",
        "enabled": True,
        "pattern": r"^(gpt|o[134]|chatgpt)-",
        "type": "tiktoken",
        "encoding": "cl100k_base",
    },
]
_DEFAULT_TOKENIZER_RULE = {"type": "chars", "chars_per_token": 2}


def _tokenizer_rules() -> list[dict]:
    custom_rules = config.Config.get_tokenizer_rules()
    rules = custom_rules if isinstance(custom_rules, list) else []
    return [*rules, *_BUILTIN_TOKENIZER_RULES]


def _resolve_tokenizer_rule(model: str) -> dict:
    model = model or ""
    for rule in _tokenizer_rules():
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        try:
            if re.search(pattern, model):
                return rule
        except re.error as e:
            logger.warning(f"tokenizer 规则正则无效: name={rule.get('name')}, pattern={pattern}, error={e}")
            continue
    return dict(_DEFAULT_TOKENIZER_RULE)


def _estimate_chars_tokens(text: str, rule: dict) -> int:
    try:
        chars_per_token = float(rule.get("chars_per_token") or 2)
    except (TypeError, ValueError):
        chars_per_token = 2
    if chars_per_token <= 0:
        chars_per_token = 2
    return max(1, int((len(text) + chars_per_token - 1) // chars_per_token))


def _estimate_tiktoken_tokens(text: str, rule: dict) -> int | None:
    try:
        import tiktoken
    except ImportError:
        return None
    encoding_name = str(rule.get("encoding") or "cl100k_base")
    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception as e:
        logger.warning(f"tiktoken encoding 不可用: encoding={encoding_name}, error={e}")
        return None
    return len(encoding.encode(text))


def _estimate_request_part_tokens(model: str, value) -> int:
    text = _request_part_to_text(value)
    if not text:
        return 0
    rule = _resolve_tokenizer_rule(model)
    if rule.get("type") == "tiktoken":
        tokens = _estimate_tiktoken_tokens(text, rule)
        if tokens is not None:
            return max(1, tokens)
    return _estimate_chars_tokens(text, rule)
```

- [ ] **Step 2: Update callers**

In `_validate_context_budget_for_parts()`, change:

```python
part_tokens = {name: _estimate_request_part_tokens(value) for name, value in parts if value is not None}
```

to:

```python
part_tokens = {name: _estimate_request_part_tokens(model, value) for name, value in parts if value is not None}
```

- [ ] **Step 3: Ensure import exists**

At top of `main.py`, ensure `re` is imported. If not present, add:

```python
import re
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_context_budget.py -q
```

Expected: PASS.

---

### Task 4: Add admin API for tokenizer rules

**Files:**
- Modify: `admin.py`

- [ ] **Step 1: Add validator helpers**

Near main config endpoints, add:

```python
def _clean_tokenizer_rules(data) -> list[dict]:
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="tokenizer rules 必须是数组")
    cleaned = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条规则必须是对象")
        name = str(item.get("name") or f"rule-{idx + 1}").strip()
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条规则缺少 pattern")
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条规则正则无效: {e}")
        tokenizer_type = str(item.get("type") or "chars").strip().lower()
        rule = {"name": name, "enabled": item.get("enabled") is not False, "pattern": pattern, "type": tokenizer_type}
        if tokenizer_type == "tiktoken":
            rule["encoding"] = str(item.get("encoding") or "cl100k_base").strip() or "cl100k_base"
        elif tokenizer_type == "chars":
            try:
                chars_per_token = float(item.get("chars_per_token") or 2)
            except (TypeError, ValueError):
                chars_per_token = 2
            rule["chars_per_token"] = chars_per_token if chars_per_token > 0 else 2
        else:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条规则 type 只能是 chars 或 tiktoken")
        cleaned.append(rule)
    return cleaned
```

- [ ] **Step 2: Add endpoints**

Add:

```python
@router.get("/config/main/tokenizer-rules")
async def get_tokenizer_rules(token: str = Header(None, alias="Authorization")):
    await _require_admin(token)
    cfg = _read_json(CONFIG_FILE)
    rules = (cfg.get("tokenizer") or {}).get("rules") or []
    return {"rules": rules if isinstance(rules, list) else []}


@router.put("/config/main/tokenizer-rules")
async def update_tokenizer_rules(data: dict, token: str = Header(None, alias="Authorization")):
    await _require_admin(token)
    rules = _clean_tokenizer_rules((data or {}).get("rules"))
    cfg = _read_json(CONFIG_FILE)
    cfg.setdefault("tokenizer", {})["rules"] = rules
    _write_json(CONFIG_FILE, cfg)
    return {"ok": True, "rules": rules}
```

- [ ] **Step 3: Run syntax check**

Run:

```bash
python -m py_compile admin.py
```

Expected: no output.

---

### Task 5: Add model market UI for tokenizer rules

**Files:**
- Modify: `static/admin.html`

- [ ] **Step 1: Add panel to model metadata page**

In `renderModelMetadataPage()`, inside the left filter card after the provider filter, add a button:

```html
<button class="btn" onclick="showTokenizerRulesModal()">Tokenizer 规则</button>
<div class="form-hint">按 model_id 正则匹配 tokenizer；未匹配时使用内置 chars:2。</div>
```

- [ ] **Step 2: Add modal functions**

Near model metadata JS functions, add:

```javascript
async function showTokenizerRulesModal() {
  const data = await api('/admin/config/main/tokenizer-rules');
  const rules = data.rules || [];
  const example = rules.length ? rules : [
    {name:'业务 GPT 别名', enabled:true, pattern:'^gpt-5\\.5$', type:'chars', chars_per_token:2},
    {name:'OpenAI 覆盖', enabled:true, pattern:'^(gpt|o[134]|chatgpt)-', type:'tiktoken', encoding:'cl100k_base'}
  ];
  const html = `
    <div class="form-hint" style="margin-bottom:8px">这里只保存自定义规则；内置默认规则不入库：OpenAI-like 使用 tiktoken:cl100k_base，最终兜底使用 chars:2。</div>
    <textarea id="tokenizerRulesJson" rows="14" style="width:100%;font-family:var(--fontM);font-size:12px">${esc(JSON.stringify(example, null, 2))}</textarea>
  `;
  openModal('Tokenizer 规则', html, async () => {
    let rules;
    try { rules = JSON.parse(document.getElementById('tokenizerRulesJson').value || '[]'); }
    catch(e) { throw new Error('Tokenizer 规则不是合法 JSON'); }
    await api('/admin/config/main/tokenizer-rules', {
      method:'PUT',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rules})
    });
    toast('Tokenizer 规则已保存', 'success');
  });
}
```

- [ ] **Step 3: No build step required**

This project uses plain static HTML. No frontend build command is needed.

---

### Task 6: Full verification

**Files:**
- Test: `tests/test_context_budget.py`
- Test: `tests/test_retry_policy.py`

- [ ] **Step 1: Run context tests**

Run:

```bash
python -m pytest tests/test_context_budget.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run retry policy tests**

Run:

```bash
python -m pytest tests/test_retry_policy.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run syntax checks**

Run:

```bash
python -m py_compile main.py config.py admin.py providers/custom.py providers/base.py
```

Expected: no output.

---

## Self-Review

- Spec coverage: custom regex rules, built-in defaults not stored, model market entry, tiktoken/chars support, fallback behavior all have tasks.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: rule fields are consistently `name`, `enabled`, `pattern`, `type`, `encoding`, `chars_per_token`; config path is consistently `tokenizer.rules`.
