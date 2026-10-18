import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.responses import JSONResponse

import runtime_state
from models import OpenAIRequest


def login(client):
    assert client.post('/api/login', json={'password': 'proxy-test-password'}).status_code == 200


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_timeout_settings_require_console_login(client, method):
    assert client.request(method, '/api/settings', json={
        'studio_read_timeout_seconds': 900,
    }).status_code == 401


def test_timeout_default_and_persistence(client):
    login(client)
    assert client.get('/api/settings').json()['studio_read_timeout_seconds'] == 180
    response = client.post('/api/settings', json={'studio_read_timeout_seconds': 900})
    assert response.status_code == 200
    assert response.json()['studio_read_timeout_seconds'] == 900
    assert client.get('/api/settings').json()['studio_read_timeout_seconds'] == 900
    assert runtime_state.AppState().get_studio_read_timeout_seconds() == 900


@pytest.mark.parametrize('seconds', [1, 180, 3600, 180.0])
def test_valid_timeout_values_are_saved_as_integer_seconds(client, seconds):
    login(client)
    response = client.post('/api/settings', json={'studio_read_timeout_seconds': seconds})
    assert response.status_code == 200
    assert type(response.json()['studio_read_timeout_seconds']) is int
    assert runtime_state.app_state.get_studio_read_timeout_seconds() == seconds


@pytest.mark.parametrize('bad', [0, -1, 3601, 1.5, True, False, None, '900', 'bad', [], {}])
def test_invalid_timeout_rejects_entire_settings_patch(client, bad):
    login(client)
    before = client.get('/api/settings').json()
    response = client.post('/api/settings', json={
        'studio_read_timeout_seconds': bad, 'retry_max': 42,
    })
    assert response.status_code == 400
    assert '1' in response.json()['error'] and '3600' in response.json()['error']
    assert client.get('/api/settings').json() == before


@pytest.mark.parametrize('bad', [0, -1, 3601, 1.5, True, None, 'bad', float('nan'), float('inf')])
def test_invalid_persisted_timeout_falls_back_to_default(isolated_state, bad):
    isolated_state._state['settings'] = {'studio_read_timeout_seconds': bad}
    assert isolated_state.get_studio_read_timeout_seconds() == 180


def test_studio_timeout_remains_global(isolated_state):
    isolated_state.update_settings({'studio_read_timeout_seconds': 900})
    result = isolated_state.set_model_override('gemini-4-argon', {
        'studio_read_timeout_seconds': 30, 'thinking_g3_level': 'low',
    })
    assert 'studio_read_timeout_seconds' not in result
    assert isolated_state.get_effective_settings('gemini-4-argon')['studio_read_timeout_seconds'] == 900


@pytest.mark.parametrize('is_new', [False, True])
@pytest.mark.parametrize('studio,seconds,expected_wait', [
    (True, 180, 600), (True, 900, 990), (True, 3600, 3690), (False, 3600, 600),
])
def test_json_buffer_wait_allows_configured_studio_timeout(
    monkeypatch, isolated_state, is_new, studio, seconds, expected_wait,
):
    from resumable import ResumableBuffer
    from request_lifecycle import GenerationJob
    from routes import chat_api

    isolated_state._state['use_web_proxy'] = studio
    isolated_state.update_settings({'studio_read_timeout_seconds': seconds})
    buf = ResumableBuffer('test-buffer', kind='json')
    buf.job = GenerationJob('test-buffer', 'test-key')
    payload = {'choices': []}

    async def finish_wait(*args):
        await asyncio.sleep(0)
        if buf.payload is None:
            await buf.finish('done', payload=payload, status_code=200)
        buf.job.done.set()

    wait = AsyncMock(side_effect=finish_wait)
    monkeypatch.setattr(buf, 'wait_for', wait)
    store = SimpleNamespace(
        create_if_absent=AsyncMock(return_value=(buf, is_new)), register_pump=Mock(),
    )
    monkeypatch.setattr(chat_api, 'resumable_store', store)
    call = AsyncMock(return_value=JSONResponse(content=payload))
    monkeypatch.setattr(chat_api, '_call_upstream', call)
    request = OpenAIRequest(model='gemini-4-argon', messages=[{'role': 'user', 'content': 'hello'}])
    response = asyncio.run(chat_api._resumable_json(
        SimpleNamespace(headers={}, query_params={}, is_disconnected=AsyncMock(return_value=False)),
        request, 'test-key',
    ))

    assert response.status_code == 200
    assert json.loads(response.body) == payload
    wait.assert_awaited_once_with(2 ** 62, expected_wait)
    assert call.await_count == int(is_new)


