"""Check decimal comparisons against a disposable Unix-socket-only PostgreSQL.

No deployment credentials, production records or external HTTP are used.
Run with the application's test dependencies; PG_BIN can select local PG tools.
"""
from decimal import Decimal
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PG = Path(os.environ.get('PG_BIN', '/opt/homebrew/opt/postgresql@18/bin'))


def run(*args, **kwargs):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True, timeout=45, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Synthetic PostgreSQL command failed: {Path(str(args[0])).name}')


def main():
    with tempfile.TemporaryDirectory(prefix='liquidity-comparison-pg-', dir='/tmp') as directory:
        folder = Path(directory)
        cluster, socket = folder / 'cluster', folder / 'socket'
        socket.mkdir(mode=0o700)
        run(PG / 'initdb', '-D', cluster, '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8', '--locale=C')
        run(PG / 'pg_ctl', '-D', cluster, '-l', folder / 'server.log', '-o',
            f"-k {socket} -p 55489 -c listen_addresses=''", '-w', 'start')
        try:
            from sqlalchemy import URL
            from config import TestingConfig
            TestingConfig.SQLALCHEMY_DATABASE_URI = URL.create('postgresql+psycopg2',
                username=os.environ['USER'], database='postgres',
                query={'host': str(socket), 'port': '55489'}).render_as_string(hide_password=False)
            from app import create_app
            from models import db, User, P2POffer
            app = create_app('testing')
            assert not app.config['ALLOW_EXTERNAL_HTTP']
            with app.app_context():
                assert db.engine.url.query['host'] == str(socket)
                db.create_all()
                db.session.add(User(id='synthetic-owner', username='Synthetic owner'))
                db.session.flush()
                def offer(amount, price, asset='eth-base', legacy=False):
                    record = P2POffer(creator_id='synthetic-owner', side='sell', status='open',
                        payment_asset_id=None if legacy else asset, amount_hns=Decimal(amount),
                        amount_hns_exact=None if legacy else amount,
                        price_btc_per_hns=Decimal(price) if legacy else 0,
                        price_quote_per_hns=None if legacy else price, gems_stake=0)
                    db.session.add(record)
                    db.session.flush()
                    return record.id
                # One atomic unit apart; float conversion would collapse these.
                amount = '9999999999999999.000001'
                low = offer(amount, '9.000000000000000001')
                high = offer(amount, '9.000000000000000002')
                offer('9999999999999999', '1')
                offer('9999999999999999.000002', '1')
                offer(amount, '1', asset='eth-optimism')
                legacy = offer('1000', '0.00000001', legacy=True)
                db.session.commit()
                client = app.test_client()
                def ids(url):
                    response = client.get(url)
                    assert response.status_code == 200
                    return [int(i) for i in re.findall(r'<article id="offer-(\d+)"', response.get_data(as_text=True))]
                url = f'/p2p?intent=buy&payment_asset=eth-base&min_hns={amount}&max_hns={amount}&sort='
                assert ids(url + 'rate_asc') == [low, high]
                assert ids(url + 'rate_desc') == [high, low]
                assert ids('/p2p?intent=buy&payment_asset=btc-bitcoin&min_hns=1000&max_hns=1000&sort=rate_asc') == [legacy]
                assert P2POffer.query.count() == 6
                db.session.remove()
                db.engine.dispose()
            print('PASS: PostgreSQL exact HNS bounds, 18-decimal rate order, network isolation and legacy BTC.')
        finally:
            run(PG / 'pg_ctl', '-D', cluster, '-m', 'fast', '-w', 'stop')


if __name__ == '__main__':
    main()
