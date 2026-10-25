"""历史查重（25 号文 §环节5 步骤6 / §环节9 步骤①；P1「查重」）。

设计文档原文两处口径：

- §环节5 步骤6：「**查重 pg_trgm** → ``dup_of_case_id``/``score``/``verdict``」
- §环节9 步骤①：「建单前查重（**pg_trgm≥0.85 提示疑似同因**，并单留痕）」

本模块是**纯函数为主 + DB 查询注入**：相似度计算不碰 DB（候选用例与既有用例
列表由调用方查库后传入），因此单测不需要 PG、不需要 tortoise。

**三元组相似度是纯 Python 兜底**：生产环境走 Postgres ``pg_trgm``
（``similarity()`` / ``%`` 算子，GIN 索引），本模块的
:func:`trigram_similarity` 用同一思想（字符三元组集合的 Jaccard）做**可测
兜底**——SQLite 测试库没有 pg_trgm 扩展，且「阈值 0.85 判 merge」的判定逻辑
必须能在单测里钉死。两者口径对齐（共享三元组 / 并集三元组），换 PG 时只换
取相似度那一步，阈值与处置三档不变。

DB 查询注入约定（与 :mod:`gates` 同款）：调用方查好「既有用例」列表传进来，
本模块不 import tortoise、不 import ``generation`` 内其他模块。

歧义记录（按文档裁定）：

1. **``dup_verdict`` 字面量**：§2.3 只给了中文「并入/另立/维持」，未给枚举
   字面量（models.py 同款注释）。本模块取任务定版字面量
   ``merge``（合并/并入）/ ``keep_both``（另立并存标参考）/ ``None``（独立，
   不写列）。UI 层按需映射中文标签。
2. **阈值口径**：``merge`` 用文档唯一给定的数值 **0.85**（pg_trgm≥0.85）；
   ``keep_both`` 下界 0.6 任务给定（文档未给，取 0.6 作「疑似需人看」的
   经验带，0.6 以下视为独立）。两档都是闭区间判定，边界值归上档
   （0.85 → merge，0.6 → keep_both）。
3. **无命中时的 ``dup_score``**：写 ``0.0`` 而非 NULL（列可空，但 0.0 让
   「查过了且不像」与「没查」在 SQL 里可区分——配合 ``dup_of_case_id IS NULL``）。
4. **召回阈值与处置阈值的分工**：:func:`find_duplicates` 的 ``threshold`` 是命中
   （召回）阈值——调用方语义是「哪些既有用例算疑似同因」；:func:`verdict_for`
   是处置分档阈值（0.85/0.6，文档+任务给定）。:func:`run_dedup` 用 0.6 召回
   （让 keep_both 档可达）、再由 verdict 分档；直接调 :func:`find_duplicates`
   的调用方（只要 merge 命中）用缺省 0.85。两个默认值不冲突——一个是召回门、
   一个是处置门。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "DUP_THRESHOLD_MERGE",
    "DUP_THRESHOLD_KEEP_BOTH",
    "VERDICT_MERGE",
    "VERDICT_KEEP_BOTH",
    "normalize_text",
    "trigram_similarity",
    "find_duplicates",
    "verdict_for",
    "run_dedup",
]

# ── 阈值与处置三档（§环节5 步骤6 / §环节9 步骤①）───────────────────

#: 「合并」阈值（文档唯一给定值：pg_trgm≥0.85 → 疑似同因/同测，提示并入）
DUP_THRESHOLD_MERGE = 0.85

#: 「另立并存」下界（文档未给，任务定版 0.6；[0.6, 0.85) 带内标参考）
DUP_THRESHOLD_KEEP_BOTH = 0.6

#: 处置字面量（§2.3 中文「并入/另立/维持」的 slug 化，见模块歧义记录 1）
VERDICT_MERGE = "merge"           # 并入（合并）
VERDICT_KEEP_BOTH = "keep_both"   # 另立并存，标参考

#: 括号注片段（去括号注要在去标点之前做——否则括号被当标点抹掉，注文留在正文里）。
#: 逐对匹配（不跨对），容错中文/英文/方头/空心方括号；嵌套括号按最内层优先。
_BRACKET_PATTERNS: tuple[str, ...] = (
    r"（[^（）]*）",      # 中文全角圆括号
    r"\([^()]*\)",        # 英文圆括号
    r"\[[^\[\]]*\]",      # 英文方括号
    r"【[^【】]*】",      # 方头括号
    r"〔[^〔〕]*〕",      # 空心方括号
    r"\{[^{}]*\}",        # 花括号
)
_BRACKET_RE = re.compile("|".join(_BRACKET_PATTERNS))

#: 保留字符判定用的「看起来像注引/译注」的前缀标记（如 ``注：`` ``参见`` ``参见:``）。
#: 这些标记后面的整段视为注解，随括号注一起剥除（只在前缀处于句首时生效）。
_NOTE_PREFIX_RE = re.compile(
    r"^\s*(?:注|备注|说明|参见|参考|来源)\s*[:：]?\s*.*$"
)


def normalize_text(s: Any) -> str:
    """归一化文本：小写 + 去括号注 + 去标点空白（中英混排安全）。

    步骤（顺序要紧）：

    1. 非字符串输入转字符串（``None`` → 空串）；
    2. **先去括号注**（``（…）``/``(...)``/``[...]``/``【…】`` …）——若放到去
       标点之后，括号本身被当标点删掉、注文反而留在正文里，归一化就失了意义；
    3. 去句首注引标记段（``注：…``/``参见…``）；
    4. Unicode NFKC 兼容归一（全角→半角、兼容字符折叠）；
    5. 小写；
    6. **只留字母与数字**（``str.isalnum()`` 对中日韩字符同为真，故中文按字保留，
       不会被误删）——标点、空白、下划线、符号全部剥除。

    幂等：输出只含字母数字，再跑一遍结果不变（供相似度函数内部重复调用）。

    >>> normalize_text("验证登录（含 OAuth）返回 Token")
    '验证登录返回token'
    """
    if s is None:
        return ""
    text = s if isinstance(s, str) else str(s)
    text = _BRACKET_RE.sub("", text)
    text = _NOTE_PREFIX_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    return "".join(ch for ch in text if ch.isalnum())


def _ngrams(text: str) -> frozenset[str]:
    """字符三元组集合；长度不足 3 时退化为整串（2 字符）/ 单字符集合。"""
    n = len(text)
    if n >= 3:
        return frozenset(text[i:i + 3] for i in range(n - 2))
    if n == 0:
        return frozenset()
    return frozenset({text})


def trigram_similarity(a: Any, b: Any) -> float:
    """三元组相似度（Jaccard：共享三元组 / 并集三元组），纯 Python 兜底。

    - 两端**先各自归一化**（:func:`normalize_text`）再取三元组——「同文本」=
      归一后相同 → 1.0；「仅大小写/标点/空白/括号注不同」也判 1.0（查重要的就是
      这个：LLM 换种写法仍是同一条）；
    - 两者皆空 → 1.0（都无可比内容，视为同一空串）；
    - 一方空一方非空 → 0.0；
    - 短串（<3 字符）退化为整串/单字符集合，不让短标题恒得 0 分。

    生产环境换 PG ``pg_trgm`` 时，本函数是唯一被替换的点（阈值与处置不变）。
    """
    na, nb = normalize_text(a), normalize_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    ga, gb = _ngrams(na), _ngrams(nb)
    union = ga | gb
    if not union:
        return 1.0
    return len(ga & gb) / len(union)


# ── 字段别名（候选用例/既有用例的形状由调用方决定，这里宽容取值）─────

#: 标题字段别名（候选用例 proposal payload 与正式用例列名不同）
_TITLE_KEYS: tuple[str, ...] = ("title", "case_title", "name", "intent")
#: 标识字段别名（正式用例是 code/id；查重命中回写 dup_of_case_id 用）。
#: **不含** ``testpoint_id``：它标识「挂哪个测试点」而非用例本身——若候选与既有行
#: 恰好挂同一测试点，用它做自匹配排除会把「同一测试点下的两条不同用例」误当自匹配
#: 跳过，反而漏掉真重复。
_ID_KEYS: tuple[str, ...] = ("case_id", "id", "code")


def _field(obj: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            return v
    return None


def _title_of(obj: Mapping[str, Any]) -> str:
    value = _field(obj, _TITLE_KEYS)
    return "" if value is None else str(value)


def _id_of(obj: Mapping[str, Any]) -> Any:
    return _field(obj, _ID_KEYS)


def find_duplicates(
    candidate: Mapping[str, Any],
    existing: Sequence[Mapping[str, Any]],
    *,
    threshold: float = DUP_THRESHOLD_MERGE,
) -> list[dict[str, Any]]:
    """候选用例 vs 既有用例列表 → 命中清单（降序，``score >= threshold``）。

    :param candidate: 候选用例（``title``/``case_title``/``name``/``intent`` 取标题；
        带 ``id``/``case_id``/``code`` 时用于**排除自匹配**——拿自己的历史行查重
        不应把自己算成命中）。
    :param existing: 既有用例 dict 列表（调用方查库后传入；``id``/``case_id``/
        ``code`` + 标题）。
    :param threshold: 命中阈值，缺省 :data:`DUP_THRESHOLD_MERGE`（0.85）。
    :returns: ``[{case_id, title, score}, ...]`` 按 score **降序**（同分按标题
        稳定排序）；无命中返回 ``[]``。

    纯函数：不查库、不改输入。候选与自身 id 相同的既有行跳过（自匹配排除）。
    **空标题不参与查重**：候选归一后无内容 → 直接返回 ``[]``；某既有行归一后为空
    → 跳过该行。否则「空串 vs 空串」按 :func:`trigram_similarity` 的「都无可比
    内容」口径得 1.0，会造出一条假命中（没标题的用例互相全判重复）。
    """
    cand_title = _title_of(candidate)
    if not normalize_text(cand_title):
        return []
    cand_id = _id_of(candidate)
    cand_id_str = str(cand_id) if cand_id is not None else None

    hits: list[dict[str, Any]] = []
    for row in existing or ():
        if not isinstance(row, Mapping):
            continue
        row_id = _id_of(row)
        if cand_id_str is not None and row_id is not None and str(row_id) == cand_id_str:
            continue  # 自匹配排除
        row_title = _title_of(row)
        if not normalize_text(row_title):
            continue  # 空标题不参与查重（假命中防护）
        score = trigram_similarity(cand_title, row_title)
        if score >= threshold:
            hits.append({
                "case_id": str(row_id) if row_id is not None else None,
                "title": row_title,
                "score": score,
            })
    hits.sort(key=lambda h: (-h["score"], h["title"]))
    return hits


def verdict_for(dup_score: Optional[float]) -> Optional[str]:
    """相似度 → 处置三档。

    - ``>= 0.85`` → ``"merge"``（合并/并入：疑似同因同测，并单留痕）
    - ``[0.6, 0.85)`` → ``"keep_both"``（另立并存，标参考）
    - ``< 0.6``（含 ``None``）→ ``None``（独立，不写列）

    边界归上档（0.85 → merge、0.6 → keep_both），与 §环节9 步骤①「pg_trgm≥0.85」
    的闭区间口径一致。``None`` 输入（没查重）按独立处理——不假装有命中。
    """
    if dup_score is None:
        return None
    score = float(dup_score)
    if score >= DUP_THRESHOLD_MERGE:
        return VERDICT_MERGE
    if score >= DUP_THRESHOLD_KEEP_BOTH:
        return VERDICT_KEEP_BOTH
    return None


def run_dedup(
    proposals: Sequence[Mapping[str, Any]],
    existing_cases: Sequence[Mapping[str, Any]],
    *,
    threshold: float = DUP_THRESHOLD_KEEP_BOTH,
) -> dict[str, Any]:
    """批量查重：逐提案写 ``dup_of_case_id``/``dup_score``/``dup_verdict`` + 聚合。

    :param proposals: 候选提案列表（kind=case 的 proposal payload 或提案 dict）。
    :param existing_cases: 既有正式用例列表（调用方查库后传入）。
    :param threshold: **召回**阈值，缺省 :data:`DUP_THRESHOLD_KEEP_BOTH`（0.6）。

    召回阈值取 0.6 而非 0.85（歧义记录 4）：命中带是 ``[0.6, 0.85) → keep_both`` 与
    ``[0.85, 1] → merge`` 两段，若召回就卡 0.85，``keep_both`` 档永不可达、
    ``dup_both`` 恒为 0。故这里用 0.6 召回、由 :func:`verdict_for` 分档处置——
    与 §2.3「dup_of_case_id/dup_score/dup_verdict 查重命中+处置（并入/另立/维持）」
    的「另立并存标参考」语义一致：**另立也要记下参考用例 id**，故 keep_both 同样
    写 ``dup_of_case_id``。
    :returns: ``{"proposals": [...], "checked": n, "dup_merge": n, "dup_both": n,
        "clean": n}``——

        - ``proposals``：**新 dict 列表**（不改输入，照 :func:`point_gen.assign_batches`
          的「输入不被修改」惯例），每条多出 ``dup_of_case_id``/``dup_score``/
          ``dup_verdict`` 三键；无命中时 ``dup_of_case_id=None``、
          ``dup_score=0.0``、``dup_verdict=None``；
        - ``checked``：参与查重的提案数；
        - ``dup_merge``：判 merge 的条数；
        - ``dup_both``：判 keep_both 的条数；
        - ``clean``：判独立（无命中）的条数。

    聚合口径：``dup_merge + dup_both + clean == checked``（恒等，测试钉死）。
    """
    out: list[dict[str, Any]] = []
    n_merge = n_both = n_clean = 0
    for prop in proposals or ():
        row = dict(prop) if isinstance(prop, Mapping) else {}
        hits = find_duplicates(row, existing_cases, threshold=threshold)
        if hits:
            best = hits[0]
            row["dup_of_case_id"] = best["case_id"]
            row["dup_score"] = best["score"]
            row["dup_verdict"] = verdict_for(best["score"])
            if row["dup_verdict"] == VERDICT_MERGE:
                n_merge += 1
            elif row["dup_verdict"] == VERDICT_KEEP_BOTH:
                n_both += 1
            else:  # 理论不可达（hits 已过 threshold），防御性归 clean
                n_clean += 1
        else:
            row["dup_of_case_id"] = None
            row["dup_score"] = 0.0
            row["dup_verdict"] = None
            n_clean += 1
        out.append(row)
    return {
        "proposals": out,
        "checked": len(out),
        "dup_merge": n_merge,
        "dup_both": n_both,
        "clean": n_clean,
    }
