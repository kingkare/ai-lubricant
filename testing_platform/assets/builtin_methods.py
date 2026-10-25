"""内置基础方法类：时间 / 随机 / 密钥 / 编码工具（§5.1 一脚本=一类）。

**全局内置一套**（``project_id=None``），所有项目共享——项目建好后方法库左侧即见这
四类，无需手工新建。每个类都是一段符合 :mod:`testing_platform.method_source` 严格
约定的源码（恰好一个顶层类 + 全 ``@staticmethod`` + Google 风格 docstring），
``ensure_builtin_method_classes`` 播种时经 ``create_method_module`` 自动 AST 解析并登记
``mc_test_method``（入参说明取自 ``Args:`` 段）。

播种口径（幂等、不覆盖）：**只在缺失时建**。用户对全局类的编辑不会被重启还原
（「改动全局生效」= 改一次全局类，所有项目可见）。
"""
from __future__ import annotations

__all__ = ["BUILTIN_METHOD_CLASSES", "ensure_builtin_method_classes"]


TIME_SOURCE = '''import datetime as _dt


class Time:
    """时间工具：生成 / 格式化 / 解析 / 转换 / 比较时间。"""

    @staticmethod
    def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """当前本地时间字符串。

        Args:
            fmt: 输出格式（默认 %Y-%m-%d %H:%M:%S）
        Returns:
            格式化后的时间字符串
        """
        return _dt.datetime.now().strftime(fmt)

    @staticmethod
    def timestamp() -> int:
        """当前 Unix 时间戳（秒）。

        Returns:
            当前时间戳（整数秒）
        """
        return int(_dt.datetime.now().timestamp())

    @staticmethod
    def format(ts: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """时间戳 → 格式化字符串。

        Args:
            ts: Unix 时间戳（秒）
            fmt: 输出格式
        Returns:
            格式化后的时间字符串
        """
        return _dt.datetime.fromtimestamp(int(ts)).strftime(fmt)

    @staticmethod
    def parse(text: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> int:
        """时间字符串 → Unix 时间戳（秒）。

        Args:
            text: 时间字符串
            fmt: 解析格式
        Returns:
            Unix 时间戳（秒）
        """
        return int(_dt.datetime.strptime(text, fmt).timestamp())

    @staticmethod
    def add(ts: int, seconds: int = 0, minutes: int = 0, hours: int = 0, days: int = 0) -> int:
        """时间戳加减（返回新时间戳）。

        Args:
            ts: 基准 Unix 时间戳（秒）
            seconds: 增加的秒数（可为负）
            minutes: 增加的分钟数
            hours: 增加的小时数
            days: 增加的天数
        Returns:
            新的 Unix 时间戳（秒）
        """
        base = _dt.datetime.fromtimestamp(int(ts))
        delta = _dt.timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return int((base + delta).timestamp())

    @staticmethod
    def diff(a: int, b: int, unit: str = "seconds") -> float:
        """两个时间戳的差值（a - b）。

        Args:
            a: 被减时间戳（秒）
            b: 减数时间戳（秒）
            unit: 单位（seconds/minutes/hours/days）
        Returns:
            差值（按 unit 换算，浮点）
        """
        div = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit)
        if div is None:
            raise ValueError(f"不支持的时间单位：{unit!r}（seconds/minutes/hours/days）")
        return (int(a) - int(b)) / div

    @staticmethod
    def to_timestamp(text: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> int:
        """时间字符串 → Unix 时间戳（``parse`` 的别名，语义更直白）。

        Args:
            text: 时间字符串
            fmt: 解析格式
        Returns:
            Unix 时间戳（秒）
        """
        return int(_dt.datetime.strptime(text, fmt).timestamp())

    @staticmethod
    def from_timestamp(ts: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """Unix 时间戳 → 格式化字符串（``format`` 的别名）。

        Args:
            ts: Unix 时间戳（秒）
            fmt: 输出格式
        Returns:
            格式化后的时间字符串
        """
        return _dt.datetime.fromtimestamp(int(ts)).strftime(fmt)
'''


