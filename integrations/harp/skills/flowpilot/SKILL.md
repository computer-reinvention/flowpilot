---
name: flowpilot
description: Do website tasks through FlowPilot's hosted browser agent. Use for searching flights, trains, rides, shopping carts, reading prices or account pages, and other website errands; FlowPilot drives an isolated browser for this user and returns the answer, asks you for missing details, and asks for confirmation before anything is booked or bought.
---

# FlowPilot browser tasks

`flowpilot_browse` hands one website errand to FlowPilot's browsing agent. That agent runs
in this user's own isolated browser profile, which keeps their logins, cookies and saved
page scripts between tasks. Do not drive the browser step by step yourself.

## Writing the task

Write the task as one complete instruction the agent can finish without asking you. Include:

- **The outcome and the exact data to return.** For example: "the 3 cheapest nonstop options
  with airline, departure and arrival times and price in INR, plus the source URL".
- **Every value the user gave:**
  - places, written in full, with airport or station codes when known;
  - absolute dates, resolved from "tomorrow" or "next Friday" using the user's timezone;
  - number of passengers, class, budget, quantities and filters.
- **The site the user named, if any.** Otherwise let the agent choose a public site.
- **Where to stop.** Unless the user explicitly asked to buy or book, say "do not book or
  pay". For carts, say "stop at cart review". For a ride, say "find options and fares, then
  ask me to confirm before booking".

Example task:

> Find me a flight: one-way, economy, 1 adult, from Mumbai (BOM) to Delhi (DEL) on 12 November 2026. Report the 3 cheapest nonstop options with airline, departure/arrival times and price in INR, plus the source URL. Do not book anything.

Set `follow_up=true` only when the user is continuing the same errand, for example "now
check the day after" or "add milk too". FlowPilot then reuses that conversation and page
state. A new errand starts fresh in the same profile, and saved logins still apply.

## While it runs

- **Running past your wait.** The tool returns `status: running` with a `task_id`. Tell the
  user it is still working and stop; the result arrives on its own later. Do not start the
  same errand again.
- **A question (`waiting_for_input`).** Missing details such as an address or passenger
  count are asked in the conversation. Codes, OTPs and passwords never are: the host
  collects them through a private message and you never see them. Never ask the user to
  paste a code, password or API key in chat, and never repeat one.
- **A confirmation (`waiting_for_confirmation`).** Show the user exactly what FlowPilot
  wants to do, including the item, price and time, and wait for an explicit yes. Anything
  other than a clear yes from the user who owns the task is a decline.
- **A browser step (`waiting_for_browser`).** A CAPTCHA, passkey or personal sign-in needs
  the user in the browser. The host sends them a private, short-lived link. Never post or
  repeat that link, and never share it with anyone else.

## Reporting the result

- Give the user FlowPilot's `answer` and its source URLs. Do not add prices, times or
  availability that FlowPilot did not report.
- If FlowPilot says it was blocked (login required, bot check, site down), tell the user
  what blocked it and what it tried. Offer the alternative it suggests, or a different site.
  Do not silently retry.
- If a task ended `interrupted` or `failed` after it may have acted on a website, say so.
  Do not start it again until the user has checked and agrees.
- Never claim something was booked or purchased unless FlowPilot's answer says so after an
  approved confirmation.
