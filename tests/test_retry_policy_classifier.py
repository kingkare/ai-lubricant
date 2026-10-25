"""retry_policy 纯函数分类器的离线单测（不依赖 PG/Redis）。"""

import retry_policy as rp
from retry_policy import FailureAction, FailureInput, classify_failure, classify_inner_retry


# ── parse_error_detail ──────────────────────────────────────────

def test_parse_error_detail_dict_nested_error():
    t, c, p, m = rp.parse_error_detail(
        {"error": {"type": "Invalid_Request_Error", "code": "X", "param": "metadata", "message": "Bad"}}
    )
    assert (t, c, p) == ("invalid_request_error", "x", "metadata")
    assert m == "bad"


def test_parse_error_detail_json_string():
    t, c, p, m = rp.parse_error_detail('{"error":{"type":"server_error","message":"boom"}}')
    assert t == "server_error"
    assert "boom" in m


def test_parse_error_detail_plain_string():
    assert rp.parse_error_detail("Some Plain Message") == ("", "", "", "some plain message")


# ── is_non_retryable_upstream_error ─────────────────────────────

def test_context_too_large_is_non_retryable_by_marker():
    # 纯函数：不可重试完全取决于注入的规则。默认规则（真实环境从 config 读）里含
    # context 相关 marker，这里显式注入以复现「context 超限不可重试」。
    detail = {"error": {"message": "exceeds the context window", "type": "invalid_request_error", "code": "context_too_large"}}
    rules = {"markers": ["context window", "context_too_large"]}
    assert rp.is_non_retryable_upstream_error(400, detail, rules) is True
    # 不注入任何规则时不命中 → 可重试（与旧默认行为的差异仅由配置决定）
    assert rp.is_non_retryable_upstream_error(400, detail, None) is False


def test_unsupported_param_retryable_unless_configured():
    detail = '{"error":{"message":"Unsupported parameter: metadata","type":"invalid_request_error","param":"metadata"}}'
    # 无规则配置时：虽是参数类错误，但不命中任何注入规则 → 可重试
    assert rp.is_non_retryable_upstream_error(400, detail, None) is False
    # 显式把该 param 列为不可重试 → 命中 → 不可重试
    rules = {"params": ["metadata"]}
    assert rp.is_non_retryable_upstream_error(400, detail, rules) is True


def test_server_error_wrapping_is_retryable():
    detail = {"error": {"message": "auth_unavailable: no auth", "type": "server_error", "code": "internal_server_error"}}
    assert rp.is_non_retryable_upstream_error(400, detail, None) is False


def test_plain_unsupported_model_is_retryable():
    assert rp.is_non_retryable_upstream_error(400, "The 'x/y' model is not supported.", None) is False


def test_configured_status_code_makes_non_retryable():
    detail = {"error": {"message": "invalid parameter foo", "type": "invalid_request_error"}}
    rules = {"status_codes": [422]}
    assert rp.is_non_retryable_upstream_error(422, detail, rules) is True


# ── canonicalize_upstream_error ─────────────────────────────────

def test_canonicalize_context_overflow_by_generic_message():
    detail = {
        "error": {
            "message": "This model's maximum context length is 202752 tokens. However, your messages resulted in 312644 tokens. Please reduce the length of the messages.",
            "type": "bad_response_status_code",
            "code": "bad_response_status_code",
        }
    }
    result = rp.canonicalize_upstream_error(400, detail)
    assert result is not None
    assert result.category == "context_length_exceeded"
    assert result.type == "invalid_request_error"
    assert result.code == "context_length_exceeded"
    assert result.status_code == 400
    assert "maximum context window" in result.message.lower()


def test_canonicalize_context_overflow_by_code():
    detail = {"error": {"message": "too big", "type": "invalid_request_error", "code": "context_too_large"}}
    result = rp.canonicalize_upstream_error(413, detail)
    assert result is not None
    assert result.code == "context_length_exceeded"
    assert "maximum context window" in result.message.lower()


def test_canonicalize_context_overflow_from_plain_string():
    result = rp.canonicalize_upstream_error(502, "Request too large for this model")
    assert result is not None
    assert result.category == "context_length_exceeded"
    assert result.status_code == 400


def test_plain_413_without_structured_detail_is_not_canonicalized():
    # 裸文本/空 413 可能来自 nginx 等网关请求体限制，不能误判为模型上下文超限。
    assert rp.canonicalize_upstream_error(413, "") is None


