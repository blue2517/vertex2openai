import json
import shutil
import subprocess

import pytest


def test_login_and_logout_cookie_path_respects_forwarded_prefix(client):
    r_root = client.post("/api/login", json={"password": "proxy-test-password"})
    assert r_root.status_code == 200
    assert "Path=/" in r_root.headers["set-cookie"]

    r_sub = client.post(
        "/api/login",
        json={"password": "proxy-test-password"},
        headers={"X-Forwarded-Prefix": "/vertex/"},
    )
    assert r_sub.status_code == 200
    set_cookies = r_sub.headers.get_list("set-cookie")
    assert any("Path=/vertex" in c and "Max-Age=0" not in c for c in set_cookies)
    assert any("Path=/" in c and "Path=/vertex" not in c and "Max-Age=0" in c for c in set_cookies)

    # Simulate a browser sending both the new /vertex session cookie and a stale / cookie
    valid_token = [c for c in set_cookies if "Path=/vertex" in c][0].split("ap_session=", 1)[1].split(";", 1)[0]
    r_dash = client.get(
        "/",
        headers={"Cookie": f"ap_session={valid_token}; ap_session=stale-root-token"},
    )
    assert r_dash.status_code == 200
    assert "退出登录" in r_dash.text or "logout()" in r_dash.text

    r_out = client.post(
        "/api/logout",
        headers={
            "X-Forwarded-Prefix": "/vertex",
            "Cookie": f"ap_session={valid_token}; ap_session=stale-root-token",
        },
    )
    assert r_out.status_code == 200
    out_cookies = r_out.headers.get_list("set-cookie")
    assert any("Path=/vertex" in c and "Max-Age=0" in c for c in out_cookies)
    assert any("Path=/" in c and "Path=/vertex" not in c and "Max-Age=0" in c for c in out_cookies)


def test_login_and_dashboard_frontend_prefix_under_subpath():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend subpath checks")
    import main

    login_script = main.LOGIN_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    dash_script = main.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]

    runner = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', async () => {
  const { loginScript, dashScript } = JSON.parse(raw);

  // 1. Login page under /vertex/
  {
    const calls = [];
    const loc = { pathname: '/vertex/', href: 'https://cliproxy.cloud-ip.cc/vertex/' };
    const elems = { pw: { value: 'secret' }, err: { textContent: '' } };
    const ctx = vm.createContext({
      location: loc,
      document: { getElementById: id => elems[id] },
      fetch: async (url, init) => { calls.push([url, init]); return { ok: true }; },
    });
    vm.runInContext(loginScript, ctx);
    await ctx.doLogin({ preventDefault() {} });
    assert.equal(calls[0][0], '/vertex/api/login');
    assert.equal(loc.href, '/vertex/');
  }

  // 2. Dashboard page under /vertex/
  {
    const fetched = [];
    const sseUrls = [];
    const loc = { pathname: '/vertex/', href: 'https://cliproxy.cloud-ip.cc/vertex/', reload() {} };
    const mkEl = () => ({
      value: '', textContent: '', innerHTML: '', disabled: false, checked: false,
      type: 'text', className: '', style: {}, dataset: {}, clientHeight: 0,
      scrollHeight: 0, scrollTop: 0, options: [],
      addEventListener() {}, focus() {}, querySelectorAll: () => [],
      insertAdjacentHTML() {},
      classList: { add() {}, remove() {}, toggle() {} },
    });
    const elems = {};
    class FakeEventSource {
      constructor(url) { sseUrls.push(url); }
    }
    const ctx = vm.createContext({
      location: loc,
      document: {
        getElementById: id => elems[id] || (elems[id] = mkEl()),
        querySelector: () => null,
        querySelectorAll: () => [],
      },
      setInterval: () => 1,
      setTimeout: () => 1,
      clearTimeout: () => {},
      confirm: () => true,
      AbortController,
      EventSource: FakeEventSource,
      fetch: async (url) => {
        fetched.push(url);
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    vm.runInContext(dashScript, ctx);
    await new Promise(r => setImmediate(r));
    assert.ok(fetched.length >= 6, 'expected init fetches');
    for (const u of fetched) {
      assert.ok(u.startsWith('/vertex/api/'), `unexpected fetch url: ${u}`);
    }
    assert.deepEqual(sseUrls, ['/vertex/stream-logs']);

    await ctx.logout();
    assert.equal(fetched.at(-1), '/vertex/api/logout');
    assert.equal(loc.href, '/vertex/');
  }
});
"""
    result = subprocess.run(
        [node, "-e", runner],
        input=json.dumps({"loginScript": login_script, "dashScript": dash_script}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
