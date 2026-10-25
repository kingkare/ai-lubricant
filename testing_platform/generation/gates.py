"""生成环节机器门（25 号文 §环节6 第1闸；P0 最小版）。

落地范围严格按 25 号文 §第十部分 P0 行原文：

    机器门最小版：幻觉核验 + 硬编码 + 幂等 + hard 规则四查，
    无双评无打分（终审#4 矛盾修正）。

本模块是**纯函数 + 注入的 async 查询参数**：所有 DB 读取（接口行、已存在
slot_key、active+hard 规则）通过调用方注入的 callable 传入，不 import tortoise，
不碰任何 ORM——保持与 ``quality/`` 同款「纯代码可判」的测试形态。

依赖纪律（任务边界）：

- **只 import ``quality.errors`` / ``quality.evidence``**（quality 只读不写）；
- **不 import ``generation`` 内其他模块**（``__init__.py`` / ``models.py`` 可能被
  别的 agent 并发改——本文件自包含）；
- **不 import ``engine``**——占位符语法口径复用 ``engine.variables`` 的 ``{{scope.path}}``
  形态，但只用一组本地常量刻画（数据五作用域 ``env/auth/local/data/global`` + 别名
  ``dataset``），不引入引擎依赖，避免反向耦合。

四查 + 两复用 + 汇总（共 7 个 check + 1 个 run_machine_gates）：

1. :func:`check_hallucination` — 用例 body 引用的 ``api_id`` / 参数名逐个核
   （``api_lookup`` 返回 None=接口不存在→error；参数名不在 params[]/request_body.properties→error）。
2. :func:`check_hardcoded` — 数据五作用域检查（字面 URL/Bearer/疑似密码字段
   没写成 ``{{env.*}}``/``{{data.*}}``/``{{local.*}}``/``{{auth.*}}``/``{{global.*}}``
   占位符→warn「可维护性不合格」）。
3. :func:`check_evidence` — 复用 :func:`quality.evidence.validate_evidence_claim`
   对 payload 的 intent/expected 文本跑一遍。
4. :func:`check_idempotency` — ``slot_key`` 撞已有→warn dup。
5. :func:`check_rule_receipt` — ``applied_rules[]`` 回执，每条 active+hard 规则
   必须出现（followed|not_applicable 带 why）；未声明=violated（第七部分坑#2）；
   回执里 violated→error。
6. :func:`check_api_mandatory_scenarios` — surface=api 的用例簇必须覆盖 Token 失效
   与越权两场景（Skill15 必覆盖，§环节5 步骤2）；payload.scenario_tag 字段判定，
   缺→warn。
7. :func:`run_machine_gates` — 汇总以上，返回
   ``{valid, issues[], review_state: machine_pending|machine_rejected}``
   （有 error→machine_rejected）。

所有 Issue 用 :class:`quality.errors.ValidationIssue`（``path/rule/msg/severity/hint``），
rule 前缀统一 ``MG_``。
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from ..quality.errors import (
    SEVERITY_ERROR,
    SEVERITY_WARN,
    ValidationIssue,
    ValidationResult,
)
from ..quality.evidence import validate_evidence_claim

# ── 数据五作用域（25 号文 §2.7）─────────────────────────────────
#: 五作用域前缀（env/auth/local/data/global）+ engine.variables 的 dataset 别名（=data）。
#: 占位符口径 ``{{scope.path}}`` 复用 engine.variables 的 ``_PLACEHOLDER_RE`` 语义
#: （允许内部空白），这里不引入 engine 依赖，用本地正则刻画同一形态。
FIVE_SCOPES: tuple[str, ...] = ("env", "auth", "local", "data", "global")
SCOPE_ALIASES: dict[str, str] = {"dataset": "data"}  # 与 engine.variables 一致
KNOWN_PLACEHOLDER_SCOPES: frozenset[str] = frozenset(FIVE_SCOPES) | frozenset(SCOPE_ALIASES)

#: ``{{ scope.path }}`` 占位符（允许内部空白；与 engine.variables._PLACEHOLDER_RE 同口径）
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

#: 字面 URL（去掉所有 {{}} 占位符后仍以 http(s):// 开头）
_URL_LITERAL_RE = re.compile(r"https?://", re.IGNORECASE)

#: 字面 host（scheme 即使来自占位符，``://`` 后面残留字面 host 也是硬编码环境地址）：
#: ``://api.example.com`` / ``://10.0.0.1:8000`` ——占位符抹掉后 ``://`` 仍跟着
#: 至少一个 host 字符（非 ``/`` 空白引号）即命中。
_URL_HOST_LITERAL_RE = re.compile(r"://[^/\s\"'{}，。；]" )

#: Bearer 字面 token（Authorization: Bearer <非占位符串>）
_BEARER_LITERAL_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-/+=]+", re.IGNORECASE)

#: 疑似密码/密钥字段名（与 quality/validators._CREDENTIAL_KEY_RE 同口径）。
#: 词边界防子串误报（e2e 实测修正）：`secretary`/`tokenizer`/`passwordless` 这类
#: 非凭据键不许命中——前后各限非字母数字（`_` 仍算分隔，`my_password` 照报）。
_CREDENTIAL_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:password|passwd|pwd|token|secret|access_key|api_key|"
    r"apikey|authorization|bearer|client_id|client_secret)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: api_id 引用提取：payload 里的 ``api_id`` 字段（标量）或 steps 里 ``{{... api_id ...}}``；
#: 本检查只认显式 api_id 字段（confirm_ref.api_id / api_id），不靠语义猜。
_API_ID_FIELDS: tuple[str, ...] = ("api_id",)

#: Slot key 生成口径（25 号文 §2.3 + transform_table.slot_key 同形）：
#:   api 轴：``api:{api_id}:{param}:{axis}:{variant}``
#:   flow 轴：``flow:{tp_code}:{surface}:{seq}``
#:   auth 通用轴：``auth:{api_id}:{variant}``；idem 通用轴：``idem:{api_id}:{variant}``
_SLOT_KEY_FLOW_PREFIX = "flow:"

#: Token 失效 / 越权两场景的 scenario_tag 判定（Skill15 必覆盖）。
#: 设计文档原文只说「Token 失效与越权两场景必覆盖」，未给定 scenario_tag 枚举值——
#: 这里用一组宽松别名集合刻画（命中其一即视为已覆盖），避免与未来 tag 词典强耦合。
_TOKEN_INVALID_TAGS: frozenset[str] = frozenset(
    {
        "token_expired", "token-expired", "token_invalid", "token-invalid",
        "no-token", "expired-token", "invalid-token", "token_failure",
        "auth_failure", "auth-failure", "token失效", "token 失效",
    }
)
_CROSS_TENANT_TAGS: frozenset[str] = frozenset(
    {
        "cross_tenant", "cross-tenant", "unauthorized", "cross_tenant_access",
        "privilege_escalation", "privilege-escalation", "forbidden", "越权",
        "越权id", "越权 id", "cross-tenant-access",
    }
)
#: api 端必覆盖的两类场景（顺序即报告顺序）
MANDATORY_API_SCENARIOS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("token_invalid", "Token 失效", _TOKEN_INVALID_TAGS),
    ("cross_tenant", "越权", _CROSS_TENANT_TAGS),
)

#: api_lookup 签名：``async (api_id: str) -> dict | None``
ApiLookup = Callable[[str], Awaitable[Optional[dict]]]


# ── 辅助（纯函数）──────────────────────────────────────────────
def _placeholder_paths(text: str) -> list[str]:
    """提取文本里所有 ``{{path}}`` 占位符的路径（去空白）。"""
    return [m.group(1).strip() for m in _PLACEHOLDER_RE.finditer(text or "")]


def _scope_of(path: str) -> str:
    """``env.base_url`` → ``env``；``x`` → ``x``（无点号=裸名）。"""
    return path.split(".", 1)[0].strip()


def _is_placeholder_only(text: str) -> bool:
    """整串恰好是一个 ``{{...}}`` 占位符（允许首尾空白）。"""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return bool(stripped.startswith("{{") and stripped.endswith("}}")
                and stripped.count("{{") == 1 and stripped.count("}}") == 1)


def _strip_placeholders(text: str) -> str:
    """把所有 ``{{...}}`` 占位符抹掉（用于字面量扫描：剩下的才是真字面量）。"""
    return _PLACEHOLDER_RE.sub("", text or "")


def _iter_strings(obj: Any, path: str):
    """递归遍历 JSON 结构，产出 (位置路径, 字符串值)。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _iter_strings(v, f"{path}[{i}]")


