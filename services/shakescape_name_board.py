"""Bounded, read-only projection of signed ShakeScape name advertisements."""

import re
import threading
import time
from decimal import Decimal

from services.shakescape_board import BoardUnavailable, _read_response


MAX_NAME_OFFERS = 100
MAX_FEED_AGE_SECONDS = 30
CACHE_SECONDS = 10
HEX_32 = re.compile(r'^[0-9a-f]{64}$')
HEX_33 = re.compile(r'^[0-9a-f]{66}$')
CANONICAL_NAME_OFFER_URI_TEMPLATE = 'shakescape://name-offer/v1/{offer_id}?network={network}'
NAME_OFFER_NETWORKS = frozenset(('mainnet', 'testnet', 'regtest'))
NAME = re.compile(r'^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$')
AMOUNT = re.compile(r'^(?:0|[1-9][0-9]{0,19})$')
MAX_DOLLARYDOOS = 2**64 - 1
_cache_lock = threading.Lock()
_cache_key = None
_cache_until = 0.0
_cache_value = None


def _unix(value):
    if type(value) is not int or value < 0:
        raise BoardUnavailable('Invalid name-feed time')
    return value


def _amount(value, *, positive):
    if (not isinstance(value, str) or not AMOUNT.fullmatch(value)
            or int(value) > MAX_DOLLARYDOOS or (positive and value == '0')):
        raise BoardUnavailable('Invalid name-feed amount')
    return Decimal(value) / Decimal(1_000_000)


def _name_offer_uri(template, offer_id, network):
    # Keep emitted and copied URIs identical to the app recipients' strict v1 parser.
    if template != CANONICAL_NAME_OFFER_URI_TEMPLATE:
        return None
    return template.format(offer_id=offer_id, network=network)


def _normalize(data, expected_network, uri_template, now):
    if expected_network not in NAME_OFFER_NETWORKS:
        raise BoardUnavailable('Unsupported name-feed network')
    if not isinstance(data, dict) or data.get('schema_version') != 1 or type(data.get('schema_version')) is not int:
        raise BoardUnavailable('Unexpected name-feed schema')
    if data.get('network') != expected_network:
        raise BoardUnavailable('Unexpected name-feed network')
    revision = data.get('revision')
    if not isinstance(revision, str) or not HEX_32.fullmatch(revision):
        raise BoardUnavailable('Invalid name-feed revision')
    generated_at = _unix(data.get('generated_at'))
    expires_at = _unix(data.get('expires_at'))
    if generated_at > now + 5 or now - generated_at > MAX_FEED_AGE_SECONDS:
        raise BoardUnavailable('Name feed is stale')
    if expires_at <= now or expires_at - generated_at > MAX_FEED_AGE_SECONDS:
        raise BoardUnavailable('Name feed has expired')
    rows = data.get('offers')
    if not isinstance(rows, list) or len(rows) > MAX_NAME_OFFERS:
        raise BoardUnavailable('Invalid name-offer count')

    offers = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
                'offer_id', 'seller_public_key', 'name', 'price_base_units',
                'marketplace_fee_base_units', 'created_at', 'expires_at'}:
            raise BoardUnavailable('Invalid name offer')
        offer_id = row['offer_id']
        signer = row['seller_public_key']
        name = row['name']
        if (not isinstance(offer_id, str) or not HEX_32.fullmatch(offer_id)
                or offer_id == '0' * 64 or offer_id in seen):
            raise BoardUnavailable('Invalid or duplicate name-offer ID')
        if not isinstance(signer, str) or not HEX_33.fullmatch(signer):
            raise BoardUnavailable('Invalid name-offer signer')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise BoardUnavailable('Invalid name-offer name')
        seen.add(offer_id)
        price = _amount(row['price_base_units'], positive=True)
        fee = _amount(row['marketplace_fee_base_units'], positive=False)
        created_at = _unix(row['created_at'])
        offer_expires_at = _unix(row['expires_at'])
        if created_at > generated_at or offer_expires_at <= now or offer_expires_at <= created_at:
            raise BoardUnavailable('Expired or future name offer')
        offers.append({
            'source': 'shakescape',
            'offer_id': offer_id,
            'seller_fingerprint': signer[:12],
            'name': name,
            'price_hns': format(price, 'f'),
            'marketplace_fee_hns': format(fee, 'f'),
            'expires_at': offer_expires_at,
            'offer_uri': _name_offer_uri(uri_template, offer_id, expected_network),
        })
    return {
        'schema_version': 1,
        'network': expected_network,
        'revision': revision,
        'generated_at': generated_at,
        'expires_at': expires_at,
        'offers': offers,
    }


def name_board_snapshot(feed_url, expected_network, uri_template, now=None):
    """Return a fresh advertisement snapshot or fail closed."""
    global _cache_key, _cache_until, _cache_value
    if not feed_url:
        raise BoardUnavailable('Name feed is not configured')
    now = int(time.time()) if now is None else now
    key = (feed_url, expected_network, uri_template)
    with _cache_lock:
        if key == _cache_key and time.monotonic() < _cache_until:
            if (_cache_value['expires_at'] > now
                    and now - _cache_value['generated_at'] <= MAX_FEED_AGE_SECONDS
                    and all(offer['expires_at'] > now for offer in _cache_value['offers'])):
                return _cache_value
        snapshot = _normalize(_read_response(feed_url), expected_network, uri_template, now)
        _cache_key = key
        _cache_until = time.monotonic() + CACHE_SECONDS
        _cache_value = snapshot
        return snapshot