def test_canonicalize_input_token_limit_as_input_too_long():
    detail = {"error": {"message": "输入太长", "type": "invalid_request_error", "code": "input_token_limit_exceeded"}}
    result = rp.canonicalize_upstream_error(400, detail)
    assert result is not None
    assert result.category == "input_too_long"
    assert result.code == "input_too_long"
    assert "too long" in result.message.lower()


def test_canonicalize_chinese_input_message_when_status_downgraded():
    result = rp.canonicalize_upstream_error(502, "输入太长（估算 327107，上限 256000），请删减后重试。")
    assert result is not None
    assert result.category == "input_too_long"
    assert result.code == "input_too_long"


def test_canonicalize_max_tokens_exceeded():
    result = rp.canonicalize_upstream_error(
        400,
        {"error": {"message": "The requested max_tokens exceeds the model's context window.", "code": "invalid_request_error"}},
    )
    assert result is not None
    assert result.category == "max_tokens_exceeded"
    assert result.code == "max_tokens_exceeded"
    assert "context window" in result.message.lower()


def test_canonicalize_prompt_too_long():
    result = rp.canonicalize_upstream_error(400, "prompt is too long")
    assert result is not None
    assert result.category == "prompt_too_long"
    assert result.code == "prompt_too_long"
    assert result.message == "Prompt is too long"


def test_canonicalize_model_context_limit():
    result = rp.canonicalize_upstream_error(400, "The model context window was exceeded")
    assert result is not None
    assert result.category == "model_context_limit"
    assert result.code == "model_context_limit"
    assert result.message == "Model context window exceeded"


def test_canonicalize_specific_category_wins_over_413_fallback():
    result = rp.canonicalize_upstream_error(
        413,
        {"error": {"message": "prompt is too long", "code": "prompt_too_long"}},
    )
    assert result is not None
    assert result.category == "prompt_too_long"
    assert result.code == "prompt_too_long"
    assert result.status_code == 400


# ── 超限 message 必须携带客户端压缩触发锚点 ─────────────────────────────────
# Claude Code 对 error message 做大小写不敏感的子串匹配，认 "prompt is too long" /
# "input is too long for requested model" / "context window" 才自动压缩后重发；
# OpenAI 系客户端靠 "context window"/"maximum context length"。归一化把 message 换成
# 固定文案，一旦丢锚点 = code 归一了、客户端却只把它当普通错误显示，不压缩——
# 于是卡在原地。这张表是那个不变量的守卫。
_CLIENT_OVERFLOW_ANCHORS = (
    "prompt is too long",
    "input is too long for requested model",
    "context window",
    "input length and `max_tokens` exceed context limit",
)


def test_every_overflow_message_carries_client_trigger_anchor():
    for rule in rp._ERROR_CANONICAL_RULES:
        canonical = rule.result()
        if not canonical.is_overflow:
            continue
        message = canonical.message.lower()
        assert any(anchor in message for anchor in _CLIENT_OVERFLOW_ANCHORS), (
            f"{canonical.code} 的固定文案缺客户端压缩触发锚点: {canonical.message!r}"
        )


def test_overflow_categories_exclude_thinking_budget_invalid():
    # thinking_budget 参数冲突走归一（400 + 不可重试），但与超限无关：不能被
    # canonical_overflow_error 当超限上报给客户端压缩，也不该归入超限类目。
    assert rp.canonicalize_upstream_error(400, "thinking.budget_tokens must be less than max_tokens") is not None
    assert rp.canonical_overflow_error(400, "thinking.budget_tokens must be less than max_tokens") is None
    assert rp.canonical_overflow_error(400, "prompt is too long") is not None


def test_overflow_code_of_accepts_all_client_facing_overflow_codes():
    for code in (
        "context_too_large", "context_length_exceeded", "prompt_too_long",
        "input_too_long", "model_context_limit", "max_tokens_exceeded",
        "CONTEXT_LENGTH_EXCEEDED",
    ):
        assert rp.overflow_code_of(code) is not None, code
    for code in ("retry_exhausted", "rate_limit_exceeded", "no_available_account", "", None, "invalid_request_error"):
        assert rp.overflow_code_of(code) is None, code


# ── 上下文窗口大小的抽取与呈现 ─────────────────────────────────────────────

