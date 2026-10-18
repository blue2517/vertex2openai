"""Optional integration tests against installed, unmodified client sources.

Set SILLYTAVERN_ROOT to a SillyTavern checkout with node_modules installed.
All requests use local fixtures; no real credentials or model calls are used.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.responses import StreamingResponse

from api_errors import UpstreamError, error_code_for_status, error_sse
from request_lifecycle import DisconnectMiddleware
from resumable import ResumableStore
from routes import chat_api
from auth import get_api_key
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(os.environ.get('SILLYTAVERN_ROOT', '__not_configured__'))
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(
    not NODE or not (ROOT / 'node_modules/openai/package.json').is_file(),
    reason='Set SILLYTAVERN_ROOT to run real-client protocol checks',
)


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status', [403, 429, 503])
def test_installed_clients_end_on_terminal_errors(monkeypatch, isolated_state, tmp_path, buffered, stream, status):
    isolated_state.update_settings({'resumable_enabled': buffered})
    app = FastAPI()
    app.include_router(chat_api.router)
    app.add_middleware(DisconnectMiddleware)
    app.dependency_overrides[get_api_key] = lambda: 'fixture-key'
    monkeypatch.setattr(chat_api, 'resumable_store', ResumableStore())
    calls = []

    async def fail(*args):
        calls.append(1)
        if not stream:
            raise UpstreamError(status, 'fixture error')

        async def frames():
            yield ': keep-alive\n\n'
            yield error_sse(UpstreamError(status, 'fixture error'))
        return StreamingResponse(frames(), media_type='text/event-stream')

    monkeypatch.setattr(chat_api, '_call_upstream', fail)
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={
            'model': 'gemini-4-argon', 'messages': [{'role': 'user', 'content': 'Hi'}], 'stream': stream,
        })
    assert calls == [1]
    packet = {'status': response.status_code, 'body': response.text, 'headers': dict(response.headers),
              'code': error_code_for_status(status), 'stream': stream}
    fixture = tmp_path / 'response.json'
    fixture.write_text(json.dumps(packet), encoding='utf-8')
    (tmp_path / 'config.yaml').write_text('openai: {}\n', encoding='utf-8')
    script = r'''
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
const root = process.env.SILLYTAVERN_ROOT;
const dir = process.env.CLIENT_FIXTURE_DIR;
const packet = JSON.parse(fs.readFileSync(path.join(dir, 'response.json'), 'utf8'));
const require = createRequire(path.join(root, 'package.json'));
const { default: OpenAI } = require('openai');
let sdkCalls = 0;
const sdk = new OpenAI({ apiKey: 'fixture', maxRetries: 3, fetch: async () => {
    sdkCalls++;
    return new Response(packet.body, { status: packet.status, headers: packet.headers });
} });
await assert.rejects(sdk.chat.completions.create({ model: 'fixture', messages: [] }), error => {
    assert.equal(error.status, packet.status);
    assert.equal(error.code, packet.code);
    return true;
});
assert.equal(sdkCalls, 1, 'SDK must honor terminal retry hint even for 429/503');

// Mount the installed SillyTavern backend in isolation, not its live server.
const express = require('express');
const util = await import(pathToFileURL(path.join(root, 'src/util.js')).href);
util.setConfigFilePath(path.join(dir, 'config.yaml'));
const { router } = await import(pathToFileURL(path.join(root, 'src/endpoints/backends/chat-completions.js')).href);
let backendCalls = 0;
const upstream = express();
upstream.post('/v1/chat/completions', (_request, result) => {
    backendCalls++;
    result.status(packet.status).set(packet.headers).send(packet.body);
});
const listen = app => new Promise(resolve => { const server = app.listen(0, '127.0.0.1', () => resolve(server)); });
const stop = server => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); });
const upstreamServer = await listen(upstream);
const tavern = express();
tavern.use(express.json());
tavern.use('/api/backends/chat-completions', router);
const tavernServer = await listen(tavern);
try {
    const generateData = {
        chat_completion_source: 'openai',
        reverse_proxy: `http://127.0.0.1:${upstreamServer.address().port}/v1`,
        proxy_password: 'fixture', model: 'gemini-4-argon', stream: packet.stream,
        messages: [{role: 'user', content: 'Hi'}],
    };
    // Execute the actual installed frontend error path with DOM-only collaborators stubbed.
    const source = fs.readFileSync(path.join(root, 'public/scripts/openai.js'), 'utf8');
    function extract(marker) {
        const start = source.indexOf(marker);
        assert.ok(start >= 0);
        const end = source.indexOf('\n}', start);
        assert.ok(end > start);
        return source.slice(start, end + 2).replace(/^export /, '');
    }
    let frontendCalls = 0;
    const deps = {
        oai_settings: {}, getChatCompletionModel: () => 'gemini-4-argon',
        createGenerationParameters: async () => ({ generate_data: generateData, stream: packet.stream }),
        eventSource: { emit: async () => {} }, event_types: { CHAT_COMPLETION_SETTINGS_READY: 'ready' },
        getRequestHeaders: () => ({'Content-Type': 'application/json'}),
        fetch: async (url, opts) => {
            frontendCalls++;
            return fetch(`http://127.0.0.1:${tavernServer.address().port}${url}`, opts);
        },
        checkQuotaError: () => {}, checkModerationError: () => {},
        toastr: { error: () => {} }, t: strings => strings.join(''),
    };
    const send = new Function(...Object.keys(deps),
        extract('export function tryParseStreamingError(') + '\n' +
        extract('async function sendOpenAIRequest(') + '\nreturn sendOpenAIRequest;')(...Object.values(deps));
    await assert.rejects(send('normal', generateData.messages, new AbortController().signal));
    assert.equal(frontendCalls, 1, 'SillyTavern frontend must end this generation');
    assert.equal(backendCalls, 1, 'SillyTavern backend must issue one POST');
    console.log('REAL_CLIENT_TERMINAL_ERROR_OK', packet.status, packet.stream, sdkCalls, backendCalls, frontendCalls);
} finally {
    await stop(tavernServer);
    await stop(upstreamServer);
}
'''
    result = subprocess.run(
        [NODE, '--input-type=module', '-e', script], capture_output=True, text=True,
        encoding='utf-8', timeout=30,
        env={**os.environ, 'SILLYTAVERN_ROOT': str(ROOT.resolve()), 'CLIENT_FIXTURE_DIR': str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'REAL_CLIENT_TERMINAL_ERROR_OK' in result.stdout