def _iter_strings_skipping(obj: Any, path: str, skip_keys: frozenset):
    """同 ``_iter_strings``，但跳过 dict 中指定键（如 applied_rules/scenario_tag
    本身是元数据不是步骤字面量，不参与硬编码扫描）。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in skip_keys:
                continue
            yield from _iter_strings_skipping(v, f"{path}.{k}" if path else str(k), skip_keys)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _iter_strings_skipping(v, f"{path}[{i}]", skip_keys)


def _api_param_names(api: Mapping[str, Any]) -> set[str]:
    """接口行的全部参数名集合（params[] + request_body 顶层 properties 键）。

    request_body 形状兼容两种（与 design.transform_table.enumerate_api 同口径）：
    ``{"schema": {"properties": {...}}}`` 优先，退 ``body["properties"]``。
    """
    names: set[str] = set()
    for p in api.get("params") or []:
        if isinstance(p, Mapping):
            n = str(p.get("name") or "").strip()
            if n:
                names.add(n)
    body = api.get("request_body") or {}
    if not isinstance(body, Mapping):
        return names
    schema = body.get("schema")
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping):
        props = body.get("properties")
    if isinstance(props, Mapping):
        for k in props.keys():
            if k:
                names.add(str(k))
    return names


def _collect_referenced_api_ids(payload: Mapping[str, Any]) -> list[str]:
    """收集 payload 里显式声明的 api_id 引用（去重保序）。

    认 ``confirm_ref.api_id`` / 顶层 ``api_id`` / steps[].confirm_ref.api_id`` 等
    显式字段——不靠文本语义猜（幻觉核验只查显式引用，避免误报）。
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def _push(v: Any) -> None:
        if isinstance(v, str) and v.strip() and v not in seen_set:
            seen_set.add(v)
            seen.append(v.strip())

    # 顶层 api_id
    _push(payload.get("api_id"))
    # confirm_ref.api_id（骨架用例的标准挂点，design/skeleton.py）
    cr = payload.get("confirm_ref")
    if isinstance(cr, Mapping):
        _push(cr.get("api_id"))
    # steps 里的 confirm_ref
    for step in payload.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        _push(step.get("api_id"))
        scr = step.get("confirm_ref")
        if isinstance(scr, Mapping):
            _push(scr.get("api_id"))
    return seen