RANDOM_SOURCE = '''import random as _random
import string as _string
import uuid as _uuid


class Random:
    """随机数据生成：字符串 / 数字 / 手机号 / 邮箱 / UUID / 取样。"""

    @staticmethod
    def string(length: int = 8) -> str:
        """随机字母数字字符串。

        Args:
            length: 长度
        Returns:
            随机字符串
        """
        alphabet = _string.ascii_letters + _string.digits
        return "".join(_random.choice(alphabet) for _ in range(int(length)))

    @staticmethod
    def integer(min_value: int = 0, max_value: int = 100) -> int:
        """随机整数（含端点）。

        Args:
            min_value: 下界（含）
            max_value: 上界（含）
        Returns:
            随机整数
        """
        return _random.randint(int(min_value), int(max_value))

    @staticmethod
    def boolean() -> bool:
        """随机布尔值。

        Returns:
            True 或 False
        """
        return _random.choice([True, False])

    @staticmethod
    def phone() -> str:
        """随机中国大陆手机号。

        Returns:
            11 位手机号字符串
        """
        return "1" + _random.choice("3456789") + "".join(
            _random.choice("0123456789") for _ in range(9)
        )

    @staticmethod
    def email() -> str:
        """随机邮箱地址。

        Returns:
            邮箱字符串
        """
        user = "".join(_random.choice(_string.ascii_lowercase) for _ in range(8))
        return f"{user}@example.com"

    @staticmethod
    def uuid() -> str:
        """随机 UUID4 字符串。

        Returns:
            UUID 字符串
        """
        return str(_uuid.uuid4())

    @staticmethod
    def choice(items: list):
        """从列表随机取一个元素。

        Args:
            items: 候选列表
        Returns:
            随机选中的元素
        """
        return _random.choice(list(items))

    @staticmethod
    def sample(items: list, k: int = 1) -> list:
        """从列表随机取 k 个不重复元素。

        Args:
            items: 候选列表
            k: 取样个数
        Returns:
            随机样本列表
        """
        return _random.sample(list(items), int(k))
'''


CRYPTO_SOURCE = '''import base64 as _base64
import hashlib as _hashlib
import hmac as _hmac
import os as _os


class Crypto:
    """密钥与加解密：摘要 / HMAC 签名 / Base64 / AES-GCM / 随机密钥。"""

    @staticmethod
    def md5(text: str) -> str:
        """MD5 摘要（十六进制）。

        Args:
            text: 待摘要文本
        Returns:
            32 位十六进制摘要
        """
        return _hashlib.md5(str(text).encode("utf-8")).hexdigest()

    @staticmethod
    def sha1(text: str) -> str:
        """SHA1 摘要（十六进制）。

        Args:
            text: 待摘要文本
        Returns:
            40 位十六进制摘要
        """
        return _hashlib.sha1(str(text).encode("utf-8")).hexdigest()

    @staticmethod
    def sha256(text: str) -> str:
        """SHA256 摘要（十六进制）。

        Args:
            text: 待摘要文本
        Returns:
            64 位十六进制摘要
        """
        return _hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    @staticmethod
    def hmac_sha256(text: str, key: str) -> str:
        """HMAC-SHA256 签名（十六进制）。

        Args:
            text: 待签名文本
            key: 密钥
        Returns:
            64 位十六进制签名
        """
        return _hmac.new(
            str(key).encode("utf-8"), str(text).encode("utf-8"), _hashlib.sha256
        ).hexdigest()

    @staticmethod
    def base64_encode(text: str) -> str:
        """Base64 编码。

        Args:
            text: 原文
        Returns:
            Base64 字符串
        """
        return _base64.b64encode(str(text).encode("utf-8")).decode("ascii")

    @staticmethod
    def base64_decode(text: str) -> str:
        """Base64 解码。

        Args:
            text: Base64 字符串
        Returns:
            解码后的原文
        """
        return _base64.b64decode(str(text).encode("ascii")).decode("utf-8")

    @staticmethod
    def aes_encrypt(text: str, key: str) -> str:
        """AES-GCM 加密（密钥经 SHA256 派生，输出 Base64(nonce+密文)）。

        Args:
            text: 明文
            key: 密钥（任意长度字符串）
        Returns:
            Base64 编码的密文
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw_key = _hashlib.sha256(str(key).encode("utf-8")).digest()
        nonce = _os.urandom(12)
        ct = AESGCM(raw_key).encrypt(nonce, str(text).encode("utf-8"), None)
        return _base64.b64encode(nonce + ct).decode("ascii")

    @staticmethod
    def aes_decrypt(text: str, key: str) -> str:
        """AES-GCM 解密（对应 aes_encrypt）。

        Args:
            text: Base64 编码的密文
            key: 密钥（与加密一致）
        Returns:
            解密后的明文
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw_key = _hashlib.sha256(str(key).encode("utf-8")).digest()
        blob = _base64.b64decode(str(text).encode("ascii"))
        nonce, ct = blob[:12], blob[12:]
        return AESGCM(raw_key).decrypt(nonce, ct, None).decode("utf-8")

    @staticmethod
    def random_key(length: int = 32) -> str:
        """随机密钥（十六进制）。

        Args:
            length: 字节数（输出长度为 2 倍）
        Returns:
            十六进制密钥字符串
        """
        return _os.urandom(int(length)).hex()
'''


