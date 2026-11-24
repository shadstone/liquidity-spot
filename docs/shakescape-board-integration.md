# ShakeScape offer links in the Liquidity Spot order book

## Scope

Append externally sourced ShakeScape HNS/BTC offers as clearly marked rows in
the existing `/orders` book. Each row displays the signed offer's whole HNS
amount, BTC total, derived BTC/HNS price, side, short signer fingerprint, and
offer ID. Its only action is a link to the exact offer in ShakeScape when the
mobile app and extension support one canonical offer URI. Until that URI is
implemented and configured, the row displays a copyable offer ID without an
action link.

Liquidity Spot's existing `Order` rows, create form, `/orders/<int:order_id>/accept`
flow, `Swap` records, and `/api/channel` v1 are unchanged. A ShakeScape row
never becomes a local order or opens a Liquidity Spot swap room. The recipient
app or extension must re-fetch and verify the current signed offer before
showing any acceptance flow. No website-provider wallet bridge is required for
this first integration.

## Data flow

```text
ShakeScape wallets <-> existing hsrd raw Handshake/ShakeScape P2P relay
                         -> verified, in-memory direct-offer board
                         -> opt-in, read-only HTTP snapshot on loopback
                         -> HTTPS reverse proxy for remote site deployments
                         -> Liquidity Spot server-side reader and short cache
       -> GET /api/external-offers/shakescape
       -> external rows appended to /orders after the local book renders
       -> canonical offer URI -> ShakeScape app or extension
```

### Relay deployment choice

Reuse the **same `hsrd` process that receives ShakeScape offers**. Its current
`--rpc-bind` listener already speaks HTTP but includes administrative and wallet
routes; keep it authenticated and bound to loopback. Its public P2P listener
speaks raw Handshake/ShakeScape TCP, not HTTP. The opt-in argument
`--shakescape-board-http-bind 127.0.0.1:12039` starts a **separate**,
loopback-only HTTP listener. It serves only a bounded `GET` snapshot from the
existing verified direct-offer board; it does not mount the general RPC router
or accept offer submissions, takes, or session messages. Default is disabled.
The feed itself does not need a wallet index or active-state chain connection.
If an existing `hsrd` already has a wallet index, keep its stored index profile
when updating that process; removing indexed components from an existing data
directory is rejected. A new relay-only deployment may use a separate data
directory without a wallet index.
An exact-path HTTPS reverse proxy on that host can publish the snapshot to
Liquidity Spot even when the two services run on different machines. Rate
limit and bound responses at the proxy as well as in `hsrd`. The public path
and hostname are deployment choices, not a requirement to share the
`status.learnhns.com` web server.

A second small `hsrd` is a fallback only when the existing node cannot run the
ShakeScape relay profile or its operator cannot expose the narrow HTTPS path.
It needs its own P2P/federation link, board cache, monitoring, and reconciliation;
merely running it on the Liquidity Spot server does not make the boards identical.
For a local preview while the existing wallet-index node stays online, an
updated sidecar may connect to that node's raw P2P listener and use a **different**
data directory and ports. This is a temporary preview arrangement; the normal
deployment puts the narrow HTTP listener on the offer-receiving relay itself.
First verify the operator's running binary and flags: an ordinary Handshake
node, or `hsrd` without its ShakeScape cross-chain role, has no direct-offer
board to publish. The existing relay board is process-local and can be empty
after restart until peers reconnect and reconcile.

The dedicated feed returns only public offer fields. Liquidity Spot never
accepts a feed URL from a visitor. Its browser fetches Liquidity Spot's read
endpoint after the existing book renders and refreshes every 10 seconds, so a
slow or missing relay feed does not delay the local order form and rows. Feed
failure removes external offers and reports them as unavailable without
affecting site orders.

## Feed contract, version 1

The source `hsrd` listener and Liquidity Spot reader implement the following
contract. Live rows require an updated running node with cross-chain relay
peers and current offers. The reader expects one JSON document with Unix
seconds and decimal-string integer base units:

```json
{
  "schema_version": 1,
  "network": "mainnet",
  "revision": "opaque-consistent-snapshot-id",
  "generated_at": 1790000000,
  "expires_at": 1790000020,
  "offers": [
    {
      "offer_id": "<64 lowercase hex>",
      "signer_public_key": "<66 lowercase hex>",
      "offered_asset": "HNS",
      "offered_amount_base_units": "100000000",
      "received_asset": "BTC",
      "received_amount_base_units": "50000",
      "created_at": 1789999990,
      "expires_at": 1790000600
    }
  ]
}
```

The relay must include only offers that passed canonical signature, offer-ID,
network, expiry, and cancellation checks. Liquidity Spot also rejects a wrong
network, stale or oversized snapshot, more than 100 offers, duplicate or
malformed IDs, invalid signers, impossible pair or amounts, and expired rows.
It converts HNS with 1,000,000 base units per coin and BTC with 100,000,000
satoshis per coin using decimal arithmetic. An offer *offering* HNS displays
**sell HNS**; an offer *receiving* HNS displays **buy HNS**. The price is for
display only. The signed pair is one indivisible offer, with no partial fill.