#: 请求体在步骤 params 里的常见挂载键（engine request 步骤的 body 形状）——
#: MG_H2 只核「请求参数名」，这些键的顶层 key 才是参数名；transport 级键
#: （method/url/headers）不核，响应字段（expected.key_fields）不核（属 responses 契约）。
_REQUEST_BODY_KEYS: frozenset = frozenset({"body", "json", "data", "form", "query", "payload"})


def _collect_referenced_param_names(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """收集 payload 里引用的**请求参数名**（位置, 参数名），用于幻觉核验 MG_H2。

    只取显式的请求参数引用，避免误伤：

    - ``payload.params[]`` 里带 ``name`` 的条目（用例作者显式列的请求参数）
    - ``payload.body`` / ``payload.request_body`` 的顶层键（用例级 body 模板）
    - ``steps[].params`` 下 ``body/json/data/form/query/payload`` 的顶层键（请求体字段）

    **不取** ``steps[].params`` 的 transport 级键（method/url/headers——那是引擎形状），
    **不取** ``expected.key_fields``（那是响应字段，属 ``responses`` 契约，非请求参数）。
    """
    out: list[tuple[str, str]] = []

    for i, p in enumerate(payload.get("params") or []):
        if isinstance(p, Mapping):
            n = str(p.get("name") or "").strip()
            if n:
                out.append((f"$.params[{i}].name", n))

    for field in ("body", "request_body"):
        b = payload.get(field)
        if isinstance(b, Mapping):
            for k in b.keys():
                if k:
                    out.append((f"$.{field}.{k}", str(k)))

    for si, step in enumerate(payload.get("steps") or []):
        if not isinstance(step, Mapping):
            continue
        sp = step.get("params")
        if not isinstance(sp, Mapping):
            continue
        for bk in _REQUEST_BODY_KEYS:
            nested = sp.get(bk)
            if isinstance(nested, Mapping):
                for k in nested.keys():
                    if k:
                        out.append((f"$.steps[{si}].params.{bk}.{k}", str(k)))
    return out


# ── 1. 幻觉核验 ────────────────────────────────────────────────
async def check_hallucination(
    case_payload: dict,
    api_lookup: ApiLookup,
) -> list[ValidationIssue]:
    """MG_H1/MG_H2：用例 body 引用的 api_id 与参数名逐个对 ``mc_test_api`` 核。

    - ``api_lookup(api_id)`` 返回 ``None`` → 接口不存在 → **error**（MG_H1）。
    - 参数名不在接口的 ``params[]``/``request_body.properties`` → **error**（MG_H2）。

    纯逻辑：DB 读取全部走注入的 ``api_lookup``（``async (api_id) -> dict|None``），
    本函数不 import tortoise。``api_lookup`` 抛异常时不吞——交由调用方处理（机器门
    不假装通过：查不到=error，查挂了=上抛）。
    """
    issues: list[ValidationIssue] = []
    api_ids = _collect_referenced_api_ids(case_payload)
    params_with_loc = _collect_referenced_param_names(case_payload)

    # 逐 api_id 核存在性；缓存查询结果给参数名核验复用
    api_cache: dict[str, Optional[Mapping[str, Any]]] = {}
    for aid in api_ids:
        api_doc = await api_lookup(aid)
        api_cache[aid] = api_doc if isinstance(api_doc, Mapping) else None
        if api_cache[aid] is None:
            issues.append(ValidationIssue(
                path="$.api_id",
                rule="MG_H1_api_not_found",
                msg=f"用例引用的接口不存在：api_id={aid!r}（api_lookup 返回 None → 幻觉核验 MISSING 打回）",
                severity=SEVERITY_ERROR,
                hint="确认 api_id 指向 mc_test_api 中真实存在的接口行；AI 不得凭空引用接口",
            ))

    # 参数名核验：对每个被引用的参数名，在「任一已确认存在的接口」的参数集合里找到才算过。
    # 多 api_id 场景（用例跨接口）取并集；接口不存在的已在上面记 error，这里跳过该接口的参数集。
    valid_param_names: set[str] = set()
    for aid, api_doc in api_cache.items():
        if api_doc is None:
            continue
        valid_param_names |= _api_param_names(api_doc)

    # 无任何 api_id 引用但 payload 又列了参数名——按「无接口可核」记 warn（MG_H2 无法判定，
    # 但参数名可能是 body 内部键而非接口参数；不强行判 error 避免误伤）。
    if api_ids and params_with_loc:
        for loc, pname in params_with_loc:
            if pname not in valid_param_names:
                issues.append(ValidationIssue(
                    path=loc,
                    rule="MG_H2_param_not_in_api",
                    msg=(f"参数名 {pname!r} 不在引用接口的 params[]/request_body.properties 中"
                         f"（接口参数集：{sorted(valid_param_names) or '空'}）——疑似凭空造参数"),
                    severity=SEVERITY_ERROR,
                    hint="参数名必须对齐 mc_test_api 的 params[] 或 request_body 顶层 properties",
                ))

    return issues


# ── 2. 硬编码扫描（数据五作用域）──────────────────────────────
def check_hardcoded(case_payload: dict) -> list[ValidationIssue]:
    """MG_HC：数据五作用域检查——步骤/请求里的字面量值应写成五作用域占位符。

    扫描形态（25 号文 §2.7「硬编码=值不在五处又写死步骤里→机器门判可维护性不合格」）：

    - 字面 URL：去掉所有 ``{{}}`` 后仍以 ``http(s)://`` 开头 → warn
    - Bearer 字面 token：``Bearer <非占位符串>`` → warn
    - 疑似密码字段明文值：键名命中 credential 形似且值不含 ``{{}}`` → warn

    占位符放行：整串是 ``{{env.*}}``/``{{data.*}}``/``{{local.*}}``/``{{auth.*}}``/
    ``{{global.*}}``（含 dataset 别名）的，或文本里完全是占位符拼贴的，不报。

    严重级按任务口径=**warn**（可维护性不合格，不阻断入库但标记）。
    跳过 ``applied_rules``/``scenario_tag``/``evidence_class``/``review_state`` 等
    元数据键（它们是评审回执不是步骤字面量）。
    """
    issues: list[ValidationIssue] = []
    skip_keys = frozenset({
        "applied_rules", "scenario_tag", "evidence_class", "review_state",
        "review_round", "rewrite_of", "version", "score", "score_breakdown",
        "reviews", "dup_of_case_id", "dup_score", "dup_verdict",
        "source_agent", "provenance", "slot_key", "status", "kind",
        "testpoint_id", "surface", "run_id", "question_id",
    })
    for path, text in _iter_strings_skipping(case_payload, "$", skip_keys):
        cleaned = _strip_placeholders(text)
        # 字面 URL（整串以 http(s):// 开头，或 scheme 用占位符但 :// 后残留字面 host）
        if _URL_LITERAL_RE.search(cleaned) or _URL_HOST_LITERAL_RE.search(cleaned):
            issues.append(ValidationIssue(
                path=path,
                rule="MG_HC_literal_url",
                msg=f"检测到硬编码 URL：{text[:80]!r}（25 号文 §2.7：URL 应走 {{env.*}} 等五作用域占位符）",
                severity=SEVERITY_WARN,
                hint="URL 写成 {{env.base_url}}/path，由环境注入；不要在步骤里写死 host",
            ))
        # Bearer 字面 token
        if _BEARER_LITERAL_RE.search(cleaned):
            issues.append(ValidationIssue(
                path=path,
                rule="MG_HC_literal_bearer",
                msg=f"检测到硬编码 Bearer token：{text[:80]!r}（token 应走 {{auth.*}} 引擎自动注入）",
                severity=SEVERITY_WARN,
                hint="Authorization 头写成 {{auth.token}}，由引擎鉴权作用域注入",
            ))

    # 疑似密码字段明文值（递归 dict，键名形似 password/token/secret 等）
    issues.extend(_scan_credential_keys(case_payload, "$", skip_keys))
    return issues


def _scan_credential_keys(obj: Any, path: str, skip_keys: frozenset) -> list[ValidationIssue]:
    """递归 dict：键名形似凭据且值是非空字符串且不含 ``{{}}`` → warn。"""
    out: list[ValidationIssue] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in skip_keys:
                continue
            if _CREDENTIAL_KEY_RE.search(str(k)) and isinstance(v, str) and v and "{{" not in v:
                out.append(ValidationIssue(
                    path=f"{path}.{k}",
                    rule="MG_HC_literal_credential",
                    msg=f"凭据字段 {k!r} 使用了明文值（25 号文 §2.7：账号/token 应走 {{env.*}}/{{auth.*}}）",
                    severity=SEVERITY_WARN,
                    hint=f"{k} 应写成 {{{{env.{k}}}}} 或 {{{{auth.{k}}}}}，不要写死真值",
                ))
            out.extend(_scan_credential_keys(v, f"{path}.{k}", skip_keys))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_scan_credential_keys(v, f"{path}[{i}]", skip_keys))
    return out


