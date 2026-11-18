import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.responses import JSONResponse

import api_helpers
import runtime_state
from models import OpenAIRequest
from upstreams import express_sdk


def _login(client):
    assert client.post('/api/login', json={'password': 'proxy-test-password'}).status_code == 200


@pytest.mark.parametrize('key', [
    'express_stream_read_timeout_seconds',
    'express_nonstream_read_timeout_seconds',
])
def test_express_timeout_default_and_persistence(client, key):
    _login(client)
    assert client.get('/api/settings').json()[key] == 180
    response = client.post('/api/settings', json={key: 420})
    assert response.status_code == 200
    assert response.json()[key] == 420
    assert client.get('/api/settings').json()[key] == 420


@pytest.mark.parametrize('key', [
    'express_stream_read_timeout_seconds',
    'express_nonstream_read_timeout_seconds',
])
@pytest.mark.parametrize('bad', [0, -1, 3601, 1.5, True, False, None, '420', 'bad', [], {}])
def test_invalid_express_timeout_rejects_entire_settings_patch(client, key, bad):
    _login(client)
    before = client.get('/api/settings').json()
    response = client.post('/api/settings', json={key: bad, 'retry_max': 42})
    assert response.status_code == 400
    assert '1' in response.json()['error'] and '3600' in response.json()['error']
    assert client.get('/api/settings').json() == before


@pytest.mark.parametrize('bad', [0, -1, 3601, 1.5, True, None, 'bad', float('nan'), float('inf')])
def test_invalid_persisted_express_timeouts_fall_back_to_default(isolated_state, bad):
    isolated_state._state['settings'] = {
        'express_stream_read_timeout_seconds': bad,
        'express_nonstream_read_timeout_seconds': bad,
    }
    assert isolated_state.get_express_stream_read_timeout_seconds() == 180
    assert isolated_state.get_express_nonstream_read_timeout_seconds() == 180


def test_express_timeouts_remain_global(isolated_state):
    isolated_state.update_settings({
        'express_stream_read_timeout_seconds': 75,
        'express_nonstream_read_timeout_seconds': 300,
    })
    result = isolated_state.set_model_override('gemini-4-argon', {
        'express_stream_read_timeout_seconds': 10,
        'express_nonstream_read_timeout_seconds': 20,
        'thinking_g3_level': 'low',
    })
    assert 'express_stream_read_timeout_seconds' not in result
    assert 'express_nonstream_read_timeout_seconds' not in result
    eff = isolated_state.get_effective_settings('gemini-4-argon')
    assert eff['express_stream_read_timeout_seconds'] == 75
    assert eff['express_nonstream_read_timeout_seconds'] == 300


@pytest.mark.parametrize('seconds,expected_wait', [(180, 600), (900, 990), (3600, 3690)])
def test_json_buffer_wait_allows_configured_express_nonstream_timeout(
    monkeypatch, isolated_state, seconds, expected_wait,
):
    from resumable import ResumableBuffer
    from request_lifecycle import GenerationJob
    from routes import chat_api

    isolated_state._state['use_web_proxy'] = False
    isolated_state.update_settings({'express_nonstream_read_timeout_seconds': seconds})
    buf = ResumableBuffer('express-buffer', kind='json')
    buf.job = GenerationJob('express-buffer', 'test-key')
    payload = {'choices': []}

    async def finish_wait(*args):
        await asyncio.sleep(0)
        if buf.payload is None:
            await buf.finish('done', payload=payload, status_code=200)
        buf.job.done.set()

    wait = AsyncMock(side_effect=finish_wait)
    monkeypatch.setattr(buf, 'wait_for', wait)
    monkeypatch.setattr(chat_api, 'resumable_store', SimpleNamespace(
        create_if_absent=AsyncMock(return_value=(buf, True)), register_pump=Mock(),
    ))
    monkeypatch.setattr(chat_api, '_call_upstream', AsyncMock(return_value=JSONResponse(content=payload)))
    request = OpenAIRequest(model='gemini-4-argon', messages=[{'role': 'user', 'content': 'hello'}])
    response = asyncio.run(chat_api._resumable_json(
        SimpleNamespace(headers={}, query_params={}, is_disconnected=AsyncMock(return_value=False)),
        request, 'test-key',
    ))
    assert response.status_code == 200
    wait.assert_awaited_once_with(2 ** 62, expected_wait)


