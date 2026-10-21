"""OS-package and bundled-runtime vulnerability gate for built FlowPilot images.

Runs a digest-pinned Trivy container against `docker save` archives of local images
(no Docker socket is handed to the scanner, no host tool installs, no registry push).
Runtimes copied in as plain binaries are invisible to package scanners, so their exact
versions are read from the image (offline container) and checked against upstream
release feeds: Chromium against Google's current Linux stable, Node.js against
nodejs.org releases flagged as security releases, and the official-image CPython against
newer python.org patch releases of the same minor line.

Exit status: 0 when every HIGH/CRITICAL finding is covered by an unexpired allowlist
entry with a justification; 1 on unaccepted findings or invalid/expired allowlist
entries; 2 on audit infrastructure errors. Language packages (Python venv / npm) are
reported for context only: their gate is `ops_dependency_audit.py`.

    python scripts/ops_image_audit.py --image backend=flowpilot-backend:local \
        --image sandbox=flowpilot-sandbox:local [--report out.json] [--skip-db-update]
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

TRIVY = "aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa"
CACHE_VOLUME = os.environ.get("FLOWPILOT_TRIVY_CACHE_VOLUME", "flowpilot-ops-trivy-cache")
LABEL = "flowpilot.ops=image-audit"
GATED = ("CRITICAL", "HIGH")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
DEFAULT_ALLOWLIST = Path(__file__).with_name("ops_image_allowlist.json")
CHROME_STABLE_URL = "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions?pageSize=1"
NODE_INDEX_URL = "https://nodejs.org/dist/index.json"
PYTHON_RELEASES_URL = "https://www.python.org/api/v2/downloads/release/?is_published=true"
RUNTIME_IDS = {"chromium": "CHROMIUM-BEHIND-STABLE", "node": "NODE-SECURITY-RELEASE-MISSED", "python": "PYTHON-PATCH-BEHIND"}
FIND_RUNTIMES = r"""
for c in $(find / -xdev \( -path '*/chrome-linux*/chrome' -o -path '*/chrome-headless-shell-linux*/chrome-headless-shell' \) -type f 2>/dev/null); do
  printf 'chromium\t%s\t' "$c"; "$c" --version 2>/dev/null | head -1; echo; done
for n in $(find / -xdev -type f -name node 2>/dev/null); do
  [ -x "$n" ] && { printf 'node\t%s\t' "$n"; "$n" --version 2>/dev/null; echo; }; done
if [ -x /usr/local/bin/python3 ]; then
  printf 'python\t/usr/local/bin/python3\t'; /usr/local/bin/python3 -c 'import platform; print(platform.python_version())'; fi