# ── 3. 证据核验（复用 quality.evidence）─────────────────────────
def check_evidence(case_payload: dict, proposal_evidence_class: str) -> list[ValidationIssue]:
    """MG_EV：对 payload 的**意图层**文本跑 quality.evidence.validate_evidence_claim。

    范围口径（e2e 实测修正）：只对 ``intent``（"测什么、为什么测"——AI 的**主张**，
    须有依据）跑全文纪律（EV1 假精确/EV2 模糊面/EV3 猜因/EV4 直证带来源）。
    **不对 ``expected`` 的每个值叶逐字符索证**——expected 是断言期望值
    （`"account_status":"active"` 这类），来源是需求而非 file:line；
    逐叶跑 EV4 会把 AI 生成用例近乎全灭（e2e 实测 43.8%→60.9% 打回率根因，
    且回炉继承同等级=死循环）。expected 只查 EV1-EV3 文本纪律（哪一级都不要求带来源）。

    EV4 对「派生用例」放宽（e2e 实测修正 #2）：static 级 intent 未带 file:line 时，
    若 payload 有测试点派生链（``testpoint_code``/``evidence``）——即本用例从已确认
    的 ``mc_test_testpoint``（上游=需求条目）派生——则视为已满足来源义务；
    ``executed`` 级不放宽（声称"真跑过"必须给运行记录）。
    """
    issues: list[ValidationIssue] = []
    intent = case_payload.get("intent")
    if isinstance(intent, str) and intent.strip():
        res = validate_evidence_claim(intent, evidence_class=proposal_evidence_class, path="$.intent")
        for i in res.issues:
            # EV4（直证带来源）对「派生用例」的放宽（e2e 实测修正 #2）：static 级
            # intent 的来源不必是 file:line——本用例是从已确认的 mc_test_testpoint
            # （上游=需求条目）派生的，payload 的 testpoint_code/evidence 就是它的
            # 静态来源链。executed 级不放宽：声称「真跑过」必须给运行记录。
            if (i.rule == "EV4_direct_needs_source"
                    and (proposal_evidence_class or "").strip() == "static"
                    and (str(case_payload.get("testpoint_code") or "").strip()
                         or (case_payload.get("evidence") or []))):
                continue
            issues.append(i)
    expected = case_payload.get("expected")
    expected_texts: list[tuple[str, str]] = []
    if isinstance(expected, Mapping):
        expected_texts = list(_iter_strings(expected, "$.expected"))
    elif isinstance(expected, str) and expected.strip():
        expected_texts = [("$.expected", expected)]
    for p, text in expected_texts:
        if isinstance(text, str) and text.strip():
            # EV4（直证带来源）只对 intent 层——expected 层只跑文本级三条。
            # 实现：临时把等级降为 claimed 跑一遍（claimed 不受 EV4 约束）。
            res = validate_evidence_claim(text, evidence_class="claimed", path=p)
            for i in res.issues:
                if i.rule == "EV4_direct_needs_source":
                    continue
                issues.append(i)
    return issues


