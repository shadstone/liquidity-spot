"""Public, neutral offer copy; never include notes, identities or room secrets."""
from services.payment_assets import format_decimal


def build_offer_share_text(offer, share_url):
    """Describe recorded terms, not a price feed or a promise of availability."""
    if offer.status != 'open' or offer.side not in ('buy', 'sell'):
        return None
    asset = offer.payment_asset
    direction = 'BUY' if offer.side == 'buy' else 'SELL'
    exchange = 'with' if offer.side == 'buy' else 'for'
    counterparty = 'seller' if offer.side == 'buy' else 'buyer'
    return (
        f'Liquidity.spot offer #{offer.id}: {direction} '
        f'{format_decimal(offer.settlement_amount_hns)} HNS {exchange} '
        f'{asset["symbol"]} on {asset["network_label"]} at '
        f'{format_decimal(offer.quote_price)} {asset["symbol"]}/HNS '
        f'({format_decimal(offer.quote_total)} {asset["symbol"]} total; network fees extra). '
        f'Looking for an HNS {counterparty}. '
        'Manual P2P, no escrow. Confirm the network, token and payment details '
        'in the trade room before sending funds. '
        f'Check availability and review the offer: {share_url}'
    )