def test_dashboard_timeout_control_and_save_interactions():
    """执行控制台真实内联 JS，验证回填、保存、空值默认与非法值拦截。"""
    import main

    assert 'id="studio_read_timeout_seconds" type="number" min="1" max="3600" step="1"' in main.DASHBOARD_HTML
    assert 'id="continue_on_disconnect"' in main.DASHBOARD_HTML
    assert 'id="disconnect_grace_seconds" type="number" min="0" max="300" step="1"' in main.DASHBOARD_HTML
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

    replies.push(reply({studio_read_timeout_seconds:900,continue_on_disconnect:true,disconnect_grace_seconds:12}));
    replies.push(reply({capabilities:{},overrides:{},models:[]}));
    await context.loadParams();
    assert.equal(get('studio_read_timeout_seconds').value,'900');
    assert.equal(get('continue_on_disconnect').checked,true);
    assert.equal(get('disconnect_grace_seconds').value,'12');

    calls.length=0;
    get('model-sel').value='__global__';
    get('studio_read_timeout_seconds').value='1800';
    replies.push(reply({studio_read_timeout_seconds:1800}));
    await context.saveSettings();
    assert.equal(calls.length,1);
    assert.equal(calls[0][0],'/api/settings');
    assert.equal(calls[0][1].method,'POST');
    assert.equal(JSON.parse(calls[0][1].body).studio_read_timeout_seconds,1800);
    assert.equal(JSON.parse(calls[0][1].body).continue_on_disconnect,true);
    assert.equal(JSON.parse(calls[0][1].body).disconnect_grace_seconds,12);
    assert.equal(get('studio_read_timeout_seconds').value,'1800');

    for (const bad of ['0','-1','3601','1.5','bad','Infinity']) {
      const before = confirmations;
      calls.length=0;
      get('studio_read_timeout_seconds').value=bad;
      await context.saveSettings();
      assert.equal(calls.length,0);
      assert.equal(confirmations,before);
      assert.equal(get('studio_read_timeout_seconds').focused,true);
      assert.match(messages.at(-1),/1.*3600/);
    }

    get('studio_read_timeout_seconds').value='180';
    for(const bad of ['-1','301','0.5','bad']){
      calls.length=0;
      get('disconnect_grace_seconds').value=bad;
      await context.saveSettings();
      assert.equal(calls.length,0);
      assert.equal(get('disconnect_grace_seconds').focused,true);
    }
    get('disconnect_grace_seconds').value='';

    // 全局基础设施值与所选模型的专属思考等参数独立保存。
    calls.length=0;
    get('model-sel').value='gemini-4-argon';
    context.OVERRIDES={'gemini-4-argon':{thinking_g3_level:'low'}};
    get('studio_read_timeout_seconds').value='';
    replies.push(reply({studio_read_timeout_seconds:180}));
    await context.saveSettings();
    assert.equal(JSON.parse(calls[0][1].body).studio_read_timeout_seconds,180);
    assert.equal(JSON.parse(calls[0][1].body).disconnect_grace_seconds,5);
    assert.equal(get('studio_read_timeout_seconds').value,'180');
    process.stdout.write('Timeout frontend checks passed\n');
  } catch(error) {console.error(error);process.exitCode=1;}
});
'''
    result = subprocess.run(
        [node, '-e', runner], input=json.dumps(source), text=True, encoding='utf-8',
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