def test_extract_context_window_from_common_dialects():
    assert rp.extract_context_window("This model's maximum context length is 202752 tokens.") == 202752
    assert rp.extract_context_window("maximum context length of 200000 tokens") == 200000
    assert rp.extract_context_window("input exceeds the context window of 128000 tokens") == 128000
    assert rp.extract_context_window("请求超过模型的最大上下文长度 200000 tokens") == 200000


def test_extract_context_window_from_structured_detail():
    detail = {"error": {"message": "context length of 131072 tokens exceeded", "code": 400}}
    assert rp.extract_context_window(detail) == 131072


def test_extract_context_window_ignores_small_numbers_and_absent():
    # HTTP 状态码 500 / 重试秒数 30 这类小数字不能被当成窗口。
    assert rp.extract_context_window("internal error, status 500, retry in 30 seconds") is None
    assert rp.extract_context_window("prompt is too long") is None
    assert rp.extract_context_window("") is None
    assert rp.extract_context_window(None) is None


def test_with_context_window_appends_and_preserves_anchor():
    message = "Your input exceeds this model's maximum context window. Please reduce the length of the conversation."
    out = rp.with_context_window(message, 200000)
    # 锚点必须原样保留（客户端靠它触发压缩），窗口大小只能追加。
    assert "maximum context window" in out.lower()
    assert "200000 tokens" in out
    assert out.startswith(message)


def test_with_context_window_noop_when_unknown():
    message = "Prompt is too long"
    assert rp.with_context_window(message, None) == message
    assert rp.with_context_window(message, 0) == message


def test_canonical_error_display_message_includes_window():
    canonical = rp.canonicalize_upstream_error(
        502, "This model's maximum context length is 202752 tokens. Please reduce the length."
    )
    assert canonical is not None
    # 原始 message 固定不变（不掺窗口），display_message 才带窗口。
    assert "202752" not in canonical.message
    assert canonical.window == 202752
    assert "202752 tokens" in canonical.display_message
    assert "context window" in canonical.display_message.lower()


def test_canonical_error_display_message_without_window():
    canonical = rp.canonicalize_upstream_error(400, "prompt is too long")
    assert canonical is not None
    assert canonical.window is None
    assert canonical.display_message == canonical.message


# ── 中继方言/降级回归：这些文案不归一就会一路落进 429 retry_exhausted ────────

def test_relay_downgraded_overflow_message_still_classified():
    # 中继把 400 降级成 502、丢 code，只剩文案。
    assert rp.canonicalize_upstream_error(502, "The request exceeds the model's context window") is not None
    assert rp.canonicalize_upstream_error(502, "对话过长，请压缩历史后重试") is not None
    assert rp.canonicalize_upstream_error(502, "上下文超限：输入 token 过多") is not None


def test_chinese_skip_reason_label_not_misclassified_as_overflow():
    # 网关自己的选路 skip_reasons 中文标签（NoAvailableAccountError 的 detail）绝不能
    # 被当成上游超限：混合原因里窗口只是少数、主因是冷却时必须照常 429 重试。
    assert rp.canonical_overflow_error(429, "模型 'm1' 所有账号不可用：账号处于冷却期 9；预估输入超过模型上下文窗口 1") is None
    assert rp.canonicalize_upstream_error(429, "预估输入超过模型上下文窗口") is None


def test_unrelated_server_error_text_not_misclassified():
    # 真 5xx 必须留在可重试通道，不能因为常见词被误判成超限。
    assert rp.canonical_overflow_error(503, "Service Temporarily Unavailable") is None
    assert rp.canonical_overflow_error(500, "context_window_index rebuilding failed") is None
    assert rp.canonical_overflow_error(502, "upstream timeout while reading response") is None


def test_canonicalize_thinking_budget_ignores_server_error_code():
    # 上游把参数冲突标成 server_error；判定只看 message，不看 code。
    result = rp.canonicalize_upstream_error(
        400,
        {"error": {"code": "server_error", "message": "thinking.budget_tokens must be less than max_tokens"}},
    )
    assert result is not None
    assert result.category == "thinking_budget_invalid"
    assert result.type == "invalid_request_error"
    assert result.code == "invalid_request_error"
    assert result.status_code == 400
    assert result.message == "thinking.budget_tokens must be less than max_tokens"


def test_canonicalize_thinking_budget_from_json_string():
    result = rp.canonicalize_upstream_error(
        500,
        '{"error":{"code":"server_error","message":"thinking.budget_tokens must be less than max_tokens"}}',
    )
    assert result is not None
    assert result.category == "thinking_budget_invalid"


