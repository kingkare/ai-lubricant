"""7 条内置工作流定义（design §17.3）——**常量数据，不调 LLM、不落库**。

design §6.2 的关键哲学：平台**不绑定任何具体 agent**。7 个角色只是
``input_schema → output_schema → tool_refs → contract`` 的**定义**；LLM 角色
由外部 agent 经能力面（design §17）跑，平台**只存定义 + 做契约校验**，
**绝不内嵌 LLM 客户端**（故本文件没有任何 ``llm_bridge`` / ``providers`` 依赖）。

executor 角色 ``nature='deterministic'``（不走 agent，引擎确定性执行），
其余 6 个 ``nature='llm'``。

表驱动同 design §17.3：

===========  ==================  ==========================  ============
角色          输入 → 输出           可调工具                    性质
===========  ==================  ==========================  ============
planner      目标+知识 → 测试计划    knowledge.query, craft.load  LLM
analyst      原始知识 → 模型+缺口    knowledge.ingest, question.raise LLM
designer     模型+缺口 → 设计+用例   case.create, question.raise LLM
coder        设计 → 场景           scenario.generate, tool.list, craft.load LLM
executor     场景 → 执行结果        run.execute                 确定性
debugger     失败证据 → 分类+根因+自愈 bug.triage, bug.root_cause LLM
reporter     全部产出 → 报告+沉淀    report.generate, craft.load LLM
===========  ==================  ==========================  ============
"""
from __future__ import annotations

import copy
from typing import Any

#: 7 角色（design §17.3），顺序即默认管道顺序
ROLES: tuple[str, ...] = (
    "planner",
    "analyst",
    "designer",
    "coder",
    "executor",
    "debugger",
    "reporter",
)

#: 各角色的 ``nature``（design §17.3「性质」列）：仅 executor 是确定性
NATURES: dict[str, str] = {role: "llm" for role in ROLES}
NATURES["executor"] = "deterministic"

#: ``confirm_source`` 合法取值（用例确认门，design §15.2；与 assets 对齐）
CONFIRM_SOURCES: tuple[str, ...] = ("debug", "doc", "user_confirm")


