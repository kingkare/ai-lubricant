"""New API 渠道类型常量与协议映射表。

数据来源：github.com/Calcium-Ion/new-api 的 ``constant/channel.go``
（``ChannelType*`` 常量与 ``ChannelTypeNames``）。**只收录导入时有意义的类型**：
媒体生成类会标注出来让用户默认不勾选，签名鉴权类直接标记不可导入。

映射到 ai-lubricant 的口径：
- ``protocol`` 取 openai / anthropic / gemini（``_normalize_chat_protocols`` 认的值）。
- ``path`` 是该协议下的对话路径。gemini 路径运行时动态拼，按约定留空。
- ``risky_path=True`` 表示该厂商的 OpenAI 兼容地址内嵌了非 ``/v1`` 的版本段
  （智谱 ``/api/paas/v4``、阿里 ``/compatible-mode``、火山 ``/api/v3``、
  Cohere ``/compatibility/v1``…），把 ``/v1/chat/completions`` 接上去大概率不对，
  需要给用户一条 warning 让其核对。
"""
from __future__ import annotations

from typing import NamedTuple


class TypeMapping(NamedTuple):
    name: str
    protocol: str
    path: str
    risky_path: bool = False
    importable: bool = True
    # 媒体生成类（图像/视频/音乐）：不是对话渠道，预览里默认不勾选。
    media: bool = False
    reason: str = ""


_OPENAI_PATH = "/v1/chat/completions"
_ANTHROPIC_PATH = "/v1/messages"
_RESPONSES_PATH = "/v1/responses"
_GEMINI_PATH = ""  # gemini 路径运行时按 {model}:{method} 拼

CHANNEL_TYPES: dict[int, TypeMapping] = {
    0: TypeMapping("Unknown", "openai", _OPENAI_PATH, risky_path=True),
    1: TypeMapping("OpenAI", "openai", _OPENAI_PATH),
    2: TypeMapping("Midjourney", "openai", _OPENAI_PATH, media=True),
    3: TypeMapping("Azure", "openai", _OPENAI_PATH, risky_path=True),
    4: TypeMapping("Ollama", "openai", _OPENAI_PATH),
    8: TypeMapping("Custom", "openai", _OPENAI_PATH),
    14: TypeMapping("Anthropic", "anthropic", _ANTHROPIC_PATH),
    15: TypeMapping("Baidu", "openai", _OPENAI_PATH, risky_path=True),
    16: TypeMapping("Zhipu", "openai", _OPENAI_PATH, risky_path=True),
    17: TypeMapping("Ali", "openai", _OPENAI_PATH, risky_path=True),
    20: TypeMapping("OpenRouter", "openai", _OPENAI_PATH),
    23: TypeMapping("Tencent", "openai", _OPENAI_PATH, risky_path=True),
    24: TypeMapping("Gemini", "gemini", _GEMINI_PATH),
    25: TypeMapping("Moonshot", "openai", _OPENAI_PATH),
    26: TypeMapping("ZhipuV4", "openai", _OPENAI_PATH, risky_path=True),
    33: TypeMapping(
        "AWS", "", "", importable=False,
        reason="AWS Bedrock 使用 SigV4 签名鉴权，无法作为静态密钥的自定义渠道导入",
    ),
    34: TypeMapping("Cohere", "openai", _OPENAI_PATH, risky_path=True),
    35: TypeMapping("MiniMax", "openai", _OPENAI_PATH),
    36: TypeMapping("SunoAPI", "openai", _OPENAI_PATH, media=True),
    39: TypeMapping("Cloudflare", "openai", _OPENAI_PATH),
    40: TypeMapping("SiliconFlow", "openai", _OPENAI_PATH),
    41: TypeMapping(
        "VertexAI", "", "", importable=False,
        reason="Google VertexAI 使用 GCP ADC 签名鉴权，无法作为静态密钥的自定义渠道导入",
    ),
    42: TypeMapping("Mistral", "openai", _OPENAI_PATH),
    43: TypeMapping("DeepSeek", "openai", _OPENAI_PATH),
    45: TypeMapping("VolcEngine", "openai", _OPENAI_PATH, risky_path=True),
    46: TypeMapping("BaiduV2", "openai", _OPENAI_PATH, risky_path=True),
    48: TypeMapping("xAI", "openai", _OPENAI_PATH),
    50: TypeMapping("Kling", "openai", _OPENAI_PATH, media=True),
    51: TypeMapping("Jimeng", "openai", _OPENAI_PATH, media=True),
    52: TypeMapping("Vidu", "openai", _OPENAI_PATH, media=True),
    55: TypeMapping("Sora", "openai", _OPENAI_PATH, media=True),
    56: TypeMapping("Replicate", "openai", _OPENAI_PATH, media=True),
    57: TypeMapping("Codex", "responses", _RESPONSES_PATH, risky_path=True),
    60: TypeMapping("NewAPI", "openai", _OPENAI_PATH),
    62: TypeMapping("VLLM", "openai", _OPENAI_PATH),
    63: TypeMapping("SGLang", "openai", _OPENAI_PATH),
}

# 未知类型统一按 OpenAI 兼容处理，但要提醒用户核对协议路径。
UNKNOWN_FALLBACK = TypeMapping("Unknown", "openai", _OPENAI_PATH, risky_path=True)


def resolve_type(channel_type: object) -> TypeMapping:
    """把上游 ``type`` 解析成映射；未知类型回落 OpenAI 兼容。"""
    try:
        value = int(channel_type)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return UNKNOWN_FALLBACK
    return CHANNEL_TYPES.get(value, UNKNOWN_FALLBACK)


def is_known_type(channel_type: object) -> bool:
    try:
        return int(channel_type) in CHANNEL_TYPES  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