def test_generic_server_error_still_not_canonicalized():
    # 同样的 server_error code，没有 budget_tokens 文案 → 不归一，仍走普通重试。
    assert rp.canonicalize_upstream_error(500, {"error": {"code": "server_error", "message": "boom"}}) is None


def test_canonicalize_returns_none_for_unrelated_errors():
    assert rp.canonicalize_upstream_error(401, "invalid api key") is None
    assert rp.canonicalize_upstream_error(429, {"error": {"message": "rate limited", "code": "rate_limit"}}) is None
    assert rp.canonicalize_upstream_error(400, {"error": {"message": "bad metadata", "param": "metadata"}}) is None
    assert rp.canonicalize_upstream_error(500, "internal server error") is None



# ── classify_failure ────────────────────────────────────────────

def _mk(**kw) -> FailureInput:
    base = dict(
        is_http_exception=True,
        status_code=500,
        detail="boom",
        downstream_started=False,
        stream=False,
        cancelled=False,
        candidates_remaining=True,
        upstream_started=True,
        non_retryable_rules=None,
        extra_retry_status_codes=set(),
    )
    base.update(kw)
    return FailureInput(**base)


def test_inner_retry_default_status_and_network_eligibility():
    assert classify_inner_retry(_mk(status_code=429)) is True
    assert classify_inner_retry(_mk(status_code=502)) is True
    assert classify_inner_retry(_mk(status_code=None, is_http_exception=False, transient_exception=True)) is True


def test_inner_retry_extra_status_and_exclusions():
    assert classify_inner_retry(_mk(status_code=408, extra_retry_status_codes={408})) is True
    assert classify_inner_retry(_mk(status_code=404, extra_retry_status_codes=set())) is False
    assert classify_inner_retry(_mk(status_code=502, downstream_started=True)) is False
    assert classify_inner_retry(_mk(status_code=400, non_retryable_override=True)) is False
    assert classify_inner_retry(_mk(
        status_code=None,
        is_http_exception=False,
        retryable_incomplete_response=True,
    )) is False



    d = classify_failure(_mk(cancelled=True, downstream_started=True))
    assert d.action == FailureAction.CANCEL_AND_STOP


def test_downstream_started_stops_with_sse():
    d = classify_failure(_mk(downstream_started=True, status_code=500))
    assert d.action == FailureAction.STREAM_ERROR_AND_STOP
    assert d.rollback_reservation is False


def test_non_retryable_client_error_passthrough():
    detail = {"error": {"type": "invalid_request_error", "param": "metadata", "message": "bad"}}
    # 注入规则命中该 param → 判为不可重试客户端错误 → 原样透传
    d = classify_failure(_mk(status_code=400, detail=detail, non_retryable_rules={"params": ["metadata"]}))
    assert d.action == FailureAction.RETURN_CLIENT_ERROR
    assert d.client_status == 400
    assert d.client_detail == detail


def test_business_parameter_error_without_rules_retries_another_candidate():
    # 参数形状本身不等于已确认的客户端错误；未命中规则时按当前候选失败处理。
    detail = {"error": {"type": "invalid_request_error", "param": "metadata", "message": "bad"}}
    d = classify_failure(_mk(status_code=400, detail=detail))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_unknown_client_4xx_retries_another_candidate():
    d = classify_failure(_mk(status_code=404, detail="provider rejected request"))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_rate_limit_429_retries_when_candidates_remain():
    # 429 在默认重试集合内，切候选重试，耗尽后统一 429。
    d = classify_failure(_mk(status_code=429, candidates_remaining=True))
    assert d.action == FailureAction.RETRY


def test_upstream_5xx_retries_when_candidates_remain():
    d = classify_failure(_mk(status_code=500, candidates_remaining=True))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_non_retryable_override_false_allows_outer_candidate_retry():
    # override=False 明确表示不是客户端请求错误；即使 400 未配置为内层重试码，外层仍换候选。
    detail = {"error": {"type": "invalid_request_error", "param": "metadata", "message": "bad"}}
    d = classify_failure(_mk(
        status_code=400,
        detail=detail,
        non_retryable_override=False,
        non_retryable_rules={"params": ["metadata"]},
    ))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_non_retryable_override_true_still_passthrough():
    # 显式判定不可重试（如上下文超限归一）仍原样透传，不重试。
    detail = {"error": {"message": "too long", "type": "invalid_request_error"}}
    d = classify_failure(_mk(status_code=400, detail=detail, non_retryable_override=True))
    assert d.action == FailureAction.RETURN_CLIENT_ERROR


