# Approvals: the contract between FlowState and flowstatemcp.com

Approvals are opt-in (`flowstate approvals on`); off, FlowState asks the user nothing and
deletes are undone with `manage_history`. With them on, an agent's delete and turning review
off wait for the user's own click or key: popup, phone, or terminal (see `approvals.py`).
This page is the phone part, which needs the site: the approve link for one item (sections
1-4) and the review list of everything waiting (section 5).

## Where the user sees what's waiting (no terminal needed)

Whatever the approvals setting, the user can see and decide the whole review queue three ways
(`waiting.py`). Nothing is shown unless the user asks, or has approvals on.

- **Chat walk:** `manage_review(action="walk")`, called when the user asks. On apps on the
  popup allowlist that declared form elicitation (`approvals.popup_allowed`: Claude Code,
  Cursor, VS Code; never claude.ai / Anthropic), FlowState sends one form per item: a required
  boolean `approve` (ticked = approve, unticked = reject), an optional note (the reject reason)
  and, for a project delete, the project's name. Decline skips the item; cancel, no answer
  within 120 s or a failure stops the walk. Each decision goes through `popup.as_user` with the
  label `chat (<app>)`. Nothing the calling app passes (`user_request`, extra arguments) is an
  answer.
  - **No boxes from outside this computer.** Requests that came through the relay listener or
    the public (tunnel) listener never get a box, here or as the approvals popup
    (`popup.no_box`): there the app's name is only its own say-so. `remote_auth.Gate` marks
    every request with its listener (`lan` / `public` / `relay`; the relay agent builds its
    app with `relay=True`), in the server, never from a header.
  - **Too fast = not shown.** A box answered in under 1 second (`popup.HUMAN_SECONDS`) was not
    seen by a person (an app that auto-declines, for example): the walk stops at once, nothing
    is logged for it, and the answer is the plain list. The approvals popup does the same and
    passes on its one-line fallback. Declines and closes are logged in `approval_log` only when
    the box was plausibly shown; approve / reject are logged as always.
  - **The plain answer** (read-only apps, other apps, the relay, too fast): `note` "No boxes
    were shown here; nothing was decided.", `waiting` (the total), `items` (one page: 20,
    oldest first: id, project, kind, one-line summary), `showing` (which part), `next_page`
    (`manage_review(action="walk", after=<last id>)`), `how_to_decide` and the links.
    `manage_review(action="list")` pages the same way (`limit`, `after`, `next_page`), also one
    line per item (with its status when `status` isn't pending); `list` with `item_id=<id>`
    gives one item with every field. After a
    real walk the note gives the counts: approved, rejected, skipped, and that only those
    answers were logged.
- **Local link:** `http://127.0.0.1:<port>/`, the review page (`review_page.py`) started inside
  the FlowState process the first time a link is needed: one per computer and database (a
  state file under the data folder names the port; another process pings `/ping` and reuses
  it), bound to 127.0.0.1 only, port `FLOWSTATE_REVIEW_PORT` (default 8770, any free port if
  taken). A visit starts a browser session at once (no key; the link is meant to be clicked).
  Every change needs that session's cookie (HttpOnly, SameSite=Strict), its CSRF token, the
  Host 127.0.0.1/localhost and a same-origin `Origin` header. `flowstate review-page` keeps its
  one-time key.
  **Only for apps on this computer** (`popup.on_this_computer`): stdio, or the LAN listener when
  the socket peer is loopback (127.0.0.0/8, ::1). `remote_auth.Gate` decides it from the
  listener and the real socket peer (`flowstate.on_this_computer` in the ASGI scope; uvicorn
  runs with `proxy_headers=False`), never from a header (`X-Forwarded-For`, `Host`,
  `X-FlowState-*` change nothing). The relay listener (its agent connects from loopback), the
  public listener, a LAN peer elsewhere and unmarked requests are not this computer: no
  127.0.0.1 URL anywhere in the brief or the walk, and the page isn't started for them. They
  get the phone link when the relay is set up, and the text "on the computer where FlowState
  runs (the FlowState review shortcut, or `flowstate review-page`), or on its tunnel review
  page if it has one" in the brief's line and the walk's `how_to_decide`.
- **Phone list (relay users):** the bookmarkable `https://flowstatemcp.com/review/<handle>`;
  section 5.
- **Desktop shortcut:** `setup_flowstate.py` puts "FlowState review" on the desktop (Windows
  `FlowState review.cmd`, macOS `FlowState review.command`, Linux `flowstate-review.desktop`).
  It runs `flowstate review-page`; when a FlowState on the computer already serves this
  database's page on that port, it opens that one.
- **Tunnel review page (own tunnel, no relay):** `https://<your tunnel>/review/tunnel` on the
  public listener, behind the owner passphrase; see docs/REMOTE-ACCESS.md section 4. It can
  decide everything the local page can, user-only items included (path `tunnel page`).

Decisions in the chat walk, on the phone list and on the tunnel page are all logged in
`approval_log` (path `chat`, `phone` or `tunnel page`), user-only items or not.

The one line an agent passes on when a user-only item waits and there is no phone link is
`Waiting for the user: ask them to open the FlowState review shortcut on their desktop and
approve it there (or run `flowstate approve <id>` in a terminal).` For an app that came in
through this FlowState's own public listener it names the tunnel page first: `... ask them to
approve it on their tunnel review page (https://<tunnel>/review/tunnel) or with the FlowState
review shortcut on their desktop (or run `flowstate approve <id>` in a terminal).` Honest limit: a program on the user's computer (an agent
that runs commands or drives the user's browser) can reach the 127.0.0.1 page, as it could run
`flowstate approve`.

## Approve / reject through the relay: phone only by default (2.4.2)

An AI that reaches FlowState through the relay listener can't decide any review item in the
chat by default. What the code enforces (`waiting.phone_only`, called first in
`server._review_decide`, so `manage_review` approve / reject and the older `review_approve` /
`review_reject` names alike):

- When the request carries an app identity (an agent, not the terminal command, the review
  page, the popup's own answer or the phone page), came through the relay listener
  (`popup.listener() == "relay"`, set by `remote_auth.Gate`, never from a header), and the item
  is pending, the call is refused whatever `user_request` says (also with none). The answer:
  `error` `phone_only`, `message` ("Nothing was decided: review item #N still waits for the
  user. ..."), `item_id`, `kind`, `status` "waiting for the user", `next` (the one line to pass
  on, with the item's phone link), `phone` (the item's own `/approve/<handle>/<id>?n=...` link
  when phone links are available, else the review list's, else no link), `how_to_decide` (the
  walk's wording) and `dont_approve`. Nothing is changed or logged as a decision. A missing or
  already decided item gets the usual answer.
- The setting: `approval_settings` key `relay_chat_approvals` (`approvals.relay_chat_on`),
  `on` or absent / `off` (the default). Only `flowstate relay chat-approvals on|off` changes it
  (`approvals.set_relay_chat`, logged in `review_mode_log` with scope `relay_chat_approvals`);
  turning it on asks for `y` on the user's own terminal (`cli_review.open_terminal`; piped input
  doesn't count), off needs nothing. No MCP tool reads or writes the key (a test checks).
- On: relay requests are checked exactly as before (quote the user in `user_request`; the
  queuing app needs the user's own words). User-only items stay locked either way: deletes
  and turning review off queued with approvals on (approve gets "waiting for the user"), and
  "did you say this?" items (`user_only`).
- Not affected: stdio, the LAN listener and the public (own tunnel) listener; the phone page,
  the phone review list and the popup (they decide as the user, with no app identity).
- A disagreement card is the same rule when an agent uses `manage_review` approve / reject:
  chat approvals off means `phone_only` and the review link; on means the agent may pass
  `keep="a"`, `"b"` or `"both"` with the user's words. The in-chat card's buttons call
  `resolve_conflict_card` instead. That tool is app-only (`_meta.ui.visibility` is `["app"]`,
  so it is left off the model's tool list unless the host advertises MCP Apps) and it refuses
  `user_request`, `message`, `text` and `reason`. Its audit path is `card`. The review page
  and the phone page audit as `review page`; an agent quoting the user audits as `chat`. A
  card closed because a decision was replaced, or because the user said the old possible-conflict
  pair fits together, is audited as `automatic`. Card taps are not cryptographically proven to
  be a person yet. That check is a known follow-up; a host that lets the model call the
  app-only tool by name can still save a resolution.

## What a queued delete shows

With approvals on, a delete stays in the queue until the user approves it. The review page,
the phone page and `manage_review` list build the list of rows from the database at the moment
they answer: the record's type, name, project and key fields, and for a project or component
the count and the named child records that go with it. The agent's own sentence is not that
list. If one was stored, it appears only as a secondary line, "AI's note".

## "The user said so" is always user-only

One kind of review item is user-only whatever the approvals setting: an app asking to change
an existing record's source tag to `user_stated` (`manage_memory` `set_source`, or
`mark_checked` with `source="user_stated"`). What the code enforces (`provenance.py`,
`server._review_decide`):

- The tag is not changed. One item of kind `source` is queued ("Did you say this yourself?");
  a repeat for the same record, from any app, gets the same open item back.
- Approvals off: nothing is shown to the user. The answer tells the agent the record stays as
  it is until the user confirms on the review page (`flowstate review-page`) or with
  `flowstate approve <id>`. Approvals on: the same paths as a delete (popup on allowlisted apps,
  the phone link, the terminal).
- Any app calling `manage_review` approve or reject on it is refused (`user_only`), whatever
  `user_request` says; with approvals on, an app's approve gets the "waiting for the user"
  answer instead. Only a call with no app identity decides it: the terminal command, the review
  page, the popup's own answer and the phone page. Every decision is logged in `approval_log`.
- Approving sets the tag to `user_stated` with `source_set_by = 'user'`. Rejecting leaves it.
- Moving a tag away from `user_stated` needs nothing and is stamped with the app.
- A `user_stated` `log_work` that the clean-up duplicate check matches with another app's
  `ai_inferred` record (same kind, same project) is saved as `ai_inferred`.

Not covered: an app tagging a new record `user_stated` at log time. That is allowed (it may be
quoting the user); FlowState stores and shows which app did it, and can't check it.

## 1. The link FlowState gives the agent

`https://flowstatemcp.com/approve/<handle>/<item>?n=<nonce>`

- `handle`: this device's relay handle (`relay.json`); only shown when the relay agent is
  enrolled and on and phone links are on (`flowstate approvals phone on|off`). This release
  pins the site's published keys (section 3), so that condition is met.
- `item`: the review item id (integer).
- `nonce`: 43 url-safe characters (`secrets.token_urlsafe(32)`), made on this computer, one
  per item, bound to the item, the action `approve_review` and an expiry (7 days; a fresh one
  is made after that). Used up by Approve AND Reject. Never reused across items.

## 2. What the site does

1. Require the user signed in (email code / Google; not signed in: `303` to
   `/signin?next=...`), check `<handle>` is an active device of that account. The site never
   sees the item's contents.
2. Show a Continue page. Only its Continue POST signs a grant and redirects the browser (`303`)
   to `https://<handle>.fsmcp.com/approve/<item>#g=<grant>` (fragment: not sent to any server
   or log).

## 3. The grant

JWS compact serialization, ES256 (P-256, SHA-256; signature = raw `r || s`, 64 bytes, base64url).

Header (exactly): `{"alg": "ES256", "kid": "fsa-1" | "fsa-2", "typ": "fsa+jwt"}`

Payload:

| claim | value | FlowState checks |
|---|---|---|
| `iss` | `"https://flowstatemcp.com"` | equal |
| `aud` | `"<handle>.fsmcp.com"` | equal to this device's handle + relay domain |
| `sub` | Supabase user id (string) | non-empty string |
| `handle` | `"<handle>"` | equal to this device's handle |
| `item` | integer from the link | equal to the page's item; JSON integer, not a string |
| `nonce` | `n` from the link | 16-64 url-safe chars; equal (constant time) to the item's open nonce |
| `action` | `"approve_review"` | equal |
| `iat` | seconds since epoch | not more than 60 s in the future |
| `exp` | `iat + 300` | after now; `exp - iat` at most 300 |

Keys: private key only as the Worker secret `FSA_SIGNING_KEY` (JWK). Public JWKs
(`{"kty": "EC", "crv": "P-256", "x": ..., "y": ...}`, kid `fsa-1` active, `fsa-2` backup)
published at `https://flowstatemcp.com/.well-known/flowstate-approval-keys.json` and pinned in
`mcp-server/flowstate/approvals.py` as `FSA_PUBLIC_KEYS`. This release pins both real
published keys, `fsa-1` and `fsa-2`; they match the published file (checked October 22, 2026).
A grant signed with any other key, or naming another kid, is refused. Rotation: the site can
switch to `fsa-2`, which this release already trusts.

## 4. What FlowState does with it

`GET /approve/<item>`: an empty page (strict CSP: `default-src 'none'; script-src 'self';
style-src 'self'; connect-src 'self'; frame-ancestors 'none'`, `X-Frame-Options: DENY`,
`Cache-Control: no-store`, no referrer). Its script reads `#g=`, removes it from the address bar
(`history.replaceState`) and posts it:

- `POST /approve/<item>/open` `{"grant"}` (JSON, same origin): checks the grant as above and that
  the item is pending and user-only. Each grant opens the page once. Answers with the item from
  the local database and a page token, also set as cookie `fsa_page` (Secure, HttpOnly,
  SameSite=Strict, 5 minutes).
- `POST /approve/<item>/decide` `{"grant", "token", "decision": "approve" | "reject",
  "confirm_name"?}`: the same grant checked again, the token in the body equal to the cookie and
  to the one stored for that grant, not used before. Then Approve or Reject; the nonce is used up.

Errors are JSON `{"error", "message"}` with status 403 (`phone_approvals_unavailable`,
`bad_grant`, `unknown_key`, `bad_signature`, `grant_expired`, `wrong_device`, `wrong_item`,
`not_open`, `grant_used`, `bad_token`, `bad_request`). Every decision is logged
(`manage_history(action="list")` -> `approvals`: path `phone`, decision, time).

Limit: a leaked site key still can't approve without the one-use code on the user's computer
and a tap on their own FlowState's page; an agent that controls the user's own logged-in browser
could.

## 5. The review list (everything waiting, one bookmarkable link)

### 5a. The bookmark link and `/review/start`

The link FlowState gives (in the brief's review-queue line and in the walk's plain answer):

`https://flowstatemcp.com/review/<handle>`

- No code in it, so nothing secret lands in chat history, and the user can bookmark it.
- Shown only when the relay agent is enrolled and on, phone links are on (`flowstate approvals
  phone on|off`) and the release pins the site's keys (this release pins `fsa-1` and `fsa-2`):
  the same conditions as section 1.

1. **The site, bare link** `GET /review/<handle>` (no query): validate the handle; not signed
   in: `303` to `/signin?next=/review/<handle>`; the handle not an active device of the
   account: the existing refusal page; else `303` to
   `https://<handle>.fsmcp.com/review/start?t=<ticket>` (`Cache-Control: no-store`,
   `Referrer-Policy: no-referrer`). The site sees nothing about the contents.

   **The start ticket** (2.4.0, task 38) is a JWS compact token signed exactly like the grants
   in section 3 (same signing key, the pinned `FSA_PUBLIC_KEYS`, kids `fsa-1` / `fsa-2`):

   - header `{"alg": "ES256", "kid": "fsa-1", "typ": "fsa+jwt"}` (kid = the site's signing kid)
   - payload `{"iss": "https://flowstatemcp.com", "aud": "<handle>.fsmcp.com", "sub": "<site
     user id>", "handle": "<handle>", "item": 0, "action": "start_review", "nonce": "<fresh
     random base64url, 22-43 characters>", "iat": N, "exp": N + 300}`

   FlowState checks it with the same code as every grant (`approvals.verify_start_ticket` ->
   `_claims(ticket, handle, now, keys, "start_review", 0)`): pinned key and signature, times
   (at most 5 minutes' life, 60 s clock skew), `iss`, `aud` and `handle` against this device's
   own `relay.json` (never the request), `action`, `item` 0, a non-empty `sub`, and the nonce's
   format. The action check keeps the kinds apart: an `open_review` or `approve_review` grant
   is not a ticket, and a `start_review` ticket opens no list and approves nothing.
2. **FlowState** `GET /review/start?t=<ticket>` (`review_phone.py`, on the HTTP app, so the
   relay reaches it; GET only, `HEAD` and `POST` get 405). The ticket is its only sign-in.
   - Phone review off or unavailable (checked first): a plain small page saying phone review
     is off on this computer, pointing to the desktop shortcut / `flowstate review-page`, the
     tunnel review page (`https://<your tunnel>/review/tunnel`, owner passphrase) for users with
     their own tunnel, and the relay (`flowstate relay enroll`, `flowstate approvals phone on`)
     as one more option. No code is made.
   - No ticket, or one that fails any check above, or one already used here: a plain page in
     the same style and headers, status `400` when it isn't even shaped like a ticket (checked
     before any crypto or database work) and `403` otherwise. It says in plain words to open the
     "Waiting for you" link from flowstatemcp.com again (an expired one: "This link expired after
     5 minutes."). No code is made, nothing counts toward any limit, the ticket is never echoed,
     and it isn't logged (the access log has the path without the query). No redirect, so no
     loops.
   - A good ticket: a FRESH one-use code for the action `open_review` (43 url-safe characters,
     `secrets.token_urlsafe(32)`, in `review_open_nonces`, valid 15 minutes), then `303` to
     `https://flowstatemcp.com/review/<handle>?n=<code>`. The handle comes from this device's
     `relay.json`, never from the request (no open redirect).
   - Limits (`approvals.start_nonce`): each ticket makes at most one code (a hash of its nonce
     is kept with the code, migration 022); at most 10 codes made here open at once (making
     one more expires the oldest); at most 30 made per hour per site user (`sub`, stored with
     each code); and 120 per hour for all users together, as a backstop (only reachable with
     a leaked signing key, and it keeps the table small even then). Over a limit: a plain
     `429` page (`Retry-After: 3600`). Codes from before 022 (no `sub`) count only toward the
     120.
   - Headers: the `/review` page's (CSP `default-src 'none'; script-src 'self'; style-src
     'self'; connect-src 'self'; frame-ancestors 'none'`, `X-Frame-Options: DENY`), plus
     `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
3. **The site, with the code** `GET /review/<handle>?n=<code>`: its normal flow: sign-in check,
   device check, a Continue page. Only the Continue POST signs the grant and `303`s to
   `https://<handle>.fsmcp.com/review#g=<grant>`.

Codes made by older 2.4 builds for chat links (`?n=` in the link itself, valid 7 days) keep
working until they expire; they don't count against the limits above.

### 5b. The grant and the list (unchanged)

The grant: exactly as section 3 (header `{"alg": "ES256", "kid": "fsa-1" | "fsa-2", "typ":
"fsa+jwt"}`, ES256, keys, `iss`, `aud`, `sub`, `handle`, `nonce` format, `iat` / `exp` with
`exp = iat + 300`), except `"action": "open_review"` and `"item": 0` (a JSON integer).
FlowState checks every claim the same way, then uses up the code (one conditional UPDATE that
only succeeds while it is unused and unexpired), whichever way the code was made.

What FlowState does (`review_phone.py`):

- `GET /review`: an empty page with the headers above. Its script reads `#g=`, removes it from
  the address bar (`history.replaceState`) and posts it.
- `POST /review/open` `{"grant"}` (JSON, same origin): checks the grant and uses up the code;
  starts a session: cookie `fs_review_phone` (HttpOnly, Secure, SameSite=Strict, Max-Age 900,
  Path `/review`) plus a page token in the answer; answers with the waiting items (all
  projects, oldest first, up to 50) and the total waiting, from the local database.
- `POST /review/decide` `{"token", "item_id", "decision": "approve" | "reject",
  "confirm_name"?, "reason"?}` (JSON, same origin): the cookie and the page token must match a
  live session. Approve or Reject goes through the same user-only path as the approve page
  (`popup.as_user`, label `phone (<handle>)`) and is logged (`approval_log`, path `phone`).
  Answers with the updated list and total.

Errors are JSON `{"error", "message"}` with status 403 (`phone_approvals_unavailable`,
`relay_not_enrolled`, `bad_grant`, `unknown_key`, `bad_signature`, `grant_expired`,
`wrong_device`, `wrong_item`, `grant_used`, `bad_token`, `bad_request`); a decision FlowState
refuses (for example a project delete without its exact name) is 409.

### 5c. Honest limits of the bookmark link

- Only the site can make `/review/start` issue a code (it signs the ticket for the signed-in
  owner), and each site user has their own hourly limit, so hitting `/review/start` without a
  ticket can't lock the owner out. The list's protection is the site's sign-in plus its
  signing key: a leaked site signing key alone could open the list on a reachable device (and
  fill the 120-an-hour backstop). Per-item `/approve` links still need the one-use code made on
  this computer for that item.
- Compatibility: the site sends tickets first (it must be deployed before devices update).
  An older device ignores `?t=` (its `/review/start` reads no query) and works as before, still
  lockable until it updates. A newer device with an older site gets no ticket and shows the
  plain page, so phone review is unavailable until the site is updated. Chat links with 7-day
  codes and per-item `/approve` links don't use `/review/start` and are unchanged.
- `flowstate approvals phone off` turns every phone link and `/review/start` off.
- If the computer is off or the relay agent is down, the bare link can't open the list.
