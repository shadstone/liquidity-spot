"""Synthetic PostgreSQL migration/race checks; never connects to production.

Run with the project's Python environment. Requires local PostgreSQL binaries.
Creates a fresh Unix-socket-only cluster in a temporary directory and stops it
in finally. It uses no real identities, secrets, trades or wallet requests.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(command, **kwargs):
    completed = subprocess.run([str(part) for part in command], capture_output=True,
                               text=True, timeout=45, **kwargs)
    if completed.returncode:
        raise RuntimeError(f'{Path(str(command[0])).name} failed; diagnostic output withheld')
    return completed.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@18/bin')
    args = parser.parse_args()
    pg = Path(args.pg_bin)
    for executable in ('initdb', 'pg_ctl', 'createdb'):
        if not (pg / executable).is_file():
            parser.error(f'Missing PostgreSQL binary: {executable}')
    folder = Path(tempfile.mkdtemp(prefix='liquidity-maker-pg-', dir='/tmp'))
    cluster, sock = folder / 'cluster', folder / 'socket'
    sock.mkdir(mode=0o700)
    try:
        run([pg / 'initdb', '-D', cluster, '--auth-local=trust', '--auth-host=reject',
             '--encoding=UTF8', '--locale=C'])
        run([pg / 'pg_ctl', '-D', cluster, '-l', folder / 'server.log', '-o',
             f"-k {sock} -p 55484 -c listen_addresses=''", '-w', 'start'])
        env = {**os.environ, 'PGHOST': str(sock), 'PGPORT': '55484',
               'PGUSER': os.environ['USER'], 'PGDATABASE': 'liquidity_maker_test'}
        for name in ('PGPASSWORD', 'PGOPTIONS', 'PGSERVICE', 'PGSSLMODE'):
            env.pop(name, None)
        run([pg / 'createdb', 'liquidity_maker_test'], env=env)
        from sqlalchemy import URL, text
        from config import TestingConfig
        uri = URL.create('postgresql+psycopg2', username=env['PGUSER'],
                         database='liquidity_maker_test',
                         query={'host': str(sock), 'port': '55484'}).render_as_string(hide_password=False)
        TestingConfig.SQLALCHEMY_DATABASE_URI = uri
        from app import create_app
        from models import db, User, P2POffer, P2PTrade, P2PTradeMessage
        from services.agent_workspace import AgentConnection, lock_owner
        from services.agent_sso import create_sso_grant
        from services.trade_events import AgentTradeEvent
        import services.agent_maker as maker
        import routes.main as human_routes
        app = create_app('testing')
        app.config['AGENT_MAKER_ENABLED'] = True
        with app.app_context():
            assert db.engine.url.database == 'liquidity_maker_test'
            assert str(db.engine.url.query.get('host')).startswith('/tmp/liquidity-maker-pg-')
            new_tables = {maker.AgentMakerPolicy.__table__.name, maker.AgentMakerAction.__table__.name}
            baseline_tables = [table for table in db.metadata.sorted_tables if table.name not in new_tables]
            db.metadata.create_all(db.engine, tables=baseline_tables)
            db.session.add(User(id='legacy-synthetic-owner', username='Legacy synthetic owner'))
            db.session.flush()
            db.session.add(AgentConnection(owner_id='legacy-synthetic-owner', label='Unchanged old reader',
                token_hash='1' * 64, scope='events:read trades:read',
                created_at=datetime(2026, 1, 1), expires_at=datetime(2026, 1, 8)))
            db.session.commit()

            def digest():
                rows = {}
                for table in baseline_tables:
                    records = db.session.execute(table.select()).mappings().all()
                    rows[table.name] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in records)
                return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

            before = digest()
            db.session.rollback()

        # Exercise the actual repeated/concurrent startup path against this
        # synthetic baseline, without running the real server or external HTTP.
        startup_env = {**env, 'DATABASE_URL': uri, 'FLASK_ENV': 'production',
                       'SECRET_KEY': 'synthetic-postgres-test-only', 'PYTHONPATH': str(ROOT),
                       'PYTHON_DOTENV_DISABLED': '1', 'GFAVIP_WALLET_API_KEY': '',
                       'GFAVIP_WALLET_LOOKUP_API_KEY': '', 'BTC_WATCHER_BASE_URL': '',
                       'HNS_WATCHER_BASE_URL': '', 'AGENT_MAKER_ENABLED': 'false'}
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: run([sys.executable, '-c', 'import run'], env=startup_env, cwd=ROOT), range(2)))
        run([sys.executable, '-c', 'import run'], env=startup_env, cwd=ROOT)
        with app.app_context():
            assert digest() == before, 'Additive startup changed baseline records'
            assert maker.AgentMakerPolicy.query.count() == maker.AgentMakerAction.query.count() == 0
            db.session.rollback()

        wallet_id = '11111111-2222-4333-8444-555555555555'
        policy = {'payment_asset': 'usdc-base', 'side': 'sell', 'min_price': '0.01',
                  'max_price': '0.02', 'max_offer_hns': '100', 'total_hns_budget': '100',
                  'max_open_offers': 1, 'max_offers_per_hour': 20, 'allow_replies': False,
                  'max_replies_per_hour': 0, 'max_replies_total': 0}
        payload = {'side': 'sell', 'payment_asset': 'usdc-base', 'amount_hns': '100', 'price': '0.01'}

        def connection_for(owner, **overrides):
            with app.app_context():
                db.session.add(User(id=owner, username=owner))
                db.session.commit()
                connection = create_sso_grant(owner, 'Synthetic race grant', wallet_id,
                    profile='maker-assistant', maker_policy={**policy, **overrides}, reviewed_username=True)
                db.session.commit()
                return connection.id

        def maker_request(connection_id, key, path, body):
            return app.test_client().post(path, json=body,
                headers={'Authorization': 'Bearer gfavip-session-' + 'a' * 32,
                         'X-Liquidity-Connection': str(connection_id), 'Idempotency-Key': key}).status_code

        def post(connection_id, key):
            return maker_request(connection_id, key, '/api/agent/v1/offers', payload)

        def offer_for(connection_id, owner):
            assert post(connection_id, 'initial-offer') == 201
            with app.app_context():
                offer = P2POffer.query.filter_by(creator_id=owner).one()
                return offer.id

        def human_accept(offer_id):
            human = app.test_client()
            with human.session_transaction() as state:
                state['user_id'] = 'synthetic-counterparty'
            return human.post(f'/p2p/offers/{offer_id}/accept', data={'confirm_network': 'yes'}).status_code

        def reply(connection_id, trade_id, key):
            return maker_request(connection_id, key, f'/api/agent/v1/trades/{trade_id}/messages',
                                 {'message': 'Synthetic agent reply; no transfer is requested.'})

        def room_for(connection_id, owner):
            offer_id = offer_for(connection_id, owner)
            assert human_accept(offer_id) == 302
            with app.app_context():
                trade = P2PTrade.query.filter_by(offer_id=offer_id).one()
                return trade.id

        with app.app_context():
            db.session.add(User(id='synthetic-counterparty', username='Synthetic counterparty'))
            db.session.commit()

        with patch('services.agent_sso.validate_agent_identity', return_value={'gfavip_user_id': wallet_id}), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP is forbidden in synthetic checks')):
            duplicate = connection_for('synthetic-duplicate')
            with ThreadPoolExecutor(max_workers=2) as executor:
                assert sorted(executor.map(lambda _: post(duplicate, 'same-key'), range(2))) == [200, 201]
            quota = connection_for('synthetic-quota')
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = sorted(executor.map(lambda key: post(quota, key), ('quota-a', 'quota-b')))
                assert outcomes[0] == 201 and outcomes[1] in (403, 429), outcomes
            revoke = connection_for('synthetic-revoke')
            waiting = Event()
            real_lock = maker.lock_owner

            def tracked_lock(owner_id):
                waiting.set()
                return real_lock(owner_id)

            with app.app_context():
                lock_owner('synthetic-revoke')
                connection = db.session.get(AgentConnection, revoke)
                with patch('services.agent_maker.lock_owner', tracked_lock), ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(post, revoke, 'after-revoke')
                    try:
                        assert waiting.wait(8), 'Writer did not reach the lock'
                        connection.revoked_at = datetime.utcnow()
                        db.session.commit()
                    except BaseException:
                        db.session.rollback()
                        raise
                    assert pending.result(timeout=15) == 401
                assert P2POffer.query.filter_by(creator_id='synthetic-revoke').count() == 0
                assert P2POffer.query.filter_by(creator_id='synthetic-quota').count() == 1
                assert P2POffer.query.filter_by(creator_id='synthetic-duplicate').count() == 1
                db.session.remove()

            # Both real paths have read "open". Hold human acceptance just
            # before its CAS so cancellation commits first; acceptance must not
            # create a room from stale availability or refund/cancel anything.
            cancel_winner = connection_for('synthetic-cancel-wins')
            cancel_offer_id = offer_for(cancel_winner, 'synthetic-cancel-wins')
            accept_ready, release_accept = Event(), Event()
            real_snapshot = human_routes.make_terms_snapshot

            def paused_snapshot(offer):
                snapshot = real_snapshot(offer)
                accept_ready.set()
                assert release_accept.wait(8), 'Cancellation winner did not release acceptance'
                return snapshot

            with patch('routes.main.make_terms_snapshot', paused_snapshot), ThreadPoolExecutor(max_workers=1) as executor:
                accepted = executor.submit(human_accept, cancel_offer_id)
                try:
                    assert accept_ready.wait(8), 'Acceptance did not reach its CAS'
                    assert maker_request(cancel_winner, 'cancel-winner',
                        f'/api/agent/v1/offers/{cancel_offer_id}/cancel', {}) == 201
                finally:
                    release_accept.set()
                assert accepted.result(timeout=15) == 302
            with app.app_context():
                assert db.session.get(P2POffer, cancel_offer_id).status == 'canceled'
                assert P2PTrade.query.filter_by(offer_id=cancel_offer_id).count() == 0
                assert maker.AgentMakerAction.query.filter_by(connection_id=cancel_winner, action='cancel').count() == 1

            # Reverse the winner: human acceptance holds the offer's row lock
            # after claiming it. Agent cancellation queues on that row, then
            # must lose after acceptance commits. The existing room survives.
            accept_winner = connection_for('synthetic-accept-wins')
            accept_offer_id = offer_for(accept_winner, 'synthetic-accept-wins')
            claimed, release_claim, cancel_waiting = Event(), Event(), Event()
            real_emit = human_routes.emit_trade_event
            real_publication = maker._own_publication

            def paused_emit(*arguments, **keywords):
                claimed.set()
                assert release_claim.wait(8), 'Acceptance winner was not released'
                return real_emit(*arguments, **keywords)

            def tracked_publication(*arguments, **keywords):
                result = real_publication(*arguments, **keywords)
                cancel_waiting.set()
                return result

            with patch('routes.main.emit_trade_event', paused_emit), \
                    patch('services.agent_maker._own_publication', tracked_publication), \
                    ThreadPoolExecutor(max_workers=2) as executor:
                accepted = executor.submit(human_accept, accept_offer_id)
                try:
                    assert claimed.wait(8), 'Acceptance did not claim the offer'
                    canceled = executor.submit(maker_request, accept_winner, 'cancel-loser',
                        f'/api/agent/v1/offers/{accept_offer_id}/cancel', {})
                    assert cancel_waiting.wait(8), 'Cancellation did not reach the availability check'
                finally:
                    release_claim.set()
                assert accepted.result(timeout=15) == 302
                assert canceled.result(timeout=15) == 409
            with app.app_context():
                assert db.session.get(P2POffer, accept_offer_id).status == 'matched'
                accepted_trade = P2PTrade.query.filter_by(offer_id=accept_offer_id).one()
                assert accepted_trade.status == 'matched'
                assert accepted_trade.maker_bond_amount == 0
                assert maker.AgentMakerAction.query.filter_by(connection_id=accept_winner, action='cancel').count() == 0

            reply_policy = {'allow_replies': True, 'max_replies_per_hour': 1, 'max_replies_total': 1}
            reply_duplicate = connection_for('synthetic-reply-duplicate', **reply_policy)
            duplicate_room = room_for(reply_duplicate, 'synthetic-reply-duplicate')
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = sorted(executor.map(lambda _: reply(reply_duplicate, duplicate_room, 'same-reply'), range(2)))
                assert outcomes == [200, 201], outcomes

            reply_quota = connection_for('synthetic-reply-quota', **reply_policy)
            quota_room = room_for(reply_quota, 'synthetic-reply-quota')
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = sorted(executor.map(lambda key: reply(reply_quota, quota_room, key), ('reply-a', 'reply-b')))
                assert outcomes == [201, 429], outcomes
            with app.app_context():
                for connection_id, trade_id in ((reply_duplicate, duplicate_room), (reply_quota, quota_room)):
                    assert P2PTradeMessage.query.filter_by(trade_id=trade_id).count() == 1
                    assert maker.AgentMakerAction.query.filter_by(connection_id=connection_id, action='reply').count() == 1
                    assert AgentTradeEvent.query.filter_by(trade_id=trade_id, kind='p2p.message_added').count() == 2

            # Inject failure only after both participant events have flushed.
            # The message, action and events must all disappear on rollback;
            # the same key must subsequently succeed without consuming quota.
            rollback_connection = connection_for('synthetic-reply-rollback', **reply_policy)
            rollback_room = room_for(rollback_connection, 'synthetic-reply-rollback')
            real_maker_emit = maker.emit_trade_event

            def failed_after_events(*arguments, **keywords):
                real_maker_emit(*arguments, **keywords)
                raise RuntimeError('Synthetic failure after participant event inserts')

            with app.app_context():
                trade = db.session.get(P2PTrade, rollback_room)
                baseline_trade = (trade.status, trade.milestone, trade.updated_at,
                                  trade.latest_note, trade.alice_lock_txid, trade.bob_lock_txid)
                connection = db.session.get(AgentConnection, rollback_connection)
                with patch('services.agent_maker.emit_trade_event', failed_after_events):
                    try:
                        maker.send_reply(connection, rollback_room, 'retry-after-rollback', {'message': 'Synthetic rollback check.'})
                        raise AssertionError('Injected failure was not raised')
                    except RuntimeError:
                        db.session.rollback()
                assert P2PTradeMessage.query.filter_by(trade_id=rollback_room).count() == 0
                assert maker.AgentMakerAction.query.filter_by(connection_id=rollback_connection, action='reply').count() == 0
                assert AgentTradeEvent.query.filter_by(trade_id=rollback_room, kind='p2p.message_added').count() == 0
                result, created = maker.send_reply(connection, rollback_room, 'retry-after-rollback', {'message': 'Synthetic rollback check.'})
                db.session.commit()
                assert created and result['message']['trade_id'] == rollback_room
                db.session.refresh(trade)
                assert (trade.status, trade.milestone, trade.updated_at,
                        trade.latest_note, trade.alice_lock_txid, trade.bob_lock_txid) == baseline_trade
                assert P2PTradeMessage.query.filter_by(trade_id=rollback_room).count() == 1
                assert AgentTradeEvent.query.filter_by(trade_id=rollback_room, kind='p2p.message_added').count() == 2
                assert all(user.gems_balance == 0 for user in User.query.all())
                db.session.remove()
                db.engine.dispose()
        print(json.dumps({'synthetic_only': True, 'additive_startup_preserves_old_records': True,
                          'concurrent_and_repeat_startup': 'passed', 'concurrent_idempotency': 'passed',
                          'concurrent_budget': 'passed', 'queued_write_after_revocation': 'denied',
                          'cancel_wins_accept_race': 'passed', 'accept_wins_cancel_race': 'passed',
                          'concurrent_reply_idempotency': 'passed', 'concurrent_reply_quota': 'passed',
                          'reply_audit_and_events_rollback': 'passed', 'external_http': 'blocked',
                          'cluster_directory': str(folder)}))
    finally:
        # Even a failed pg_ctl start can leave a live server. Detect and stop
        # only this exact temporary cluster; no TCP or shared database target.
        status = subprocess.run([str(pg / 'pg_ctl'), '-D', str(cluster), 'status'],
                                capture_output=True, text=True, timeout=15)
        if status.returncode == 0:
            run([pg / 'pg_ctl', '-D', cluster, '-m', 'fast', '-w', 'stop'])


if __name__ == '__main__':
    main()
