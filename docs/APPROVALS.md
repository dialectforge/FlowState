# Approvals: the contract between FlowState and flowstatemcp.com

Approvals are opt-in (`flowstate approvals on`); off, FlowState asks the user nothing and
deletes are undone with `manage_history`. With them on, an agent's delete and turning review
off wait for the user's own click or key: popup, phone, or terminal (see `approvals.py`).
This page is the phone part, which needs the site.

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
  enrolled and on, phone links are on (`flowstate approvals phone on|off`), and the release
  pins at least one real key (below).
- `item`: the review item id (integer).
- `nonce`: 43 url-safe characters (`secrets.token_urlsafe(32)`), made on this computer, one
  per item, bound to the item, the action `approve_review` and an expiry (7 days; a fresh one
  is made after that). Used up by Approve AND Reject. Never reused across items.

## 2. What the site does

1. Require the user signed in (email code / Google), check `<handle>` belongs to that account.
   The site never sees the item's contents.
2. Sign a grant and redirect the browser (302) to
   `https://<handle>.fsmcp.com/approve/<item>#g=<grant>` (fragment: not sent to any server or log).

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
`mcp-server/flowstate/approvals.py` as `FSA_PUBLIC_KEYS`. **In this release both are a marked
placeholder (`None`): every grant is refused and no phone link is shown until a release pins
the keys the web task produces.** Rotation: sign with `fsa-2` once a release pinning it is out.

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
