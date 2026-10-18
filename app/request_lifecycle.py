"""Connection observation and cooperative cancellation shared by both upstreams."""

import asyncio
import hashlib
import hmac
import inspect
import time
from collections import OrderedDict
from contextlib import suppress

import anyio
from fastapi.responses import StreamingResponse


class ClientDisconnected(Exception):
    pass


class DisconnectMiddleware:
    """Own ASGI receive once; expose disconnect without racing body/SSE readers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/v1/chat/completions":
            return await self.app(scope, receive, send)
        disconnected = asyncio.Event()
        scope.setdefault("state", {})["client_disconnected"] = disconnected
        messages = asyncio.Queue(maxsize=1)

        async def observe():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return
                await messages.put(message)

        async def observed_receive():
            if not messages.empty():
                return messages.get_nowait()
            if disconnected.is_set():
                return {"type": "http.disconnect"}
            item = asyncio.create_task(messages.get())
            gone = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait((item, gone), return_when=asyncio.FIRST_COMPLETED)
                return item.result() if item.done() else {"type": "http.disconnect"}
            finally:
                await cancel_and_wait(item, gone)

        async def observed_send(message):
            try:
                await send(message)
            except OSError:
                disconnected.set()
                raise

        observer = asyncio.create_task(observe())
        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            await cancel_and_wait(observer)


async def cancel_and_wait(*tasks):
    """Drain owned tasks even when Starlette's enclosing cancel scope has fired."""
    with anyio.CancelScope(shield=True):
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)


async def wait_disconnected(request):
    signal = getattr(getattr(request, "state", None), "client_disconnected", None)
    if isinstance(signal, asyncio.Event):
        await signal.wait()
        return
    # Also supports direct unit calls / embedders without the ASGI middleware.
    while not await request.is_disconnected():
        await asyncio.sleep(0.05)


async def while_connected(awaitable, request):
    operation = asyncio.ensure_future(awaitable)
    watcher = asyncio.create_task(wait_disconnected(request))
    try:
        await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
        if operation.done():
            return operation.result()
        watcher.result()
        raise ClientDisconnected()
    finally:
        await cancel_and_wait(operation, watcher)


def owner_digest(api_key):
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


active_jobs = {}
finished_jobs = OrderedDict()


def finished_job(request_id):
    record = finished_jobs.get(request_id)
    if record is not None and time.monotonic() - record["at"] > 300:
        finished_jobs.pop(request_id, None)
        return None
    return record


