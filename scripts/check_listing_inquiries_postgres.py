"""Synthetic Unix-socket PostgreSQL enquiry migration and race checks only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import re
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PG = Path('/opt/homebrew/opt/postgresql@18/bin')


def run(arguments, **kwargs):
    result = subprocess.run([str(arg) for arg in arguments], capture_output=True, text=True, timeout=90, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{Path(str(arguments[0])).name} failed; diagnostic output withheld')
    return result.stdout


def main():
    folder = Path(tempfile.mkdtemp(prefix='liquidity-inquiries-pg-', dir='/tmp'))
    cluster, socket = folder / 'cluster', folder / 'socket'
    socket.mkdir(mode=0o700)
    try:
        run([PG / 'initdb', '-D', cluster, '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8', '--locale=C'])
        run([PG / 'pg_ctl', '-D', cluster, '-l', folder / 'server.log', '-o',
             f"-k {socket} -p 55486 -c listen_addresses=''", '-w', 'start'])
        environment = {key: value for key, value in os.environ.items() if not key.startswith(('PG', 'GFAVIP_', 'BTC_', 'HNS_'))}
        environment.update(PGHOST=str(socket), PGPORT='55486', PGUSER=os.environ['USER'], PGDATABASE='liquidity_inquiry_test')
        run([PG / 'createdb', 'liquidity_inquiry_test'], env=environment)
        from sqlalchemy import URL, text
        from config import TestingConfig
        uri = URL.create('postgresql+psycopg2', username=environment['PGUSER'], database='liquidity_inquiry_test',
                         query={'host': str(socket), 'port': '55486'}).render_as_string(hide_password=False)
        TestingConfig.SQLALCHEMY_DATABASE_URI = uri
        from app import create_app
        from models import db, User, Order, P2POffer, Swap, P2PTrade
        from services.agent_sso import AgentSSOGrant, create_sso_grant
        from services.agent_workspace import AgentConnection, WorkspaceError
        from services.agent_maker import AgentMakerPolicy
        from services.inquiry_schema import ensure_inquiry_schema
        import services.listing_inquiries as inquiries
        app = create_app('testing')
        app.config.update(AGENT_LISTING_CONVERSATIONS_ENABLED=True, AGENT_MAKER_ENABLED=True,
                          GFAVIP_WALLET_LOOKUP_API_KEY='synthetic-unused-lookup-key')
        with app.app_context():
            assert db.engine.url.database == 'liquidity_inquiry_test'
            assert str(db.engine.url.query.get('host')).startswith('/tmp/liquidity-inquiries-pg-')
            db.create_all()
            db.session.add_all([User(id=name, username=name) for name in (
                'maker', 'first', 'second', 'quota', 'bundle-duplicate', 'bundle-capacity', 'bundle-rollback')])
            db.session.flush()
            db.session.add(Order(id=23, user_id='maker', side='sell', amount_hns=100, price_btc_per_hns='0.00000001', status='open'))
            db.session.add(P2POffer(id=1, creator_id='maker', side='sell', amount_hns=100, price_btc_per_hns=0,
                                   price_quote_per_hns='0.005', payment_asset_id='usdc-base', status='open'))
            db.session.commit()
            for table in ('orders', 'p2p_offers'):
                db.session.execute(text(f'ALTER TABLE {table} DROP COLUMN allow_pretrade_chat'))
            for table in ('listing_inquiry_actions', 'listing_inquiry_messages', 'listing_inquiries'):
                db.session.execute(text(f'DROP TABLE {table}'))
            db.session.commit()
            engine = db.engine
            before = {table: db.session.execute(text(f'SELECT row_to_json(r)::text FROM {table} r ORDER BY id')).scalars().all()
                      for table in ('users', 'orders', 'p2p_offers')}
            db.session.rollback()

        startup_env = {**environment, 'DATABASE_URL': uri, 'FLASK_ENV': 'production', 'PYTHONPATH': str(ROOT),
            'PYTHON_DOTENV_DISABLED': '1', 'SECRET_KEY': 'synthetic-inquiry-only',
            'GFAVIP_WALLET_API_KEY': '', 'GFAVIP_WALLET_LOOKUP_API_KEY': '',
            'AGENT_MAKER_ENABLED': 'false', 'AGENT_LISTING_CONVERSATIONS_ENABLED': 'false',
            'BTC_WATCHER_BASE_URL': '', 'HNS_WATCHER_BASE_URL': ''}
        startup = [sys.executable, '-c', "import requests; requests.sessions.Session.request=lambda *a,**k: (_ for _ in ()).throw(AssertionError('HTTP forbidden')); import config; config.Config.ALLOW_EXTERNAL_HTTP=False; config.ProductionConfig.ALLOW_EXTERNAL_HTTP=False; import run"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: run(startup, env=startup_env, cwd=ROOT), range(2)))
        run(startup, env=startup_env, cwd=ROOT)
        with app.app_context():
            for table in ('users', 'orders', 'p2p_offers'):
                # Compare original columns, excluding only the new opt-in flag.
                after = db.session.execute(text(f"SELECT (to_jsonb(r) - 'allow_pretrade_chat')::text FROM {table} r ORDER BY id")).scalars().all()
                assert [json.loads(row) for row in before[table]] == [json.loads(row) for row in after]
            assert db.session.get(Order, 23).allow_pretrade_chat is True
            assert db.session.get(P2POffer, 1).allow_pretrade_chat is True
            db.session.get(P2POffer, 1).allow_pretrade_chat = False
            db.session.commit()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: ensure_inquiry_schema(engine), range(2)))
        with app.app_context():
            assert db.session.get(P2POffer, 1).allow_pretrade_chat is False
            assert inquiries.ListingInquiry.query.count() == 0

        identity = '11111111-2222-4333-8444-555555555555'
        def grant(owner):
            with app.app_context():
                connection = create_sso_grant(owner, 'Synthetic enquiry agent', identity,
                    profile='listing-conversations', reviewed_username=True)
                db.session.commit()
                return connection.id

        def post(connection_id, key, inquiry_id=None):
            path = (f'/api/agent/v1/inquiries/{inquiry_id}/messages' if inquiry_id else
                    '/api/agent/v1/listings/atomic/23/inquiries')
            response = app.test_client().post(path, json={'message': 'Synthetic private question'}, headers={
                'Authorization': 'Bearer gfavip-session-' + 'a' * 32,
                'X-Liquidity-Connection': str(connection_id), 'Idempotency-Key': key})
            return response.status_code, response.get_json()

        def setup_bundle(owner, label):
            human = app.test_client()
            with human.session_transaction() as state:
                state['user_id'] = owner
            assert human.get('/agents').status_code == 200
            with human.session_transaction() as state:
                csrf = state['agent_workspace_csrf']
            fields = {'csrf_token': csrf, 'agent_username': 'pl-synthetic-assistant',
                'label': label, 'publish_buy': 'yes', 'publish_sell': 'yes', 'ask_listing_owners': 'yes'}
            policy = {'payment_asset': 'usdc-base', 'min_price': '0.003', 'max_price': '0.01',
                'max_offer_hns': '10', 'total_hns_budget': '100', 'max_open_offers': '2',
                'max_offers_per_hour': '3', 'allow_replies': '', 'max_replies_per_hour': '', 'max_replies_total': ''}
            for side in ('buy', 'sell'):
                fields.update({side + '_' + name: value for name, value in policy.items()})
            account = {'id': identity, 'username': 'pl-synthetic-assistant', 'accountKind': 'ai_agent'}
            with patch('routes.agents.lookup_agent_username', return_value=account):
                review = human.post('/agents/setup/review', data=fields)
            assert review.status_code == 200
            match = re.search(r'name="setup_proof"[^>]*value="([^"]+)"', review.get_data(as_text=True))
            assert match is not None
            approval = {'csrf_token': csrf, 'setup_proof': unescape(match.group(1)),
                'confirm_agent': 'yes', 'confirm_maker_risk': 'yes', 'confirm_inquiries': 'yes'}
            return human.get_cookie('session').value, approval

        def approve_bundle(plan):
            cookie, fields = plan
            human = app.test_client()
            human.set_cookie('session', cookie)
            return human.post('/agents/setup/approve', data=fields).status_code

        def bundle_counts(owner):
            with app.app_context():
                connections = AgentConnection.query.filter_by(owner_id=owner).all()
                ids = [connection.id for connection in connections]
                return (len(connections), AgentSSOGrant.query.filter(AgentSSOGrant.connection_id.in_(ids)).count(),
                        AgentMakerPolicy.query.filter(AgentMakerPolicy.connection_id.in_(ids)).count())

        with patch('services.agent_sso.validate_agent_identity', return_value={'gfavip_user_id': identity}), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('No external HTTP in synthetic tests')):
            first, second, quota = grant('first'), grant('second'), grant('quota')
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda _: post(first, 'duplicate'), range(2)))
                assert sorted(status for status, _ in outcomes) == [200, 201]
                first_thread = outcomes[0][1]['inquiry']['id']
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda key: post(first, key, first_thread), ('reply-a', 'reply-b')))
                assert [status for status, _ in outcomes] == [201, 201]
            second_status, second_result = post(second, 'other-inquirer')
            assert second_status == 201 and second_result['inquiry']['id'] != first_thread
            quota_status, quota_result = post(quota, 'quota-start')
            assert quota_status == 201
            with patch.dict(inquiries.INQUIRY_LIMITS, {'messages_hourly': 2}), ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda key: post(quota, key, quota_result['inquiry']['id']), ('quota-a', 'quota-b')))
                assert sorted(status for status, _ in outcomes) == [201, 429]

            # Serialize against the same listing lock as owner opt-out and
            # acceptance. A queued message must see the newly committed state.
            real_get = inquiries.get_listing
            for mutation in ('off', 'matched'):
                waiting = Event()
                def tracked_get(*args, **kwargs):
                    waiting.set()
                    return real_get(*args, **kwargs)
                with app.app_context():
                    listing = real_get('atomic', 23, lock=True)
                    listing.allow_pretrade_chat = mutation != 'off'
                    listing.status = 'matched' if mutation == 'matched' else 'open'
                    with patch('services.listing_inquiries.get_listing', tracked_get), ThreadPoolExecutor(max_workers=1) as executor:
                        pending = executor.submit(post, first, 'after-' + mutation, first_thread)
                        try:
                            assert waiting.wait(8), 'Writer did not reach listing lock'
                            db.session.commit()
                        except BaseException:
                            db.session.rollback()
                            raise
                        assert pending.result(timeout=15)[0] == 409

            with app.app_context():
                order = db.session.get(Order, 23)
                order.status, order.allow_pretrade_chat = 'open', True
                db.session.commit()
                connection = db.session.get(AgentConnection, first)
                count = inquiries.ListingInquiryMessage.query.count()
                audit_count = inquiries.ListingInquiryAction.query.count()
                # Exercise rollback after audit/message flush and retry.
                inquiries.reply_inquiry(first_thread, 'first', 'rollback', {'message': 'Synthetic rollback'}, connection)
                db.session.rollback()
                assert inquiries.ListingInquiryMessage.query.count() == count
                assert inquiries.ListingInquiryAction.query.count() == audit_count
                inquiries.reply_inquiry(first_thread, 'first', 'rollback', {'message': 'Synthetic rollback'}, connection)
                db.session.commit()
                assert inquiries.ListingInquiryMessage.query.count() == count + 1
                inquiries.close_inquiry(first_thread, 'first')
                db.session.commit()
            assert post(first, 'after-close', first_thread)[0] == 409
            assert post(first, 'duplicate')[0] == 200  # replay after close adds nothing

            # Exercise the real owner-reviewed three-grant approval transaction.
            # Independent clients retain identical pre-approval signed cookies,
            # so a CSRF rotation cannot accidentally stand in for DB replay protection.
            duplicate_plan = setup_bundle('bundle-duplicate', 'Duplicate bundle')
            with ThreadPoolExecutor(max_workers=2) as executor:
                assert sorted(executor.map(lambda _: approve_bundle(duplicate_plan), range(2))) == [200, 409]
            assert approve_bundle(duplicate_plan) == 409
            assert bundle_counts('bundle-duplicate') == (3, 3, 2)

            capacity_plans = [setup_bundle('bundle-capacity', 'Capacity A'),
                              setup_bundle('bundle-capacity', 'Capacity B')]
            with ThreadPoolExecutor(max_workers=2) as executor:
                assert sorted(executor.map(approve_bundle, capacity_plans)) == [200, 429]
            assert bundle_counts('bundle-capacity') == (3, 3, 2)

            rollback_plan = setup_bundle('bundle-rollback', 'Atomic rollback')
            created_grants = []
            def fail_second_grant(*arguments, **keywords):
                created_grants.append(1)
                if len(created_grants) == 2:
                    raise WorkspaceError('Synthetic second-grant failure.', 503)
                return create_sso_grant(*arguments, **keywords)
            with patch('routes.agents.create_sso_grant', side_effect=fail_second_grant):
                assert approve_bundle(rollback_plan) == 503
            assert bundle_counts('bundle-rollback') == (0, 0, 0)
            assert approve_bundle(rollback_plan) == 200
            assert bundle_counts('bundle-rollback') == (3, 3, 2)
            with app.app_context():
                assert Swap.query.count() == P2PTrade.query.count() == 0
                assert all(user.gems_balance == 0 for user in User.query.all())
                assert inquiries.ListingInquiry.query.count() == 3
                db.session.remove()
                db.engine.dispose()
        print(json.dumps({'synthetic_only': True, 'concurrent_startup_migration': True,
            'old_records_preserved': True, 'opt_out_preserved_on_repeat': True,
            'idempotency_race': True, 'conversation_isolation': True, 'concurrent_quota': True,
            'queued_message_after_opt_out_or_match_denied': True, 'rollback_retry': True,
            'closed_replay_no_duplicate': True, 'no_trades_or_funds_changed': True,
            'three_grant_same_proof_concurrency': True, 'three_grant_capacity_concurrency': True,
            'three_grant_partial_failure_rollback_and_retry': True,
            'external_http_blocked': True, 'cluster_directory': str(folder)}))
    finally:
        status = subprocess.run([str(PG / 'pg_ctl'), '-D', str(cluster), 'status'], capture_output=True, text=True, timeout=15)
        if status.returncode == 0:
            run([PG / 'pg_ctl', '-D', cluster, '-m', 'fast', '-w', 'stop'])
        status = subprocess.run([str(PG / 'pg_ctl'), '-D', str(cluster), 'status'], capture_output=True, text=True, timeout=15)
        assert status.returncode == 3, 'Temporary PostgreSQL did not stop'


if __name__ == '__main__':
    main()