true
"""


class AuditError(RuntimeError):
    pass


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AuditError("docker %s failed: %s" % (args[0], result.stderr.strip()[-400:]))
    return result


def trivy_scan(image, skip_db_update):
    with tempfile.TemporaryDirectory(prefix="flowpilot-image-audit-") as tmp:
        archive = os.path.join(tmp, "image.tar")
        docker("save", "-o", archive, image)
        command = [
            "run", "--rm", "--label", LABEL, "--cpus", "2", "--memory", "3g", "--pids-limit", "256",
            "--security-opt", "no-new-privileges:true", "-v", CACHE_VOLUME + ":/root/.cache/trivy",
            "-v", tmp + ":/in:ro", TRIVY, "image", "--input", "/in/image.tar", "--scanners", "vuln",
            "--pkg-types", "os,library", "--format", "json", "--quiet", "--timeout", "20m",
        ]
        if skip_db_update:
            command.append("--skip-db-update")
        result = docker(*command)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise AuditError("trivy did not produce JSON for %s" % image) from None


def parse_runtimes(output):
    found = {}
    for line in output.splitlines():
        kind, _, rest = line.partition("\t")
        path, _, banner = rest.partition("\t")
        version = next((part.lstrip("v") for part in banner.split()
                        if part.lstrip("v")[:1].isdigit() and part.count(".") >= 2), None)
        if kind in RUNTIME_IDS and version:
            found.setdefault(kind, {})[path] = version
    return found


def runtime_versions(image):
    result = docker("run", "--rm", "--label", LABEL, "--network", "none", "--platform", "linux/amd64",
                    "--entrypoint", "/bin/sh", image, "-c", FIND_RUNTIMES, check=False)
    return parse_runtimes(result.stdout)


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def upstream():
    node = [(r["version"].lstrip("v"), bool(r.get("security"))) for r in fetch_json(NODE_INDEX_URL)]
    python = []
    for release in fetch_json(PYTHON_RELEASES_URL):
        version = release["name"].split()[-1]
        if release["name"].startswith("Python ") and not release.get("pre_release") and version.replace(".", "").isdigit():
            python.append(version)
    return {"chromium": fetch_json(CHROME_STABLE_URL)["versions"][0]["version"], "node": node, "python": python}


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def runtime_gap(kind, version, feeds):
    """Return (newest fixing version, reason) when a runtime misses upstream fixes."""
    key = version_key(version)
    if kind == "chromium":
        stable = feeds["chromium"]
        return (stable, "Bundled Chromium is older than current Linux stable %s" % stable) if key < version_key(stable) else None
    if kind == "node":
        missed = sorted((v for v, security in feeds["node"] if security and version_key(v)[0] == key[0] and version_key(v) > key), key=version_key)
        return (missed[-1], "Node.js misses security release(s) " + ", ".join(missed)) if missed else None
    newer = sorted((v for v in feeds["python"] if version_key(v)[:2] == key[:2] and version_key(v) > key), key=version_key)
    return (newer[-1], "CPython misses patch release(s) " + ", ".join(newer)) if newer else None


def normalize(name, report, runtimes, feeds):
    findings, context = [], {}
    for result in report.get("Results") or []:
        cls = result.get("Class")
        for vuln in result.get("Vulnerabilities") or []:
            record = {
                "image": name, "id": vuln["VulnerabilityID"], "package": vuln["PkgName"],
                "installed": vuln.get("InstalledVersion"), "fixed": vuln.get("FixedVersion") or None,
                "status": vuln.get("Status"), "severity": vuln.get("Severity", "UNKNOWN"),
                "class": cls, "type": result.get("Type"), "target": result.get("Target"),
                "title": (vuln.get("Title") or "")[:160],
            }
            if cls == "os-pkgs":
                findings.append(record)
            else:
                bucket = context.setdefault(record["type"], {})
                bucket[record["severity"]] = bucket.get(record["severity"], 0) + 1
    for kind, paths in sorted(runtimes.items()):
        for path, version in sorted(paths.items()):
            gap = runtime_gap(kind, version, feeds)
            if gap:
                findings.append({
                    "image": name, "id": RUNTIME_IDS[kind], "package": kind, "installed": version, "fixed": gap[0],
                    "status": "fixed", "severity": "HIGH", "class": "bundled-runtime", "type": "binary",
                    "target": path, "title": gap[1],
                })
    return findings, context


def load_allowlist(path, today):
    data = json.loads(Path(path).read_text())
    entries, problems = {}, []
    for entry in data.get("accepted", []):
        key = (entry.get("id"), entry.get("package"))
        if not all(entry.get(field) for field in ("id", "package", "images", "justification", "review_by")):
            problems.append("allowlist entry %s missing id/package/images/justification/review_by" % (key,))
            continue
        if entry["id"] in RUNTIME_IDS.values() and not entry.get("installed"):
            problems.append("runtime allowlist entry %s must name the exact installed version" % (key,))
            continue
        if datetime.date.fromisoformat(entry["review_by"]) < today:
            problems.append("allowlist entry %s expired on %s" % (key, entry["review_by"]))
            continue
        entries[key] = entry
    return entries, problems


def accepted(finding, entries):
    entry = entries.get((finding["id"], finding["package"]))
    if not entry or finding["image"] not in entry["images"]:
        return False
    if finding["class"] == "os-pkgs" and finding["fixed"]:
        return False  # a distro fix exists: rebuild/upgrade instead of accepting

    if finding["id"] in RUNTIME_IDS.values():
        return entry["installed"] == finding["installed"]
    return True


def evaluate(findings, entries):
    summary, unaccepted = {}, []
    for finding in findings:
        image = summary.setdefault(finding["image"], {"by_severity": {s: 0 for s in SEVERITIES}, "fixable": 0,
                                                      "gated_accepted": 0, "gated_unaccepted": 0})
        image["by_severity"][finding["severity"]] = image["by_severity"].get(finding["severity"], 0) + 1
        if finding["fixed"]:
            image["fixable"] += 1
        if finding["severity"] in GATED:
            if accepted(finding, entries):
                image["gated_accepted"] += 1
            else:
                image["gated_unaccepted"] += 1
                unaccepted.append(finding)
    return summary, unaccepted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", action="append", required=True, help="name=local-image-tag")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--report", help="write the full normalized findings JSON here")
    parser.add_argument("--skip-db-update", action="store_true", help="reuse the cached advisory DB")
    args = parser.parse_args(argv)
    today = datetime.date.today()
    try:
        entries, problems = load_allowlist(args.allowlist, today)
        docker("volume", "create", "--label", LABEL, CACHE_VOLUME)
        feeds = upstream()
        all_findings, context, runtimes, metadata = [], {}, {}, {}
        for spec in args.image:
            name, _, image = spec.partition("=")
            if not image:
                raise AuditError("--image must be name=tag")
            report = trivy_scan(image, args.skip_db_update)
            runtimes[name] = runtime_versions(image)
            findings, context[name] = normalize(name, report, runtimes[name], feeds)
            all_findings.extend(findings)
            meta = report.get("Metadata") or {}
            metadata[name] = {"image": image, "image_id": meta.get("ImageID"), "os": meta.get("OS")}
    except (AuditError, OSError, ValueError, KeyError) as error:
        print("AUDIT INFRASTRUCTURE ERROR: %s" % error)
        return 2
    summary, unaccepted = evaluate(all_findings, entries)
    output = {
        "date": today.isoformat(), "scanner": TRIVY, "chrome_stable_linux": feeds["chromium"], "images": metadata,
        "runtimes": runtimes, "summary": summary, "language_packages_context_only": context,
        "allowlist_problems": problems, "unaccepted": unaccepted,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(dict(output, findings=all_findings), indent=2, sort_keys=True) + "\n")
    print(json.dumps(output, indent=2, sort_keys=True))
    if unaccepted or problems:
        print("FAIL: %d unaccepted HIGH/CRITICAL finding(s); %d allowlist problem(s)" % (len(unaccepted), len(problems)))
        return 1
    print("PASS: no unaccepted HIGH/CRITICAL OS-package or bundled-runtime findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
