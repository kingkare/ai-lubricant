"""AI 测试平台 · 质量校验器（07 validators-and-gates）。

「AI 不裁判，框架裁判」：校验器是确定性代码，对入库的每个资产跑规则，
不过不过库（07 §1）。

对外导出：

- `ValidationIssue` / `ValidationResult` —— 结构化错误（07 §1 的 JSON 形态）
- `validate(scenario, tools, assertions, methods=None)` —— 纯函数校验入口（07 §2/§4）
- `ValidationRegistries` / `RegistryView` —— 注册表查询适配（07 §6 registry.py）
- `STEP_TYPES` / `EPHEMERAL_SCOPES` / `KNOWN_SCOPES` —— 规则用到的枚举真相
- 疑问 / 沉淀（design §15.2 / §15.6）—— `raise_question` / `resolve_question` /
  `assert_not_blocked` / `create_craft` / `load_craft` 等（``.models`` / ``.service``）

校验门（07 §5）落在「场景/步骤/方法入库」处：AI 生成后、编辑器保存后、导入后、
存量回扫都跑同一套 `validate()`。
"""

from .errors import (
    SEVERITY_ERROR,
    SEVERITY_WARN,
    ValidationIssue,
    ValidationResult,
)
from .evidence import (
    ADVERSARIAL_CLASSES,
    EVIDENCE_CLAIMED,
    EVIDENCE_CLASSES,
    EVIDENCE_EXECUTED,
    EVIDENCE_INFERRED,
    EVIDENCE_STATIC,
    GATING_CLASSES,
    enforce_three_part_reasoning,
    gates,
    normalize_evidence_class,
    validate_evidence_claim,
)
from .models import (
    CRAFT_SCOPES,
    CRAFT_STATUSES,
    CRAFT_TARGETS,
    CRAFT_TYPES,
    QUESTION_ORIGINS,
    QUESTION_RESOLUTIONS,
    QUESTION_STATUSES,
    CraftNotFoundError,
    CraftValidationError,
    QuestionBlockingError,
    QuestionNotFoundError,
    QuestionValidationError,
    TestCraftAsset,
    TestQuestion,
)
from .registry import RegistryView, ValidationRegistries
from .service import (
    assert_not_blocked,
    create_craft,
    get_question,
    list_craft,
    list_questions,
    load_craft,
    open_questions_for,
    raise_question,
    resolve_question,
    update_craft,
)
from .validators import (
    CUSTOM_TOOL_PREFIX,
    EPHEMERAL_SCOPES,
    KNOWN_SCOPES,
    STEP_TYPES,
    validate,
)

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARN",
    "ValidationIssue",
    "ValidationResult",
    "EVIDENCE_CLASSES",
    "EVIDENCE_EXECUTED",
    "EVIDENCE_STATIC",
    "EVIDENCE_INFERRED",
    "EVIDENCE_CLAIMED",
    "GATING_CLASSES",
    "ADVERSARIAL_CLASSES",
    "normalize_evidence_class",
    "gates",
    "validate_evidence_claim",
    "enforce_three_part_reasoning",
    "RegistryView",
    "ValidationRegistries",
    "STEP_TYPES",
    "EPHEMERAL_SCOPES",
    "KNOWN_SCOPES",
    "CUSTOM_TOOL_PREFIX",
    "CRAFT_SCOPES",
    "CRAFT_STATUSES",
    "CRAFT_TARGETS",
    "CRAFT_TYPES",
    "QUESTION_ORIGINS",
    "QUESTION_RESOLUTIONS",
    "QUESTION_STATUSES",
    "CraftNotFoundError",
    "CraftValidationError",
    "QuestionBlockingError",
    "QuestionNotFoundError",
    "QuestionValidationError",
    "TestCraftAsset",
    "TestQuestion",
    "assert_not_blocked",
    "create_craft",
    "get_question",
    "list_craft",
    "list_questions",
    "load_craft",
    "open_questions_for",
    "raise_question",
    "resolve_question",
    "update_craft",
    "validate",
]