def test_5xx_retries_regardless_of_extra_codes():
    # 529 是 5xx，默认就重试；额外状态码集合不影响 5xx 的判定。
    d = classify_failure(_mk(status_code=529, extra_retry_status_codes={529}, candidates_remaining=True))
    assert d.action == FailureAction.RETRY


def test_configured_extra_4xx_retries():
    # 渠道配置的额外重试状态码让普通 4xx 也切候选重试。
    d = classify_failure(_mk(status_code=408, extra_retry_status_codes={408}, candidates_remaining=True))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_unconfigured_extra_4xx_still_retries_outer_candidate():
    # extra 状态码只控制同账号内层重试；未配置的 403 仍由外层换候选。
    d = classify_failure(_mk(status_code=403, extra_retry_status_codes=set(), candidates_remaining=True))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_unknown_4xx_exhausted_candidates_final_429():
    d = classify_failure(_mk(status_code=404, candidates_remaining=False))
    assert d.action == FailureAction.NO_RESOURCE_429


def test_upstream_self_made_426_never_passthrough():
    # 上游/网关自造码（如 426 Upgrade Required）即使被显式判成不可重试，
    # 也不得原样透传给客户端：落回重试/429 收口。
    detail = {"error": {"message": "", "type": "upstream_error", "code": "upstream_error"}}
    d = classify_failure(_mk(status_code=426, detail=detail, candidates_remaining=True))
    assert d.action == FailureAction.RETRY
    d = classify_failure(_mk(status_code=426, detail=detail, candidates_remaining=False))
    assert d.action == FailureAction.NO_RESOURCE_429
    d = classify_failure(_mk(
        status_code=426, detail=detail, non_retryable_override=True,
    ))
    assert d.action != FailureAction.RETURN_CLIENT_ERROR


def test_exhausted_candidates_final_429():
    d = classify_failure(_mk(status_code=500, candidates_remaining=False))
    assert d.action == FailureAction.NO_RESOURCE_429


def test_incomplete_stream_retries_before_any_downstream_output():
    # 空流/零 completion 属于候选渠道响应质量问题：未向客户端输出前应切候选重试，
    # 不能直接 429，否则上游"没吐内容"这种可恢复故障会变成客户端硬失败。
    d = classify_failure(_mk(
        is_http_exception=False,
        status_code=None,
        detail="upstream response usage reports zero completion tokens",
        stream=True,
        transient_exception=False,
        retryable_incomplete_response=True,
    ))
    assert d.action == FailureAction.RETRY
    assert d.exclusion_scope == "candidate"


def test_incomplete_stream_after_downstream_started_still_stops():
    # 已经向客户端发过字节：不能再切渠道（会拼接两个渠道的输出损坏响应），只能收尾。
    d = classify_failure(_mk(
        is_http_exception=False,
        status_code=None,
        detail="upstream response usage reports zero completion tokens",
        stream=True,
        downstream_started=True,
        retryable_incomplete_response=True,
    ))
    assert d.action == FailureAction.STREAM_ERROR_AND_STOP


def test_incomplete_stream_exhausted_candidates_final_429():
    d = classify_failure(_mk(
        is_http_exception=False,
        status_code=None,
        detail="upstream response usage reports zero completion tokens",
        stream=True,
        candidates_remaining=False,
        retryable_incomplete_response=True,
    ))
    assert d.action == FailureAction.NO_RESOURCE_429


def test_non_incomplete_non_http_exception_still_does_not_retry():
    # 普通业务/编程异常仍不重试，避免把非瞬态 bug 放大成多次上游请求。
    d = classify_failure(_mk(
        is_http_exception=False,
        status_code=None,
        detail="some internal bug",
        transient_exception=False,
        retryable_incomplete_response=False,
    ))
    assert d.action == FailureAction.NO_RESOURCE_429


def test_non_retryable_beats_retry_but_not_downstream():
    # 已输出优先于「不可重试透传」
    detail = {"error": {"type": "invalid_request_error", "param": "x", "message": "bad"}}
    d = classify_failure(_mk(status_code=400, detail=detail, downstream_started=True))
    assert d.action == FailureAction.STREAM_ERROR_AND_STOP