CODEC_SOURCE = '''import json as _json
import re as _re
import urllib.parse as _urlparse


class Codec:
    """字符串与编码工具：URL 编解码 / JSON / 大小写 / 正则提取 / 拼接切分。"""

    @staticmethod
    def url_encode(text: str) -> str:
        """URL 编码（百分号转义）。

        Args:
            text: 原文
        Returns:
            编码后的字符串
        """
        return _urlparse.quote(str(text), safe="")

    @staticmethod
    def url_decode(text: str) -> str:
        """URL 解码。

        Args:
            text: 已编码字符串
        Returns:
            解码后的原文
        """
        return _urlparse.unquote(str(text))

    @staticmethod
    def to_json(obj) -> str:
        """对象 → JSON 字符串（中文不转义）。

        Args:
            obj: 任意可序列化对象
        Returns:
            JSON 字符串
        """
        return _json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def from_json(text: str):
        """JSON 字符串 → 对象。

        Args:
            text: JSON 字符串
        Returns:
            反序列化后的对象
        """
        return _json.loads(text)

    @staticmethod
    def upper(text: str) -> str:
        """转大写。

        Args:
            text: 原文
        Returns:
            大写字符串
        """
        return str(text).upper()

    @staticmethod
    def lower(text: str) -> str:
        """转小写。

        Args:
            text: 原文
        Returns:
            小写字符串
        """
        return str(text).lower()

    @staticmethod
    def strip(text: str) -> str:
        """去除首尾空白。

        Args:
            text: 原文
        Returns:
            去空白后的字符串
        """
        return str(text).strip()

    @staticmethod
    def regex_extract(text: str, pattern: str, group: int = 0) -> str:
        """正则提取首个匹配（未命中返回空串）。

        Args:
            text: 待匹配文本
            pattern: 正则表达式
            group: 捕获组序号（默认 0=整体）
        Returns:
            匹配到的字符串，未命中为空串
        """
        m = _re.search(pattern, str(text))
        return m.group(int(group)) if m else ""

    @staticmethod
    def join(items: list, sep: str = ",") -> str:
        """列表拼接为字符串。

        Args:
            items: 元素列表
            sep: 分隔符
        Returns:
            拼接后的字符串
        """
        return str(sep).join(str(x) for x in items)

    @staticmethod
    def split(text: str, sep: str = ",") -> list:
        """字符串切分为列表。

        Args:
            text: 原文
            sep: 分隔符
        Returns:
            切分后的列表
        """
        return str(text).split(str(sep))
'''


#: 内置基础类：``(类名, 源码)`` 有序列表。类名即 ``mc_test_method_module.code``。
BUILTIN_METHOD_CLASSES: list[tuple[str, str]] = [
    ("Time", TIME_SOURCE),
    ("Random", RANDOM_SOURCE),
    ("Crypto", CRYPTO_SOURCE),
    ("Codec", CODEC_SOURCE),
]


async def ensure_builtin_method_classes() -> int:
    """播种内置基础方法类（全局作用域 ``project_id=None``，幂等、不覆盖）。

    逐个检查「全局是否已有同类名」——已有则跳过（保留用户对全局类的编辑），
    缺失才 ``create_method_module``（自动 AST 解析并登记 ``mc_test_method``，
    ``status="active"`` 保证运行期 ``_make_method_loader`` 会加载）。

    返回本次**新建**的类数量（0 = 已齐备）。
    """
    from .models import TestMethodModule
    from .service import create_method_module

    created = 0
    for name, source in BUILTIN_METHOD_CLASSES:
        if await TestMethodModule.filter(project_id=None, code=name).exists():
            continue
        await create_method_module(project_id=None, source=source, status="active")
        created += 1
    return created