# ── 4. 幂等查重 ────────────────────────────────────────────────
def make_slot_key(
    api_id: Optional[str],
    testpoint_code: Optional[str],
    surface: str,
    variant: str,
) -> str:
    """生成幂等 slot_key（25 号文 §2.3 口径：``api:{id}:{param}:{axis}:{variant}`` /
    ``flow:{tp}:{surface}:{seq}``）。

    与 design.transform_table.slot_key 的分工：那个是「机械枚举产 slot」的专用键
    （keyword-only、只覆盖 api/auth/idem 三种前缀）；本函数是「机器门侧按 proposal
    现场算键」的统一入口，覆盖 api 轴与 flow 轴两种形态。api 轴上两者格式一致
    （``api:{id}:...``），撞键判定同一真相。

    参数到口径的映射（函数签名按任务定版，4 个位置参）：

    - **api 轴**（``api_id`` 非空）→ ``api:{api_id}:{variant}``，其中 ``variant``
      由调用方打包为 ``param:axis:variant`` 三段（取 transform_table slot 的
      同名三字段），拼出设计口径 ``api:{id}:{param}:{axis}:{variant}``。
    - **flow 轴**（``api_id`` 为空）→ ``flow:{testpoint_code}:{surface}:{variant}``，
      其中 ``variant`` 即序号 ``seq``——产出 ``flow:{tp}:{surface}:{seq}``。
    - 空段兜底 ``_``（不产出悬空冒号键）。
    """
    if api_id:
        return f"api:{api_id}:{variant}"
    tp = (testpoint_code or "").strip() or "_"
    surf = (surface or "").strip() or "_"
    return f"{_SLOT_KEY_FLOW_PREFIX}{tp}:{surf}:{variant}"


