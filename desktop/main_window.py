"""Desktop launcher — the ``AiLubricant.exe`` entry point.

Flow:
  1. Apply env bootstrap (relocate .env to %LOCALAPPDATA%, seed keys/defaults).
  2. Probe PG + Redis. Both are mandatory. If either is unreachable, run the
     configuration wizard in the window until the user saves a working config.
  3. Start main service -> node_server -> tunnel_server (serialized).
  4. Point the window at the management console.
  5. On window close, tear the children down.

When frozen, this same exe is re-executed with ``--run-child -m <module>`` to
act as a plain interpreter for the child processes (see the dispatch below).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import runpy
import sys
import threading


def _run_child() -> None:
    """Frozen-mode child dispatch: behave like ``python -m <module>``.

    The supervisor launches children as ``AiLubricant.exe --run-child -m pkg``
    because in a PyInstaller bundle ``sys.executable`` is the exe, not python.
    """
    argv = sys.argv[2:]  # strip exe path + --run-child
    if not argv:
        raise SystemExit("--run-child requires arguments")
    if argv[0] == "-m":
        module = argv[1]
        sys.argv = [module, *argv[2:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return
    script = argv[0]
    sys.argv = list(argv)
    runpy.run_path(script, run_name="__main__")


def _serve_wizard(port: int) -> threading.Thread:
    """Run the wizard app in a background thread; returns the thread."""
    import uvicorn

    from desktop import config_wizard

    config = uvicorn.Config(
        config_wizard.app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    thread.server = server  # type: ignore[attr-defined]
    return thread


def _wizard_flow(window_title: str) -> bool:
    """Show the wizard until the user saves a working config. True if saved."""
    import webview

    from desktop import config_wizard

    port = 8099
    thread = _serve_wizard(port)
    saved = {"ok": False}

    def _on_closed() -> None:
        saved["ok"] = config_wizard.saved_ok

    window = webview.create_window(
        window_title, f"http://127.0.0.1:{port}/", width=880, height=760
    )
    window.events.closed += _on_closed

    def _watch() -> None:
        """Close the wizard window as soon as the config is saved."""
        import time

        while True:
            if config_wizard.saved_ok:
                time.sleep(1.0)  # let the "starting…" page render
                with contextlib.suppress(Exception):
                    window.destroy()
                return
            time.sleep(0.4)

    threading.Thread(target=_watch, daemon=True).start()
    webview.start()
    server = getattr(thread, "server", None)
    if server is not None:
        server.should_exit = True
    return saved["ok"] or config_wizard.saved_ok


def _error_window(title: str, message: str) -> None:
    import webview

    html = (
        "<html><head><meta charset='utf-8'><style>"
        "body{font:14px/1.7 'Segoe UI','Microsoft YaHei',sans-serif;padding:26px;"
        "background:#f6f7f9;color:#1a1d21}"
        "pre{background:#f0f1f3;padding:12px;border-radius:6px;white-space:pre-wrap}"
        "@media(prefers-color-scheme:dark){body{background:#16181d;color:#e5e7eb}"
        "pre{background:#1e2127}}</style></head><body>"
        f"<h2>启动失败</h2><pre>{message}</pre></body></html>"
    )
    webview.create_window(title, html=html, width=760, height=520)
    webview.start()


def _auto_provision_flow(window_title: str) -> "DepsRuntime | None":
    """Download + start local PG/Redis/ClickHouse with a blocking status window.

    Returns the live :class:`DepsRuntime` for the caller to tear down on exit,
    or ``None`` if provisioning failed or was cancelled.
    """
    import webview

    from native_deps import lifecycle

    html = (
        "<html><head><meta charset='utf-8'><style>"
        "body{font:14px/1.7 'Segoe UI','Microsoft YaHei',sans-serif;padding:30px;"
        "background:#f6f7f9;color:#1a1d21}"
        "@media(prefers-color-scheme:dark){body{background:#16181d;color:#e5e7eb}}"
        "</style></head><body>"
        "<h2>正在准备本地数据库</h2>"
        "<p>首次启动需下载 PostgreSQL / Redis / ClickHouse 并初始化，"
        "可能需要数分钟。进度见日志目录 native-deps/。</p>"
        "<p style='color:#666'>请勿关闭此窗口…</p></body></html>"
    )
    window = webview.create_window(window_title, html=html, width=620, height=360)

    result: dict[str, object] = {"runtime": None, "error": None}

    def _worker() -> None:
        try:
            cfg = asyncio.run(lifecycle.ensure_all())
            from desktop import paths

            lifecycle.write_env_file(paths.env_file_path(), lifecycle.env_updates(cfg))
            runtime = lifecycle.DepsRuntime(cfg)
            if asyncio.run(runtime.start_all()):
                result["runtime"] = runtime
            else:
                runtime.stop_all()
                result["error"] = "依赖服务启动失败"
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=_worker, daemon=True).start()

    def _watch() -> None:
        import time

        while result["runtime"] is None and result["error"] is None:
            time.sleep(0.4)
        time.sleep(0.8)  # let the final status render briefly
        with contextlib.suppress(Exception):
            window.destroy()

    threading.Thread(target=_watch, daemon=True).start()
    webview.start()
    if result["error"]:
        _error_window(window_title, str(result["error"]))
    return result["runtime"]  # type: ignore[return-value]


def main() -> None:
    from desktop import env_bootstrap, paths

    env_bootstrap.apply_env()

    title = "Ai Lubricant"
    main_port = int(os.environ.get("DESKTOP_MAIN_PORT", "8001"))

    # ── 1. dependency check ───────────────────────────────────────────────
    from desktop import config_wizard

    deps_runtime = None
    ready = asyncio.run(config_wizard.env_is_ready())
    if not ready:
        # Prefer auto-provisioning local PG/Redis/ClickHouse; fall back to the
        # manual external-config wizard if the user declines or it fails.
        deps_runtime = _auto_provision_flow(f"{title} — 准备依赖")
        if deps_runtime is not None:
            env_bootstrap.apply_env()
            from dotenv import load_dotenv

            load_dotenv(dotenv_path=paths.env_file_path(), override=True)
        else:
            if not _wizard_flow(f"{title} — 首次配置"):
                return  # user closed the wizard without saving
            env_bootstrap.apply_env()
            from dotenv import load_dotenv

            load_dotenv(dotenv_path=paths.env_file_path(), override=True)

    # ── 2. start services ─────────────────────────────────────────────────
    from desktop.supervisor import Supervisor

    supervisor = Supervisor()
    if not supervisor.start_all():
        log_hint = str(paths.logs_dir() / "main.log")
        supervisor.stop_all()
        _error_window(
            title,
            "主服务未能在 60 秒内就绪。\n\n"
            f"请查看日志：\n{log_hint}\n\n"
            "常见原因：PostgreSQL / Redis 连接信息有误，或端口 "
            f"{main_port} 已被占用。",
        )
        return

    # ── 3. open the console ───────────────────────────────────────────────
    import webview

    webview.create_window(
        title,
        f"http://127.0.0.1:{main_port}/manager",
        width=1440,
        height=900,
        min_size=(1024, 680),
    )
    try:
        webview.start()
    finally:
        supervisor.stop_all()
        if deps_runtime is not None:
            deps_runtime.stop_all()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--run-child":
        _run_child()
    else:
        main()