An accepted offer may remain advertised briefly. Call it **advertised on the
board**, not **reserved** or **available to settle**. The app or extension owns
the live acceptance and settlement decision.

## Offer URI handoff

The mobile app currently handles `handshake:` payment URIs; no canonical
ShakeScape offer URI was found in the checked-in mobile or extension source.
Define a single versioned URI contract for both recipients before setting
`SHAKESCAPE_OFFER_URI_TEMPLATE`. It must carry the exact 32-byte offer ID and
network, reject malformed or cross-network IDs, and make the recipient fetch
and verify the live offer. A link must never mean that the offer is reserved or
accepted. The site's template is fixed deployment configuration with one
`{offer_id}` and one `{network}` placeholder; no feed row or request parameter
may supply a URL.

## Local configuration and release checks

- `SHAKESCAPE_BOARD_FEED_URL`: HTTPS snapshot endpoint, or loopback HTTP for
  same-machine local preview; empty means the feed is unavailable.
- `SHAKESCAPE_BOARD_NETWORK`: expected `mainnet` or `regtest` binding.
- `SHAKESCAPE_OFFER_URI_TEMPLATE`: blank until the app and extension handle the
  same exact offer URI; only configured HTTPS or `shakescape:` templates produce
  links.

For a same-machine preview, start an updated mainnet `hsrd` with the cross-chain
relay role and `--shakescape-board-http-bind 127.0.0.1:12039`, then run
`python -m venv .venv` and `.venv/bin/python -m pip install -r requirements-lock.txt`
in the site checkout, followed by `scripts/run-shakescape-preview.sh`. The script uses a
separate ignored SQLite database, so the preview does not load existing live
Liquidity Spot orders. Visit `http://127.0.0.1:8000/orders`. The feed may be
empty until the relay has reachable offer owners; it never creates demo offers.
Use the managed node's existing data directory with only one `hsrd` process at
a time; starting another process against the same directory is not a preview
strategy.
The preview script serves only on `127.0.0.1:8000`; it needs the phone's signed
offer to reach the relay's P2P board before a row appears. A wallet's locally
stored active offer alone is not proof that the relay currently has a reachable
offer owner. The page refreshes the external rows every 10 seconds.

Test HNS-for-BTC and BTC-for-HNS rows, exact base-unit conversion, cancellation,
expiry, stale and malformed feed, duplicate IDs, and feed loss. Confirm that
existing `/orders` creation and acceptance, local order display, and
`/api/channel` payloads still behave as before. Enable live links only after
both recipients demonstrate an exact-offer readback and a cancellation/expiry
check on the target network.

## Name offer board

The same opt-in `hsrd` board listener also serves
`GET /api/v1/shakescape/name-offers`. It returns at most 100 active,
signature-verified, uncancelled HIP-0001/Shakedex name advertisements from the
existing relay adapter. The public response includes a listing ID, seller
public key, canonical name, price and marketplace fee in integer HNS base
units, and creation and expiry times. The relay does not verify the current
name coin for a public snapshot. ShakeScape must reacquire the exact listing
and verify its lock against the current chain before preparing a purchase.

Liquidity Spot reads that feed through
`GET /api/external-offers/shakescape/names`, validates its network, freshness,
shape, amounts, IDs and expiry, then renders a separate name board under the
existing atomic order table. Feed failure clears those rows and does not block
the HNS/BTC board or local Liquidity Spot orders. The browser receives only
Liquidity Spot's bounded projection; it never calls the relay RPC directly.

`SHAKESCAPE_NAME_BOARD_FEED_URL` may override the source URL. If it is unset
and `SHAKESCAPE_BOARD_FEED_URL` ends in
`/api/v1/shakescape/direct-offers`, the name URL is derived on the same host.
The canonical v1 name-offer app URI is
`shakescape://name-offer/v1/{offer_id}?network={network}`, where `offer_id` is
exactly 64 lowercase hexadecimal characters, not all zero, and `network` is
exactly `mainnet`, `testnet`, or `regtest`. Both app recipients reject
noncanonical encodings, extra parameters, and links for a different selected
network. The URI is a pointer to a listing, not a signed offer, payment
instruction, or authorization. A link only opens a current-offer readback and review; the wallet
rechecks the signed proof and current lock again when purchase is prepared.
The site's default `SHAKESCAPE_NAME_OFFER_URI_TEMPLATE` uses that contract;
only that exact template is accepted, or set it to an empty value to disable
links during a staged app rollout. The board's primary copy action copies the
full app URI, including the network; a separate action copies the listing ID
for the app's "Accept by listing ID" lookup. The link's custom scheme can be
claimed by other installed apps, so the recipient must treat every URI as
untrusted input and obtain current terms itself. Name listings remain
advertisements, not reserved purchases.
