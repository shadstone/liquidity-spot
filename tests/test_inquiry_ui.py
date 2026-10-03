"""Synthetic human UI checks: questions must remain separate from acceptance."""
from html.parser import HTMLParser
import unittest

from app import create_app
from models import db, User, Order, P2POffer, P2PTrade, Swap
from services.listing_inquiries import ListingInquiry, ListingInquiryMessage


class InquiryDocument(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.forms = []
        self.current = None
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        self.tags.append((tag, attributes))
        if tag == 'form':
            self.current = {'attributes': attributes, 'tags': []}
            self.forms.append(self.current)
        elif self.current is not None:
            self.current['tags'].append((tag, attributes))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None

    def form(self, action):
        return next(form for form in self.forms if form['attributes'].get('action') == action)

    def has_form(self, action):
        return any(form['attributes'].get('action') == action for form in self.forms)

    def fields(self, action):
        return {attributes['name']: attributes for tag, attributes in self.form(action)['tags']
                if tag in ('input', 'textarea') and 'name' in attributes}

    def links(self):
        return {attributes.get('href') for tag, attributes in self.tags if tag == 'a'}


class InquiryUITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                User(id='owner', username='Listing owner', tier='free'),
                User(id='interested', username='Interested person', tier='free'),
                User(id='other', username='Other interested person', tier='free'),
            ])
            db.session.add(P2POffer(id=11, creator_id='owner', side='sell',
                amount_hns='1250', amount_hns_exact='1250', price_btc_per_hns='0',
                payment_asset_id='usdt-ethereum', price_quote_per_hns='0.037',
                status='open', allow_pretrade_chat=True, gems_stake=0))
            db.session.add(Order(id=22, user_id='owner', side='buy', amount_hns='3500',
                price_btc_per_hns='0.00000004', status='open', allow_pretrade_chat=True, gems_stake=0))
            db.session.commit()
        self.sign_in('interested')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def sign_in(self, user_id):
        with self.client.session_transaction() as state:
            state.clear()
            state['user_id'] = user_id

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        document = response.get_data(as_text=True)
        return document, InquiryDocument(document)

    def inquiry(self, inquiry_id=1, inquirer_id='interested', kind='p2p', content='Can we discuss the price?'):
        with self.app.app_context():
            row = ListingInquiry(id=inquiry_id, kind=kind, listing_id=11 if kind == 'p2p' else 22,
                owner_id='owner', inquirer_id=inquirer_id)
            db.session.add(row)
            db.session.flush()
            message = ListingInquiryMessage(inquiry_id=row.id, user_id=inquirer_id,
                actor='human', content=content)
            db.session.add(message)
            db.session.commit()
            return message.id

    def test_both_creation_forms_default_to_private_questions_on(self):
        for path, action, checkbox in (('/p2p', '/p2p/offers', 'p2p-allow-pretrade-chat'),
                                       ('/orders', '/orders', 'atomic-allow-pretrade-chat')):
            with self.subTest(path=path):
                body, tags = self.get(path)
                fields = tags.fields(action)
                self.assertEqual(fields['inquiry_setting_present']['value'], 'yes')
                self.assertEqual(fields['inquiry_setting_present']['type'], 'hidden')
                self.assertEqual(fields['allow_pretrade_chat']['id'], checkbox)
                self.assertEqual(fields['allow_pretrade_chat']['value'], 'yes')
                self.assertIn('checked', fields['allow_pretrade_chat'])
                self.assertIn('Allow private questions before accepting.', body)

    def test_both_listings_expose_ask_before_accept_without_hiding_acceptance(self):
        for kind, listing_id, detail, accept in (
            ('p2p', 11, '/p2p/offers/11', '/p2p/offers/11/accept'),
            ('atomic', 22, '/orders/22', '/orders/22/accept'),
        ):
            with self.subTest(kind=kind):
                body, tags = self.get(detail)
                self.assertIn(f'/listings/{kind}/{listing_id}/inquire', tags.links())
                self.assertTrue(tags.has_form(accept))
                self.assertLess(body.index('id="listing-inquiries"'), body.index(f'action="{accept}"'))
                self.assertIn('An inquiry does not accept or reserve this listing', body)
                self.assertNotIn(f'/listings/{kind}/{listing_id}/chat-setting', [
                    form['attributes'].get('action') for form in tags.forms])

    def test_anonymous_detail_offers_sign_in_not_unavailable_or_self_chat(self):
        self.client = self.app.test_client()
        for path, enquiry in (('/p2p/offers/11', '/listings/p2p/11/inquire'),
                              ('/orders/22', '/listings/atomic/22/inquire')):
            body, tags = self.get(path)
            self.assertIn('Sign in to ask / negotiate first', body)
            self.assertIn(enquiry, tags.links())
            response = self.client.get(enquiry)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers['Location'].endswith('/login'))

    def test_owner_sees_inbox_and_csrf_toggle_not_self_inquiry(self):
        self.sign_in('owner')
        for kind, listing_id, detail in (('p2p', 11, '/p2p/offers/11'), ('atomic', 22, '/orders/22')):
            body, tags = self.get(detail)
            self.assertIn('Open inquiry inbox', body)
            self.assertIn('/inquiries', tags.links())
            self.assertNotIn(f'/listings/{kind}/{listing_id}/inquire', tags.links())
            action = f'/listings/{kind}/{listing_id}/chat-setting'
            fields = tags.fields(action)
            self.assertEqual(tags.form(action)['attributes']['method'], 'POST')
            self.assertEqual(set(fields), {'csrf_token', 'allow_pretrade_chat'})
            self.assertEqual(len(fields['csrf_token']['value']), 43)
            self.assertIn('checked', fields['allow_pretrade_chat'])
            body, tags = self.get(f'/listings/{kind}/{listing_id}/inquire')
            self.assertIn('Open your inquiry inbox', body)
            self.assertIn('/inquiries', tags.links())
            self.assertFalse(tags.has_form(f'/listings/{kind}/{listing_id}/inquire'))

    def test_start_form_shows_exact_asset_terms_and_no_accept_action(self):
        body, tags = self.get('/listings/p2p/11/inquire')
        self.assertIn('Inquiry — no trade accepted.', body)
        self.assertIn('1250 HNS', body)
        self.assertIn('0.037 USDT', body)
        self.assertIn('46.25 USDT', body)
        self.assertIn('Ethereum', body)
        self.assertIn('no instant reply is promised', body)
        fields = tags.fields('/listings/p2p/11/inquire')
        self.assertEqual(set(fields), {'csrf_token', 'idempotency_key', 'message'})
        self.assertEqual(len(fields['csrf_token']['value']), 43)
        self.assertTrue(fields['idempotency_key']['value'])
        self.assertEqual(fields['message']['maxlength'], '1000')
        self.assertIn('required', fields['message'])
        self.assertFalse(any('/accept' in form['attributes'].get('action', '') for form in tags.forms))
        with self.app.app_context():
            self.assertEqual(ListingInquiry.query.count(), 0)
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(Swap.query.count(), 0)

    def test_question_submission_creates_only_inquiry_and_replay_is_safe(self):
        for kind, listing_id in (('p2p', 11), ('atomic', 22)):
            with self.subTest(kind=kind):
                path = f'/listings/{kind}/{listing_id}/inquire'
                _, tags = self.get(path)
                data = {name: attrs.get('value', '') for name, attrs in tags.fields(path).items()}
                data['message'] = 'Which network should I review? No funds have been sent.'
                first = self.client.post(path, data=data)
                replay = self.client.post(path, data=data)
                self.assertEqual(first.status_code, 302)
                self.assertEqual(replay.headers['Location'], first.headers['Location'])
                self.assertTrue(first.headers['Location'].startswith('/inquiries/'))
        with self.app.app_context():
            self.assertEqual(ListingInquiry.query.count(), 2)
            self.assertEqual(ListingInquiryMessage.query.count(), 2)
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(Swap.query.count(), 0)
            self.assertEqual(db.session.get(P2POffer, 11).status, 'open')
            self.assertEqual(db.session.get(Order, 22).status, 'open')

    def test_inbox_is_private_and_paginated_without_message_previews(self):
        self.inquiry(1, content='PRIVATE MESSAGE NOT AN INBOX PREVIEW')
        self.inquiry(2, inquirer_id='other', content='OTHER PERSON SECRET QUESTION')
        body, tags = self.get('/inquiries')
        self.assertIn('/inquiries/1', tags.links())
        self.assertNotIn('/inquiries/2', tags.links())
        self.assertNotIn('OTHER PERSON SECRET QUESTION', body)
        self.assertNotIn('PRIVATE MESSAGE NOT AN INBOX PREVIEW', body)
        self.assertEqual(self.client.get('/inquiries/2').status_code, 404)
        self.sign_in('owner')
        _, tags = self.get('/inquiries?limit=1')
        self.assertIn('/inquiries/1', tags.links())
        self.assertNotIn('/inquiries/2', tags.links())
        self.assertIn('/inquiries?after=1', tags.links())

    def test_thread_escapes_untrusted_text_and_labels_ai_messages(self):
        self.inquiry(content='<img src=x onerror="alert(1)"> & a price question')
        with self.app.app_context():
            db.session.add(ListingInquiryMessage(inquiry_id=1, user_id='owner', actor='agent',
                content='[AI agent] <script>not trusted</script>'))
            db.session.commit()
        body, tags = self.get('/inquiries/1')
        self.assertIn('&lt;img src=x onerror=', body)
        self.assertNotIn('<img src=x', body)
        self.assertIn('&lt;script&gt;not trusted&lt;/script&gt;', body)
        self.assertIn('AI agent', body)
        self.assertIn('Messages are unverified participant statements.', body)
        self.assertIn('/p2p/offers/11', tags.links())
        self.assertEqual(tags.fields('/inquiries/1')['message']['maxlength'], '1000')
        self.assertTrue(tags.has_form('/inquiries/1/close'))
        self.assertFalse(any('/accept' in form['attributes'].get('action', '') for form in tags.forms))
        response = self.client.get('/inquiries/1')
        self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')

    def test_read_status_is_explicit_and_marks_only_displayed_page(self):
        first_id = self.inquiry()
        with self.app.app_context():
            db.session.add(ListingInquiryMessage(inquiry_id=1, user_id='interested',
                actor='human', content='A second message on a later page.'))
            db.session.commit()
        self.sign_in('owner')
        body, tags = self.get('/inquiries/1?limit=1')
        self.assertIn('2 unread', body)
        self.assertIn('aria-label="2 unread inquiry messages"', body)
        self.assertIn(f'/inquiries/1?after={first_id}', tags.links())
        fields = tags.fields('/inquiries/1/seen')
        self.assertEqual(fields['through_id']['value'], str(first_id))
        with self.app.app_context():
            self.assertEqual(db.session.get(ListingInquiry, 1).owner_seen_message_id, 0)
        response = self.client.post('/inquiries/1/seen', data={
            name: attributes['value'] for name, attributes in fields.items()})
        self.assertEqual(response.status_code, 302)
        body, _ = self.get('/inquiries/1')
        self.assertIn('1 unread', body)

    def test_turning_off_listing_keeps_private_history_without_reply_form(self):
        self.inquiry(content='History must remain available.')
        self.sign_in('owner')
        _, tags = self.get('/p2p/offers/11')
        csrf = tags.fields('/listings/p2p/11/chat-setting')['csrf_token']['value']
        response = self.client.post('/listings/p2p/11/chat-setting', data={'csrf_token': csrf})
        self.assertEqual(response.status_code, 302)
        self.sign_in('interested')
        body, tags = self.get('/inquiries/1')
        self.assertIn('History must remain available.', body)
        self.assertIn('turned off pre-trade enquiries', body)
        self.assertFalse(tags.has_form('/inquiries/1'))
        self.assertTrue(tags.has_form('/inquiries/1/close'))
        body, tags = self.get('/listings/p2p/11/inquire')
        self.assertIn('/inquiries/1', tags.links())
        self.assertFalse(tags.has_form('/listings/p2p/11/inquire'))

    def test_either_participant_can_close_without_hiding_history_or_changing_listing(self):
        self.inquiry(1)
        self.inquiry(2, kind='atomic')
        for user_id, inquiry_id in (('interested', 1), ('owner', 2)):
            with self.subTest(user=user_id):
                self.sign_in(user_id)
                path = f'/inquiries/{inquiry_id}'
                _, tags = self.get(path)
                fields = tags.fields(f'{path}/close')
                response = self.client.post(f'{path}/close', data={
                    name: attrs['value'] for name, attrs in fields.items()})
                self.assertEqual(response.status_code, 302)
                body, tags = self.get(path)
                self.assertIn('Can we discuss the price?', body)
                self.assertIn('cannot be reopened', body)
                self.assertFalse(tags.has_form(path))
                self.assertFalse(tags.has_form(f'{path}/close'))
        with self.app.app_context():
            self.assertEqual(db.session.get(P2POffer, 11).status, 'open')
            self.assertEqual(db.session.get(Order, 22).status, 'open')
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(Swap.query.count(), 0)

    def test_closed_listing_is_read_only_and_disabled_entry_is_clear(self):
        self.inquiry()
        with self.app.app_context():
            db.session.get(P2POffer, 11).status = 'canceled'
            db.session.commit()
        body, tags = self.get('/inquiries/1')
        self.assertIn('conversation is read-only', body)
        self.assertFalse(tags.has_form('/inquiries/1'))
        self.client = self.app.test_client()
        body, tags = self.get('/p2p/offers/11')
        self.assertNotIn('/listings/p2p/11/inquire', tags.links())
        self.assertIn('no longer open', body)


if __name__ == '__main__':
    unittest.main()
