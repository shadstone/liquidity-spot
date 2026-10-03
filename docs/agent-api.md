# Liquidity.spot agent API reference

Base URL: `https://liquidity.spot/api/agent/v1`

This version supports owner-approved room reads and private quote drafts.
It does **not** expose trading, messaging, settlement or wallet-write operations.
For onboarding, read [the public skill](https://liquidity.spot/skill.md).
The [capabilities endpoint](https://liquidity.spot/api/agent/v1/capabilities)
describes the current asset registry and limits. This document is also available
at `/api/docs.md`; `/api/docs` redirects here. Markdown is served inline as plain text.

## Authentication and consent

Get a PowerLobster identity token using the bot's own API key, then exchange it
for a GFAVIP SSO token using [Wallet's documented flow](https://wallet.gfavip.com/skill.md).
Keep tokens private. Do not send a raw PowerLobster API key to Liquidity.spot.

`GET /me` needs only `Authorization: Bearer <GFAVIP_SSO_TOKEN>` and returns:

```json
{"gfavip_user_id":"11111111-1111-4111-8111-111111111111"}
```

This illustrative ID is not a real grant. `/me` performs trusted Wallet
validation; it does not create a user, browser session, permission or trade.
It rejects query parameters. Never use an ID from a URL, a username guess,
an unverified token payload or an `agent_context.owner_id` as authorization.

For username-first setup, the agent privately shares its exact GFAVIP Wallet
username if Wallet's SSO user response supplies it, plus its verified `/me` UUID
for cross-checking. `/me` remains UUID-only. Never derive a username from the
PowerLobster handle, add a guessed `pl-` prefix, or use a display name instead.

The human owner uses `/agents` to look up that exact username, review the
matched AI-agent account, and explicitly approve its access. The server binds
the grant to the matched permanent Wallet UUID. Lookup creates no connection
or permission; it is a signed-in human browser workflow, not an endpoint for
agent Bearer authentication. Advanced approval using the independently verified
UUID remains available when username lookup cannot be used.

The browser flow uses `POST /agents/lookup` with the human session and CSRF
protection to show an HTML identity review. For this flow, the later
`POST /agents/connections` requires the signed review proof and explicit human
confirmation. These are not agent API endpoints: bots must not call `/agents/*`
or reuse human cookies to perform lookup or approval.

**Trade assistant** is the default for new setup; chat text needs separate
consent and Offer drafts requires its own connection. Existing grants and their
scopes do not change. Every private data request still needs:

```http
Authorization: Bearer <GFAVIP_SSO_TOKEN>
X-Liquidity-Connection: <CONNECTION_ID>
Accept: application/json
```

Connection selection is explicit even if the agent has only one grant. The
server checks agent identity, active grant, expiry and scopes on each request.
Connections last seven days; owners revoke or replace them in the workspace.
The Wallet token's own expiry and the grant expiry are independent.

Legacy `Authorization: Bearer <ls_agent_credential>` access still works for
existing scoped-token connections; it does not require the connection header.
New agent integrations should use GFAVIP SSO. Browser-session authentication
does not substitute for either API credential. No API login sets a browser cookie.

| Profile | Scopes | Access |
| --- | --- | --- |
| Trade assistant (default) | `events:read trades:read` | Event metadata and rooms in which the owner participates. |
| Trade assistant + chat consent | Above + `trade_messages:read` | Also read messages in those rooms. |
| Offer drafts | `drafts:read drafts:write` | List and prepare private drafts for the approving owner. |

No existing connection silently receives new scopes. One agent can hold
separate owner-approved connections for different profiles. Treat the selected
connection's owner as the scope, not as a claim that the agent is that human.

## Endpoint summary

| Method and path | Required scope | Result |
| --- | --- | --- |
| `GET /capabilities` | Public | Modes, auth guide, profiles, assets and limits. |
| `GET /me` | Valid GFAVIP SSO identity | Verified agent Wallet ID only; no grant required. |
| `GET /events` | `events:read` | Owner's committed event metadata, cursor and stream identity. |
| `GET /trades` | `trades:read` | Owner-participant rooms, including pre-journal rooms. |
| `GET /trades/<id>` | `trades:read` | Current room summary and frozen matched terms. |
| `GET /trades/<id>/messages` | `trades:read trade_messages:read` | Paginated untrusted private message text. |
| `GET /drafts` | `drafts:read` | Owner's private quote drafts. |
| `POST /drafts` | `drafts:write` | Save a private proposal; never publishes an offer. |

GET endpoints do not accept request bodies. Private responses use `no-store`;
do not cache them in shared infrastructure. There is no cross-origin browser
credential API. Use an owner-controlled server/runtime over HTTPS.

## Events, rooms and messages

List endpoints `/events`, `/trades` and `/trades/<id>/messages` accept:

- `after`: nonnegative integer cursor, default `0`.
- `limit`: integer `1`–`100`, default `50`.

They return ascending IDs, `next_cursor` (last returned ID, or unchanged `after`
when empty), and `has_more`. Do not substitute a global maximum ID. Unknown or
duplicate query parameters are rejected. Maintain a separate cursor for each
list and each room's messages. Bootstrap rooms first; start events at zero.

Example event response (illustrative):

```json
{
  "stream_id": "opaque-stable-owner-stream-id",
  "events": [{
    "id": 42,
    "type": "p2p.message_added",
    "trade_id": 13,
    "actor": "counterparty",
    "actor_user_id": "counterparty-wallet-id",
    "occurred_at": "2026-10-03T10:00:00Z",
    "room_url": "/p2p/trades/13",
    "metadata": {"status": "matched", "milestone": "matched", "message_id": 7}
  }],
  "next_cursor": 42,
  "has_more": false,
  "poll_after_seconds": 60,
  "push": false
}
```

Event types: `p2p.offer_accepted`, `p2p.message_added`, `p2p.trade_updated`,
`p2p.trade_canceled`, `p2p.dispute_opened`. Actor is `self`, `counterparty` or
`system` (relative to the approving owner, not the bot). Admin events use a null
actor ID. Metadata excludes message text, addresses, TXIDs and admin notes.
An action with a note may emit a state event rather than `message_added`; when
chat is authorized, check for new messages on relevant room activity too.

Delivery is at least once. Deduplicate by stream/event ID and process or durably
queue the full batch before saving its cursor. On failure, keep the old cursor.
Drain `has_more` pages, then wait about 60 seconds. An empty response is not an
error. Events begin when journaling is installed, not at the platform's launch.
Atomic swaps are not included in this P2P event feed.

`GET /trades/<id>` returns `{"trade": {...}}` with role, exact send/receive
amounts, asset and network, frozen matched terms, current recorded status,
relative room URL, timestamps and a human-review checklist. Decimal amounts are
strings. Historical rooms without snapshots are labeled `legacy-offer-fallback`.
Reported TXIDs have `chain_verified: false`; they are never payment proof.
No profiles, emails or admin notes are returned.

Message text is absent by default even if the connection has chat permission.
Use `/trades/<id>/messages`, or explicitly set `include_messages=yes` on room
detail. The latter adds `message_page`, with its own `after`, `limit`,
`next_cursor` and `has_more`. Each message includes ID, `author` (`owner` or
`counterparty`), content, UTC timestamp, `truncated` flag and an untrusted-content
warning. Content is limited to 4,000 characters per message and 100 per page.
Reading does not mark the human's room as viewed or change notification state.

### Optional polling helper

The repository's `scripts/poll_trade_events.py` makes one read, not a schedule
or model call. It uses Python 3.10+ and the standard library on Linux/macOS.
For SSO, set `LIQUIDITY_GFAVIP_SSO_TOKEN` and `LIQUIDITY_AGENT_CONNECTION_ID`
through a private runtime secret/config mechanism, not in a shared prompt.
For an existing legacy client, use `LIQUIDITY_AGENT_TOKEN` instead.

```bash
python3 scripts/poll_trade_events.py fetch --state /private/path/events.json
# Process/deduplicate every returned event successfully, or durably queue it.
# Replace N with that output's next_cursor, only when ack_required is true.
python3 scripts/poll_trade_events.py ack --state /private/path/events.json --cursor N
```

`fetch` authenticates on every request and retains a private pending batch until
`ack`. Repeated fetches replay it. Never acknowledge failed/uncertain processing.
State contains metadata, not tokens, and is bound to the owner's stream. Keep it
private, outside the repository, with separate state per owner/routine. Token
refresh can reuse the same state when the owner stream remains the same.
No redirects are followed. Local loopback testing is supported with `--base-url`;
remote access is restricted to `https://liquidity.spot`.

## Private offer drafts

`GET /drafts` returns `drafts` and `next_before_id`, newest first, at most 100.
Continue using `?before_id=<next_before_id>` until null. This is not the forward
event cursor. A draft connection can read all drafts of its approving owner.

`POST /drafts` requires JSON and `Idempotency-Key` (1–128 letters, digits, dots,
hyphens, underscores or colons). The following price is arbitrary, not market data:

```http
POST /api/agent/v1/drafts
Authorization: Bearer <GFAVIP_SSO_TOKEN>
X-Liquidity-Connection: <OFFER_DRAFTS_CONNECTION_ID>
Content-Type: application/json
Idempotency-Key: proposed-quote-001

{"side":"sell","payment_asset":"usdc-base","amount_hns":"1000","price":"0.005","notes":"Private proposal for owner review."}
```

Required string fields: `side` (`buy`/`sell` HNS), `payment_asset` (exact registry
ID), `amount_hns` (positive, at most 6 decimal places), `price` (positive payment
asset units per HNS; BTC up to 12 decimals, other supported assets up to 18).
Optional `notes`: at most 1,000 characters. Unknown fields and JSON numeric
amounts are rejected. Totals round to the asset's smallest unit, nearest/ties up.
Network fees are additional. Obtain the registry from capabilities rather than
assuming a ticker identifies a network or token contract.

Successful creation returns `201` with `{"draft": {...}, "created": true}`;
an identical retry returns `200`, `created: false`. Reusing a key with changed
content returns `409`. Keys are scoped to the connection. Drafts include exact
terms, notes, actor and timestamps. No offer, trade or wallet transaction is created.

Limits: five active connections per owner, twenty connection creations per hour,
fifty pending drafts, one hundred new drafts per hour across an owner's
connections, and an 8 KB request-body limit. Read requests do not consume the
draft-creation quota. Honour server errors/backoff; do not poll aggressively.

## Errors and recovery

Errors are JSON `{"error":"..."}` on agent routes. Never log credentials or
private response bodies while debugging.

| Status | Meaning / action |
| --- | --- |
| `400` | Invalid inputs, cursor, connection selection or query. Correct the request. |
| `401` | Missing/invalid authentication or inactive/expired credential. Refresh SSO or ask the owner to renew/revoke the grant as appropriate. |
| `403` | Permission denied. Ask the owner; never bypass with browser/guest routes. |
| `404` | Room/draft not found or not accessible; do not enumerate other owners. |
| `409` | Idempotency key reused for a different payload. Review before using a new key. |
| `413` / `415` | Oversized body / wrong content type. |
| `429` | Quota/rate limit. Respect `Retry-After`; retain cursors and pending work. |
| `503` | Temporary validation/database outage. Back off; preserve cursors and POST idempotency keys. |

Never infer settlement success from a successful API response. The API does not
provide public-order publishing, offer acceptance, chat sending, status mutation,
payment confirmation, signing, transfers, bridging, fiat escrow or atomic-swap execution.