class GenerationJob:
    """One upstream task, potentially shared by several connected consumers."""

    def __init__(self, request_id, api_key, grace_seconds=0, on_cancel=None):
        self.id = request_id
        self.owner = owner_digest(api_key)
        self.grace_seconds = grace_seconds
        self.on_cancel = on_cancel
        self.task = None
        self.idle_task = None
        self.clients = set()
        self.cancelled = asyncio.Event()
        self.done = asyncio.Event()
        self.produced = asyncio.Event()     # Generation complete; transport cleanup may still be running.
        self.ready = asyncio.Event()
        self.response = None
        self.reason = ""
        self._cancel_notified = False
        self._cancel_lock = asyncio.Lock()
        self.resources = []

    def owned_by(self, api_key):
        return hmac.compare_digest(self.owner, owner_digest(api_key))

    def attach(self):
        token = object()
        self.clients.add(token)
        if not self.cancelled.is_set() and self.idle_task is not None and not self.idle_task.done():
            self.idle_task.cancel()
        return token

    def start(self, work):
        async def run():
            try:
                await work()
                self.produced.set()
            except asyncio.CancelledError:
                self.cancelled.set()
                self.reason = self.reason or "服务停止，请求已取消。"
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    for client in reversed(self.resources):
                        for target, method in ((getattr(client, "aio", None), "aclose"), (client, "close")):
                            close = getattr(target, method, None)
                            if close is not None:
                                with suppress(Exception):
                                    result = close()
                                    if inspect.isawaitable(result):
                                        await result
                    if self.cancelled.is_set():
                        await self._notify_cancel()
                    elif self.idle_task is not None:
                        self.idle_task.cancel()
                    self._finish()

        active_jobs[self.id] = self
        self.task = asyncio.create_task(run(), name=f"upstream:{self.id}")
        return self.task

    def _finish(self):
        self.ready.set()
        self.done.set()
        current = active_jobs.get(self.id)
        record = finished_jobs.get(self.id)
        if current is not None and current is not self:
            return
        if current is None and record is not None and record["generation"] != id(self):
            return
        if current is self:
            active_jobs.pop(self.id, None)
        finished_jobs[self.id] = {
            "owner": self.owner, "cancelled": self.cancelled.is_set(), "at": time.monotonic(),
            "generation": id(self),
        }
        finished_jobs.move_to_end(self.id)
        while len(finished_jobs) > 1024:
            finished_jobs.popitem(last=False)

    async def _notify_cancel(self):
        if self._cancel_notified:
            return
        self._cancel_notified = True
        if self.on_cancel is not None:
            await self.on_cancel(self.reason)
        print(f"ℹ️ [请求取消] {self.id} | {self.reason}")

    async def cancel(self, reason="调用方已停止生成，请求已取消。"):
        with anyio.CancelScope(shield=True):
            async with self._cancel_lock:
                if self.done.is_set():
                    return self.cancelled.is_set()
                if self.produced.is_set() and not self.cancelled.is_set():
                    return False
                already_cancelled = self.cancelled.is_set()
                self.reason = self.reason or reason
                self.cancelled.set()
                if already_cancelled:
                    if self.task is not None:
                        await asyncio.gather(self.task, return_exceptions=True)
                else:
                    await cancel_and_wait(self.task)
                # A task cancelled before its first instruction skips run()'s finally.
                await self._notify_cancel()
                self._finish()
                return True

    async def release(self, token):
        if token not in self.clients:
            return
        self.clients.discard(token)
        if self.clients or self.done.is_set() or self.produced.is_set():
            return
        if self.grace_seconds <= 0:
            await self.cancel("调用方连接已断开，上游请求及重试已取消。")
            return

        async def expire():
            await asyncio.sleep(self.grace_seconds)
            # No await between checking the last consumer and starting cancel().
            if not self.clients and not self.done.is_set():
                await self.cancel("重连宽限已到期，上游请求及重试已取消。")

        self.idle_task = asyncio.create_task(expire(), name=f"reconnect:{self.id}")


class JobRequest:
    """Upstream sees job cancellation, never a particular shared reader's socket."""

    def __init__(self, request, job):
        self.request = request
        self.job = job

    async def is_disconnected(self):
        return self.job.cancelled.is_set()

    def register_upstream_client(self, client):
        if all(client is not other for other in self.job.resources):
            self.job.resources.append(client)
        return client

    def __getattr__(self, name):
        return getattr(self.request, name)


class ManagedStreamingResponse(StreamingResponse):
    """A connection lease lasts through response sending, including failed writes."""

    def __init__(self, content, request, job, token, **kwargs):
        super().__init__(content, **kwargs)
        self.request = request
        self.job = job
        self.token = token

    async def __call__(self, scope, receive, send):
        try:
            await while_connected(self.stream_response(send), self.request)
        except (ClientDisconnected, OSError):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    with suppress(Exception):
                        await close()
                await self.job.release(self.token)
                if self.background is not None:
                    await self.background()


async def shutdown_jobs():
    jobs = list(active_jobs.values())
    await asyncio.gather(*(job.cancel("服务停止，请求已取消。") for job in jobs))
    await asyncio.gather(*(job.task for job in jobs if job.task is not None), return_exceptions=True)
    await cancel_and_wait(*(job.idle_task for job in jobs))
