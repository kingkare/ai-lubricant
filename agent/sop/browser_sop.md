# Browser SOP

## Purpose

Use the least powerful web path that can finish the task. The single `web` tool aggregates public HTTP reading and live-browser operations.

## Pick by sense

| What you want to know | Operation |
|---|---|
| what the page **says** (clean text) | `read_page` |
| what the page **sends** (HTTP requests / WS frames / SSE) | `watch_traffic` → `poll_traffic` → `unwatch_traffic` |
| what the page **looks like** (image) | `screenshot` |
| a login form asks for a **password** or **2FA code** | `fill_credential` |
| one live DOM value, or an action (click/fill) | `execute` |

`read_page` reads. It does **not** search by keyword, does **not** monitor traffic, does
**not** navigate. Looking for one fact inside a big page is `execute` + a selector, not a
bigger `read_page` — its text is truncated at `max_chars`, so a bigger page does not give
you more, it just cuts off further from the top.

## Tool

`web` takes `operation`:

- `read_page`: take a page's readable text. Give a `url` (plain HTTP fetch, no browser at
  all) or a `tab` (the rendered DOM of that open tab); HTML noise (scripts, styles,
  comments, invisible elements) is stripped and anchors kept as `text (url)`. It never
  opens or navigates anything; if you pass both `url` and `tab` it reads the **tab** and
  ignores the url (and warns you). Output is truncated at `max_chars` (default 8000) —
  `truncated: true` means there is more, so get the rest with `execute`, not by raising
  `max_chars`.
