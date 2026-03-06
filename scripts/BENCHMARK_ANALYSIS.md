# Benchmark Analysis — Internal, Unfiltered

No marketing. Just what's real, what's weak, and how we compare.

## Raw Numbers

```
Scenario               Vanilla   FP Cold   FP Warm   Speedup
Simple form fill        74.6s     18.5s      5.4s     13.7x
Login + add to cart     36.5s     59.0s      5.0s      7.3x
Form with validation    35.7s     17.1s      3.8s      9.4x
Checkboxes + tree       66.6s     34.9s      7.5s      8.8x
Modal + table + search  61.0s     47.3s      5.1s     11.9x
TOTAL                  274.5s              26.9s      10.2x

Pass rate: 5/5 all columns
```

## What we're actually measuring

Vanilla browser-use does this per step:

1. Take screenshot
2. Extract DOM / build element tree
3. Send screenshot + DOM + history to LLM
4. LLM decides next action
5. Execute action
6. Repeat (5-7 times per task)

That's 5-7 LLM round-trips at ~10-15s each = 50-100s per task.

FlowPilot warm does this:

1. Send one JS script to CDP Runtime.evaluate
2. Done

The speedup is fundamentally: **we skip the LLM entirely on repeat runs.**
That's not magic. It's caching. The question is whether the cache is useful.

## Honest problems

### 1. Cold runs are sometimes SLOWER than vanilla

Scenario 2 (login + cart): FP Cold took 59.0s vs vanilla's 36.5s. The cold path
navigates to the page, dumps DOM elements, asks the LLM to write a complete JS
script, executes it, and if it fails, tries to repair it. When the script works
first try, cold is fast. When it needs repair or the page is complex, cold loses.

### 2. Single-page only

We tested and confirmed: multi-page flows BREAK. When a reflex script navigates
(`window.location.href = ...`), it kills the CDP WebSocket session. browser-use
can't recover. Scenarios 3-5 in our first attempt all failed because they involved
login → redirect → interact.

The workaround for saucedemo (scenario 2) only works because the LLM-generated
script happens to do login + navigation + cart in one evaluate() call, and CDP's
awaitPromise keeps the connection alive through SPA-style navigations. But real
multi-page flows with full page reloads will fail.

**This means:** Reflexes work for single-page interactions. Multi-step flows
still need the agent loop. We're NOT 10x faster on complex real-world flows.

### 3. Brittle to DOM changes

The cached JS uses hardcoded CSS selectors from the DOM at generation time.
If the site deploys a new version with different selectors, IDs, or class names,
the reflex breaks. We have repair logic, but it costs another LLM call and
sometimes fails.

We did NOT test: running a warm reflex against a changed DOM. That's the real
test of robustness and we skipped it.

### 4. Test sites are toys

- demoqa.com — static HTML, no JS framework, no dynamic rendering
- saucedemo.com — React app but trivially simple, designed for testing

Real sites have: lazy loading, shadow DOM, iframes, CSP headers, bot detection,
dynamic IDs, virtualized lists, client-side routing. We tested against none of
these.

### 5. The "warm" comparison is borderline unfair

Comparing "zero LLM calls" vs "5-7 LLM calls" will always show a massive speedup.
The fairer question is: does the cold path + caching amortize to a win over N runs?

Break-even: if cold takes ~40s and warm takes ~5s, and vanilla takes ~55s:

- 1 run: cold = 40s, vanilla = 55s → FP wins
- 2 runs: cold + warm = 45s, 2x vanilla = 110s → FP wins easily
- Even if cold is slower (59s vs 36.5s), by run 2 you're ahead

So yes, caching amortizes. But only if the DOM doesn't change between runs.

## How others solve this

### Hand-written Playwright/Puppeteer scripts

- Fastest possible (no LLM overhead, hand-optimized selectors)
- But requires a developer to write and maintain each flow
- FlowPilot's value: auto-generates these scripts from a task description

### Playwright Codegen / Selenium IDE

- Record-and-replay: user clicks through manually, tool generates script
- Faster than LLM-generated scripts (no LLM overhead)
- But requires manual recording per flow, can't adapt to new inputs
- FlowPilot's value: fully automated, parameterized with inputs

### RPA tools (UiPath, Automation Anywhere, Power Automate)

- Enterprise-grade record-and-replay with visual selectors
- Handle multi-page, retries, error handling
- Expensive, heavyweight, require training
- FlowPilot's value: lightweight, LLM-driven, no manual flow design

### Skyvern

- Similar concept: AI agent for browser automation
- Uses vision models to understand pages
- No caching/reflex mechanism (every run uses LLM)
- FlowPilot's edge: cached reflexes skip LLM on repeat runs

### LaVague

- Generates Selenium code from natural language
- Code generation approach similar to our reflex generation
- No built-in caching/replay mechanism
- Similar cold-run performance, no warm-run optimization

### browser-use with custom tools

- You can add custom tools to browser-use to shortcut common actions
- Manually defined, not auto-generated
- Our approach auto-discovers and caches these shortcuts

### AgentQL / Browserbase / Steel

- Managed browser infrastructure, not automation logic
- Orthogonal — could run FlowPilot on their infra

## What we can actually claim

**Honest claim:** For repeatable single-page browser interactions, FlowPilot's
reflex system eliminates LLM calls on subsequent runs, reducing execution time
from ~60s to ~5s (10x). The first run generates the script via one LLM call,
which is comparable to or faster than a full agent loop.

**What we can't claim:**

- Works on complex multi-page flows (it doesn't)
- Robust to DOM changes (untested)
- Works on real production sites (only tested on toy sites)
- Faster than hand-written scripts (it's not — those are instant)
- Novel approach (it's LLM-assisted macro recording + replay)

## What would make this actually impressive

1. Multi-page reflex chains (reflex per page, orchestrated in sequence)
2. Self-healing selectors that survive DOM changes
3. Benchmarks against real sites (booking.com, amazon, github)
4. Comparison with cold run vs Playwright codegen output
5. Showing it works with different input values (not just the same form data)
6. Cost comparison (LLM API cost per run: vanilla vs FlowPilot)