@pytest.mark.parametrize('stream,fake,expected_timeout', [
    (True, False, 45),
    (True, True, 240),
    (False, False, 240),
])
def test_express_selects_stream_vs_nonstream_timeout(monkeypatch, isolated_state, stream, fake, expected_timeout):
    isolated_state.update_settings({
        'fake_streaming': fake,
        'fake_streaming_interval': 0,
        'express_stream_read_timeout_seconds': 45,
        'express_nonstream_read_timeout_seconds': 240,
    })
    seen_timeouts = []

    def spy_attach(client, trace, read_timeout_seconds=None):
        seen_timeouts.append(read_timeout_seconds)
        return client

    monkeypatch.setattr(api_helpers, 'attach_express_trace', spy_attach)
    raw = SimpleNamespace(candidates=[SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text='ok', thought=False)]), finish_reason='STOP',
    )])

    async def chunks():
        yield raw

    models = SimpleNamespace(
        generate_content=AsyncMock(return_value=raw),
        generate_content_stream=AsyncMock(return_value=chunks()),
    )
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    req = OpenAIRequest(model='gemini-4-argon', stream=stream, messages=[{'role': 'user', 'content': 'hi'}])

    async def scenario():
        res = await api_helpers.execute_gemini_call(client, req.model, lambda _: [], {}, req)
        if stream:
            _ = [f async for f in res.body_iterator]

    asyncio.run(scenario())
    assert seen_timeouts and all(t == expected_timeout for t in seen_timeouts)


@pytest.mark.parametrize('stream', [False, True])
def test_express_real_client_enforces_configured_read_timeout_without_retry(monkeypatch, isolated_state, stream):
    from google import genai
    from google.genai import types
    from api_errors import prepare_stream_response

    isolated_state.update_settings({
        'fake_streaming': False,
        'retry_max': 3,
        'retry_backoff_seconds': 0,
        'express_stream_read_timeout_seconds': 1,
        'express_nonstream_read_timeout_seconds': 1,
    })
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    accepted = []
    stop_accept = threading.Event()

    def accept_and_hang():
        srv.settimeout(0.2)
        while not stop_accept.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            accepted.append(conn)
            try:
                conn.recv(4096)
            except OSError:
                pass

    t = threading.Thread(target=accept_and_hang, daemon=True)
    t.start()

    try:
        monkeypatch.setattr(
            express_sdk,
            'get_http_options',
            lambda: types.HttpOptions(base_url=f'http://127.0.0.1:{port}'),
        )
        # Keep test fast (<1s) by shortening the validated 1s timeout in the getter just for this socket check.
        monkeypatch.setattr(isolated_state, 'get_express_stream_read_timeout_seconds', lambda: 0.25)
        monkeypatch.setattr(isolated_state, 'get_express_nonstream_read_timeout_seconds', lambda: 0.25)
        manager = SimpleNamespace(
            get_total_keys=lambda: 1,
            get_express_record=lambda: {'key': 'TEST_KEY', 'project_id': '', 'note': ''},
        )
        conn_req = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(express_key_manager=manager)),
            is_disconnected=AsyncMock(return_value=False),
        )
        req = OpenAIRequest(model='gemini-3.8-flash', stream=stream, messages=[{'role': 'user', 'content': 'hi'}])

        async def scenario():
            started = time.monotonic()
            if stream:
                resp = await express_sdk.ExpressSDKUpstream().chat_completions(req, conn_req)
                prepared = await prepare_stream_response(resp)
                assert prepared.status_code == 504
                assert '读取上游响应超时' in json.loads(prepared.body)['error']['message']
            else:
                with pytest.raises(api_helpers.UpstreamError) as exc:
                    await express_sdk.ExpressSDKUpstream().chat_completions(req, conn_req)
                assert exc.value.status_code == 504
                assert '读取上游响应超时' in str(exc.value)
            assert (time.monotonic() - started) < 2.0

        asyncio.run(scenario())
        assert len(accepted) == 1
    finally:
        stop_accept.set()
        for c in accepted:
            try:
                c.close()
            except OSError:
                pass
        srv.close()
        t.join(timeout=2)


