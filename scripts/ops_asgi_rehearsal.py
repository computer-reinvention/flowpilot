"""Real ASGI server receive limits against a fake app; no task/provider dependencies."""
import asyncio
import contextlib
import io
import json
import logging
from pathlib import Path
import socket
import threading
import time

import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


received = []


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            value = message.get("text") or message.get("bytes") or ""
            received.append(len(value))
            await send({"type": "websocket.send", "text": "accepted"})


async def probe(port):
    url = f"ws://127.0.0.1:{port}/api/v1/browser-tasks/help/ASGI_CAPABILITY_CANARY/stream"
    async with connect(url) as connection:
        await connection.send("x" * 65536)
        assert await connection.recv() == "accepted"
    for message in ("x" * 65537, ["x" * 32769, "x" * 32769]):
        async with connect(url) as connection:
            await connection.send(message)
            try:
                await connection.recv()
                raise AssertionError("oversized assembled WebSocket command reached application")
            except ConnectionClosed as exc:
                assert exc.rcvd and exc.rcvd.code == 1009
    assert received == [65536], received


def main():
    stream = io.StringIO()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, access_log=False, ws="websockets", ws_max_size=65536, ws_max_queue=16, forwarded_allow_ips="127.0.0.1", log_config=str(Path(__file__).with_name("ops_logging.json"))))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for attempt in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        with contextlib.redirect_stderr(stream), contextlib.redirect_stdout(stream):
            asyncio.run(probe(port))
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    # uvicorn's websocket INFO messages contain secret-bearing paths unless disabled!
    assert "ASGI_CAPABILITY_CANARY" not in stream.getvalue(), "ASGI WebSocket logger leaks capability path"
    print("PASS: real ASGI 65536-byte assembled/fragmented receive bound and capability-free server logs")


if __name__ == "__main__":
    main()