def check_idempotency(case_payload: dict, existing_slot_keys: set[str]) -> list[ValidationIssue]:
    """MG_ID：slot_key 撞已有 → warn dup。

    ``case_payload`` 若带 ``slot_key`` 字段则直接比对；否则按 ``make_slot_key`` 现算
    （需要 api_id/testpoint_code/surface/variant 字段齐备，缺字段则跳过——不强行编键
    避免假撞）。撞已有 → warn（25 号文 §门②步骤6：reopen 用 ``:v2`` 后缀让位，撞键不
    阻断入库但需标记）。
    """
    issues: list[ValidationIssue] = []
    key = case_payload.get("slot_key")
    if not key:
        # 现算：从 payload 取 api_id/testpoint_code/surface/variant
        api_id = case_payload.get("api_id") or (case_payload.get("confirm_ref") or {}).get("api_id")
        tp = case_payload.get("testpoint_code") or case_payload.get("testpoint_id")
        surface = case_payload.get("surface") or ""
        variant = case_payload.get("variant") or (case_payload.get("confirm_ref") or {}).get("slot")
        if api_id or tp:
            key = make_slot_key(api_id, tp, surface, variant or "")
    if not key:
        return issues
    if key in existing_slot_keys:
        issues.append(ValidationIssue(
            path="$.slot_key",
            rule="MG_ID_dup_slot_key",
            msg=f"slot_key 撞已有：{key!r}（25 号文 §门②步骤6：reopen 用 :v2 后缀让位，绕部分唯一索引）",
            severity=SEVERITY_WARN,
            hint="若为 reopen 派生，slot_key 带 :v2 后缀；否则检查是否重复生成同一 slot",
        ))
    return issues