- `execute`: execute JavaScript in the active authorised browser tab through the browser-class MCP.
- `tabs`: list available browser tabs through the browser-class MCP.
- `screenshot`: capture a screenshot through the browser-class MCP.
- `clients`: list the browsers (CDP clients) you may operate — each with `client_id`, name, connection state, its tabs, and which one this conversation is bound to. Read-only; call it before picking a `client_id` for other operations.
- `navigate`: load a URL in the current tab.
- `open_tab`: open a new tab (`url`, `new_window`, `active`).
- `close_tab`: close a tab by id (pass `tab`). Use it to clean up a tab you opened once done with it. Do NOT close the tab this conversation is bound to unless the user asked — operations on a closed tab_id fail afterwards.
- `switch_tab`: retarget the MCP's current tab without moving what the user sees.
- `focus_tab`: bring a tab to the foreground (user-visible).
- `batch`: run several CDP commands in one attached session, so later commands can reference earlier results.
- `wait`: poll until `condition_js` returns a truthy value (background tabs are throttled — prefer this over a `setTimeout` loop).
- `watch_traffic` / `poll_traffic` / `unwatch_traffic`: subscribe to everything this tab sends and receives — HTTP requests **and WebSocket frames and SSE messages** (URL, method, headers, status, timing, bodies). `watch_traffic` attaches the debugger (keeps a banner on the page); `poll_traffic` reads what has arrived so far (summary first, then drill in with `search`/`request_id`/`frame_index`); `unwatch_traffic` detaches and returns everything. **This is how to see what a page actually sends.** Read it before theorising, and **do not hand-write JS in `execute` to hook `fetch`/`XMLHttpRequest`/`WebSocket`** — those hooks miss sockets that were already open and cross-origin traffic. (`watch_network`/`read_network`/`unwatch_network` and `network_start`/`network_get`/`network_stop` are all accepted aliases of the traffic trio; `scan` is an alias of `read_page`.)
- `fill_credential`: type a saved credential — a **2FA code** (`kind="otp"`) or a **login password** (`kind="password"`) — **straight into the page**. Pass `url` (the login page) or `domain`, plus `account` when the site has several saved entries; optionally `selector` to pin the field. The value is resolved server-side and handed to the browser extension, which types it into the DOM — **the code/password is never returned to you**; the result is only a receipt (`status`, `filled`, `field`, `entry`, and for otp `expiresAt`). **Use this the moment a login form asks for a password or a two-factor / verification / authenticator code.** If the result lists several `entries`, retry with `account`. If it returns no entry, the site is not saved: tell the user to add it in the extension popup (🛡 icon) or web console — never invent a value.
- `totp_code`: get the current 2FA (TOTP / Google Authenticator) verification code for a site the user has saved in their authenticator. Pass `url` (the login page — its host is matched against the entry's linked domains) or `domain`. Returns entries with `code` + `expiresAt` (absolute epoch seconds when the code rotates). **Use this only when you must *read* a code yourself** (e.g. to relay it into a non-DOM step); to fill a login form, prefer `fill_credential`, which keeps the code out of your context. An empty result means the user has not linked that site: tell them to add it in the extension popup (🛡 icon) or web console, and **never invent or guess a code**.

`client_id` selects a browser within your authorised set when several are available; `clients` lists the ids you may pass. A scene-bound conversation is pinned to one browser, so you normally do not pass `client_id`. An id outside your authorised set is rejected.

1. For public/static content, start with `web(operation="read_page", url="...")`. Do not launch a browser merely to read text.
2. Use `web(operation="execute", script="...")` only when the task needs a live session, JavaScript-rendered state, navigation, login state or page interaction.
3. Use `web(operation="tabs")` to inspect tabs before selecting one when the browser session is ambiguous. **It is also the cheapest way to read the current page's URL and title** — its result carries every tab's `url` / `title` plus `active_tab`. Never write `return {url: location.href, title: document.title}` through `execute` just to learn what page you are on: that pays a whole-page HTML snapshot + diff you did not need. Use `execute` for live DOM state you must actually read (a field's value, a node's text, `document.readyState` on a page not yet registered), and pass `no_monitor=true` when you only want the returned value.
4. Before changing a live page, observe its current URL/state (cheapest: `tabs` already returns the active tab's URL; if you need deeper state, `read_page` or `execute` with `no_monitor=true`).
5. To go to another page within the same task/site, prefer clicking an in-page link / element (via `web(operation="execute")`) over directly opening a URL (`web(operation="navigate")`). Directly opening a new URL tears down the chat panel's page context; clicking keeps the page session and referrer intact. Use direct navigation only when no in-page link exists.
6. Keep JavaScript small and reversible. After an action, inspect the returned result/page change before issuing the next action.
7. Browser services are detected dynamically by their browser capability labels/primitives; do not assume a service name.
8. `read_page` with no `tab` remains usable without a browser MCP. Every other operation — and `read_page` with a `tab` — requires a configured browser-class MCP. If unavailable, the tool returns `browser_mcp_not_configured`; ask the user to bind a CDP-capable MCP.

## Login and credential fill

When a login / auth form asks for a **password** or a **2FA / verification code**, use
`web(operation="fill_credential", kind=..., url=..., account=...)`. It types the saved
value **into the page** and returns a receipt only — the code or password **never enters
your context** (not in the result, not in the conversation log, not in any upstream LLM).
This is the intended way to log in; do not ask the user to paste a password into the chat.

- `kind="password"` fills the username + password of a saved login entry.
- `kind="otp"` fills a 2FA / TOTP code from the user's saved authenticator.
- Pass `url` (the login page — its host selects the entry) or a bare `domain`; pass
  `account` when the site has several saved entries. If the result lists several
  `entries` and `filled: false`, retry with `account`. If it reports no entry, the site
  is not saved — tell the user to add it in the extension popup (🛡 / 🔑 icon) or the web
  console, and **never invent a value**.
- Optional `selector` pins the target field when auto-detection would pick the wrong one.
- After filling, submit the form with `execute` (click the submit button) — filling does
  not submit.

`totp_code` remains available but is the fallback: it **returns** the current code into
your context, so use it only when you genuinely must read a code yourself (e.g. to relay
it into a non-DOM step). For a login form, prefer `fill_credential`.

## No keyword search

There is **no keyword search in this tool.** `read_page` only reads a URL you already have.
If you need to *find* a page you have no URL for, `open_tab` a search engine and
`read_page`/`execute` its results — do not expect `read_page` to search, and do not ask it
to monitor traffic (that is `watch_traffic`).

## New tab vs. in-page navigation

**New task → new tab.** A fresh, independent task — a different site, an unrelated
objective, or anything that should not mutate the page the user is currently on —
starts with `web(operation="open_tab", url=...)` rather than being driven in the
current tab, which would pollute the user's open page session (history, scroll,
form state) and risk losing their context.

`open_tab(url, new_window=False, active=True)` opens a fresh tab (or window) through
the extension's native `chrome.tabs.create`.

- A newly opened tab needs a moment to load and register as a session; its id may not
  appear in `web(operation="tabs")` immediately — wait briefly, then re-list.
- Once the new tab is the working surface, navigate *within its site* by clicking
  in-page elements (rule 5), not by opening further tabs.
- When the task that justified the new tab is finished, clean it up with
  `web(operation="close_tab", tab=...)` — the user's browser should not accumulate
  tabs you opened. Never close the conversation-bound tab unless the user asked;
  operations on a closed tab_id fail. Closing the last tab of a window closes
  the window.
- `web(operation="navigate", url=...)` changes the URL of the current tab only; it is
  not a way to start a new task. Reserve it for same-task URL changes where no in-page
  link exists.
- `switch_tab` retargets which tab the MCP commands act on *without* moving the user's
  view; `focus_tab` is the one that changes what the user sees. Reach for `switch_tab`
  when the user should not be interrupted.

## Verified pitfalls (CDP bridge)

These are measured behaviours of the Chrome-extension CDP bridge, not guesses. Skipping
them is the usual cause of "the script ran but nothing happened".

### JavaScript execution

- **`await` needs an explicit `return`.** The script is wrapped in an async function; an
  awaited expression without `return` yields `null`, not the value.
- `read_page` (on a `tab`) pierces same-origin iframes automatically. Cross-origin
  iframes need the CDP path (below) — `contentDocument` is unreachable.
- **Synthetic events carry `isTrusted=false`.** File inputs and some buttons reject them.
  When a plain JS click does nothing, escalate to CDP input events rather than retrying
  the same click.
- A JS click that should open a new tab but doesn't is usually popup blocking — CDP
  clicking gets through.
- **File upload:** prefer the pure-JS DataTransfer route —
  `new File([content], name, {type})` → `new DataTransfer().items.add(file)` →
  `input.files = dt.files` → dispatch `input` and `change`. Check `input.accept` first,
  and disambiguate multiple inputs by `accept` or container semantics.

### Traffic capture — watch_traffic / poll_traffic / unwatch_traffic

To learn what a page actually sends (endpoint, headers, body, WebSocket frames, SSE
messages), subscribe with `watch_traffic` instead of writing JS hooks.

- **Why not `execute`:** a `fetch`/`XMLHttpRequest`/`WebSocket.prototype` hook you inject
  only sees sockets created *after* it runs, misses cross-origin traffic that never
  passes through `window`, and is transparent to page code that captures a reference to
  the original function first. `watch_traffic` attaches the CDP debugger and observes
  the network at the browser level — it sees everything, including connections opened
  before you attach.
- **Summary first, details on demand.** `poll_traffic` returns a **summary** by default:
  one row per request (requestId, method, url, status, mimeType, size) and WS/SSE frame
  summaries (no payloads). Do NOT assume you got bodies. Drill in per need:
  - `poll_traffic(search="api/orders")` — only rows whose URL matches (filter before reading).
  - `poll_traffic(request_id="...")` — the ONE request's full record (headers + body).
  - `poll_traffic(frame_index=12)` — that one WS/SSE frame's payload.
  This keeps tokens bounded: skim the summary to find the interesting request, then
  fetch only that one in full. Never pull every body.
- **Three-step lifecycle:** `watch_traffic` (starts, keeps the debugger attached and a
  banner on the page) → `poll_traffic` (read what has arrived) → `unwatch_traffic`
  (detach and return everything). Always `unwatch_traffic` when done — an abandoned
  watch leaves the debugger attached and the banner visible to the user.
- **Frame shape:** each summary row has `kind` (`websocket` | `sse`), `event`,
  `requestId`, `url`, `length`; use `frame_index` to fetch the payload of a specific
  frame. Use it to read a chat page's live traffic without touching page JS.
- **Pattern filter:** pass `url_pattern` to `watch_traffic` to keep only traffic whose
  URL contains that substring — cheap when the tab is chatty.
- **Polling beats `setTimeout`:** background tabs are throttled; `poll_traffic` runs
  outside the page, so its timing is unaffected.

### Navigation

- **Never navigate and then act in the same script.** After `location.href = ...` the
  execution context is destroyed and the rest of the script fails with
  `Inspected target navigated or closed`. Navigate, wait for load, then act in a second
  call.
- Prefer clicking an in-page link over assigning `location.href` (see rule 5).

### CDP click sequence

- A reliable click is **three events**: `mouseMoved` → `mousePressed` → `mouseReleased`,
  50-100ms apart. Omitting `mouseMoved` breaks any hover-dependent component (tooltips,
  dropdown menus) — the click lands but the menu never opened.
- **Coordinates equal `getBoundingClientRect()` once the session is stable — but not on
  first attach.** Attaching the debugger makes Chrome show the "being controlled by
  automated software" infobar (~20px), which pushes page content down. Coordinates
  measured before attach are wrong after it. Send one harmless `mouseMoved(0,0)` to warm
  up, then measure.
- Custom dropdowns need two rounds: click the trigger, *then* measure the option element
  (it doesn't exist in the DOM until the menu opens).
- If the page uses `transform: scale` or `zoom`, multiply by
  `getComputedStyle(document.documentElement).zoom` and `visualViewport.scale`.
- For an element inside an iframe, compose coordinates: `iframeRect.x + elRect.x`.
- `DOM.getBoxModel` returns eight values; take the centre as the **average of all four
  points**, not the diagonal midpoint — a rotated or skewed element is not a rectangle.
- `nodeId` goes stale after any DOM change. Prefer `backendNodeId`, or re-fetch the
  document.

### Tabs and throttling

- Background tabs are throttled: `setTimeout` fires at most about once a minute. Do not
  build polling loops on it.
- Some SPAs only load data in the foreground — `Page.bringToFront` first.
- Screenshots via `Page.captureScreenshot` work on background tabs and need no
  foregrounding; for a captcha canvas, `canvas.toDataURL()` is cleaner still.
- Cross-tab operation needs no foregrounding, only an explicit `tabId`.

### Batched CDP commands

- Batching keeps one attached session and lets later commands reference earlier results.
- **A failed earlier command makes later references silently `undefined`.** Check every
  entry's ok status instead of trusting the last result.
- Keep one lookup path per chain — don't mix selector-based and search-based node lookup
  for the same node.

## Connection troubleshooting

When browser operations fail, work through this checklist in order — most failures
are one of the first three, and you can fix them yourself:

1. Is the browser even running, and on a real page? Extensions do not load on
   `about:blank` and other internal pages.
2. Is the bridge listener up on the host?
3. Is the extension actually installed and enabled?
4. If all three check out and the operation still fails, surface it to the user
   with `ask_user` — include what you checked, the exact error, and what you need
   from them (e.g. open the extension settings, restart the browser).

## Safety

- Never enter secrets, passwords, payment data or tokens without explicit user direction and an authorised context.
- To fill a saved password or 2FA code into a login form, use `fill_credential` — it keeps the value out of your context. Never ask the user to paste a password or code into the chat, and never echo one you have read.
- Do not bypass authentication, anti-bot protections or access controls.
- Do not browse unrelated pages.
- Do not steal or expose cookies or session tokens; use the authorised browser session only for the requested task.

## Long-term learning

After a successful, reusable browser workflow has been verified, call `start_long_term_update` with `summary`, `verified_by`, `reuse_reason` and ordered `evidence.steps`. The backend stores the result as an L3 SOP and updates L1.

## Limitations

- All browser operations other than a plain-HTTP `read_page` are unavailable when no browser-class MCP is bound; the tool returns `browser_mcp_not_configured`.
- Plain-HTTP `read_page` does not execute JavaScript or access authenticated browser state.
