# -*- coding: utf-8 -*-
"""pytest 基础设施：把 app/ 加入 sys.path，并逐个测试隔离运行态。

app 里的模块用顶层导入（`import config`），所以必须把 app/ 放进 sys.path。

隔离要点：`runtime_state` 在**导入时**就把 STATE_FILE 定成 `STATE_DIR/web_state.json`
（STATE_DIR 默认 "."），因此仅靠 chdir 隔离不住——已经算好的路径不会跟着变。
这里同时改写模块级 STATE_FILE 到临时目录，并清空进程内缓存（新实现是
`self._state`，只在启动时读盘一次），否则测试之间会互相看到对方写的 Project ID 等值。
"""
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import runtime_state  # noqa: E402
from runtime_state import app_state  # noqa: E402


def _reset_memory():
    with app_state._lock:
        app_state._state = {"use_web_proxy": False}


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_state, "STATE_FILE", str(tmp_path / "web_state.json"))
    _reset_memory()
    yield
    _reset_memory()
