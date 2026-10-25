#!/usr/bin/env python3
"""把服务端升级包上传到 GitHub Release（publish_github.sh 第 4 步调用）。

为什么独立成脚本而不是塞进 shell：建 Release 与上传资产要走 GitHub REST API
（多段 JSON + 二进制流 + 重定向），用 shell+curl 拼容易出错；项目里已有
``user_platform/marketplace/github.py`` 的同款实现（create_release /
upload_release_asset），这里复用同一套 API 语义。

token 复用 MARKETPLACE_GITHUB_TOKEN（.env 已配）；与节点/移动端/设备控制三条
线的发行仓上传同源。未配 token 时报错退出（调用方按「警告不中止」处理）。

用法：
    python3 script/publish_server_bundle.py \
        --bundle /tmp/ai-lubricant-v260912.tar.gz \
        --tag v260912 --owner wuxin-gh --repo ai-lubricant \
        --notes "服务端升级包 v260912"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 仓库根（script/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "server"))


def _token() -> str:
    """发行 token：优先服务端专用，回落市场 token（与 release_repos.py 同口径）。"""
    for key in ("SERVER_RELEASE_GITHUB_TOKEN", "MARKETPLACE_GITHUB_TOKEN"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    # 兜底：从 .env 读（publish 脚本在开发机跑，环境变量不一定导出）
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        wanted = {"SERVER_RELEASE_GITHUB_TOKEN", "MARKETPLACE_GITHUB_TOKEN"}
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in wanted and value.strip():
                # 记住优先级：先命中 SERVER_ 就直接用
                if key == "SERVER_RELEASE_GITHUB_TOKEN":
                    return value.strip()
                if key == "MARKETPLACE_GITHUB_TOKEN" and not os.environ.get(
                    "SERVER_RELEASE_GITHUB_TOKEN"
                ):
                    return value.strip()
    return ""


async def _upload(args: argparse.Namespace) -> int:
    from user_platform.marketplace import github as mp_github

    token = _token()
    if not token:
        print(
            "✗ 未配置 MARKETPLACE_GITHUB_TOKEN（或 SERVER_RELEASE_GITHUB_TOKEN），"
            "无法上传 Release",
            file=sys.stderr,
        )
        return 1

    bundle = Path(args.bundle)
    if not bundle.is_file():
        print(f"✗ 包不存在：{bundle}", file=sys.stderr)
        return 1
    content = bundle.read_bytes()
    print(f"  · 上传 {bundle.name}（{len(content) / 1024 / 1024:.1f} MB）→ "
          f"{args.owner}/{args.repo}@{args.tag}")

    # 已存在同名 Release 则复用（重发同一天版本时 tag 被强推，Release 也复用）
    release = await mp_github.get_release_by_tag(args.owner, args.repo, token, args.tag)
    if release is None:
        release = await mp_github.create_release(
            owner=args.owner,
            repo=args.repo,
            token=token,
            tag=args.tag,
            name=f"服务端升级包 {args.tag}",
            body=args.notes or "",
            prerelease=False,
        )
    if not release:
        print("✗ 创建 Release 失败", file=sys.stderr)
        return 1

    upload_url = str(release.get("upload_url") or "")
    if not upload_url:
        print("✗ Release 未返回 upload_url", file=sys.stderr)
        return 1

    # 同名资产的重传由 upload_release_asset 内部处理（撞 422 时自动删旧重传），
    # 这里不重复删除——少一个失败点。
    asset = await mp_github.upload_release_asset(
        upload_url, token, bundle.name, content, "application/gzip"
    )
    if not asset:
        print("✗ 资产上传失败", file=sys.stderr)
        return 1

    url = str(asset.get("browser_download_url") or "")
    print(f"  ✓ 已上传：{url}")

    # 写 version.json 到市场仓（消费侧轮询的数据源，与 node/mobile/device-control
    # 三条线同构）。失败不致命：包已在 Release 上，version.json 可事后补写。
    if url:
        ok = await _write_version_json(args, url, len(content))
        if not ok:
            print("⚠ version.json 写入失败（包已上传，可事后补写）", file=sys.stderr)

    if url and args.print_url:
        print(url)
    return 0


async def _write_version_json(args: argparse.Namespace, download_url: str, size: int) -> bool:
    """把版本信息写进市场仓的 ``server-releases/version.json``。

    与另三条线同构：消费侧（升级卡片）定时拉这个文件比对版本。写入用市场仓的
    ``MarketplaceGitHub.write_json``（带 sha 的 contents API，自动处理已存在）。
    """
    try:
        from user_platform.marketplace import config as mp_config
        from user_platform.marketplace.github import MarketplaceGitHub

        settings = mp_config.settings
        if not settings.writable:
            print("  · 市场仓未配 token，跳过 version.json", file=sys.stderr)
            return False
        payload = {
            "schema": "ai-lubricant.server-release/v1",
            "version": args.tag,
            "version_notes": args.notes or "",
            "release_tag": args.tag,
            "download_url": download_url,
            "digest": "",
            "size_bytes": size,
            "updated_at": _utc_now(),
        }
        client = MarketplaceGitHub(settings)
        await client.write_json(
            "server-releases/version.json", payload, f"server-release: {args.tag}"
        )
        print("  ✓ version.json 已写入市场仓")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ version.json 写入异常：{exc}", file=sys.stderr)
        return False


def _utc_now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="上传服务端升级包到 GitHub Release")
    parser.add_argument("--bundle", required=True, help="tar.gz 包路径")
    parser.add_argument("--tag", required=True, help="git 标签（如 v260912）")
    parser.add_argument("--owner", required=True, help="GitHub owner（如 wuxin-gh）")
    parser.add_argument("--repo", required=True, help="GitHub 仓库名")
    parser.add_argument("--notes", default="", help="Release 说明")
    parser.add_argument("--print-url", action="store_true", help="末尾单独打印下载地址")
    args = parser.parse_args()
    try:
        return asyncio.run(_upload(args))
    except Exception as exc:  # noqa: BLE001 - 上传失败只报错，调用方按警告处理
        print(f"✗ 上传异常：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
