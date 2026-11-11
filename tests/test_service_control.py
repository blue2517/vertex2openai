import subprocess
from unittest.mock import Mock

import pytest

import service_control as control


@pytest.fixture
def controller(monkeypatch):
    import main

    manager = control.ServiceControl()
    monkeypatch.setattr(main, "service_control", manager)
    monkeypatch.setattr(control.sys, "platform", "linux")
    monkeypatch.setattr(control.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(control.config, "RESTART_SYSTEMD_UNIT", "vertex2openai.service")
    timer = Mock()
    monkeypatch.setattr(control.threading, "Timer", timer)
    return manager, timer


def test_restart_requires_login_and_custom_header(client, controller):
    manager, timer = controller
    assert client.post("/api/service/restart").status_code == 401
    client.post("/api/login", json={"password": "proxy-test-password"})
    assert client.post("/api/service/restart").status_code == 403
    assert client.get("/api/service/restart").status_code == 405
    timer.assert_not_called()


def test_restart_is_delayed_deduplicated_and_ignores_browser_command(client, controller):
    manager, timer = controller
    client.post("/api/login", json={"password": "proxy-test-password"})
    before = client.get("/api/service/status")
    assert before.headers["cache-control"] == "no-store"
    assert before.json()["restarting"] is False
    for _ in range(2):
        result = client.post("/api/service/restart",
                             headers={"X-Requested-With": "service-restart"},
                             json={"unit": "unrelated.service", "command": "ignored"})
        assert result.status_code == 202
        assert result.json()["instance_id"] == before.json()["instance_id"]
        assert result.json()["restarting"] is True
    timer.assert_called_once_with(1.0, manager._restart, args=(
        ["/usr/bin/systemctl", "--no-ask-password", "--no-block", "restart", "vertex2openai.service"],
    ))
    timer.return_value.start.assert_called_once()
    assert timer.return_value.daemon is True
    client.post("/api/logout")
    # Still available after the old session has expired, with no config/credentials.
    assert set(client.get("/api/service/status").json()) == {"instance_id", "restarting", "error"}


def test_unsupported_restart_returns_error_without_stopping_service(client, controller, monkeypatch):
    monkeypatch.setattr(control.shutil, "which", lambda _: None)
    client.post("/api/login", json={"password": "proxy-test-password"})
    result = client.post("/api/service/restart", headers={"X-Requested-With": "service-restart"})
    assert result.status_code == 503
    assert controller[0].status()["restarting"] is False
    controller[1].assert_not_called()


@pytest.mark.parametrize("unit", ["*.service", "--help", "x.service;reboot", "x.service\nother.service", "../x.service"])
def test_invalid_unit_is_rejected(controller, monkeypatch, unit):
    monkeypatch.setattr(control.config, "RESTART_SYSTEMD_UNIT", unit)
    with pytest.raises(ValueError):
        controller[0].request_restart()
    controller[1].assert_not_called()


@pytest.mark.parametrize("cgroup", [
    "0::/system.slice/vertex2openai.service\n",
    "12:memory:/system.slice/vertex2openai.service\n1:name=systemd:/system.slice/vertex2openai.service\n",
])
def test_detects_own_systemd_unit(controller, monkeypatch, cgroup):
    monkeypatch.setattr(control.config, "RESTART_SYSTEMD_UNIT", "")
    monkeypatch.setattr(control.Path, "read_text", lambda _: cgroup)
    assert control.restart_command()[-1] == "vertex2openai.service"


@pytest.mark.parametrize("cgroup", [
    "0::/user.slice/session-1.scope", "0::/user.slice/user@1000.service/app.slice/session.scope",
    "0::/system.slice/docker.service/container", "0::/user.slice/user@1000.service",
])
def test_no_systemd_unit_and_non_linux_are_rejected(controller, monkeypatch, cgroup):
    monkeypatch.setattr(control.config, "RESTART_SYSTEMD_UNIT", "")
    monkeypatch.setattr(control.Path, "read_text", lambda _: cgroup)
    with pytest.raises(ValueError):
        control.restart_command()
    monkeypatch.setattr(control.sys, "platform", "win32")
    with pytest.raises(ValueError):
        control.restart_command()


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, "systemctl", stderr=b"PRIVATE_HOST_DETAILS"),
    subprocess.TimeoutExpired("systemctl", 10), OSError("PRIVATE_HOST_DETAILS"),
])
def test_submission_failure_can_be_retried(controller, monkeypatch, error):
    manager, timer = controller
    runner = Mock(side_effect=error)
    monkeypatch.setattr(control.subprocess, "run", runner)
    manager.request_restart()
    command = control.restart_command()
    manager._restart(command)
    runner.assert_called_once_with(command, check=True, timeout=10, capture_output=True)
    assert manager.status()["restarting"] is False
    assert manager.status()["error"]
    assert "PRIVATE_HOST_DETAILS" not in str(manager.status())
    manager.request_restart()
    assert manager.status()["restarting"] is True
    assert manager.status()["error"] is None
    assert timer.call_count == 2


def test_systemd_invocation_id_is_shared_by_workers(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "boot-one")
    assert control.ServiceControl().instance_id == control.ServiceControl().instance_id == "boot-one"
    monkeypatch.setenv("INVOCATION_ID", "boot-two")
    assert control.ServiceControl().instance_id == "boot-two"


def test_successful_submission_waits_for_new_process(controller, monkeypatch):
    manager, _ = controller
    runner = Mock()
    monkeypatch.setattr(control.subprocess, "run", runner)
    manager.request_restart()
    command = control.restart_command()
    manager._restart(command)
    runner.assert_called_once_with(command, check=True, timeout=10, capture_output=True)
    assert manager.status()["restarting"] is True
    assert manager.status()["error"] is None


def test_timer_failure_does_not_latch_restart_state(controller):
    manager, timer = controller
    timer.return_value.start.side_effect = RuntimeError("thread limit")
    with pytest.raises(RuntimeError):
        manager.request_restart()
    assert manager.status()["restarting"] is False
