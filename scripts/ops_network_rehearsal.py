"""Only for a disposable --network none container with private network namespaces.

Needs NET_ADMIN, SYS_ADMIN and seccomp unconfined to create CHILD namespaces.
No host mounts, host networking, Docker socket or live destinations are used.
"""
import json
import os
from pathlib import Path
import subprocess
import time


def cmd(*args):
    return subprocess.run(args, capture_output=True, check=True, text=True).stdout


def ns(pid, *args):
    return cmd("nsenter", "-t", str(pid), "-n", *args)


def main():
    children = []
    servers = []
    results = {}
    try:
        policy = str(Path(__file__).with_name("ops_network_policy.nft"))
        checked = subprocess.run(["nft", "--check", "-f", policy], capture_output=True, text=True)
        if checked.returncode:
            print(json.dumps({"HOST_NETWORK_POLICY_UNVERIFIED": "kernel rejected policy check in isolated namespace", "detail": checked.stderr[:2000]}))
            raise RuntimeError("kernel prerequisites missing; no host modules changed")
        for name in ("browser", "backend", "website"):
            child = subprocess.Popen(["unshare", "--net", "sleep", "300"], stderr=subprocess.PIPE)
            children.append(child)
            time.sleep(0.1)
            if child.poll() is not None:
                raise RuntimeError("child network namespace unavailable")
        browser, backend, website = [c.pid for c in children]
        cmd("ip", "link", "add", "fp-sandbox0", "type", "bridge")
        cmd("ip", "addr", "add", "172.30.240.1/24", "dev", "fp-sandbox0")
        cmd("ip", "link", "set", "fp-sandbox0", "up")
        for label, pid, ip in (("br", browser, "172.30.240.3/24"), ("be", backend, "172.30.240.2/24")):
            cmd("ip", "link", "add", label + "-host", "type", "veth", "peer", "name", label + "-peer")
            cmd("ip", "link", "set", label + "-host", "master", "fp-sandbox0")
            cmd("ip", "link", "set", label + "-host", "up")
            cmd("ip", "link", "set", label + "-peer", "netns", str(pid))
            ns(pid, "ip", "link", "set", "lo", "up")
            ns(pid, "ip", "addr", "add", ip, "dev", label + "-peer")
            ns(pid, "ip", "link", "set", label + "-peer", "up")
            ns(pid, "ip", "route", "add", "default", "via", "172.30.240.1")
        # All 'public', host and metadata IPs below are namespace-local fixtures.
        cmd("ip", "link", "add", "web-host", "type", "veth", "peer", "name", "web-peer")
        cmd("ip", "addr", "add", "93.184.216.1/24", "dev", "web-host")
        cmd("ip", "link", "set", "web-host", "up")
        cmd("ip", "link", "set", "web-peer", "netns", str(website))
        ns(website, "ip", "addr", "add", "93.184.216.34/24", "dev", "web-peer")
        ns(website, "ip", "link", "set", "web-peer", "up")
        ns(website, "ip", "link", "set", "lo", "up")
        ns(website, "ip", "route", "add", "default", "via", "93.184.216.1")
        ns(backend, "ip", "addr", "add", "169.254.169.254/32", "dev", "lo")
        ns(backend, "ip", "-6", "addr", "add", "fd00::2/64", "dev", "be-peer", "nodad")
        ns(browser, "ip", "-6", "addr", "add", "fd00::3/64", "dev", "br-peer", "nodad")
        cmd("ip", "route", "add", "169.254.169.254/32", "via", "172.30.240.2")
        code = """from http.server import BaseHTTPRequestHandler, HTTPServer
import socket, threading
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(302 if self.path == '/redirect' else 200)
  if self.path == '/redirect': self.send_header('Location','http://172.30.240.2/')
  self.end_headers(); self.wfile.write(b'fixture')
 def log_message(self,*a): pass
class V6(HTTPServer):
 address_family = socket.AF_INET6
 def server_bind(self):
  self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
  super().server_bind()
for address, port, cls in [('0.0.0.0',80,HTTPServer),('0.0.0.0',2375,HTTPServer),('::',80,V6)]:
 threading.Thread(target=cls((address,port),H).serve_forever,daemon=True).start()
threading.Event().wait()
"""
        for pid in (backend, website, os.getpid()):
            servers.append(subprocess.Popen(["nsenter", "-t", str(pid), "-n", "python", "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.3)

        def reachable(url, *extra):
            proc = subprocess.run(["nsenter", "-t", str(browser), "-n", "curl", "--noproxy", "*", "-fsSL", "--max-time", "2", *extra, url], capture_output=True)
            return proc.returncode == 0

        targets = {"backend": "http://172.30.240.2/", "docker_control_port": "http://172.30.240.2:2375/", "host_private": "http://172.30.240.1/", "host_public": "http://93.184.216.1/", "metadata": "http://169.254.169.254/", "ipv6_private": "http://[fd00::2]/", "redirect_private": "http://93.184.216.34/redirect"}
        # Emulated Python server startup can take seconds. Every denied target
        # must be proven reachable BEFORE policy load to avoid false positives.
        for url in ["http://93.184.216.34/", *targets.values()]:
            for attempt in range(10):
                if reachable(url):
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError("isolated fixture unreachable before policy: " + url)
        cmd("nft", "-f", policy)
        results["public_http"] = reachable("http://93.184.216.34/")
        for label, url in targets.items():
            results[label + "_denied"] = not reachable(url)
        results["dns_public"] = reachable("http://fixture.test/", "--resolve", "fixture.test:80:93.184.216.34")
        results["dns_rebind_private_denied"] = not reachable("http://fixture.test/", "--resolve", "fixture.test:80:172.30.240.2")
        print(json.dumps(results, sort_keys=True, indent=2))
        if not all(results.values()):
            raise RuntimeError("isolated policy behavior failed")
    finally:
        for process in servers + children:
            process.terminate()
        for process in servers + children:
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
