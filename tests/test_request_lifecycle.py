import asyncio
import json
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from auth import get_api_key
from request_lifecycle import (
    DisconnectMiddleware, GenerationJob, JobRequest, active_jobs, finished_jobs, shutdown_jobs,
)
from resumable import ResumableStore
from routes import chat_api


BODY = {'model': 'gemini-4-argon', 'messages': [{'role': 'user', 'content': 'hello'}]}


class Connection:
    """Real ASGI disconnect events; unlike TestClient, never buffers the whole SSE reply."""

    def __init__(self, app, *, body=None, method='POST', path='/v1/chat/completions',
                 key='A', fail_send=False):
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()
        self.messages = []
        self.fail_send = fail_send
        url = urlsplit(path)
        raw = json.dumps(body if body is not None else BODY).encode()
        self.input.put_nowait({'type': 'http.request', 'body': raw, 'more_body': False})
        self.scope = {
            'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': url.path,
            'raw_path': url.path.encode(), 'root_path': '', 'query_string': url.query.encode(),
            'headers': [(b'content-type', b'application/json'), (b'authorization', f'Bearer {key}'.encode())],
            'server': ('test', 80), 'client': ('127.0.0.1', 12345),
        }
        self.task = asyncio.create_task(app(self.scope, self.input.get, self.send))

    async def send(self, message):
        if self.fail_send and message['type'] == 'http.response.body':
            raise OSError('client socket closed')
        self.messages.append(message)
        await self.output.put(message)

    async def start_headers(self):
        while True:
            message = await asyncio.wait_for(self.output.get(), 1)
            if message['type'] == 'http.response.start':
                return dict(message['headers'])

    async def disconnect(self):
        self.input.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(self.task, 1)

    async def finish(self):
        await asyncio.wait_for(self.task, 1)
        start = next(m for m in self.messages if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in self.messages if m['type'] == 'http.response.body')
        return start['status'], body


@pytest.fixture
def app(monkeypatch):
    active_jobs.clear()
    finished_jobs.clear()
    result = FastAPI()

    async def test_key(authorization: str = Header(default='')):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401)
        return authorization[7:]

    result.dependency_overrides[get_api_key] = test_key
    result.include_router(chat_api.router)
    result.add_middleware(DisconnectMiddleware)
    monkeypatch.setattr(chat_api, 'resumable_store', ResumableStore())
    yield result
    assert not active_jobs


async def eventually(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('stream', [False, True])
def test_disconnect_cancels_waiting_upstream_before_headers(app, monkeypatch, isolated_state, buffered, stream):
    isolated_state.update_settings({'resumable_enabled': buffered})

    async def scenario():
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def upstream(request, connection):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app, body={**BODY, 'stream': stream})
        await asyncio.wait_for(started.wait(), 1)
        job = next(iter(active_jobs.values()))
        await connection.disconnect()
        assert cleaned.is_set()
        assert job.task.cancelled() and job.done.is_set()
        assert job.cancelled.is_set()
        assert not active_jobs

    asyncio.run(scenario())


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('send_failure', [False, True])
def test_stream_disconnect_or_failed_write_closes_iterator(
    app, monkeypatch, isolated_state, buffered, send_failure,
):
    isolated_state.update_settings({'resumable_enabled': buffered})

    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()

        async def frames():
            try:
                entered.set()
                yield 'data: {"content":"first"}\n\n'
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def upstream(*args):
            return StreamingResponse(frames(), media_type='text/event-stream')

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app, body={**BODY, 'stream': True}, fail_send=send_failure)
        await connection.start_headers()
        await entered.wait()
        if send_failure:
            await asyncio.wait_for(connection.task, 1)
        else:
            await connection.disconnect()
        assert closed.is_set()
        assert not active_jobs

    asyncio.run(scenario())


def test_shared_json_job_only_stops_after_last_connection_leaves(app, monkeypatch):
    async def scenario():
        started, cleaned = asyncio.Event(), asyncio.Event()
        calls = 0

        async def upstream(*args):
            nonlocal calls
            calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        first = Connection(app)
        await started.wait()
        job = next(iter(active_jobs.values()))
        second = Connection(app)
        await eventually(lambda: len(job.clients) == 2)
        await first.disconnect()
        assert not cleaned.is_set() and not job.cancelled.is_set()
        assert len(job.clients) == 1
        await second.disconnect()
        assert cleaned.is_set() and calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('reconnect', [False, True])
