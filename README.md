# Liquidity Spot

Liquidity Spot is an open-source P2P coordination app for HNS/BTC-style liquidity trades.

The current product direction is human P2P first: users can browse offers, create offers, accept offers, and coordinate non-custodial trades without the platform taking custody of funds. Experimental trustless/atomic-swap screens remain in the codebase for learning and testing, but the primary flow is guided P2P coordination.

## Status

This project is early and should be treated as experimental software.

- Public browsing and normal P2P actions are designed to work in guest mode.
- GFAVIP login is optional for normal P2P use.
- GFAVIP is only required for features that depend on a GFAVIP account, such as Gems or account-linked benefits.
- The app does not custody user funds.
- Users are responsible for verifying counterparties, addresses, transaction IDs, and settlement before releasing any asset.

## Features

- Public P2P offer browsing.
- Guest-mode offer creation and acceptance.
- Trade rooms for buyer/seller coordination.
- Optional GFAVIP login for Gems and account-linked benefits.
- Optional maker Gems bond flow when GFAVIP wallet API credentials are configured.
- Experimental HTLC/atomic-swap learning screens.
- Wallet-intent endpoints for Bob Wallet HNS helpers and Bitcoin PSBT helpers.
- Optional BTC/HNS watcher callbacks for checking submitted atomic-swap TXIDs.
- Admin review surfaces for trades and disputes.

## Local Development

Requirements:

- Python 3.11 (the deployment and CI version)
- SQLite for local development
- PostgreSQL for production-style deployments

Setup:

```bash
git clone https://github.com/shadstoneofficial/liquidity-spot.git
cd liquidity-spot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
cp .env.example .env
python run.py
```

Open:

```txt
http://localhost:8000
```

The app creates local database tables on startup.

See `CONTRIBUTING.md` for the isolated test setup and complete verification
command. Tests do not require `.env`, credentials, or external services.

## Environment Variables

Copy `.env.example` to `.env` for local development.

```txt
FLASK_ENV=development
SECRET_KEY=change-me-local-dev-only
DATABASE_URL=sqlite:///app.db
GFAVIP_SERVICE_NAME=liquidity-spot
REDIRECT_URI=http://localhost:8000/callback
GFAVIP_WALLET_API_KEY=
GFAVIP_WALLET_BASE_URL=https://wallet.gfavip.com
BTC_WATCHER_BASE_URL=https://blockstream.info/api
HNS_WATCHER_BASE_URL=
ATOMIC_SWAP_NETWORK=main
```

Production notes:

- Always set a strong `SECRET_KEY`.
- Use a production database through `DATABASE_URL`.
- Leave `GFAVIP_WALLET_API_KEY` empty unless Gems wallet integration is intentionally enabled.
- Do not commit `.env`, local databases, deploy logs, or private specs.

## GFAVIP Policy

Liquidity Spot should not require GFAVIP login for basic P2P usage.

Guest users can:

- browse public offers;
- create offers;
- accept offers;
- enter trade rooms;
- coordinate trades manually.

GFAVIP login adds:

- Gems-backed features;
- account-linked history;
- future reputation and notification features;
- wallet-service integration.

## Safety Model

### Network-aware manual P2P

The P2P board supports BTC on Bitcoin; USDT on Ethereum mainnet; native USDC
on Ethereum, Base and OP Mainnet; and native ETH on those three EVM networks.
Each offer fixes an asset/network combination. No arbitrary contract entry,
USDC.e, USDbC, WETH, or unapproved USDT L2 variant is accepted. Issuer/network
sources and the review date are maintained in `services/payment_assets.py`.

EVM takers must confirm the token and network before opening a room. Amounts
and rates use exact decimal strings; totals round to the nearest atomic unit
(ties up). Fees are additional: the sender pays gas on the selected network.
Accepted rooms snapshot amount, rate, total, chain, contract and payment method.
Transaction IDs are format-checked links, not proof of payment. Participants
must independently verify recipient, contract, network, success and finality.
There is no automatic bridging, custody, escrow or EVM atomic swap.

The additive startup upgrade in `services/p2p_schema.py` adds nullable columns
and snapshots legacy rooms without changing offer amounts/statuses. Back up
the database before deploying and verify startup migration logs. Failed schema
initialization prevents startup. Old app releases retain existing BTC columns
but must not be used to serve new non-BTC offers: those have a zero legacy BTC
placeholder and would be mislabeled. Disable new writes and coordinate a data-
aware rollback instead of blindly rolling back the application after adoption.

Release verification checklist:

1. Create a provider volume snapshot and a private `pg_dump -Fc` export. Keep
   credentials and database exports outside this repository; record a checksum.
2. Restore the export into an isolated PostgreSQL instance. Compare row counts
   and hashes of the original columns before and after migration, including
   atomic tables. Run `ensure_payment_schema(engine)` twice and concurrently.
3. Boot the application against the restored copy with external HTTP disabled;
   check public pages, both channel API versions, legacy rooms and receipts.
4. Apply the additive migration before app cutover. It uses transaction-local
   10-second lock and 60-second statement timeouts; investigate a timeout rather
   than repeatedly retrying against a busy database.
5. Deploy, verify both workers boot and database-backed pages respond, and keep
   the backup until the release is accepted. A restore rehearsal is not a restore
   of production; never replace live data without reconciling intervening trades.