def test_dashboard_express_timeout_controls_and_save_interactions():
    import main

    assert 'id="express_stream_read_timeout_seconds" type="number" min="1" max="3600" step="1"' in main.DASHBOARD_HTML
    assert 'id="express_nonstream_read_timeout_seconds" type="number" min="1" max="3600" step="1"' in main.DASHBOARD_HTML
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for frontend interaction checks')
    source = main.DASHBOARD_HTML.split('/* ---------- Params ---------- */', 1)[1]
    source = source.split('/* ---------- Logs ---------- */', 1)[0]
    runner = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', async () => {
  try {
    const elements = {}, calls = [], replies = [], messages = [];
    let confirmations = 0;
    const element = () => {
      let value = '';
      return {
        get value(){return value;}, set value(v){value=String(v);},
        type:'number', checked:false, innerHTML:'', focused:false,
        focus(){this.focused=true;}
      };
    };
    const get = id => elements[id] || (elements[id] = element());
    get('continue_on_disconnect').type='checkbox';
    const context = vm.createContext({
      $:get, GLOBAL_SETTINGS:{}, OVERRIDES:{}, CAPS:{}, curAR:'',
      toast:message=>messages.push(message), confirm:()=>{confirmations++;return true;},
      fetch:async (...args)=>{calls.push(args);return replies.shift();}
    });
    vm.runInContext(JSON.parse(raw), context);
    context.renderCaps = () => {};
    const reply = (value, ok=true) => ({ok,json:async()=>value});

    replies.push(reply({
      studio_read_timeout_seconds:180,
      express_stream_read_timeout_seconds:60,
      express_nonstream_read_timeout_seconds:420,
      continue_on_disconnect:false,
      disconnect_grace_seconds:5,
    }));
    replies.push(reply({capabilities:{},overrides:{},models:[]}));
    await context.loadParams();
    assert.equal(get('express_stream_read_timeout_seconds').value,'60');
    assert.equal(get('express_nonstream_read_timeout_seconds').value,'420');

    calls.length=0;
    get('model-sel').value='__global__';
    get('express_stream_read_timeout_seconds').value='90';
    get('express_nonstream_read_timeout_seconds').value='600';
    replies.push(reply({
      studio_read_timeout_seconds:180,
      express_stream_read_timeout_seconds:90,
      express_nonstream_read_timeout_seconds:600,
    }));
    await context.saveSettings();
    const sent = JSON.parse(calls[0][1].body);
    assert.equal(sent.express_stream_read_timeout_seconds,90);
    assert.equal(sent.express_nonstream_read_timeout_seconds,600);
    assert.equal(get('express_stream_read_timeout_seconds').value,'90');
    assert.equal(get('express_nonstream_read_timeout_seconds').value,'600');

    for (const field of ['express_stream_read_timeout_seconds','express_nonstream_read_timeout_seconds']) {
      for (const bad of ['0','-1','3601','1.5','bad','Infinity']) {
        const before = confirmations;
        calls.length=0;
        get(field).focused=false;
        get(field).value=bad;
        await context.saveSettings();
        assert.equal(calls.length,0);
        assert.equal(confirmations,before);
        assert.equal(get(field).focused,true);
        assert.match(messages.at(-1),/1.*3600/);
      }
      get(field).value='';
    }

    calls.length=0;
    replies.push(reply({
      studio_read_timeout_seconds:180,
      express_stream_read_timeout_seconds:180,
      express_nonstream_read_timeout_seconds:180,
    }));
    await context.saveSettings();
    const fallbackSent = JSON.parse(calls[0][1].body);
    assert.equal(fallbackSent.express_stream_read_timeout_seconds,180);
    assert.equal(fallbackSent.express_nonstream_read_timeout_seconds,180);
    process.stdout.write('Express timeout frontend checks passed\n');
  } catch(error) {console.error(error);process.exitCode=1;}
});
'''
    result = subprocess.run(
        [node, '-e', runner], input=json.dumps(source), text=True, encoding='utf-8',
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
