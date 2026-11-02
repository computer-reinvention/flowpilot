"""Actual dependency boundaries, using only synthetic local data and transports."""
import importlib.metadata
import ssl
import hashlib
import json
from pathlib import Path
import asyncio
import base64
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anyio
import pytest
from PIL import Image

import certifi
from cryptography import x509
from cryptography.x509.oid import NameOID


def test_certifi_revoked_root_removed_and_verification_stays_strict():
    assert importlib.metadata.version("certifi") == "2026.10.19"
    with open(certifi.where(), "rb") as source:
        certificates = x509.load_pem_x509_certificates(source.read())
    names = {
        value.value
        for certificate in certificates
        for value in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    }
    assert "emSign ECC Root CA - G3" not in names
    context = ssl.create_default_context(cafile=certifi.where())
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) > 100


def test_vendored_stylesheet_is_exact_licensed_release_without_cli():
    root = Path(__file__).resolve().parents[1]
    css = (root / 'frontend/src/shadcn-tailwind.css').read_bytes()
    assert len(css) == 16340
    assert hashlib.sha256(css).hexdigest() == '4c371f7a1ff5d219ae2f7ff28bd256b4346fd546fe46fbae22092e57db2f0fae'
    license_text = (root / 'frontend/src/shadcn-tailwind.LICENSE').read_text()
    assert 'MIT License' in license_text
    assert 'Copyright (c) 2023 shadcn' in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    package = json.loads((root / 'frontend/package.json').read_text())
    assert 'shadcn' not in package['devDependencies']
    lock = json.loads((root / 'frontend/package-lock.json').read_text())
    for name in ('shadcn', 'braces', 'micromatch', 'fast-glob', 'ts-morph'):
        assert not any(key.endswith('node_modules/' + name) for key in lock['packages'])
    assert '@import "./shadcn-tailwind.css";' in (root / 'frontend/src/index.css').read_text()
    assert 'COPY frontend/src/shadcn-tailwind.LICENSE /usr/share/licenses/shadcn-tailwind/LICENSE' in (root / 'Dockerfile.backend').read_text()


def test_exact_runtime_versions_and_mcp_schema_without_listener():
    expected = {'browser-use': '0.13.11', 'browser-harness': '0.1.13',
                'aiohttp': '3.14.4', 'anyio': '4.14.2', 'mcp': '2.2.0',
                'mcp-types': '2.2.0', 'pillow': '13.0.0', 'pypdf': '6.19.0'}
    for name, version in expected.items():
        assert importlib.metadata.version(name) == version
    # The optional upstream MCP CLI server intentionally reconfigures/disables all
    # process logging on import. Exercise it in its own process, not inside an ASGI
    # process (where Flowpilot never imports it) or the shared pytest interpreter.
    import subprocess
    import sys
    subprocess.run([sys.executable, '-c', '''
import socket
calls = []
def forbid(*args, **kwargs):
    calls.append(True)
    raise AssertionError('MCP import attempted network/listener')
socket.socket.bind = forbid
socket.socket.connect = forbid
import mcp.types as types
import browser_use.mcp.client
import browser_use.mcp.server
request = types.CallToolRequest.model_validate({'method': 'tools/call',
    'params': {'name': 'synthetic', 'arguments': {'value': 1}}})
assert request.params.arguments == {'value': 1}
result = types.CallToolResult(content=[types.TextContent(type='text', text='ok')])
assert types.CallToolResult.model_validate_json(result.model_dump_json()).content[0].text == 'ok'
assert not calls
'''], check=True, timeout=20, capture_output=True)


@pytest.mark.parametrize('codec', ['PNG', 'JPEG', 'GIF', 'WEBP', 'TIFF', 'BMP'])
def test_pillow_benign_codec_roundtrip_and_pixel_bound(codec, monkeypatch):
    image = Image.new('RGB', (32, 24), (230, 30, 10))
    encoded = io.BytesIO()
    image.save(encoded, format=codec)
    with Image.open(io.BytesIO(encoded.getvalue())) as decoded:
        decoded.load()
        assert decoded.size == (32, 24) and decoded.format == codec
        pixel = decoded.convert('RGB').getpixel((16, 12))
        assert pixel[0] > 200 and pixel[1] < 60
    # A deliberately smaller limit proves rejection, not hostile codec fuzz coverage.
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)
    with pytest.raises(Image.DecompressionBombError):
        Image.open(io.BytesIO(encoded.getvalue()))