`/api/channel` remains BTC-only for released Bob clients. New clients must
explicitly request `/api/channel?version=2` and consume `quote_asset`,
`quote_network`, `chain_id`, `token_contract`, `price_quote_per_hns` and
`total_quote`; never interpret a null BTC rate as a BTC offer.

Liquidity Spot is a coordination layer, not a custodian.

The app should never ask for:

- seed phrases;
- private keys;
- wallet passwords;
- remote desktop access;
- irreversible transfers before the user independently verifies terms.

For now, users should treat all trades as manual P2P coordination and verify every step out of band.

## Agent Workspace: Draft-only Pilot

The signed-in **Agent workspace** at `/agents` lets an owner connect their own
agent or script to prepare private offer proposals. This does not install a
model, run a bot, publish liquidity, accept or fill orders, read private trade
messages, confirm payments, or sign/send funds. Drafts are not public offers,
verified balances, reservations or promises to trade.

Create a named connection in the workspace. Its token has only
`drafts:read drafts:write` permissions, expires after seven days and is shown
once on a standalone page without external scripts. Store it privately; do not
put it in URLs, model prompts, logs, screenshots or source control. The server
stores a digest, not the recoverable token. Revoke a connection in the workspace
to stop its API access; existing drafts, real offers and trades are unaffected.

API contract:

- `GET /api/agent/v1/capabilities`: public supported payment routes and limits.
- `GET /api/agent/v1/drafts`: all private drafts belonging to the connection's
  owner, up to 100 per page. Continue with `?before_id=<next_before_id>` when
  the response supplies a cursor.
- `POST /api/agent/v1/drafts`: create a private draft using JSON fields `side`
  (`buy` or `sell` HNS), `payment_asset`, `amount_hns`, `price` and optional
  `notes`. Monetary values must be plain decimal **strings**. Use a registry
  payment identifier such as `usdc-base`, not a ticker alone.

Private endpoints require `Authorization: Bearer <token>`. Draft creation also
requires `Idempotency-Key`: use a fresh key for each distinct draft and reuse
the same key and payload only when retrying that request.
An owner may have five active connections, 50 pending drafts and 100 new draft
creations per hour across those connections; reads and idempotent replays do
not consume that creation quota.

For example, with a token already set privately as `LIQUIDITY_AGENT_TOKEN` and
the application running locally:

```bash
curl --request POST 'http://localhost:8000/api/agent/v1/drafts' \
  --header "Authorization: Bearer $LIQUIDITY_AGENT_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: example-draft-001' \
  --data '{"side":"sell","payment_asset":"usdc-base","amount_hns":"1000","price":"0.0035","notes":"Example only; owner review required."}'
```

The sample price is arbitrary, not market data. Use HTTPS for remote access.
The owner must review the amount, price, total, network, token contract and
ability to fulfill the trade, then open the existing P2P board and **manually
create** any desired offer. The workspace link opens a blank form: it does not
approve, auto-fill or publish the draft. Dismissing a draft changes only that
private proposal. Wallet keys always remain with the human owner.

## Atomic Swap Wallet Adapters

Liquidity Spot exposes machine-readable swap intents for local wallet helpers:

- `GET /api/swaps/<id>` returns public swap status.
- `GET /api/swaps/<id>/intents` returns participant-only wallet intents.
- `POST /api/swaps/<id>/txids/alice-lock?token=...` records Alice's HNS lock TXID.
- `POST /api/swaps/<id>/txids/bob-lock?token=...` records Bob's BTC lock TXID.
- `POST /api/swaps/<id>/txids/alice-claim?token=...` records Alice's BTC claim TXID plus the revealed secret.
- `POST /api/swaps/<id>/txids/bob-claim?token=...` records Bob's HNS claim TXID and completes the swap.

The intent JSON includes callback tokens scoped to that swap. Wallet helpers should never send seed phrases, private keys, wallet passwords, or unsigned private wallet state to Liquidity Spot.

BTC watcher checks use `BTC_WATCHER_BASE_URL` and expect a Blockstream-compatible `/tx/<txid>` JSON endpoint. HNS watcher checks use `HNS_WATCHER_BASE_URL` and expect a compatible `/tx/<txid>` JSON endpoint. Watchers are optional; if not configured, users can still record TXIDs manually.

## Bob Wallet Addon Direction

Liquidity Spot is intended to become the first external Bob Wallet Add On candidate after Shakedex Marketplace.

Initial integration should be conservative:

- open externally or in a constrained embedded view;
- no automatic wallet signing;
- no seed/private-key access;
- explicit user prompts for any future wallet-assisted action.

The draft Bob Add On manifest is available at:

```txt
https://liquidity.spot/bob-addon.json
```

See the hub planning docs in `hub-learnhns/temp-specs` for the broader Bob Add Ons roadmap.

## Deployment

The repo includes a Dockerfile and Railway config.

```bash
docker build -t liquidity-spot .
docker run --env-file .env -p 8000:8000 liquidity-spot
```

Railway deploys use:

```txt
./start.sh
```

## Security

Please do not open public issues for security-sensitive findings. See `SECURITY.md`.

## License

MIT. See `LICENSE`.
