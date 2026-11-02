"""Run bounded, networkless checks against an already-built dependency test image.

No build, host Docker socket, credentials, host settings, or unrelated mounts.
The overlay inherits the production app; /verify/backend points to /app/backend.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='flowpilot-lane005-deps:offline-tests')
    parser.add_argument('--suite', choices=('full', 'focused', 'video', 'asgi', 'frontend', 'provenance'), default='full')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    command = ['docker', 'run', '--rm', '--platform', 'linux/amd64',
               '--label', 'flowpilot.ops=lane005-deps', '--network', 'none',
               '--cpus', '2', '--memory', '2g', '--pids-limit', '1024', '--shm-size', '256m',
               '--tmpfs', '/app/profiles:uid=10001,gid=10001,mode=0700,size=256m']
    for path in ('tests', 'scripts', 'frontend', 'pyproject.toml', 'uv.lock', 'Dockerfile.backend'):
        command += ['--mount', f'type=bind,source={root / path},target=/verify/{path},readonly']
    command += ['--entrypoint', '/app/.venv/bin/python', args.image]
    if args.suite == 'provenance':
        expected = {
            'backend': {str(p.relative_to(root / 'backend')): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (root / 'backend').rglob('*.py')},
            'frontend': {str(p.relative_to(root / 'frontend/dist')): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (root / 'frontend/dist').rglob('*') if p.is_file()},
        }
        command += ['-c', '''
import hashlib, importlib.metadata as metadata, json, os, pathlib, tomllib
expected = json.loads(''' + repr(json.dumps(expected)) + ''')
assert os.getuid() == 10001
for name in ('pyproject.toml', 'uv.lock'):
    assert pathlib.Path('/app', name).read_bytes() == pathlib.Path('/verify', name).read_bytes(), name
lock = tomllib.loads(pathlib.Path('/app/uv.lock').read_text())
locked = {(p['name'].lower().replace('_', '-'), p['version']) for p in lock['package']}
installed = {(d.metadata['Name'].lower().replace('_', '-'), d.version) for d in metadata.distributions()}
assert installed <= locked, installed - locked
for name, version in {'browser-use':'0.13.11', 'browser-harness':'0.1.13',
    'aiohttp':'3.14.4', 'anyio':'4.14.2', 'certifi':'2026.10.19',
    'mcp':'2.2.0', 'mcp-types':'2.2.0', 'pillow':'13.0.0', 'pypdf':'6.19.0'}.items():
    assert metadata.version(name) == version
for directory, hashes in expected.items():
    actual_root = pathlib.Path('/app', directory)
    actual = {str(p.relative_to(actual_root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in actual_root.rglob('*.py' if directory == 'backend' else '*') if p.is_file()}
    assert actual == hashes, directory
assert pathlib.Path('/usr/share/licenses/shadcn-tailwind/LICENSE').read_bytes() == pathlib.Path('/verify/frontend/src/shadcn-tailwind.LICENSE').read_bytes()
print(json.dumps({'image_uid':os.getuid(), 'installed_distributions_match_frozen_lock':len(installed),
    'backend_source_files_matched':len(expected['backend']), 'frontend_dist_files_matched':len(expected['frontend']),
    'exact_manifests_and_runtime_license':'PASS'}))
''']
    elif args.suite in ('full', 'focused', 'video'):
        command += ['-m', 'pytest', '-q', '-p', 'no:cacheprovider']
        if args.suite == 'full':
            command += ['-m', 'not live']
        elif args.suite == 'focused':
            command += ['tests/test_dependency_compatibility.py', 'tests/test_observation_chromium.py',
                        'tests/test_task_assistance_proxy.py', 'tests/test_task_control_retention.py']
        else:
            command += ['tests/test_dependency_compatibility.py::test_optional_video_actual_encoding_with_auxiliary_tools']
    elif args.suite == 'asgi':
        command += ['/verify/scripts/ops_asgi_rehearsal.py']
    else:
        # This wrapper is only a disposable headless test fixture, not runtime
        # sandbox configuration. No access to personal Chrome profiles or tabs.
        command += ['-c', '''
import os, subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    executable = p.chromium.executable_path
wrapper = Path('/tmp/flowpilot-deps-chromium')
wrapper.write_text(''' + repr('''#!/app/.venv/bin/python
import os, signal, subprocess, sys
child = subprocess.Popen([EXECUTABLE, '--no-sandbox', '--disable-dev-shm-usage', *sys.argv[1:]], start_new_session=True)
def stop(signum, frame):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    code = child.wait()
finally:
    # Reap only this fixture's Chromium process group before node removes its
    # temporary profile. Root Chrome can exit while renderer children still write.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
sys.exit(code if code >= 0 else 128 - code)
''') + '''.replace('EXECUTABLE', repr(executable)))
wrapper.chmod(0o700)
subprocess.run(['node', '--test', 'frontend/tests/accountHttp.test.mjs',
    'frontend/tests/browserTasks.test.mjs', 'frontend/tests/ui.test.mjs'],
    check=True, env={**os.environ, 'CHROME_BIN': str(wrapper)}, timeout=120)
''']
    subprocess.run(command, check=True, timeout=1200)


if __name__ == '__main__':
    main()
