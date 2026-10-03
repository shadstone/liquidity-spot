"""Additive P2P schema upgrade. Existing atomic swap tables are untouched."""
from decimal import Decimal
from types import SimpleNamespace
from sqlalchemy import JSON, bindparam, inspect, text
from services.payment_assets import get_payment_asset, make_terms_snapshot, quote_total


def ensure_payment_schema(engine):
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            # Bound waits before taking the advisory lock or any table lock.
            # LOCAL settings end with this transaction and do not leak to requests.
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            # Both Gunicorn workers may start together. Inspect only after lock.
            connection.execute(text('SELECT pg_advisory_xact_lock(683937120)'))
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table, columns in {
            'p2p_offers': {'payment_asset_id': 'VARCHAR(32)', 'price_quote_per_hns': 'VARCHAR(80)', 'amount_hns_exact': 'VARCHAR(80)'},
            'p2p_trades': {'terms_snapshot': 'JSON'},
        }.items():
            if table not in tables:
                continue
            existing = {column['name'] for column in inspector.get_columns(table)}
            for name, column_type in columns.items():
                if name not in existing:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {column_type}'))
        if not {'p2p_offers', 'p2p_trades'}.issubset(tables):
            return
        # Pin pre-existing rooms to their current agreed BTC terms once, without
        # reclassifying amounts, resetting statuses, or rewriting offers.
        rows = connection.execute(text('''
            SELECT t.id, o.side, o.amount_hns, o.price_btc_per_hns,
                   o.payment_asset_id, o.price_quote_per_hns, o.amount_hns_exact, o.payment_method
            FROM p2p_trades t JOIN p2p_offers o ON o.id = t.offer_id
            WHERE t.terms_snapshot IS NULL
        ''')).mappings()
        for row in rows:
            asset = get_payment_asset(row['payment_asset_id'] or 'btc-bitcoin')
            price = Decimal(str(row['price_quote_per_hns'] if row['price_quote_per_hns'] is not None else row['price_btc_per_hns']))
            amount = Decimal(str(row['amount_hns_exact'] if row['amount_hns_exact'] is not None else row['amount_hns']))
            offer = SimpleNamespace(side=row['side'], settlement_amount_hns=amount,
                                    quote_price=price, payment_asset=asset,
                                    quote_total=quote_total(amount, price, asset['decimals']),
                                    payment_method=row['payment_method'])
            statement = text('UPDATE p2p_trades SET terms_snapshot=:terms WHERE id=:id AND terms_snapshot IS NULL').bindparams(bindparam('terms', type_=JSON))
            connection.execute(statement, {'id': row['id'], 'terms': make_terms_snapshot(offer)})
