# QA testing protocol — read this before claiming you tested anything

> Written 2026-04-30 after a session where I claimed end-to-end testing
> but had only driven curl + screenshots of static pages. The user shipped
> v0.6.0 to the live cluster and immediately found bugs I should have
> caught: theme picker doesn't apply across pages, hardcoded "your dad"
> copy that's tone-deaf in his blended-family setup, Envoy upstream-reset
> on session end as the kid, no enter-to-send / shift-enter-for-newline
> in the chat composer, no auto-scroll on new content. Every one of those
> is a thirty-second find with a real browser. None survive curl-only.

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
- `$B snapshot -i` — list interactive elements with `@e<n>` refs.
- `$B fill @eN "text"` — fill an input.
- `$B click @eN` — click.
- `$B press @eN <key>` — send a key (Enter, Shift+Enter, Escape, Tab).
- `$B js "<expr>"` — read DOM state, document.activeElement,
  scrollTop, getComputedStyle, etc.
- `$B screenshot <path>` then `Read` the file inline.
- `$B network` — every request fired since the last navigate, with
  status codes. If `0` results came back from a fetch, see it here.
- `$B console --errors` — JS console errors. Watch for HTMX errors,
  fetch failures, promise rejections.

## What "tested" means per surface

For every feature, these are the minimum interactions and the minimum
assertions. If any check would have surfaced the bug, the bug counts as
"missed by you," not "missed because it was subtle."

### 0. Run mode selection

Pick one of two run modes per session and announce it before doing anything:

- **Live** (production cluster). Higher signal, surfaces proxy / network
  bugs (Envoy resets, slow auditor calls hitting timeout). Needs creds:
  HTTP basic auth password for adult, kid passphrase for kid mode.
  ASK FOR CREDENTIALS. If the user does not provide them, do not
  pretend to "test" by hitting only public surfaces.
- **Local** (`uv run uvicorn`). Lower friction, but `CLAUDIA_OPS_MODE=local`
  swaps in `InMemorySessionStore` + mock chat replies. Use this only
  for layout / form / nav / theme work where API behaviour does not
  matter. Anything involving the Claude API loop, auditor timing, or
  proxy behaviour MUST go through live or `dev` mode.

If the user previously said "disable auth," the right move is `local`
mode — not patching production auth. Write that down once and stop
relitigating it.

### 1. Chat composer

Per scenario (adult AND kid both):

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
6. Click "End chat". Watch the network. The 303 redirect should land
   on the same session URL with status=ended. Take a screenshot of
   the ended state. **Wait 30s and refresh**, then watch for an Envoy
   "upstream connect error" — it indicates the auditor BackgroundTask
   blocked a worker thread.
7. Click `←home` / brand link to navigate to `/`. Verify the past chat
   shows up in the "Past chats" list. Click in. Verify all messages
   render in order.

### 2. Settings + theme picker

The bug I missed: theme cookie sets but doesn't visually apply on
non-/settings pages. Test this exact thing:

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
4. Step 2: drop a real document into /library (use `/library/paste`
   for a hermetic test — text only, no real Claude needed). Verify it
   shows up in the step 2 doc count.
5. Step 3: confirm the recap reflects what you entered. Click "done".
   Confirm /admin (kid mode) or / (adult mode) loads, and `/setup/1`
   no longer redirects (marker file written).

### 4. Kid /login + /help

1. Logged out, navigate to `/login`. Verify the "need help right now?"
   link is visible and **clickable** (`$B click` it, confirm /help
   renders without auth).
2. Submit a wrong passphrase. Verify the error message renders inline
   AND the rate limit eventually engages (≥6 wrong tries → 429).
3. Set the passphrase via /login first-time form. Confirm the cookie
   gets set (read `document.cookie`). Confirm reload still keeps you
   logged in (this is what KidSessionStore is supposed to fix —
   verify it actually works against a real pod restart by `kubectl
   delete pod` and reconnecting).

### 5. Setup-name editability

The bug I missed: `kid_parent_display_name` is set at install via Helm
and there is no in-app way to change it. If the parent realises after
deploy that "your dad" is wrong (because the bio dad is dead and they
are the step-parent), they have to `helm upgrade` to change one string.
That is a UX failure. The setup wizard should let the parent edit this,
and the default should be neutral ("your parent") rather than "your
parents" or any role-loaded word.

Verification: navigate to /setup/1 in kid mode. Look for an editable
field for "what claudia should call the parent in copy". If absent,
file a bug.

### 6. People + library detail

1. Add a person via the form. Confirm the row appears.
2. Click the row's `<details>` to open. Confirm the HTMX swap fires
   and the inline detail loads. Read the actual rendered DOM, not just
   the network response.
3. Edit a field, save, re-open. Confirm the change persisted.
4. Copy the URL `/people/<id>` and paste into a new tab (or `$B goto`
   directly). Confirm it 303-redirects to `/people#<id>` (the fix
   landed in v0.5.2 — if it doesn't redirect any more, that's a
   regression).

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
- Skipping the kid login step ("would need passphrase") and not asking
  for it. ASK. If the user says no, switch to local mode and say so
  explicitly.
- Closing tasks before re-running the checklist post-fix.
- Reporting "all themes work" because the screenshots showed slightly
  different colours — without checking that pages OTHER THAN `/settings`
  also adopt the chosen theme.

## Open bugs from the v0.6.0 ship the next session inherits

These were flagged by the user on 2026-04-30 after v0.6.0 went live and
must be the first work the next session does. Each gets its own commit,
its own atomic fix, its own regression test, and its own re-verification.

1. **Theme picker does not actually apply across pages.** /settings
   sets the cookie but the rendered theme on /, /session/<id>, /help,
   /people, /library does not switch. Diagnose: read
   `document.cookie`, `document.documentElement.className`, and
   `getComputedStyle(document.body).backgroundColor` on each page after
   a theme change.
2. **`kid_parent_display_name` is not editable in-app.** The user has
   "dad" baked in via Helm value but the kid's bio dad is dead and he
   is the step-dad. Fix: (a) move the parent display name to a
   /setup/1 editable field, persisted to /data; (b) change the Helm
   default from "your parents" to "your parent" (or drop entirely and
   require setup); (c) audit the prompts and templates for any
   hardcoded "dad/mum/parents" strings that should defer to the
   configured value.
3. **Envoy "upstream connect error or disconnect/reset before headers,
   reset reason: connection termination" when ending a chat as Jasper.**
   Diagnose: pod logs around the time of session-end. The auditor
   `BackgroundTask` may be holding a worker thread; the synchronous
   Anthropic client call inside the task blocks the loop. Likely fix:
   move `_run_audit_and_apply` to `asyncio.create_task` with the
   client call wrapped in `asyncio.to_thread` (similar to the
   tool_loop call in /session/{id}/message).
4. **No keyboard shortcuts in the chat textarea.** Enter does nothing,
   send is mouse-only. Fix: add a JS handler — Enter sends (calls
   `form.requestSubmit()`), Shift+Enter inserts a newline, IME
   composition events do not trigger send.
5. **No auto-scroll to latest message on send/receive.** The
   `claudiaScrollBottom()` helper exists but the user reports manually
   scrolling. Diagnose: check if it's wired to the right HTMX event
   AND whether the swap target's `scrollIntoView` arrives before the
   layout settles. May need a `requestAnimationFrame` round-trip or a
   delay.

## After fixing

Re-run the checklist for the affected surface. Take before / after
screenshots. Ship as a v0.6.x patch. Report each bug with
its commit SHA so the user can verify against the deployment.
