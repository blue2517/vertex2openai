import json
import shutil
import subprocess

import pytest


def test_logs_open_at_latest_and_preserve_manual_scrolling():
    """执行真实日志页 JS，模拟隐藏容器的零尺寸及浏览器滚动范围。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend interaction checks")
    import main

    source = "function switchTab(t){" + main.DASHBOARD_HTML.split(
        "function switchTab(t){", 1
    )[1].split("/* ---------- Overview", 1)[0]
    source += main.DASHBOARD_HTML.split("/* ---------- Logs ---------- */", 1)[1].split(
        "/* Cookie Project IDs", 1
    )[0]
    assert 'onclick="scrollLogsToLatest()">回到最新</button>' in main.DASHBOARD_HTML

    runner = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', () => {
  const views = Object.fromEntries(['overview', 'channel', 'params', 'logs'].map(name => [
    'view-' + name,
    {classList: {hidden: name !== 'overview', toggle(key, value) { this[key] = value; }}}
  ]));
  let height = 0, top = 0, scroll, stream;
  const visible = () => !views['view-logs'].classList.hidden;
  const logwin = {
    get clientHeight() { return visible() ? 600 : 0; },
    get scrollHeight() { return visible() ? height : 0; },
    get scrollTop() { return visible() ? top : 0; },
    set scrollTop(value) {
      top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
    },
    addEventListener(name, handler) { assert.equal(name, 'scroll'); scroll = handler; },
    insertAdjacentHTML(position, html) {
      assert.equal(position, 'beforeend');
      assert.ok(html.includes('</div>'));
      height += 80;
    }
  };
  const context = vm.createContext({
    $: id => id === 'logwin' ? logwin : views[id],
    document: {querySelectorAll: () => []},
    EventSource: function(url) { assert.equal(url, '/stream-logs'); stream = this; }
  });
  vm.runInContext(JSON.parse(raw), context);
  const message = () => stream.onmessage({data: '[12:00:00] test log'});
  const atBottom = () => assert.equal(top, Math.max(0, height - 600));

  // Empty first visit, then a long history arriving while the tab is hidden.
  context.switchTab('logs');
  atBottom();
  context.switchTab('overview');
  for(let i = 0; i < 100; i++) message();
  assert.equal(top, 0);
  context.switchTab('logs');
  atBottom();
  scroll();
  message();
  atBottom();

  // Reading older entries pauses following, including across multiple messages.
  logwin.scrollTop = 400;
  scroll();
  message(); message();
  assert.equal(top, 400);

  // Hidden-container scroll events must not reset the user's following state.
  context.switchTab('params');
  scroll();
  assert.equal(vm.runInContext('autoscroll', context), false);
  message();
  context.switchTab('logs');
  atBottom();
  message();
  atBottom();

  // The button resumes following without switching tabs.
  logwin.scrollTop = 200;
  scroll();
  context.scrollLogsToLatest();
  atBottom();
  message();
  atBottom();

  // Manually returning to the bottom also resumes following.
  logwin.scrollTop = 300;
  scroll();
  logwin.scrollTop = logwin.scrollHeight;
  scroll();
  message();
  atBottom();
  const before = height;
  stream.onmessage({data: 'keep-alive'});
  assert.equal(height, before);
});
"""
    result = subprocess.run(
        [node, "-e", runner], input=json.dumps(source), text=True,
        encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