#: 7 条内置定义的原始常量（角色 → 该角色 ``workflow_def`` 字段子集）。
_BUILTIN: dict[str, dict[str, Any]] = {
    "planner": {
        "role": "planner",
        "nature": "llm",
        "tool_refs": ["knowledge.query", "craft.load"],
        "input_schema": {
            "type": "object",
            "required": ["goal", "knowledge"],
            "properties": {
                "goal": {"type": "string", "description": "测试目标"},
                "knowledge": {"type": "array", "description": "相关知识锚点/条目"},
            },
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["plan"],
            "properties": {
                "plan": {
                    "type": "object",
                    "required": ["summary", "objectives"],
                    "properties": {
                        "summary": {"type": "string", "description": "计划一句话概括"},
                        "objectives": {"type": "array", "items": {"type": "string"}},
                        "scope": {"type": "object"},
                        "risks": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "计划必须基于给定目标与知识，不含凭空臆造的功能点",
        },
    },
    "analyst": {
        "role": "analyst",
        "nature": "llm",
        "tool_refs": ["knowledge.ingest", "question.raise"],
        "input_schema": {
            "type": "object",
            "required": ["raw_knowledge"],
            "properties": {
                "raw_knowledge": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["knowledge_model", "gaps"],
            "properties": {
                "knowledge_model": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "结构化知识（实体/关系/约束）",
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "缺口（不确定处，可触发 question.raise）",
                },
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "只抽取原始知识中出现的事实；不确定处进 gaps 而非编造",
        },
    },
    "designer": {
        "role": "designer",
        "nature": "llm",
        "tool_refs": ["case.create", "question.raise"],
        "input_schema": {
            "type": "object",
            "required": ["knowledge_model", "gaps"],
            "properties": {
                "knowledge_model": {"type": "array", "items": {"type": "object"}},
                "gaps": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["design", "cases"],
            "properties": {
                "design": {"type": "object", "description": "测试设计结构"},
                "cases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "confirm_source"],
                        "properties": {
                            "title": {"type": "string"},
                            "intent": {"type": "string"},
                            "priority": {"type": "string"},
                            "confirm_source": {
                                "enum": list(CONFIRM_SOURCES),
                                "description": "用例确认来源（确认门，必填）",
                            },
                        },
                        "additionalProperties": True,
                    },
                },
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "每条用例必须有 confirm_source（debug/doc/user_confirm 之一），否则不进确认门",
        },
    },
    "coder": {
        "role": "coder",
        "nature": "llm",
        "tool_refs": ["scenario.generate", "tool.list", "craft.load"],
        "input_schema": {
            "type": "object",
            "required": ["design"],
            "properties": {"design": {"type": "object"}},
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["scenarios"],
            "properties": {
                "scenarios": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "steps"],
                        "properties": {
                            "name": {"type": "string"},
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["tool_ref"],
                                    "properties": {
                                        "step_key": {"type": "string"},
                                        "tool_ref": {"type": "string"},
                                    },
                                    "additionalProperties": True,
                                },
                            },
                        },
                        "additionalProperties": True,
                    },
                },
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "只用已注册工具（AI 只能用已注册工具：tool_ref 必须在注册表内，或 custom: 前缀）",
        },
    },
    "executor": {
        "role": "executor",
        "nature": "deterministic",
        "tool_refs": ["run.execute"],
        "input_schema": {
            "type": "object",
            "required": ["scenarios"],
            "properties": {"scenarios": {"type": "array", "items": {"type": "object"}}},
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["results"],
            "properties": {
                "results": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "结果由确定性引擎产生，不由 LLM 生成；证据原样回填",
        },
    },
    "debugger": {
        "role": "debugger",
        "nature": "llm",
        "tool_refs": ["bug.triage", "bug.root_cause"],
        "input_schema": {
            "type": "object",
            "required": ["evidence"],
            "properties": {"evidence": {"type": "array", "items": {"type": "object"}}},
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["classification", "root_cause"],
            "properties": {
                "classification": {
                    "type": "string",
                    "description": "失败分类（product|env|flaky|script|data ...）",
                },
                "root_cause": {"type": "string"},
                "healing_suggestion": {"type": "string", "description": "自愈建议"},
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "必须引用证据定位根因；根因门外只给建议不自行改库",
        },
    },
    "reporter": {
        "role": "reporter",
        "nature": "llm",
        "tool_refs": ["report.generate", "craft.load"],
        "input_schema": {
            "type": "object",
            "required": ["all_outputs"],
            "properties": {"all_outputs": {"type": "array", "items": {"type": "object"}}},
            "additionalProperties": True,
        },
        "output_schema": {
            "type": "object",
            "required": ["report"],
            "properties": {
                "report": {"type": "object", "description": "报告主体"},
                "craft": {"type": "array", "description": "沉淀资产（样例/约定）"},
            },
            "additionalProperties": True,
        },
        "contract": {
            "requirement": "报告只汇总各角色真实产出，附带沉淀；不得覆盖未确认事实",
        },
    },
}


def builtin_workflow_defs() -> list[dict[str, Any]]:
    """返回 7 条内置工作流定义（每条 ``workflow_def`` 一行的 dict）。

    每条含 ``role / input_schema / output_schema / tool_refs / contract /
    nature``（design §17.3 + §15.9 ``mc_test_workflow_def`` 列）。返回**深拷贝**
    ——调用方改动不影响模块常量。顺序固定为 :data:`ROLES`。
    """
    return [copy.deepcopy(_BUILTIN[role]) for role in ROLES]


def builtin_workflow_def(role: str) -> dict[str, Any] | None:
    """按角色取一条内置定义（深拷贝）；未知角色返回 ``None``。"""
    src = _BUILTIN.get(role)
    return copy.deepcopy(src) if src is not None else None


def builtin_tool_refs(role: str) -> list[str]:
    """某角色可调的工具方法名（design §17.3「可调工具」列）。"""
    return list(_BUILTIN.get(role, {}).get("tool_refs", []))


__all__ = [
    "ROLES",
    "NATURES",
    "CONFIRM_SOURCES",
    "builtin_workflow_defs",
    "builtin_workflow_def",
    "builtin_tool_refs",
]