# ── 5. hard 规则回执 ───────────────────────────────────────────
def check_rule_receipt(case_payload: dict, active_hard_rules: list[dict]) -> list[ValidationIssue]:
    """MG_RR：applied_rules[] 回执——每条 active+hard 规则必须出现，未声明=violated。

    25 号文 §第七部分坑#2 原文：「我们生成回执 ``applied_rules[]`` 时，'未声明'必须
    判为违规而非合规」。落地口径：

    - 每条 ``active_hard_rules``（``{rule_id, ...}``）必须在 ``payload.applied_rules[]``
      里出现且 ``status`` ∈ {``followed``, ``not_applicable``}（后者需带 ``why``）。
    - 未声明 → **error**（``MG_RR_undeclared``，未声明=违规）。
    - 已声明但 ``status=violated`` → **error**（``MG_RR_violated``）。
    - ``not_applicable`` 缺 ``why`` → **warn**（``MG_RR_na_no_why``）。

    ``active_hard_rules`` 由调用方注入（DB 查 ``mc_test_rule where kind=hard and
    enforcement=active`` 的结果），本函数不查库。
    """
    issues: list[ValidationIssue] = []
    receipts = case_payload.get("applied_rules") or []
    if not isinstance(receipts, (list, tuple)):
        issues.append(ValidationIssue(
            path="$.applied_rules",
            rule="MG_RR_schema",
            msg="applied_rules 必须是数组（规则回执容器）",
            severity=SEVERITY_ERROR,
            hint="applied_rules: [{rule_id, status, why}, ...]",
        ))
        receipts = []
    # 索引：rule_id → receipt
    by_rule: dict[str, Mapping[str, Any]] = {}
    for i, r in enumerate(receipts):
        if not isinstance(r, Mapping):
            issues.append(ValidationIssue(
                path=f"$.applied_rules[{i}]",
                rule="MG_RR_schema",
                msg=f"回执条目必须是对象：{r!r}",
                severity=SEVERITY_ERROR,
            ))
            continue
        rid = str(r.get("rule_id") or "").strip()
        if rid:
            by_rule[rid] = r

    for rule in active_hard_rules or []:
        if not isinstance(rule, Mapping):
            continue
        rid = str(rule.get("rule_id") or rule.get("id") or "").strip()
        if not rid:
            continue
        receipt = by_rule.get(rid)
        if receipt is None:
            # 未声明=违规（25 号文 §第七部分坑#2）
            issues.append(ValidationIssue(
                path="$.applied_rules",
                rule="MG_RR_undeclared",
                msg=(f"active+hard 规则 {rid!r} 未在 applied_rules 声明——未声明=违规"
                     "（25 号文 §第七部分坑#2）"),
                severity=SEVERITY_ERROR,
                hint=f"在 applied_rules[] 里声明 {rid}：followed / not_applicable（带 why）",
            ))
            continue
        status = str(receipt.get("status") or "").strip().lower()
        if status == "violated":
            issues.append(ValidationIssue(
                path="$.applied_rules",
                rule="MG_RR_violated",
                msg=f"规则 {rid!r} 回执声明 violated（hard 规则违反 → 机器门拒入）",
                severity=SEVERITY_ERROR,
                hint="修正用例使其 followed，或证明 not_applicable 并带 why",
            ))
        elif status in ("followed", "not_applicable"):
            if status == "not_applicable" and not str(receipt.get("why") or "").strip():
                issues.append(ValidationIssue(
                    path="$.applied_rules",
                    rule="MG_RR_na_no_why",
                    msg=f"规则 {rid!r} 标记 not_applicable 但缺 why（豁免需带理由）",
                    severity=SEVERITY_WARN,
                    hint="not_applicable 必须带 why 说明为何该规则对本用例不适用",
                ))
        else:
            issues.append(ValidationIssue(
                path="$.applied_rules",
                rule="MG_RR_bad_status",
                msg=(f"规则 {rid!r} 回执 status 非法：{status!r}（合法：followed/"
                     "not_applicable/violated）"),
                severity=SEVERITY_ERROR,
                hint="status ∈ {followed, not_applicable, violated}",
            ))
    return issues


# ── 6. api 端必覆盖场景（Skill15）──────────────────────────────
def check_api_mandatory_scenarios(case_payload: dict, surface: str) -> list[ValidationIssue]:
    """MG_MS：surface=api 的用例必须覆盖 Token 失效与越权两场景（Skill15 必覆盖）。

    25 号文 §环节5 步骤2 原文：「api 端 Token 失效与越权两场景必覆盖（Skill15）」。
    本检查是**单条用例级**的「自我声明」：payload 的 ``scenario_tag`` 字段标明本
    用例属于哪类必覆盖场景。**簇级覆盖**（一个 testpoint 下多条用例合起来是否两类
    都有）由调用方按 testpoint 分组后自行判定——本函数只判单条 payload 是否带
    ``scenario_tag`` 声明，缺 → warn（设计文档原文只要求「必覆盖」，未给单条 payload
    的强约束口径，按 warn 处理避免误伤正常正向用例）。

    设计歧义：25 号文未定义 ``scenario_tag`` 的取值枚举。本实现用宽松别名集合
    （见 ``_TOKEN_INVALID_TAGS`` / ``_CROSS_TENANT_TAGS``）判定，命中其一即视为已声明
    该类场景。``scenario_tag`` 缺失或不在别名集 → warn「缺 Token 失效/越权场景声明」。
    surface≠api 时直接放行（Skill15 只约束 api 端）。
    """
    issues: list[ValidationIssue] = []
    if (surface or "").strip() != "api":
        return issues
    tag = case_payload.get("scenario_tag")
    if isinstance(tag, (list, tuple)):
        tags = {str(t).strip() for t in tag if isinstance(t, str)}
    elif isinstance(tag, str) and tag.strip():
        tags = {tag.strip()}
    else:
        tags = set()

    # 命中判定：tag 在对应别名集里即视为已声明该类场景
    declared = set()
    for canon, _label, aliases in MANDATORY_API_SCENARIOS:
        if tags & aliases:
            declared.add(canon)

    for canon, label, _aliases in MANDATORY_API_SCENARIOS:
        if canon not in declared:
            issues.append(ValidationIssue(
                path="$.scenario_tag",
                rule=f"MG_MS_missing_{canon}",
                msg=(f"surface=api 的用例未声明「{label}」场景（Skill15 必覆盖，"
                     f"25 号文 §环节5 步骤2）——当前 scenario_tag={sorted(tags) or '空'}"),
                severity=SEVERITY_WARN,
                hint=(f"在 scenario_tag 标明本用例覆盖「{label}」"
                      f"（别名：{sorted(_aliases)[:6]}）"),
            ))
    return issues


