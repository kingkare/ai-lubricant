"""token 预算（18 号文 §6.2 / D 的 context_limits 思路落成代码）。

**纯函数**——模型上下文表 + 估算 + 5% 预留 + 三态判定（OK / RECOMMENDED / SPLIT_REQUIRED）。
治「一万个需求一次性加载 → token 天文数字」（18 号文 §6.3）：批次大小由**本模块动态算**，
不由「需求数」定。加标题感知切分（§6.1），把整篇文档切成章，逐章喂。

估算优先用 ``tiktoken``（装了就用）；没装走**中英文感知的启发式**（CJK ≈ 1.5 字符/token，
ASCII ≈ 4 字符/token）——宁可略高估（保守），不低估（低估会 OOM 上游）。
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

#: 常见模型上下文窗口（token）。缺省查不到 → DEFAULT_CONTEXT_WINDOW。
#: 可用环境变量 ``TESTING_GEN_CONTEXT_WINDOW`` 覆盖（本机/网关按事实源配）。
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,
    "claude-opus-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-fable-5-1": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "gpt-4.1": 1_000_000,
    "gpt-4o": 128_000,
    "deepseek-chat": 64_000,
    "qwen-max": 32_000,
}

DEFAULT_CONTEXT_WINDOW = 128_000

#: 输出预留（token）——给模型产出留的空间，从窗口里扣。
DEFAULT_OUTPUT_RESERVE = 4_000

#: 安全预留比例（D 的 5%）+ 下限（不低于 4000）。
RESERVE_RATIO = 0.05
RESERVE_FLOOR = 4_000

#: 三态阈值：输入占「可用预算」的比例。
#: ≤OK_RATIO → OK；≤SPLIT_RATIO → RECOMMENDED（该缩）；>SPLIT_RATIO → SPLIT_REQUIRED（必须拆）。
OK_RATIO = 0.70
SPLIT_RATIO = 0.90

STATUS_OK = "OK"
STATUS_RECOMMENDED = "RECOMMENDED"
STATUS_SPLIT_REQUIRED = "SPLIT_REQUIRED"

#: 中日韩字符范围（估算用）
_CJK = re.compile(r"[　-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")


def context_window(model: str | None) -> int:
    """模型上下文窗口（token）。环境变量优先 → 表 → 缺省。"""
    override = os.getenv("TESTING_GEN_CONTEXT_WINDOW")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    if model:
        m = model.strip()
        if m in MODEL_CONTEXT_WINDOWS:
            return MODEL_CONTEXT_WINDOWS[m]
        # 前缀匹配（带日期后缀的模型 id）
        for key, val in MODEL_CONTEXT_WINDOWS.items():
            if m.startswith(key):
                return val
    return DEFAULT_CONTEXT_WINDOW


def estimate_tokens(text: str) -> int:
    """估算 token 数。tiktoken 可用则用它，否则 CJK 感知启发式（保守略高估）。"""
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001 — 没装 tiktoken / 编码不可用 → 启发式
        cjk = len(_CJK.findall(text))
        other = len(text) - cjk
        # CJK ≈ 1.5 字符/token（向上取整保守）；其余 ≈ 4 字符/token
        return cjk // 2 + 1 + (other + 3) // 4 if cjk else (other + 3) // 4


def available_budget(model: str | None = None, *, output_reserve: int = DEFAULT_OUTPUT_RESERVE) -> int:
    """可用输入预算 = 窗口 − 输出预留 − 安全预留（5%，下限 4000）。"""
    window = context_window(model)
    reserve = max(int(window * RESERVE_RATIO), RESERVE_FLOOR)
    return max(window - output_reserve - reserve, 0)


def check_budget(
    texts: Iterable[str] | str,
    *,
    model: str | None = None,
    output_reserve: int = DEFAULT_OUTPUT_RESERVE,
) -> dict[str, Any]:
    """三态判定：这批输入能不能一次喂。

    :returns: ``{status, used, budget, ratio, window}``——
        ``status`` ∈ OK / RECOMMENDED / SPLIT_REQUIRED。
    """
    if isinstance(texts, str):
        texts = [texts]
    used = sum(estimate_tokens(t) for t in texts)
    budget = available_budget(model, output_reserve=output_reserve)
    ratio = (used / budget) if budget else 1.0
    if ratio > SPLIT_RATIO:
        status = STATUS_SPLIT_REQUIRED
    elif ratio > OK_RATIO:
        status = STATUS_RECOMMENDED
    else:
        status = STATUS_OK
    return {
        "status": status,
        "used": used,
        "budget": budget,
        "ratio": round(ratio, 4),
        "window": context_window(model),
    }


# ── 标题感知切分（18 号文 §6.1：文档 → 章节）────────────────────
#: markdown 标题行（## / ### …）
_HEADING = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)


def split_by_heading(text: str, *, max_chars: int = 4000) -> list[dict[str, Any]]:
    """按 markdown 标题切章；无标题或超长章再按字符兜底切。

    :returns: ``[{heading, level, text, seq}]``——每项是一个可独立喂的 chunk。
    ``max_chars`` 是单 chunk 字符上限（兜底切，防一章几万字）。
    """
    if not text.strip():
        return []

    marks = list(_HEADING.finditer(text))
    raw_chunks: list[dict[str, Any]] = []
    if not marks:
        raw_chunks.append({"heading": "", "level": 0, "text": text})
    else:
        # 首个标题前的序言
        if marks[0].start() > 0:
            raw_chunks.append({"heading": "", "level": 0, "text": text[: marks[0].start()]})
        for i, m in enumerate(marks):
            start = m.start()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            heading_line = text[start:end].splitlines()[0] if text[start:end] else ""
            raw_chunks.append({
                "heading": heading_line.lstrip("# ").strip(),
                "level": len(m.group(1)),
                "text": text[start:end],
            })

    # 兜底：超长 chunk 再按字符切（保留 heading 归属）
    out: list[dict[str, Any]] = []
    for rc in raw_chunks:
        body = rc["text"]
        if len(body) <= max_chars:
            out.append(rc)
            continue
        for j in range(0, len(body), max_chars):
            out.append({
                "heading": rc["heading"],
                "level": rc["level"],
                "text": body[j: j + max_chars],
            })

    for seq, item in enumerate(out):
        item["seq"] = seq
    return out


__all__ = [
    "MODEL_CONTEXT_WINDOWS", "DEFAULT_CONTEXT_WINDOW", "DEFAULT_OUTPUT_RESERVE",
    "STATUS_OK", "STATUS_RECOMMENDED", "STATUS_SPLIT_REQUIRED",
    "context_window", "estimate_tokens", "available_budget", "check_budget",
    "split_by_heading",
]