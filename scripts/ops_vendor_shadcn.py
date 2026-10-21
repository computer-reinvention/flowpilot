"""Reproduce unmodified, licensed CSS from the exact formerly locked npm release.

Explicit networked maintenance command, not invoked by application or test startup.
No CLI JavaScript is extracted or executed. Refuses unknown bytes before writing.
"""
import base64
import hashlib
import io
from pathlib import Path
import tarfile
from urllib.request import urlopen

URL = 'https://registry.npmjs.org/shadcn/-/shadcn-4.22.0.tgz'
INTEGRITY = 'QKR3XUnatWlgo8IbZqGHrpsxjgdsfpPSAvJvLntQqAxPEf5U2sNsjDYLAS2xmFrOkC9lpj5UpZbrh/BuHrE2CQ=='
CSS_SHA256 = '4c371f7a1ff5d219ae2f7ff28bd256b4346fd546fe46fbae22092e57db2f0fae'


def main():
    with urlopen(URL, timeout=60) as response:
        archive = response.read(1024 * 1024)
    assert base64.b64encode(hashlib.sha512(archive).digest()).decode() == INTEGRITY
    with tarfile.open(fileobj=io.BytesIO(archive)) as release:
        css = release.extractfile('package/dist/tailwind.css').read()
        license_text = release.extractfile('package/LICENSE.md').read()
    assert hashlib.sha256(css).hexdigest() == CSS_SHA256
    assert len(css) == 16340
    assert license_text.startswith(b'MIT License\n\nCopyright (c) 2023 shadcn\n')
    destination = Path(__file__).resolve().parents[1] / 'frontend/src'
    (destination / 'shadcn-tailwind.css').write_bytes(css)
    (destination / 'shadcn-tailwind.LICENSE').write_bytes(license_text)
    print('Verified exact npm tarball SHA512, stylesheet SHA256 and release MIT license; extracted CSS/license only')


if __name__ == '__main__':
    main()
