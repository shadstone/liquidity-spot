"""Read a bounded public ShakeScape offer snapshot for display only.

The source relay verifies signatures and cancellations. This reader validates
the public feed contract and never turns an offer into a local Order or Swap.
"""

import json
import re
import threading
import time
from decimal import Decimal, localcontext
from urllib.parse import urlsplit

from requests import RequestException

from services.http_client import get as http_get


MAX_FEED_BYTES = 256 * 1024
MAX_OFFERS = 100
MAX_FEED_AGE_SECONDS = 30
CACHE_SECONDS = 10
HEX_32 = re.compile(r'^[0-9a-f]{64}$')
HEX_33 = re.compile(r'^[0-9a-f]{66}$')
BASE_UNITS = {'HNS': Decimal(1_000_000), 'BTC': Decimal(100_000_000)}
MAX_BASE_UNIT_AMOUNT = 2**128 - 1
_cache_lock = threading.Lock()
_cache_key = None
_cache_until = 0.0
_cache_value = None


class BoardUnavailable(Exception):
    pass


def _unix(value):
    if type(value) is not int or value < 0:
        raise BoardUnavailable('Invalid feed time')
    return value


def _amount(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,38}', value)
            or int(value) > MAX_BASE_UNIT_AMOUNT):
        raise BoardUnavailable('Invalid base-unit amount')
    return Decimal(value)


def _plain(value):
    plain = format(value, 'f')
    return plain.rstrip('0').rstrip('.') if '.' in plain else plain


def _offer_uri(template, offer_id, network):
    if not template or template.count('{offer_id}') != 1 or template.count('{network}') != 1:
        return None
    url = template.replace('{offer_id}', offer_id).replace('{network}', network)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
        return url
    if parts.scheme == 'shakescape' and (parts.netloc or parts.path) and not parts.fragment:
        return url
    return None


def _read_response(url):
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BoardUnavailable('Feed URL is invalid') from exc
    local_http = parts.scheme == 'http' and parts.hostname in ('127.0.0.1', '::1')
    if not (parts.scheme == 'https' or local_http) or not parts.hostname or parts.username or parts.password:
        raise BoardUnavailable('Feed URL must be HTTPS or loopback HTTP')
    try:
        response = http_get(url, timeout=(1, 2), allow_redirects=False, stream=True)
        try:
            if response.status_code != 200:
                raise BoardUnavailable('Feed request failed')
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=16_384):
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise BoardUnavailable('Feed response is too large')
                chunks.append(chunk)
            return json.loads(b''.join(chunks))
        finally:
            response.close()
    except (RequestException, ValueError, UnicodeError) as exc:
        raise BoardUnavailable('Feed response is unavailable') from exc


def _normalize(data, expected_network, uri_template, now):
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise BoardUnavailable('Unexpected feed schema')
    if data.get('network') != expected_network:
        raise BoardUnavailable('Unexpected feed network')
    revision = data.get('revision')
    if not isinstance(revision, str) or not 1 <= len(revision) <= 128 or not revision.isascii():
        raise BoardUnavailable('Invalid feed revision')
    generated_at = _unix(data.get('generated_at'))
    expires_at = _unix(data.get('expires_at'))
    if generated_at > now + 5 or now - generated_at > MAX_FEED_AGE_SECONDS:
        raise BoardUnavailable('Feed is stale')
    if expires_at <= now or expires_at - generated_at > MAX_FEED_AGE_SECONDS:
        raise BoardUnavailable('Feed has expired')
    rows = data.get('offers')
    if not isinstance(rows, list) or len(rows) > MAX_OFFERS:
        raise BoardUnavailable('Invalid offer count')

    offers = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BoardUnavailable('Invalid offer')
        offer_id = row.get('offer_id')
        signer = row.get('signer_public_key')
        if not isinstance(offer_id, str) or not HEX_32.fullmatch(offer_id) or offer_id in seen:
            raise BoardUnavailable('Invalid or duplicate offer ID')
        if not isinstance(signer, str) or not HEX_33.fullmatch(signer):
            raise BoardUnavailable('Invalid offer signer')
        seen.add(offer_id)
        offered_asset = row.get('offered_asset')
        received_asset = row.get('received_asset')
        if not isinstance(offered_asset, str) or not isinstance(received_asset, str) or {offered_asset, received_asset} != {'HNS', 'BTC'}:
            raise BoardUnavailable('Invalid offer pair')
        offered = _amount(row.get('offered_amount_base_units'))
        received = _amount(row.get('received_amount_base_units'))
        created_at = _unix(row.get('created_at'))
        offer_expires_at = _unix(row.get('expires_at'))
        if created_at > generated_at or offer_expires_at <= now or offer_expires_at <= created_at:
            raise BoardUnavailable('Expired or future offer')
        hns = offered if offered_asset == 'HNS' else received
        btc = offered if offered_asset == 'BTC' else received
        with localcontext() as context:
            context.prec = 80
            hns_coins = hns / BASE_UNITS['HNS']
            btc_coins = btc / BASE_UNITS['BTC']
            price = btc_coins / hns_coins
        offers.append({
            'source': 'shakescape',
            'offer_id': offer_id,
            'signer_fingerprint': signer[:12],
            'side': 'sell' if offered_asset == 'HNS' else 'buy',
            'amount_hns': _plain(hns_coins),
            'total_btc': _plain(btc_coins),
            'price_btc_per_hns': _plain(price),
            'expires_at': offer_expires_at,
            'offer_uri': _offer_uri(uri_template, offer_id, expected_network),
        })
    return {
        'schema_version': 1,
        'network': expected_network,
        'revision': revision,
        'generated_at': generated_at,
        'expires_at': expires_at,
        'offers': offers,
    }


def board_snapshot(feed_url, expected_network, uri_template, now=None):
    """Return only a fresh snapshot; failures never serve expired cached offers."""
    global _cache_key, _cache_until, _cache_value
    if not feed_url:
        raise BoardUnavailable('Feed is not configured')
    now = int(time.time()) if now is None else now
    key = (feed_url, expected_network, uri_template)
    with _cache_lock:
        if key == _cache_key and time.monotonic() < _cache_until:
            if (_cache_value['expires_at'] > now
                    and now - _cache_value['generated_at'] <= MAX_FEED_AGE_SECONDS
                    and all(offer['expires_at'] > now for offer in _cache_value['offers'])):
                return _cache_value
        data = _read_response(feed_url)
        snapshot = _normalize(data, expected_network, uri_template, now)
        _cache_key = key
        _cache_value = snapshot
        _cache_until = time.monotonic() + CACHE_SECONDS
        return snapshot
