"""GSA 身份探针：实测哪个 X-Mme-Client-Info 能避开 Apple 的 -80045。

iPASide CHANGELOG (v1.2.x) 的诊断：自 2026-10-12 起，anisette 数据由 Apple 的
Android 库生成、而 X-Mme-Client-Info 报 Mac+Xcode 身份的请求，Apple 一律答
-80045；Windows 身份则被接受。本探针用**不存在的 Apple ID**打 gsa.apple.com
的 SRP init（iPASide 同款安全验证法，不涉真实账号），逐个身份试：

  -80045       = 该身份仍被拒（错）
  非 -80045    = 身份被接受，错误变成普通的「账号不存在/密码错」（对）

用法：PYTHONIOENCODING=utf-8 python tools/probe_gsa_identity.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from user_platform.apple_signing import gsa, remote_anisette  # noqa: E402

FAKE_EMAIL = "probe-nonexistent-8842@example.com"

AKD = "com.apple.AuthKit/1 (com.apple.akd/1.0)"
CANDIDATES: list[tuple[str, str, str]] = [
    # (标签, X-Mme-Client-Info, X-Apple-App-Info)
    ("现状 Mac+Xcode（基线，应 -80045）",
     f"<Mac15,7> <macOS;27.0;26A5378j> <{AKD}>", "com.apple.gs.xcode.auth"),
    ("Windows 10",
     f"<Windows> <Windows;10.0;19045> <{AKD}>", "com.apple.gs.xcode.auth"),
    ("Windows 11",
     f"<Windows> <Windows;11.0;22631> <{AKD}>", "com.apple.gs.xcode.auth"),
    ("PC/Windows（iCloud for Windows 风格）",
     f"<PC> <Windows;10.0.19045;> <{AKD}>", "com.apple.gs.xcode.auth"),
]


def main() -> int:
    server = os.environ.get("ANI_SERVER", "ani.sidestore.io")
    print(f"anisette 源: {server}")
    headers = remote_anisette.get_headers(server)
    print(f"anisette 头: X-Apple-I-MD len={len(headers.get('X-Apple-I-MD', ''))} "
          f"MD-M={headers.get('X-Apple-I-MD-M', '')[:20]}…")
    print()

    # 用真实模块默认身份（修复后的 Windows 串）探一下，确认端到端不 -80045。
    print(f"使用模块默认身份: {gsa._BASE_HEADERS['X-Mme-Client-Info']}")
    results: list[tuple[str, str]] = []
    for label, client_info, app_info in CANDIDATES:
        gsa._BASE_HEADERS["X-Mme-Client-Info"] = client_info
        gsa._BASE_HEADERS["X-Apple-App-Info"] = app_info
        import srp

        usr = srp.User(FAKE_EMAIL, b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        _, a_pub = usr.start_authentication()
        try:
            resp = gsa._gs_request(
                {"A2k": a_pub, "ps": ["s2k", "s2k_fo"], "u": FAKE_EMAIL, "o": "init"},
                headers,
            )
            status = resp.get("Status", resp)
            ec = status.get("ec")
            em = status.get("em", "")
            verdict = "✅ 身份被接受" if str(ec) != "-80045" else "❌ 仍 -80045"
            line = f"{verdict}  ec={ec}  em={em!r}"
        except Exception as exc:  # noqa: BLE001
            line = f"⚠️ 请求异常: {exc}"
        results.append((label, line))
        print(f"[{label}]\n  {line}\n")
        time.sleep(2)

    print("=== 汇总 ===")
    for label, line in results:
        print(f"  {label}: {line.split('  ')[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
