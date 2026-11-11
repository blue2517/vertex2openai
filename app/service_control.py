"""Restart only this application's systemd unit; never accept a browser command."""

import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import config


def restart_command():
    if sys.platform != "linux":
        raise ValueError("重启按钮适用于 Linux systemd 部署。")
    executable = shutil.which("systemctl")
    if not executable:
        raise ValueError("未检测到 systemctl，请使用部署平台的重启功能。")
    unit = config.RESTART_SYSTEMD_UNIT.strip()
    if not unit:
        try:
            # Require a service cgroup, not an ancestor such as user@1000.service
            # around an interactive shell or docker.service around a container.
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                part = line.split(":", 2)[-1].rstrip("/").rsplit("/", 1)[-1]
                if part.endswith(".service") and not re.fullmatch(r"user@\d+\.service", part):
                    unit = part
                    break
        except OSError:
            pass
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*\.service", unit):
        raise ValueError("请在服务环境中设置 RESTART_SYSTEMD_UNIT 为本服务的 systemd 单元名。")
    return [executable, "--no-ask-password", "--no-block", "restart", unit]


class ServiceControl:
    def __init__(self):
        self.instance_id = os.environ.get("INVOCATION_ID") or secrets.token_hex(16)
        self._lock = threading.Lock()
        self._pending = False
        self._error = None

    def status(self):
        with self._lock:
            return {"instance_id": self.instance_id, "restarting": self._pending,
                    "error": self._error}

    def request_restart(self):
        with self._lock:
            if self._pending:
                return
            command = restart_command()
            self._pending = True
            self._error = None
            # Leave time for the HTTP response to reach the browser before systemd
            # stops this process. --no-block avoids waiting for our own shutdown.
            timer = threading.Timer(1.0, self._restart, args=(command,))
            timer.daemon = True
            try:
                timer.start()
            except Exception:
                self._pending = False
                raise

    def _restart(self, command):
        try:
            print("🔄 [服务重启] 控制台已请求 systemd 重启本服务；进行中的请求可能中断。")
            subprocess.run(command, check=True, timeout=10, capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            # Keep subprocess output (possibly containing host details) out of HTTP.
            print(f"❌ [服务重启] systemctl 提交失败：{type(exc).__name__}；请检查服务权限与 systemd 日志。")
            with self._lock:
                self._pending = False
                self._error = "重启提交失败，请检查 systemd 服务名、执行权限和服务日志。"


service_control = ServiceControl()
