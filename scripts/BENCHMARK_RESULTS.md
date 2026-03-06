# Benchmark Results

**Date:** 2026-03-06
**LLM:** Gemini 3.1 Pro (preview) / Gemini 3 Flash (preview)

## Results

| #   | Scenario               | What it does                                                               | Baseline   | Cold  | Warm      | Speedup   |
| --- | ---------------------- | -------------------------------------------------------------------------- | ---------- | ----- | --------- | --------- |
| 1   | Simple form fill       | 8 fields (text, radio, checkbox, autocomplete, textarea) + submit          | 74.6s      | 18.5s | **5.4s**  | **13.7x** |
| 2   | Login + add to cart    | Login with credentials, add product to cart, navigate to cart, verify item | 36.5s      | 59.0s | **5.0s**  | **7.3x**  |
| 3   | Form with validation   | 4 fields (name, email, 2 addresses) + submit, verify output                | 35.7s      | 17.1s | **3.8s**  | **9.4x**  |
| 4   | Checkboxes + tree      | Expand 3 tree nodes, check 2 specific checkboxes, verify result area       | 66.6s      | 34.9s | **7.5s**  | **8.8x**  |
| 5   | Modal + table + search | Open modal, fill 6-field form, submit, search table, verify row            | 61.0s      | 47.3s | **5.1s**  | **11.9x** |
|     | **TOTAL**              |                                                                            | **274.5s** |       | **26.9s** | **10.2x** |

**Pass rate:** Baseline 5/5 — Cold 5/5 — Warm 5/5

## Columns

- **Baseline:** Standard browser-use agent. Nothing modified.
- **Cold:** FlowPilot First Run
- **Warm:** Subsequent runs.