@pytest.mark.asyncio
async def test_anyio_cancellation_and_strict_loopback_tls(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'local.pem', tmp_path / 'local.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))

    async def echo(reader, writer):
        try:
            value = await reader.read(32)
            writer.write(value)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(echo, '127.0.0.1', 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    try:
        with anyio.fail_after(3):
            stream = await anyio.connect_tcp('127.0.0.1', port, tls=True,
                tls_hostname='localhost', ssl_context=client_context)
            await stream.send(b'tls-ok')
            assert await stream.receive(32) == b'tls-ok'
            await stream.aclose()
        with pytest.raises(ssl.SSLCertVerificationError):
            await anyio.connect_tcp('127.0.0.1', port, tls=True,
                tls_hostname='wrong.invalid', ssl_context=client_context)
        settled = anyio.Event()

        async def cancellable():
            try:
                await anyio.sleep_forever()
            finally:
                settled.set()

        async with anyio.create_task_group() as group:
            group.start_soon(cancellable)
            await anyio.sleep(0)
            group.cancel_scope.cancel()
        assert settled.is_set()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_actual_chromium_screenshots_aiohttp_discovery_and_history_gif(tmp_path):
    from playwright.async_api import async_playwright
    from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
    from browser_use.browser.python_highlights import create_highlighted_screenshot
    from browser_use.agent.gif import create_history_gif
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True,
            args=[f'--remote-debugging-port={port}', '--disable-background-networking'])
        try:
            assert await LocalBrowserWatchdog._wait_for_cdp_url(port, timeout=3) == f'http://127.0.0.1:{port}/'
            page = await browser.new_page(viewport={'width': 640, 'height': 480})
            await page.set_content('<button style="background:red">Synthetic screenshot</button>')
            frames = []
            for kind in ('png', 'jpeg'):
                raw = await page.screenshot(type=kind)
                with Image.open(io.BytesIO(raw)) as decoded:
                    assert decoded.size == (640, 480)
                    assert decoded.format == ('PNG' if kind == 'png' else 'JPEG')
                    resized = decoded.resize((320, 240), Image.Resampling.LANCZOS)
                    assert resized.size == (320, 240)
                encoded = base64.b64encode(raw).decode()
                highlighted = await create_highlighted_screenshot(encoded, {})
                with Image.open(io.BytesIO(base64.b64decode(highlighted))) as image:
                    assert image.format == 'PNG'  # JPEG fallback would fail this
                frames.append(encoded)
                await page.evaluate("document.body.style.background='blue'")
            history = SimpleNamespace(history=[SimpleNamespace(
                state=SimpleNamespace(url='http://synthetic.invalid', get_screenshot=lambda: frames[0]),
                model_output=SimpleNamespace(current_state=SimpleNamespace(next_goal='Synthetic goal')))
                for _ in frames], screenshots=lambda **_: frames)
            target = tmp_path / 'history.gif'
            create_history_gif('Synthetic task', history, output_path=str(target), show_logo=False)
            with Image.open(target) as gif:
                assert gif.format == 'GIF' and gif.n_frames >= 2
                gif.seek(1)
                assert gif.convert('RGB').size == (640, 480)
        finally:
            await browser.close()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        unused = sock.getsockname()[1]
    with pytest.raises(TimeoutError):
        await LocalBrowserWatchdog._wait_for_cdp_url(unused, timeout=0.15)


@pytest.mark.asyncio
async def test_browser_use_real_pdf_extraction_and_output_budget(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    from browser_use.filesystem.file_system import FileSystem
    fs = FileSystem(tmp_path / 'workspace', create_default_files=False)
    pdf = tmp_path / 'synthetic.pdf'
    canvas = Canvas(str(pdf))
    canvas.drawString(10, 700, 'Synthetic receipt 42')
    canvas.save()
    result = await fs.read_file(str(pdf), external_file=True)
    assert 'Synthetic receipt 42' in result and '1 pages' in result
    large = tmp_path / 'large.pdf'
    canvas = Canvas(str(large))
    for page in range(24):
        text = canvas.beginText(10, 700)
        for line in range(30):
            text.textLine(f'Page {page} row {line} synthetic output budget ' + 'receipt ' * 12)
        canvas.drawText(text)
        canvas.showPage()
    canvas.save()
    result = await fs.read_file(str(large), external_file=True)
    assert 'chars total' in result and ('truncated' in result or 'Skipped pages' in result)
    assert len(result) < 62000
    malformed = tmp_path / 'invalid.pdf'
    malformed.write_bytes(b'%PDF-1.7\nnot a valid document\n%%EOF')
    assert 'Error: Could not read file' in await fs.read_file(str(malformed), external_file=True)
    # The upstream 60k limit is OUTPUT ONLY: it parses all pages synchronously.
    # This test deliberately does not claim bounded hostile-parser CPU/memory.


def test_optional_video_absence_is_explicit(tmp_path):
    from browser_use.browser import video_recorder
    if video_recorder.IMAGEIO_AVAILABLE:
        pytest.skip('optional video codec installed; exercised by separate codec test')
    recorder = video_recorder.VideoRecorderService(tmp_path / 'absent.mp4',
        {'width': 32, 'height': 32}, 5)
    recorder.start()
    assert not recorder._is_active
    assert not (tmp_path / 'absent.mp4').exists()


def test_optional_video_actual_encoding_with_auxiliary_tools(tmp_path):
    """Run additionally with isolated video extra tools on PYTHONPATH.

    Production has no video extra; this does not silently enable recording there.
    Exercises browser-use's actual Pillow resize/pad -> ndarray -> ffmpeg pipeline.
    """
    import os
    if os.environ.get('FLOWPILOT_TEST_VIDEO') != '1':
        pytest.skip('explicit auxiliary video compatibility run required')
    import imageio.v2 as imageio
    from browser_use.browser import video_recorder
    assert video_recorder.IMAGEIO_AVAILABLE
    output = tmp_path / 'synthetic.mp4'
    recorder = video_recorder.VideoRecorderService(output, {'width': 33, 'height': 25}, 5)
    recorder.start()
    assert recorder._is_active
    for color in ('red', 'blue', 'green'):
        for kind in ('PNG', 'JPEG'):
            image = Image.new('RGB', (65, 49), color)
            buffer = io.BytesIO()
            image.save(buffer, format=kind)
            recorder.add_frame(base64.b64encode(buffer.getvalue()).decode())
    recorder.stop_and_save()
    assert not recorder._is_active and output.stat().st_size > 100
    reader = imageio.get_reader(str(output))
    try:
        assert reader.count_frames() == 6
        assert reader.get_data(0).shape == (32, 48, 3)
    finally:
        reader.close()
