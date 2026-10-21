# Concurrent browser session capacity

Measured 17 October 2026 with `scripts/benchmark_sessions.py`. Raw measurements
are in `reports/session-capacity-*.json`. This is a **browser execution benchmark**,
not a concurrent Codex/Pi inference benchmark or a production capacity guarantee.

## Results

| Constrained profile | Highest count passing both 120-second runs without limit events | Peak browser-pool RAM | Peak controller RSS | Worst five-addition p95 | Worst startup p95 |
|---|---:|---:|---:|---:|---:|
| 2 vCPU / 2 GiB budget | 2 | 1,394 MiB | 367 MiB | 1.025 s | 2.34 s |
| 4 vCPU / 4 GiB budget | 3 | 2,063 MiB | 404 MiB | 0.989 s | 2.51 s |
| 8 vCPU / 6 GiB budget | 6 | 4,103 MiB | 442 MiB | 0.990 s | 3.24 s |

Counts are the highest passing **tested** configurations, not mathematical maxima.
There were 33 cases, 110 fresh browser-container instances, and 3,946 completed
five-item cart cycles (19,730 verified additions) across the pilot, sweep,
boundary tests, and longer confirmation runs. These additions were to a local
synthetic page, never to a real shopping account.

### Failure boundaries

- 2-GiB profile, 3 sessions: all three browser targets crashed, with cgroup OOM
  kills. Four sessions also failed during the sweep.
- 4-GiB profile, 4 sessions: every action completed in both 120-second runs, but
  one run incurred memory-limit/reclaim events. This is **marginal**, not an action
  failure. At 5 sessions, four targets suffered OOM kills; 6 also failed.
- 6-GiB profile, 7 sessions: all actions completed in the 60-second boundary run,
  but one container hit its memory limit. At 8, only five sessions finished intact.

Memory was the first constraint. At the confirmed 2/3/6-session levels, measured
browser-pool CPU usage averaged roughly 0.55/0.95/2.10 cores. Do not extrapolate
this CPU cost to video-heavy pages, downloads, or constant human-viewer streaming.

## What was actually constrained

The physical host is an ARM64 Mac with 64 GiB RAM and 18 logical CPUs. Docker
Desktop exposes 18 CPUs but only 7.75 GiB RAM. No Docker Desktop resource settings
were changed, and no separate 2/4/6-GiB physical machines were used.

All workers in a case share a CPU set of 2, 4, or 8 virtual CPUs. After reserving
25% of the named RAM budget, the remainder is divided evenly into hard per-worker
memory limits; swap is disabled. This is a **partitioned browser-pool simulation**,
not an aggregate VM limit. The macOS Python/Playwright controller runs outside
these constraints; its RSS is reported separately. The reserve must cover that
controller, OS, Docker, backend, and any model-agent subprocesses in deployment.
Particularly on a real 2-GiB machine, that reserve can be tight.

Workers use the actual `flowpilot-sandbox` image: full Chromium on Xvfb, separate
container filesystems, no profile mounts, default seccomp and no-new-privileges,
and loopback-only published CDP ports. Every worker has a fresh profile. The
benchmark attaches Playwright and browser-use as the REPL does. It intentionally
uses a **1024 PID/thread limit**, rather than the application's still-current 256
default that failed during real multi-site browsing. Production defaults were
not changed by this benchmark.

### Workload and isolation checks

Each session simultaneously loads a synthetic shopping page with 300 rendered
product cards and two cross-origin iframe components, searches the catalog,
clicks five Add buttons, verifies exactly five distinct quantity-one entries,
reloads, and repeats with a 250 ms think interval. It takes a JPEG screenshot every
third cycle. All requests are locally fulfilled; third-party websites, networks,
images, ad stacks, anti-bot systems and live model inference are excluded.

Every profile starts without the fixture's cookies/local storage. Each session
writes a different identity at the same origin and checks its own cookie/storage
throughout the load. This verifies browser-storage isolation and no shared mounts;
it is not a hostile-tenant network or container escape security test. Saved-profile
restart persistence is separately covered by `scripts/smoke_repl_state.py`.

Passing requires every session to finish with correct state, no OOM/PID/memory-max
events, startup p95 <= 60 seconds and five-addition p95 <= 5 seconds. The sweep
used two 20-second repetitions at passing counts, boundary checks ran up to 60
seconds, and confirmed counts used two separate 120-second runs. A five-addition
latency excludes the following screenshot, reload and think interval; completed
cycle counts include those overheads. Browser RAM is sampled once per second,
so very short spikes can be missed; cgroup event counters capture limit hits.

## Larger machine planning estimates — not measured

For initial provisioning, allow **1–1.5 GiB per active session**, leave 25% machine
RAM for overhead, and budget about 0.5 CPU core per active synthetic-style worker.
Real page complexity, per-agent Node/Python processes and streaming can require
more. Applying those assumptions gives these starting ranges:

| Machine size | Planning range of active sessions |
|---|---:|
| 4 vCPU / 8 GiB | 4–6 |
| 8 vCPU / 16 GiB | 8–12 |
| 16 vCPU / 32 GiB | 16–24 |
| 32 vCPU / 64 GiB | 32–48 |

These are **unverified estimates**, not tested support levels. The current Mac's
64-GiB physical RAM does not make 64 GiB available to Docker. Provider concurrency
limits can cap simultaneous AI-driven sessions long before local browser capacity.
Idle sessions may cost less CPU but still retain RAM and processes. Neither idle
density, multi-hour soak stability nor simultaneous full-rate viewers was measured.

## Reproduce

```sh
docker build -f Dockerfile.sandbox -t flowpilot-sandbox .
uv run python -m scripts.benchmark_sessions \
  --output reports/session-capacity-new.json \
  --profiles 2:2,4:4,8:6 --counts 1,2,4,6,8,12,16 \
  --seconds 20 --repeats 2

# Confirm a chosen configuration for longer:
uv run python -m scripts.benchmark_sessions \
  --output reports/session-capacity-confirm.json \
  --profiles 4:4 --counts 3 --seconds 120 --repeats 2
```

Use a new output filename to retain earlier evidence. The script records image ID,
machine facts, per-session results, raw resource samples and cgroup counters. It
stops increasing concurrency after a failed count and cleans up only containers
with its exact benchmark run label. All benchmark containers were removed after
this run. User profiles, carts, and login state were not used or changed.
