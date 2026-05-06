# QA testing protocol — read this before claiming you tested anything

> Curl + screenshot is not a test. Past sessions claimed end-to-end
> testing on the basis of `curl` 200s and shipped releases with
> theme-doesn't-apply-across-pages, no-Enter-to-send, no auto-scroll
> bugs that a real browser surfaces in seconds. This file exists so
> that loop doesn't repeat.

## The rule

**A green status code is not a passing test.** A 200 from `curl` says the
HTML rendered. It does not say:

- The cookie persisted across pages.
- The JavaScript ran.
- The HTMX swap landed in the right element.
- The keyboard shortcut fires.
- The scroll position is at the bottom.
- The focus moved to where it should.
- The CSS variables resolved (theme actually applied).
- The proxy didn't reset between FastAPI and Envoy.
- The user can actually do the thing.

Every one of those needs a real browser driving real interactions and
checking real DOM / pixel state. `curl` proves the route compiles. A
screenshot proves the page rendered once. Neither proves the *flow*.

## Required tooling

Use the gstack browse daemon. It is already on disk at
`~/.claude/skills/gstack/browse/dist/browse` (the QA skill prep step
verifies it). Call it `$B` in shell. Anything else (curl, requests,
view-source) is supplementary, not authoritative.

Drive the actual flow:

- `$B goto <url>` — navigate.
- `$B fill <selector> <text>` — type into a field.
- `$B click <selector>` — click.
- `$B key <key>` — press a key (Enter, Tab, etc).
- `$B js "<expr>"` — read DOM / computed style / cookie / location.
- `$B screenshot` — pixel proof.

## The minimum-viable per-surface checklist

For every feature, these are the minimum interactions and the minimum
assertions. If any check would have surfaced the bug, the bug counts as
"missed by you," not "missed because it was subtle."

### 0. Run mode selection

Pick one of two run modes per session and announce it before doing anything:

- **Live** (production cluster). Higher signal, surfaces proxy / network
  bugs (Envoy resets, slow auditor calls hitting timeout). Needs creds.
  ASK FOR CREDENTIALS. If the user does not provide them, do not
  pretend to "test" by hitting only public surfaces.
- **Local** (`uv run uvicorn`). Lower friction, but `CLAUDIA_OPS_MODE=local`
  swaps in mock chat replies. Use this only for layout / form / nav /
  theme work where API behaviour does not matter. Anything involving
  the Claude API loop, auditor timing, or proxy behaviour MUST go
  through live or `dev` mode.

If the user previously said "disable auth," the right move is `local`
mode — not patching production auth.

### 1. Chat composer

1. Navigate to `/session/new`. Verify a redirect to `/session/<id>`
   landed (`$B js "location.href"`).
2. **Type a message via fill.** Then send via the send button. Read
   the network and console. Verify the user-msg appears as a `.msg.user`
   article and the bot reply appears as a `.msg.bot` article.
3. **Press Enter in the textarea.** Verify the message sent (a new
   `.msg.user` appeared, the textarea cleared). If Enter does NOT send,
   that's a bug — file it.
4. **Press Shift+Enter in the textarea.** Verify a newline was inserted
   into the textarea, NOT a send. Read `document.getElementById('content').value`
   to confirm the `\n`.
5. **Type a long message that wraps.** Send three of these in a row.
   Read `document.getElementById('messages').lastElementChild` —
   verify it's visible in the viewport (`getBoundingClientRect().bottom`
   ≤ `window.innerHeight`). If not visible, auto-scroll is broken —
   file it.
6. Click "End session". Watch the network. The 303 redirect should land
   on the same session URL with status=ended. Take a screenshot of
   the ended state. **Wait 30s and refresh**, then watch for an Envoy
   "upstream connect error" — it indicates the auditor BackgroundTask
   blocked a worker thread.
7. Click `←home` / brand link to navigate to `/`. Verify the past session
   shows up in the "Past sessions" list. Click in. Verify all messages
   render in order.

### 2. Settings + theme picker

Theme cookie sets but doesn't visually apply on non-/settings pages is a
classic regression. Test this exact thing:

1. Navigate to `/settings`. Note the `<html class>` attribute via
   `$B js "document.documentElement.className"`. Should be `theme-sage`.
2. Click each theme swatch in turn. After each save:
   - Read `document.documentElement.className` on `/settings` itself.
   - **Navigate away** to `/` (and `/session/<id>` if logged-in chat
     exists). Read `document.documentElement.className` on those pages.
     Both must reflect the new theme.
   - Read `getComputedStyle(document.body).backgroundColor` — verify
     it actually changed between themes (sage vs blush vs contrast).
     Two consecutive themes returning the same hex is a bug.
3. **Refresh the page.** Theme must persist (cookie path / max-age
   correct). Read the cookie via `$B js "document.cookie"` to confirm
   `claudia_theme=<choice>` is present.

### 3. Setup wizard

1. With a fresh data root, navigate to `/`. Confirm the redirect to
   `/setup/1` actually fires (look at network log for a 303 → /setup/1).
2. Walk every field. Submit. Confirm step 2 renders.
3. **Walk back.** `/setup/1` after step 2 was submitted should still
   show the values you entered. (Backed by `_setup_state_load()`.)
   If they're blank, the state save is broken.
4. Step 4: drop a real document into /library (use `/library/paste`
   for a hermetic test — text only, no real Claude needed). Verify it
   shows up in the step 4 doc count.
5. Step 5: confirm the recap reflects what you entered. Click "done".
   Confirm `/` loads, and `/setup/1` no longer redirects (marker
   file written).

### 4. /login

1. Logged out, navigate to `/login`. Verify the password form renders.
2. Submit a wrong passphrase. Verify the error message renders inline
   AND the rate limit eventually engages (≥6 wrong tries → 429).
3. Log in with the correct passphrase. Confirm the cookie gets set
   (read `document.cookie`). Confirm reload still keeps you logged in
   (verify session persists across pod restart by `kubectl delete pod`
   and reconnecting).

### 5. People + library detail

1. Add a person via the form. Confirm the row appears.
2. Click the row's `<details>` to open. Confirm the HTMX swap fires
   and the inline detail loads. Read the actual rendered DOM, not just
   the network response.
3. Edit a field, save, re-open. Confirm the change persisted.
4. Copy the URL `/people/<id>` and paste into a new tab (or `$B goto`
   directly). Confirm it 303-redirects to `/people#<id>`.

## When you find a bug

1. **Take a screenshot of the broken state.** Read the file inline so
   the user sees it.
2. **Capture the diagnostic.** Console errors, network responses, DOM
   read of the offending element, computed style if relevant.
3. **Write the repro as a one-liner.** "Click X, observe Y, expected Z."
4. **File it as a TaskCreate AND append to TODOS.md** if it's not going
   to be fixed in the same session.
5. Don't fix and forget. Atomic commit per fix, regression test, and
   re-run the relevant per-surface checklist after each fix.

## Forbidden patterns

- Reading a screenshot, calling it "looks good," and moving on without
  verifying interaction. A screenshot of `/settings` does not prove
  the theme applies on `/`.
- Driving the flow with curl alone, then claiming "tested end-to-end."
  curl proves routes compile. It does not prove the flow.
- Skipping the login step and not asking for the password. ASK. If the
  user says no, switch to local mode and say so explicitly.
- Closing tasks before re-running the checklist post-fix.
- Reporting "all themes work" because the screenshots showed slightly
  different colours — without checking that pages OTHER THAN `/settings`
  also adopt the chosen theme.