# ── 7. 汇总 ────────────────────────────────────────────────────
async def run_machine_gates(
    proposal: dict,
    *,
    api_lookup: ApiLookup,
    existing_slot_keys: set[str],
    active_hard_rules: list[dict],
) -> dict:
    """汇总四查 + 两复用，返回 ``{valid, issues[], review_state}``。

    :param proposal: proposal payload（含 ``kind``/``surface``/``payload``/``evidence_class``
        或直接是 case payload——两种形态都兼容：若 ``proposal`` 有 ``payload`` 子 dict
        且 ``kind=case``，则取 ``payload`` 作为 case 本体；否则 proposal 本身即 case）。
    :param api_lookup: ``async (api_id) -> dict|None``（注入，不 import tortoise）。
    :param existing_slot_keys: 本 project 已存在的 slot_key 集合（调用方查库后传入）。
    :param active_hard_rules: active+hard 规则行列表（``{rule_id, ...}``）。
    :returns: ``{valid: bool, issues: [ValidationIssue, ...],
        review_state: "machine_pending" | "machine_rejected"}``。
        有 error → ``machine_rejected``；否则 ``machine_pending``（等双评/打分/人审）。
    """
    # 兼容两种 proposal 形态：{kind:"case", payload:{...}} 或直接 case dict
    if isinstance(proposal, Mapping) and isinstance(proposal.get("payload"), Mapping) \
            and str(proposal.get("kind") or "").lower() == "case":
        case_payload = proposal["payload"]
        surface = proposal.get("surface") or case_payload.get("surface") or ""
        evidence_class = proposal.get("evidence_class") or case_payload.get("evidence_class")
    else:
        case_payload = proposal
        surface = case_payload.get("surface") or ""
        evidence_class = case_payload.get("evidence_class")

    if not isinstance(case_payload, Mapping):
        return {
            "valid": False,
            "issues": [ValidationIssue(
                path="$", rule="MG_schema",
                msg="case payload 必须是对象",
                severity=SEVERITY_ERROR,
            )],
            "review_state": "machine_rejected",
        }

    case_dict = dict(case_payload)
    issues: list[ValidationIssue] = []

    # 1. 幻觉核验（async：调注入的 api_lookup）
    issues.extend(await check_hallucination(case_dict, api_lookup))
    # 2. 硬编码扫描
    issues.extend(check_hardcoded(case_dict))
    # 3. 证据核验（复用 quality.evidence）
    issues.extend(check_evidence(case_dict, evidence_class or None))
    # 4. 幂等查重
    issues.extend(check_idempotency(case_dict, existing_slot_keys))
    # 5. hard 规则回执
    issues.extend(check_rule_receipt(case_dict, active_hard_rules))
    # 6. api 端必覆盖场景
    issues.extend(check_api_mandatory_scenarios(case_dict, surface))

    has_error = any(i.severity == SEVERITY_ERROR for i in issues)
    return {
        "valid": not has_error,
        "issues": issues,
        "review_state": "machine_rejected" if has_error else "machine_pending",
    }


__all__ = [
    "FIVE_SCOPES",
    "SCOPE_ALIASES",
    "KNOWN_PLACEHOLDER_SCOPES",
    "MANDATORY_API_SCENARIOS",
    "ApiLookup",
    "make_slot_key",
    "check_hallucination",
    "check_hardcoded",
    "check_evidence",
    "check_idempotency",
    "check_rule_receipt",
    "check_api_mandatory_scenarios",
    "run_machine_gates",
]
