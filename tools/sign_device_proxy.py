"""Spike 专用：用平台已有 apple_id 签名配置（profile id=2）的 session，为
DeviceProxy（com.ailubricant.deviceproxy）物化新的签名材料并签 unsigned.ipa。

复用（零改动）：
  - user_platform.apple_signing.provision.ensure_signing_assets（AppID/设备/描述文件）
  - user_platform.apple_signing.signing.build_p12（p12 组装）
  - go-ios `ios sign app`（go-codesign，同节点 SignWithFiles 路径）

跑法：python tools/sign_device_proxy.py
输出：spike-work/device-proxy-signed.ipa（bundle id = team 后缀的 deviceproxy）
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "spike-work"
PROFILE_ID = 2  # 平台里那份 apple_id 签名配置
UDID = "00008120-001175642EE0A01E"
BASE_BUNDLE_ID = "com.ailubricant.deviceproxy"
IPA_IN = OUT_DIR / "unsigned.ipa"
GO_IOS = Path(r"C:\Users\Administrator\Downloads\go-ios-win\ios.exe")


async def load_secret() -> dict:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "server"))
    from db import PostgresClient

    await PostgresClient.init(lightweight=True)
    async with PostgresClient.pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT secret_data FROM ios_signing_profiles WHERE id=$1", PROFILE_ID
        )
    sd = r["secret_data"]
    if isinstance(sd, str):
        sd = json.loads(sd)
    if sd.get("session_expired"):
        raise SystemExit("签名配置 session 已过期：请先在控制台重新登录该 Apple ID")
    return sd


def _pg_dsn() -> str:
    """.env 的 PG 配置 → asyncpg DSN（项目里 PostgresClient 同款组装）。"""
    from urllib.parse import quote_plus

    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "postgres")
    pwd = os.environ.get("POSTGRES_PASSWORD", "")
    db = os.environ.get("POSTGRES_DATABASE", "agentcompose")
    return f"postgresql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:{port}/{db}"


def _auto_relogin(secret: dict, team_id: str, final_bundle: str):
    """password_enc 解密 → GSA 重登 → 新 session 写回 DB → 重跑物化。

    与平台 ``routes_ios._auto_relogin`` 同链路。需要 2FA（信任失效）时退出并提示
    走控制台重登——脚本吃不了交互验证码。
    """
    blob = str(secret.get("password_enc") or "")
    email = str(secret.get("email") or "")
    if not blob or not email:
        raise SystemExit("库里没存 password_enc，无法自动重登：请到控制台「签名配置→重新登录」一次")
    from user_platform.apple_signing import anisette, gsa, provision
    from user_platform.apple_signing.password_crypto import decrypt_apple_password

    plaintext = decrypt_apple_password(blob)
    anisette.set_remote_server("ani.sidestore.io")
    result = gsa.begin_login(email, plaintext)
    if result.get("status") != "authenticated":
        raise SystemExit(
            "Apple 要求重新输入 2FA 码（信任已失效），脚本无法自动完成："
            "请到控制台「签名配置 → 重新登录」走完 2FA 后重跑本脚本"
        )
    fresh = result["session"]
    session = {"adsid": fresh["adsid"], "auth_token": fresh["auth_token"]}
    print("   自动重登成功 adsid=%s…" % str(fresh.get("adsid"))[:12])

    # 新 session 写回 DB（平台后续物化/续签同样受益）。
    async def _persist() -> None:
        import asyncpg

        dsn = os.environ.get("APP_COMPOSE_POSTGRES_DSN") or _pg_dsn()
        conn = await asyncpg.connect(dsn)
        try:
            r = await conn.fetchrow(
                "SELECT secret_data FROM ios_signing_profiles WHERE id=$1", PROFILE_ID
            )
            sd = r["secret_data"]
            if isinstance(sd, str):
                sd = json.loads(sd)
            sd.update({
                "adsid": fresh.get("adsid"),
                "idms_token": fresh.get("GsIdmsToken"),
                "auth_token": fresh.get("auth_token"),
                "auth_token_expiry": fresh.get("auth_token_expiry"),
                "session_expired": False,
            })
            await conn.execute(
                "UPDATE ios_signing_profiles SET secret_data=$2::jsonb WHERE id=$1",
                PROFILE_ID, json.dumps(sd, ensure_ascii=False),
            )
        finally:
            await conn.close()
        print("   新 session 已写回 profile id=%s（DB）" % PROFILE_ID)

    asyncio.run(_persist())

    ensure_secret: dict = {"cert": secret.get("cert"), "team_id": team_id}
    provision.ensure_signing_assets(
        session,
        ensure_secret,
        udid=UDID,
        bundle_id=final_bundle,
        team_id=team_id,
        team_name=str(secret.get("team_name") or ""),
        app_name="DeviceProxy spike",
        device_name="spike iphone",
    )
    return session, ensure_secret


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    secret = asyncio.run(load_secret())
    session = {"adsid": secret["adsid"], "auth_token": secret["auth_token"]}
    team_id = str(secret.get("team_id") or "")

    from user_platform.apple_signing import developer, provision

    print("== 1. 为 DeviceProxy 物化 AppID + 描述文件（复用现有 session）==")
    ensure_secret: dict = {"cert": secret.get("cert"), "team_id": team_id}
    final_bundle = provision.team_scoped_bundle_id(BASE_BUNDLE_ID, team_id)
    try:
        provision.ensure_signing_assets(
            session,
            ensure_secret,
            udid=UDID,
            bundle_id=final_bundle,
            team_id=team_id,
            team_name=str(secret.get("team_name") or ""),
            app_name="DeviceProxy spike",
            device_name="spike iphone",
        )
    except Exception as exc:  # noqa: BLE001 — 会话死了（1100 等）→ 自动重登再试
        print(f"   现有 session 被拒（{exc}）")
        print("== 1.5 自动重登（password_enc 已落库，受信设备不弹 2FA 则无感）==")
        session, ensure_secret = _auto_relogin(secret, team_id, final_bundle)
    print("   证书 serial:", ensure_secret["cert"].get("serial"))
    print("   profile 到期:", ensure_secret["profile"]["expires_at"])
    print("   profile 覆盖设备:", ensure_secret["profile"]["udids"])

    print("== 2. 组装 p12 ==")
    material = provision.build_dispatch_material(ensure_secret)
    p12_path = OUT_DIR / "deviceproxy-identity.p12"
    prof_path = OUT_DIR / "deviceproxy-profile.mobileprovision"
    p12_path.write_bytes(base64.b64decode(material["p12_base64"]))
    prof_path.write_bytes(base64.b64decode(material["mobileprovision_base64"]))
    print("   p12:", p12_path, "| profile:", prof_path)
    print("   bundle id:", final_bundle)

    print("== 3. go-ios 签名 ==")
    out_ipa = OUT_DIR / "device-proxy-signed.ipa"
    cmd = [
        str(GO_IOS), "sign", "app",
        f"--path={IPA_IN}",
        f"--p12file={p12_path}",
        f"--profile={prof_path}",
        f"--p12password={material['p12_password']}",
        f"--bundleid={final_bundle}",
        f"--output={out_ipa}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.stdout.strip():
        print(proc.stdout.strip()[:800])
    if proc.stderr.strip():
        print("stderr:", proc.stderr.strip()[:800])
    if proc.returncode != 0 or not out_ipa.exists():
        raise SystemExit("go-ios 签名失败")
    print("== OK ==")
    print("签名产物:", out_ipa, out_ipa.stat().st_size, "字节")
    print("bundle id:", final_bundle)


if __name__ == "__main__":
    main()
