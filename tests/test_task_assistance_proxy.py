"""Real loopback TLS termination + Chromium; no external sites or infrastructure."""

import asyncio
import shutil
import socket
import ssl
import subprocess
from contextlib import suppress

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from backend.tasks.interaction_transport import BrowserViewport
from backend.task_contracts import AssistanceRequest
from tests.test_task_assistance_security import scenario


@pytest.mark.asyncio
async def test_tls_reverse_proxy_real_viewer_controls_and_screenshot_fallback(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("local TLS certificate generation requires openssl")
    certificate, key = tmp_path / "lane003-cert.pem", tmp_path / "lane003-key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(certificate)],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    host, bindings, _, state = scenario()
    backend_socket = socket.socket()
    backend_socket.bind(("127.0.0.1", 0))
    backend_socket.listen(128)
    backend_port = backend_socket.getsockname()[1]
    backend = uvicorn.Server(uvicorn.Config(host.app, log_level="error", access_log=False,
                                          lifespan="off", proxy_headers=True, forwarded_allow_ips="127.0.0.1"))
    backend_task = None
    connections = set()
    proxy_tasks = set()

    async def proxy(reader, writer):
        task = asyncio.current_task()
        proxy_tasks.add(task)
        connections.add(writer)
        upstream = None
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            # Only this explicitly trusted loopback terminator supplies scheme.
            lines = headers.split(b"\r\n")
            lines = [line for line in lines if not line.lower().startswith((b"x-forwarded-", b"forwarded:"))]
            if b"upgrade: websocket" not in headers.lower():
                lines = [line for line in lines if not line.lower().startswith(b"connection:")]
                lines.insert(-2, b"Connection: close")
            headers = b"\r\n".join(lines[:-2]) + b"\r\nX-Forwarded-Proto: https\r\n\r\n"
            upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", backend_port)
            connections.add(upstream)
            upstream.write(headers)
            await upstream.drain()

            async def pump(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()

            pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(upstream_reader, writer))]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for item in pumps:
                    item.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    connections.discard(stream)
                    with suppress(ConnectionError, ssl.SSLError):
                        await stream.wait_closed()
            proxy_tasks.discard(task)

    proxy_server = await asyncio.start_server(proxy, "127.0.0.1", 0, ssl=tls)
    origin = f"https://127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}"
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                target = await browser.new_page(viewport={"width": 640, "height": 480})
                await target.route("https://lane003.test/**", lambda route: route.fulfill(content_type="text/html", body='<input id="secret" type="password" style="position:absolute;left:10px;top:10px;width:200px"><div style="height:2400px"></div>'))
                await target.goto("https://lane003.test/form")
                handle = BrowserViewport(target)
                bindings.executor.browser.return_value = handle
                await host.startup()
                grant = await bindings.assistance.mint(state["lease"].principal, "task_a", AssistanceRequest(interaction_id="pause_a", generation=3))
                base = "/help/" + grant.assistance_url.rsplit("/", 1)[1]
                backend_task = asyncio.create_task(backend.serve(sockets=[backend_socket]))
                while not backend.started:
                    if backend_task.done():
                        await backend_task
                    await asyncio.sleep(.01)
                context = await browser.new_context(ignore_https_errors=True)
                viewer = await context.new_page()
                requests, messages, sockets, frames = [], [], [], []
                viewer.on("request", lambda request: requests.append(request.url))
                viewer.on("console", lambda message: messages.append(message.text))

                def record_socket(socket):
                    sockets.append(socket.url)
                    socket.on("framereceived", lambda frame: frames.append(frame))

                viewer.on("websocket", record_socket)
                await viewer.goto(origin + base)
                image = viewer.locator("#screen")
                await expect(image).to_have_js_property("naturalWidth", 640)
                for _ in range(100):
                    if frames:
                        break
                    await asyncio.sleep(.02)
                assert frames and bytes(frames[0]).startswith(b"\xff\xd8")
                assert sockets == [origin.replace("https:", "wss:") + base + "/stream"]
                box = await image.bounding_box()
                await viewer.mouse.click(box["x"] + 20, box["y"] + 20)
                await viewer.locator("#text").fill("lane003-secret-canary")
                await viewer.locator("#type").click()
                await expect(target.locator("#secret")).to_have_value("lane003-secret-canary")
                # A drag and scroll go through the HTTPS Origin-checked input path.
                await viewer.mouse.move(box["x"] + 20, box["y"] + 30)
                await viewer.mouse.down()
                await viewer.mouse.move(box["x"] + 100, box["y"] + 80, steps=3)
                await viewer.mouse.up()
                await viewer.mouse.wheel(0, 400)
                await target.wait_for_function("scrollY > 0")
                # Close the actual WSS connection and prove PNG fallback refreshes.
                await viewer.evaluate("socket.close()")
                await expect(viewer.locator("#status")).to_contain_text("screenshot fallback")
                await expect(image).to_have_js_property("naturalWidth", 640)
                assert all(url.startswith(origin + "/") for url in requests if url.startswith(("https:", "http:")))
                assert any(url.endswith("/screen") for url in requests)
                assert all("lane003-secret-canary" not in message for message in messages)
                assert state["view"].state.value == "waiting_for_browser"
                await expect(viewer.locator("#resume")).to_be_enabled()
                assert await viewer.locator("#site").text_content() == "https://lane003.test/form"
                await viewer.locator("#resume").click()
                await expect(viewer.locator("#status")).to_contain_text("Decision accepted")
                assert state["revoked"]
                await context.close()
            finally:
                await browser.close()
    finally:
        proxy_server.close()
        await proxy_server.wait_closed()
        for stream in tuple(connections):
            stream.close()
        for task in tuple(proxy_tasks):
            task.cancel()
        await asyncio.gather(*tuple(proxy_tasks), return_exceptions=True)
        if backend_task:
            backend.should_exit = True
            await asyncio.wait_for(backend_task, 10)
        backend_socket.close()
        await host.shutdown()