def test_reconnection_grace_expiry_and_reuse(app, monkeypatch, isolated_state, reconnect):
    monkeypatch.setattr(isolated_state, 'get_disconnect_grace_seconds', lambda: 0.08)

    async def scenario():
        started, finish, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def upstream(*args):
            nonlocal calls
            calls += 1
            started.set()
            try:
                await finish.wait()
                return JSONResponse({'choices': []})
            finally:
                cleaned.set()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        first = Connection(app)
        await started.wait()
        job = next(iter(active_jobs.values()))
        await first.disconnect()
        assert not job.cancelled.is_set() and not cleaned.is_set()
        if reconnect:
            second = Connection(app)
            await eventually(lambda: len(job.clients) == 1)
            await asyncio.sleep(0.1)
            assert not job.cancelled.is_set()
            finish.set()
            status, body = await second.finish()
            assert status == 200 and json.loads(body) == {'choices': []}
        else:
            await asyncio.wait_for(job.done.wait(), 1)
            assert job.cancelled.is_set()
            buf = await chat_api.resumable_store.get(job.id)
            assert buf.status == 'cancelled' and buf.status_code == 499
        assert calls == 1 and cleaned.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize('buffered', [False, True])
def test_explicit_stop_is_owned_immediate_and_idempotent(app, monkeypatch, isolated_state, buffered):
    isolated_state.update_settings({'resumable_enabled': buffered, 'continue_on_disconnect': True,
                                    'disconnect_grace_seconds': 300})

    async def scenario():
        closed = asyncio.Event()

        async def frames():
            try:
                yield 'data: {"content":"first"}\n\n'
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def upstream(*args):
            return StreamingResponse(frames(), media_type='text/event-stream')

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app, body={**BODY, 'stream': True})
        headers = await connection.start_headers()
        request_id = headers[b'x-request-id'].decode()
        wrong = Connection(app, method='DELETE', path=f'/v1/requests/{request_id}', key='B')
        assert (await wrong.finish())[0] == 404
        assert not closed.is_set()
        for _ in range(2):
            stop = Connection(app, method='DELETE', path=f'/v1/requests/{request_id}')
            status, body = await stop.finish()
            assert status == 200 and json.loads(body)['cancelled'] is True
        await connection.finish()
        assert closed.is_set()
        assert b'request_cancelled' in b''.join(m.get('body', b'') for m in connection.messages)
        if buffered:
            wrong_poll = Connection(app, method='GET', path=f'/v1/resumable/{request_id}', key='B')
            assert (await wrong_poll.finish())[0] == 404

    asyncio.run(scenario())


def test_cancel_nonstream_using_original_body_before_headers(app, monkeypatch):
    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()

        async def upstream(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app)
        await started.wait()
        stop = Connection(app, path='/v1/chat/completions/cancel')
        assert (await stop.finish())[0] == 200
        status, body = await connection.finish()
        assert status == 499 and json.loads(body)['error']['type'] == 'request_cancelled'
        assert closed.is_set()

    asyncio.run(scenario())


def test_cancel_during_backoff_does_not_retry_and_closes_registered_clients(app, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        closed = []

        class Client:
            def __init__(self):
                self.aio = SimpleNamespace(aclose=self.aclose)

            async def aclose(self):
                closed.append('async')

            def close(self):
                closed.append('sync')

        calls = 0

        async def upstream(request, connection):
            nonlocal calls
            connection.register_upstream_client(Client())
            for _ in range(10):
                calls += 1
                started.set()
                await asyncio.sleep(30)

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app)
        await started.wait()
        await connection.disconnect()
        assert calls == 1 and closed == ['async', 'sync']

    asyncio.run(scenario())


def test_cancel_before_worker_starts_still_finishes_and_wakes_waiters(capsys):
    async def scenario():
        called = False

        async def work():
            nonlocal called
            called = True

        job = GenerationJob('pre-start', 'A')
        job.start(work)
        await job.cancel()
        assert not called and job.ready.is_set() and job.done.is_set()
        assert not active_jobs

    asyncio.run(scenario())
    assert capsys.readouterr().out.count('[请求取消]') == 1


def test_failed_buffer_does_not_cache_error_across_new_attempts(app, monkeypatch):
    async def scenario():
        calls = 0

        async def upstream(*args):
            nonlocal calls
            calls += 1
            return JSONResponse({'choices': []}, status_code=429 if calls == 1 else 200)

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        first = Connection(app, body={**BODY, 'stream': True})
        assert (await first.finish())[0] == 429
        second = Connection(app, body={**BODY, 'stream': True})
        assert (await second.finish())[0] == 200
        assert calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize('buffered', [False, True])
