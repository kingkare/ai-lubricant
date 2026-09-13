"""Generate a supervisord configuration for the native stack."""
from __future__ import annotations

import os
from pathlib import Path

from . import clickhouse, layout, postgres, redis
from .lifecycle import DepsConfig


def _quote(value: str) -> str:
    return value.replace("%", "%%")


def _shquote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def generate(cfg: DepsConfig, *, root: Path | None = None) -> Path:
    """Write a deterministic supervisord config and return its path.

    The provision command must run first. DB services then run foreground and
    are restarted by supervisord; app commands use the same Python interpreter
    and the repository root as the caller.
    """
    root = root or layout.root()
    conf_path = layout.config_dir() / "supervisord-native.conf"
    python = os.environ.get("NATIVE_PYTHON", "") or os.sys.executable
    repo = Path(os.environ.get("NATIVE_APP_ROOT", Path(__file__).resolve().parent.parent))
    log_dir = layout.logs_dir()
    env_file = Path(os.environ.get("DESKTOP_ENV_FILE", repo / ".env"))

    programs: list[str] = [
        "[supervisord]",
        f"logfile={_quote(str(log_dir / 'supervisord.log'))}",
        "pidfile=%(here)s/supervisord.pid",
        "nodaemon=true",
        "",
        "[unix_http_server]",
        f"file=%(here)s/supervisor.sock",
        "",
        "[supervisorctl]",
        "serverurl=unix://%(here)s/supervisor.sock",
        "",
        "[program:provision-gate]",
        f"command={_quote(python)} -m native_deps.gate postgres redis" + (" clickhouse" if cfg.clickhouse_enabled else ""),
        f"directory={_quote(str(repo))}",
        "autostart=true",
        "autorestart=false",
        "startsecs=0",
        "exitcodes=0",
        "priority=5",
        f"stdout_logfile={_quote(str(log_dir / 'provision-gate.log'))}",
        f"stderr_logfile={_quote(str(log_dir / 'provision-gate.err.log'))}",
        "",
    ]

    def add_program(name: str, command: str, priority: int, logfile: str) -> None:
        programs.extend([
            f"[program:{name}]",
            f"command={command}",
            f"directory={_quote(str(repo))}",
            "autostart=true",
            "autorestart=true",
            "startretries=3",
            "startsecs=3",
            f"priority={priority}",
            f"stdout_logfile={_quote(str(log_dir / logfile))}",
            f"stderr_logfile={_quote(str(log_dir / logfile.replace('.log', '.err.log')))}",
            # supervisord has no env_file directive; source the generated env
            # before exec so all app processes receive the same credentials.
            f"environment=NATIVE_ENV_FILE=\"{_quote(str(env_file))}\"",
            "",
        ])

    if cfg.postgres_exe:
        add_program("postgres", f"{_quote(str(cfg.postgres_exe))} -D {postgres.data_root()}", 10, "postgres.log")
    if cfg.redis_exe:
        add_program("redis", " ".join(_quote(x) for x in redis.start_command(cfg.redis_exe)), 20, "redis.log")
    if cfg.clickhouse_enabled and cfg.clickhouse_exe:
        add_program("clickhouse", " ".join(_quote(x) for x in clickhouse.start_command(cfg.clickhouse_exe)), 30, "clickhouse.log")

    # Use /bin/sh -lc so .env is sourced before exec; supervisord does not
    # parse shell syntax in command=, so an explicit shell is mandatory.
    shell = "/bin/sh -lc " if __import__("sys").platform != "win32" else "cmd /c "

    def _app_cmd(module: str) -> str:
        body = (
            f"cd {_shquote(str(repo))} && set -a && "
            f"[ -f {_shquote(str(env_file))} ] && . {_shquote(str(env_file))}; set +a && "
            f"exec {_shquote(python)} {module}"
        )
        return shell + _shquote(body)

    add_program("node-server", _app_cmd("-m node_server"), 40, "node_server.log")
    add_program("main", _app_cmd("main.py"), 50, "main.log")
    add_program("tunnel-server", _app_cmd("-m tunnel_server"), 60, "tunnel_server.log")

    conf_path.write_text("\n".join(programs) + "\n", encoding="utf-8")
    return conf_path
