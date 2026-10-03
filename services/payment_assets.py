"""Explicit manual-P2P payment identities. This is not a wallet or price feed.

Registry reviewed 2026-10-03 against issuer/network documentation:
https://tether.to/en/supported-protocols/
https://developers.circle.com/stablecoins/usdc-contract-addresses
https://docs.base.org/get-started/connect-to-base
https://docs.optimism.io/op-mainnet/network-information/connecting-to-op
No arbitrary contract input, bridged ticker substitution or automatic bridging.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re


NETWORKS = {
    'bitcoin': ('Bitcoin', None, 'https://mempool.space', 'BTC'),
    'ethereum': ('Ethereum mainnet', 1, 'https://etherscan.io', 'ETH'),
    'base': ('Base', 8453, 'https://basescan.org', 'ETH'),
    'optimism': ('OP Mainnet (Optimism)', 10, 'https://optimistic.etherscan.io', 'ETH'),
}


def _asset(symbol, network, decimals, contract=None):
    network_label, chain_id, explorer, gas = NETWORKS[network]
    return dict(id=f'{symbol.lower()}-{network}', symbol=symbol, network=network,
                network_label=network_label, chain_id=chain_id, explorer=explorer,
                gas_asset=gas, decimals=decimals, contract=contract,
                label=f'{symbol} · {network_label}', reviewed_at='2026-10-03')


PAYMENT_ASSETS = {asset['id']: asset for asset in [
    _asset('BTC', 'bitcoin', 8),
    _asset('USDT', 'ethereum', 6, '0xdAC17F958D2ee523a2206206994597C13D831ec7'),
    _asset('USDC', 'ethereum', 6, '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'),
    _asset('USDC', 'base', 6, '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'),
    _asset('USDC', 'optimism', 6, '0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85'),
    *[_asset('ETH', network, 18) for network in ('ethereum', 'base', 'optimism')],
]}


def get_payment_asset(asset_id):
    if asset_id not in PAYMENT_ASSETS:
        raise ValueError('Unsupported payment asset/network. Choose a listed combination.')
    return dict(PAYMENT_ASSETS[asset_id])


def format_decimal(value):
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def quote_total(amount, price, decimals):
    with localcontext() as ctx:
        ctx.prec = 80
        return (Decimal(amount) * Decimal(price)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _positive_decimal(raw, label, places, maximum):
    text = str(raw or '').strip()
    if len(text) > 80 or not re.fullmatch(r'\d+(?:\.\d+)?', text):
        raise ValueError(f'{label} must be a positive decimal number, without exponent notation.')
    try:
        value = Decimal(text)
        if not value.is_finite() or not 0 < value < Decimal(maximum):
            raise ValueError(f'{label} is outside the supported range.')
        if value.as_tuple().exponent < -places:
            raise ValueError(f'{label} supports at most {places} decimal places.')
        return value
    except InvalidOperation as exc:
        raise ValueError(f'Invalid {label.lower()}.') from exc


def parse_offer_amounts(amount, price, asset_id):
    asset = get_payment_asset(asset_id)
    amount_value = _positive_decimal(amount, 'HNS amount', 6, '1e16')
    price_value = _positive_decimal(price, 'Price per HNS', 12 if asset_id == 'btc-bitcoin' else 18, '1e12')
    total = quote_total(amount_value, price_value, asset['decimals'])
    if not 0 < total < Decimal('1e24'):
        raise ValueError('Payment total is zero after rounding or outside the supported range.')
    return amount_value, price_value, total


def make_terms_snapshot(offer):
    return {
        'version': 1, 'side': offer.side,
        'amount_hns': format_decimal(offer.settlement_amount_hns),
        'price': format_decimal(offer.quote_price),
        'total': format_decimal(offer.quote_total),
        'payment_asset': dict(offer.payment_asset),
        'payment_method': offer.payment_method,
        'rounding': 'nearest atomic unit, ties up',
        'fees': 'Sender pays network fees separately; recipient receives the agreed total.',
        'settlement': 'manual P2P; no escrow, bridging or automatic verification',
    }


def transaction_url(asset, txid):
    """A format-checked explorer link is not confirmation or proof of payment."""
    if not txid:
        return None
    pattern = r'[0-9a-fA-F]{64}' if asset['network'] == 'bitcoin' else r'0x[0-9a-fA-F]{64}'
    return f"{asset['explorer']}/tx/{txid}" if re.fullmatch(pattern, txid) else None