def test_normal_response_during_slow_resource_cleanup_is_not_cancelled(
    app, monkeypatch, isolated_state, buffered,
):
    isolated_state.update_settings({'resumable_enabled': buffered})

    async def scenario():
        closing, finish_close = asyncio.Event(), asyncio.Event()

        async def close():
            closing.set()
            await finish_close.wait()

        async def upstream(request, connection):
            connection.register_upstream_client(SimpleNamespace(aio=SimpleNamespace(aclose=close)))
            return JSONResponse({'choices': []})

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app)
        assert (await connection.finish())[0] == 200
        await closing.wait()
        job = next(iter(active_jobs.values()))
        assert not job.cancelled.is_set()
        finish_close.set()
        await asyncio.wait_for(job.done.wait(), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize('stream', [False, True])
def test_main_app_middleware_stack_preserves_disconnect_and_cancel_stats(
    app, monkeypatch, isolated_state, stream,
):
    import main
    from logger import stats

    monkeypatch.setattr(main.app, 'dependency_overrides', app.dependency_overrides.copy())

    async def scenario():
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def upstream(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        errors = stats.get_json_stats()['error']
        connection = Connection(main.app, body={**BODY, 'stream': stream})
        await started.wait()
        await connection.disconnect()
        assert cleaned.is_set()
        assert stats.get_json_stats()['error'] == errors

    asyncio.run(scenario())


def test_sdk_fake_stream_cancellation_drains_the_child_api_task(app, monkeypatch):
    import api_helpers
    from models import OpenAIRequest

    async def scenario():
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

        async def upstream(request, connection):
            frames = api_helpers.gemini_fake_stream_generator(
                client, request.model, [], {}, request, False, connection,
            )
            return StreamingResponse(frames, media_type='text/event-stream')

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        connection = Connection(app, body={**BODY, 'stream': True})
        await started.wait()
        await connection.disconnect()
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_studio_disconnect_closes_actual_httpx_response_stream(app, monkeypatch, isolated_state):
    import httpx
    from unittest.mock import AsyncMock
    from upstreams import cookie_proxy

    isolated_state._state['use_web_proxy'] = True

    async def scenario():
        reading, closed = asyncio.Event(), asyncio.Event()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set()
                await asyncio.Event().wait()
                yield b''

            async def aclose(self):
                closed.set()

        real_client = httpx.AsyncClient

        def make_client(**kwargs):
            return real_client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Body())), **kwargs)

        monkeypatch.setattr(cookie_proxy.httpx, 'AsyncClient', make_client)
        monkeypatch.setattr(cookie_proxy, '_get_cookie_string', lambda: 'TEST_COOKIE')
        monkeypatch.setattr(cookie_proxy, '_get_project_id', lambda: 'TEST_PROJECT')
        monkeypatch.setattr(cookie_proxy, 'build_headers', lambda _: {'cookie': 'TEST_COOKIE'})
        monkeypatch.setattr(cookie_proxy, 'build_batch_graphql_body_async',
                            AsyncMock(return_value={'variables': {}}))
        connection = Connection(app)
        await asyncio.wait_for(reading.wait(), 1)
        await connection.disconnect()
        assert closed.is_set()

    asyncio.run(scenario())


def test_replacement_does_not_interrupt_old_cleanup_or_cancel_new_generation(app, monkeypatch):
    async def scenario():
        entered, cleaning, allow_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def upstream(*args):
            nonlocal calls
            calls += 1
            current = calls
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                if current == 1:
                    cleaning.set()
                    await allow_cleanup.wait()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        first = Connection(app)
        await entered.wait()
        old = next(iter(active_jobs.values()))
        first.input.put_nowait({'type': 'http.disconnect'})
        await cleaning.wait()
        second = Connection(app)
        await eventually(lambda: calls == 2)
        new = next(iter(active_jobs.values()))
        assert new is not old and not new.cancelled.is_set()
        assert not old.done.is_set()
        allow_cleanup.set()
        await first.finish()
        assert active_jobs.get(new.id) is new
        await second.disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('stream', [False, True])
def test_real_http_disconnect_stops_upstream(app, monkeypatch, isolated_state, buffered, stream):
    """Actual localhost TCP + Uvicorn, not just mocked ASGI disconnect signals."""
    import httpx
    import uvicorn

    isolated_state.update_settings({'resumable_enabled': buffered})

    async def scenario():
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def frames():
            try:
                started.set()
                yield 'data: {"content":"first"}\n\n'
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def upstream(*args):
            if stream:
                return StreamingResponse(frames(), media_type='text/event-stream')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(chat_api, '_call_upstream', upstream)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen(16)
        address = f'http://127.0.0.1:{sock.getsockname()[1]}/v1/chat/completions'
        server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='critical', access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await eventually(lambda: server.started)
            async with httpx.AsyncClient(trust_env=False) as client:
                if stream:
                    async with client.stream('POST', address, json={**BODY, 'stream': True},
                                             headers={'Authorization': 'Bearer A'}) as response:
                        assert response.status_code == 200
                        await asyncio.wait_for(started.wait(), 1)
                        async for line in response.aiter_lines():
                            if line:
                                break
                else:
                    call = asyncio.create_task(client.post(address, json=BODY,
                                                            headers={'Authorization': 'Bearer A'}))
                    await asyncio.wait_for(started.wait(), 1)
                    call.cancel()
                    await asyncio.gather(call, return_exceptions=True)
                await asyncio.wait_for(cleaned.wait(), 1)
                assert not active_jobs
        finally:
            await shutdown_jobs()
            server.should_exit = True
            await asyncio.wait_for(serving, 3)
            sock.close()

    asyncio.run(scenario())
