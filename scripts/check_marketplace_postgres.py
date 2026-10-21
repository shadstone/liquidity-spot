"""Isolated synthetic PostgreSQL migration and availability race checks.

Creates a Unix-socket-only temporary cluster, never reads deployment credentials,
and stops the cluster in finally. No production data, HTTP or wallet calls.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PG = Path('/opt/homebrew/opt/postgresql@18/bin')


def run(command, **kwargs):
    result = subprocess.run([str(value) for value in command], text=True,
        capture_output=True, timeout=45, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{Path(str(command[0])).name} failed in synthetic test')
    return result.stdout


def main():
    folder = Path(tempfile.mkdtemp(prefix='liquidity-marketplace-pg-', dir='/tmp'))
    cluster, sock = folder / 'cluster', folder / 'socket'
    sock.mkdir(mode=0o700)
    try:
        run([PG / 'initdb', '-D', cluster, '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8', '--locale=C'])
        run([PG / 'pg_ctl', '-D', cluster, '-l', folder / 'server.log', '-o',
             f"-k {sock} -p 55487 -c listen_addresses=''", '-w', 'start'])
        env = {**os.environ, 'PGHOST': str(sock), 'PGPORT': '55487',
               'PGUSER': os.environ['USER'], 'PGDATABASE': 'liquidity_marketplace_test'}
        for name in ('PGPASSWORD', 'PGOPTIONS', 'PGSERVICE', 'PGSSLMODE'):
            env.pop(name, None)
        run([PG / 'createdb', 'liquidity_marketplace_test'], env=env)
        from sqlalchemy import URL, event, inspect, text
        from config import TestingConfig
        TestingConfig.SQLALCHEMY_DATABASE_URI = URL.create('postgresql+psycopg2',
            username=env['PGUSER'], database='liquidity_marketplace_test',
            query={'host': str(sock), 'port': '55487'}).render_as_string(hide_password=False)
        from app import create_app
        from models import db, User, P2POffer, P2PTrade
        from services.payment_assets import make_terms_snapshot
        from services.p2p_schema import ensure_payment_schema
        app = create_app('testing')
        app.config['ALLOW_EXTERNAL_HTTP'] = False
        with app.app_context():
            assert db.engine.url.database == 'liquidity_marketplace_test'
            assert db.engine.url.query['host'].startswith('/tmp/liquidity-marketplace-pg-')
            db.create_all()
            db.session.add_all([User(id=name, username=name) for name in ('maker', 'taker')])
            db.session.flush()
            offer = P2POffer(creator_id='maker', side='sell', amount_hns=1000,
                price_btc_per_hns='0.0000001', status='matched')
            db.session.add(offer)
            db.session.flush()
            db.session.add(P2PTrade(offer_id=offer.id, creator_id='maker', counterparty_id='taker',
                status='matched', terms_snapshot=make_terms_snapshot(offer)))
            db.session.commit()
            engine = db.engine
            # Remove only the new columns in this disposable synthetic cluster.
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE p2p_offers DROP COLUMN expires_at'))
                connection.execute(text('ALTER TABLE p2p_offers DROP COLUMN availability_confirmed_at'))
            old_columns = {table: [column['name'] for column in inspect(engine).get_columns(table)]
                           for table in inspect(engine).get_table_names()}

            def snapshot():
                with engine.connect() as connection:
                    return {table: connection.execute(text('SELECT ' + ','.join('"' + c + '"' for c in columns)
                        + ' FROM "' + table + '" ORDER BY id')).all() for table, columns in old_columns.items()}

            before = snapshot()
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda _: ensure_payment_schema(engine), range(2)))
            ensure_payment_schema(engine)
            startup_env = {'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT),
                'DATABASE_URL': TestingConfig.SQLALCHEMY_DATABASE_URI, 'FLASK_ENV': 'production',
                'PYTHON_DOTENV_DISABLED': '1', 'SECRET_KEY': 'synthetic-startup-only',
                'GFAVIP_WALLET_API_KEY': '', 'GFAVIP_WALLET_LOOKUP_API_KEY': ''}
            smoke = "import run; run.app.config['ALLOW_EXTERNAL_HTTP']=False; c=run.app.test_client(); assert all(c.get(p).status_code==200 for p in ['/p2p', '/p2p?view=past', '/api/channel?version=2', '/p2p?view=post'])"
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda _: run([sys.executable, '-W', 'ignore', '-c', smoke],
                    env=startup_env, cwd=ROOT), range(2)))
            assert snapshot() == before, 'Migration changed original data'
            with engine.connect() as connection:
                assert connection.execute(text('SELECT expires_at, availability_confirmed_at FROM p2p_offers')).one() == (None, None)
            print('PASS: repeated/concurrent PostgreSQL migration preserves every original column and row')
            print('PASS: two real startup workers and database-backed marketplace pages')

        def client(user):
            browser = app.test_client()
            with browser.session_transaction() as state:
                state['user_id'] = user
            return browser

        def new_offer(expired=False):
            with app.app_context():
                offer = P2POffer(creator_id='maker', side='sell', amount_hns=1000,
                    price_btc_per_hns='0.0000001', status='open',
                    expires_at=datetime.utcnow() + timedelta(days=-1 if expired else 1))
                db.session.add(offer)
                db.session.commit()
                return offer.id

        def renewal(browser, offer_id):
            assert browser.get(f'/p2p/offers/{offer_id}').status_code == 200
            with browser.session_transaction() as state:
                token = state['offer_availability_token']
            return lambda: browser.post(f'/p2p/offers/{offer_id}/availability',
                data={'csrf_token': token, 'expiry': '7', 'confirm_available': 'yes'})

        # Hold a database row lock; wait until the application's conditional
        # availability UPDATE is submitted, then simulate acceptance winning.
        offer_id = new_offer()
        owner = client('maker')
        renew = renewal(owner, offer_id)
        started = Event()

        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('UPDATE p2p_offers SET availability_confirmed_at'):
                started.set()

        event.listen(engine, 'before_cursor_execute', observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                with engine.begin() as locker:
                    locker.execute(text("UPDATE p2p_offers SET status='matched' WHERE id=:id"), {'id': offer_id})
                    future = executor.submit(renew)
                    assert started.wait(5), 'Renewal never reached database'
                assert future.result(timeout=10).status_code == 409, 'Renewal reopened matched listing'
        finally:
            event.remove(engine, 'before_cursor_execute', observe)
        print('PASS: concurrent acceptance defeats stale owner renewal; no reopen')

        # Taker loads an available offer; a concurrent owner-side availability
        # update expires it before the conditional acceptance write.
        offer_id = new_offer()
        started = Event()

        def observe_claim(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('UPDATE p2p_offers SET status='):
                started.set()

        event.listen(engine, 'before_cursor_execute', observe_claim)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                with engine.begin() as locker:
                    locker.execute(text('UPDATE p2p_offers SET expires_at=:expires WHERE id=:id'),
                        {'id': offer_id, 'expires': datetime.utcnow() - timedelta(days=1)})
                    future = executor.submit(lambda: client('taker').post(f'/p2p/offers/{offer_id}/accept'))
                    assert started.wait(5), 'Acceptance never reached database'
                assert future.result(timeout=10).status_code == 302
        finally:
            event.remove(engine, 'before_cursor_execute', observe_claim)
        with app.app_context():
            assert P2PTrade.query.filter_by(offer_id=offer_id).count() == 0
            assert db.session.get(P2POffer, offer_id).status == 'open'
        print('PASS: conditional acceptance rejects concurrent expiry without creating a room')

        offer_id = new_offer(expired=True)
        owner = client('maker')
        assert renewal(owner, offer_id)().status_code == 302
        assert client('taker').post(f'/p2p/offers/{offer_id}/accept').status_code == 302
        with app.app_context():
            assert P2PTrade.query.filter_by(offer_id=offer_id).count() == 1
        print('PASS: explicitly renewed unmatched offer can be accepted once')
    finally:
        if (cluster / 'postmaster.pid').exists():
            run([PG / 'pg_ctl', '-D', cluster, '-m', 'fast', '-w', 'stop'])
        print(f'Synthetic cluster stopped; diagnostic files retained at {folder}')


if __name__ == '__main__':
    main()
