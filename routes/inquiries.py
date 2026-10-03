"""Private listing questions, strictly separate from trade rooms and acceptance."""
import hmac
import json
import re
import secrets

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

from models import db, User, Order, P2POffer
from routes.auth import login_required
from services.agent_workspace import WorkspaceError, authenticate_bearer
from services.payment_assets import PAYMENT_ASSETS
from services.listing_inquiries import (
    ListingInquiry, ListingInquiryMessage, INQUIRY_LIMITS, agent_inquiries_enabled,
    inquiry_csrf_token, get_listing, serialize_listing, listing_owner, listing_blocked_reason,
    participant_inquiry, serialize_inquiry, serialize_message, start_inquiry, reply_inquiry,
    set_listing_chat, close_inquiry, mark_inquiry_seen,
)

inquiries_bp = Blueprint('inquiries', __name__)


@inquiries_bp.before_request
def boundary():
    request.max_content_length = 8192
    if request.content_length is not None and request.content_length > 8192:
        raise RequestEntityTooLarge()
    is_api = request.path.startswith('/api/agent/')
    if not is_api and request.headers.get('Authorization', '').split(' ', 1)[0].lower() == 'bearer':
        raise WorkspaceError('Agent credentials cannot use human enquiry forms.', 403)
    if request.method == 'GET' and request.get_data(cache=False):
        raise WorkspaceError('GET endpoints do not accept a body.')


@inquiries_bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@inquiries_bp.errorhandler(WorkspaceError)
def error_response(error):
    db.session.rollback()
    response = jsonify({'error': str(error)}) if request.path.startswith('/api/agent/') else Response(str(error), mimetype='text/plain')
    response.status_code = error.status
    if error.status == 401 and request.path.startswith('/api/agent/'):
        response.headers['WWW-Authenticate'] = 'Bearer realm="liquidity-agent"'
    if error.status == 429:
        response.headers['Retry-After'] = '3600'
    return response


@inquiries_bp.errorhandler(RequestEntityTooLarge)
def oversized(_error):
    return error_response(WorkspaceError('Enquiry requests are limited to 8 KB.', 413))


@inquiries_bp.errorhandler(SQLAlchemyError)
def unavailable(_error):
    return error_response(WorkspaceError('Enquiries are temporarily unavailable. Retry with the same Idempotency-Key.', 503))


def human_owner():
    user = db.session.get(User, session.get('user_id'))
    if user is None:
        raise WorkspaceError('Sign in or restore your guest account before using enquiries.', 401)
    return user


def form_fields(allowed):
    if set(request.form) - set(allowed) or any(len(request.form.getlist(key)) != 1 for key in request.form):
        raise WorkspaceError('Submit only the displayed form fields, once each.')
    supplied = request.form.get('csrf_token', '')
    expected = session.get('inquiry_csrf', '')
    if (session.get('inquiry_csrf_owner') != session.get('user_id')
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied)
            or not expected or not hmac.compare_digest(expected, supplied)):
        raise WorkspaceError('Reload this page before submitting the enquiry form.')


def pagination(extra=()):
    if set(request.args) - {'after', 'limit', *extra} or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise WorkspaceError('Use only documented query parameters, once each.')
    after, limit = request.args.get('after', '0'), request.args.get('limit', '50')
    if not re.fullmatch(r'[0-9]{1,19}', after) or int(after) > 9223372036854775807:
        raise WorkspaceError('after must be a non-negative message or enquiry cursor.')
    if not re.fullmatch(r'[0-9]{1,3}', limit) or not 1 <= int(limit) <= 100:
        raise WorkspaceError('limit must be from 1 to 100.')
    return int(after), int(limit)


def threads_page(user_id, after, limit):
    rows = ListingInquiry.query.filter(ListingInquiry.id > after,
        or_(ListingInquiry.owner_id == user_id, ListingInquiry.inquirer_id == user_id)
    ).order_by(ListingInquiry.id.asc()).limit(limit + 1).all()
    page = rows[:limit]
    return {'threads': [serialize_inquiry(row, user_id) for row in page],
            'next_cursor': page[-1].id if page else after, 'has_more': len(rows) > limit}


def room_context(inquiry, user_id, after, limit):
    listing = get_listing(inquiry.kind, inquiry.listing_id)
    rows = ListingInquiryMessage.query.filter(ListingInquiryMessage.inquiry_id == inquiry.id,
        ListingInquiryMessage.id > after).order_by(ListingInquiryMessage.id.asc()).limit(limit + 1).all()
    page = rows[:limit]
    reason = listing_blocked_reason(listing, inquiry)
    return {'conversation': serialize_inquiry(inquiry, user_id),
            'listing': serialize_listing(inquiry.kind, listing, user_id),
            'messages': [serialize_message(row, user_id) for row in page],
            'can_reply': reason is None, 'blocked_reason': reason,
            'next_cursor': page[-1].id if page else after, 'has_more': len(rows) > limit}


@inquiries_bp.route('/listings/<kind>/<int:listing_id>/inquire', methods=['GET', 'POST'])
@login_required
def start(kind, listing_id):
    user = human_owner()
    if request.method == 'POST':
        if request.args:
            raise WorkspaceError('Enquiry writes do not accept query parameters.')
        form_fields({'csrf_token', 'idempotency_key', 'message'})
        result, _created = start_inquiry(kind, listing_id, user.id, request.form.get('idempotency_key'),
                                         {'message': request.form.get('message')})
        db.session.commit()
        return redirect(url_for('inquiries.room', inquiry_id=result['inquiry']['id']))
    if request.args:
        raise WorkspaceError('This page does not accept query parameters.')
    listing = get_listing(kind, listing_id)
    existing = ListingInquiry.query.filter_by(kind=kind, listing_id=listing_id, inquirer_id=user.id).first()
    reason = listing_blocked_reason(listing, existing)
    if listing_owner(kind, listing) == user.id:
        reason = 'Use your enquiry inbox to answer questions about your listing.'
    return render_template('inquiry_start.html', listing=serialize_listing(kind, listing, user.id),
        existing_thread=serialize_inquiry(existing, user.id) if existing else None,
        can_reply=reason is None, blocked_reason=reason, csrf_token=inquiry_csrf_token(),
        idempotency_key=secrets.token_urlsafe(24))


@inquiries_bp.route('/inquiries', methods=['GET'])
@login_required
def inbox():
    user = human_owner()
    after, limit = pagination()
    return render_template('inquiry_inbox.html', **threads_page(user.id, after, limit), csrf_token=inquiry_csrf_token())


@inquiries_bp.route('/inquiries/<int:inquiry_id>', methods=['GET', 'POST'])
@login_required
def room(inquiry_id):
    user = human_owner()
    if request.method == 'POST':
        if request.args:
            raise WorkspaceError('Enquiry writes do not accept query parameters.')
        form_fields({'csrf_token', 'idempotency_key', 'message'})
        reply_inquiry(inquiry_id, user.id, request.form.get('idempotency_key'), {'message': request.form.get('message')})
        db.session.commit()
        return redirect(url_for('inquiries.room', inquiry_id=inquiry_id))
    after, limit = pagination()
    inquiry = participant_inquiry(inquiry_id, user.id)
    return render_template('inquiry_thread.html', **room_context(inquiry, user.id, after, limit),
                           csrf_token=inquiry_csrf_token(), idempotency_key=secrets.token_urlsafe(24))


@inquiries_bp.route('/listings/<kind>/<int:listing_id>/chat-setting', methods=['POST'])
@login_required
def chat_setting(kind, listing_id):
    user = human_owner()
    form_fields({'csrf_token', 'allow_pretrade_chat'})
    value = request.form.get('allow_pretrade_chat', '')
    if value not in ('', 'yes', 'no') or request.args:
        raise WorkspaceError('Choose whether this listing permits enquiries.')
    listing = set_listing_chat(kind, listing_id, user.id, value == 'yes')
    db.session.commit()
    return redirect(listing['review_url'])


@inquiries_bp.route('/inquiries/<int:inquiry_id>/close', methods=['POST'])
@login_required
def close(inquiry_id):
    user = human_owner()
    form_fields({'csrf_token'})
    if request.args:
        raise WorkspaceError('Enquiry writes do not accept query parameters.')
    close_inquiry(inquiry_id, user.id)
    db.session.commit()
    return redirect(url_for('inquiries.room', inquiry_id=inquiry_id))


@inquiries_bp.route('/inquiries/<int:inquiry_id>/seen', methods=['POST'])
@login_required
def seen(inquiry_id):
    user = human_owner()
    form_fields({'csrf_token', 'through_id'})
    value = request.form.get('through_id', '')
    if request.args or not re.fullmatch(r'[0-9]{1,19}', value):
        raise WorkspaceError('Provide the last message ID you viewed.')
    mark_inquiry_seen(inquiry_id, user.id, int(value))
    db.session.commit()
    return redirect(url_for('inquiries.room', inquiry_id=inquiry_id))


def agent(scopes):
    if not agent_inquiries_enabled():
        raise WorkspaceError('Agent listing conversations are not enabled.', 403)
    return authenticate_bearer(request.headers.get('Authorization'), scopes,
                               connection_id=request.headers.get('X-Liquidity-Connection'))


def write_body():
    if request.args:
        raise WorkspaceError('Enquiry writes do not accept query parameters.')
    if not request.is_json:
        raise WorkspaceError('Use Content-Type: application/json.', 415)
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    try:
        return json.loads(request.get_data(cache=False), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise WorkspaceError('Provide valid JSON without duplicate fields.') from None


@inquiries_bp.route('/api/agent/v1/listings', methods=['GET'])
def api_listings():
    connection = agent(['listings:read'])
    after, limit = pagination(extra=('kind', 'payment_asset', 'side'))
    kind = request.args.get('kind', 'all')
    asset, side = request.args.get('payment_asset'), request.args.get('side')
    if kind not in ('all', 'p2p', 'atomic') or asset is not None and asset not in PAYMENT_ASSETS or side is not None and side not in ('buy', 'sell'):
        raise WorkspaceError('Choose a supported listing kind, exact asset/network, and buy or sell side.')
    # Each book has independent IDs. Cursor is applied to both; each book gets
    # its own next_cursor, so callers should paginate a selected kind afterward.
    books = {}
    for current_kind, model in (('p2p', P2POffer), ('atomic', Order)):
        if kind not in ('all', current_kind):
            continue
        query = model.query.filter(model.id > after, model.status == 'open')
        if side:
            query = query.filter_by(side=side)
        if asset:
            if current_kind == 'atomic':
                if asset != 'btc-bitcoin':
                    books[current_kind] = {'listings': [], 'next_cursor': after, 'has_more': False}
                    continue
            elif asset == 'btc-bitcoin':
                query = query.filter(or_(P2POffer.payment_asset_id == asset, P2POffer.payment_asset_id.is_(None)))
            else:
                query = query.filter_by(payment_asset_id=asset)
        rows = query.order_by(model.id.asc()).limit(limit + 1).all()
        page = rows[:limit]
        books[current_kind] = {'listings': [serialize_listing(current_kind, row, connection.owner_id) for row in page],
            'next_cursor': page[-1].id if page else after, 'has_more': len(rows) > limit}
    return jsonify({'books': books, 'limits': INQUIRY_LIMITS,
                    'warning': 'Listings are unverified. Enquiries do not accept, reserve, or change a listing or authorize payment.'})


@inquiries_bp.route('/api/agent/v1/inquiries', methods=['GET'])
def api_inquiries():
    connection = agent(['inquiries:read'])
    after, limit = pagination()
    return jsonify({**threads_page(connection.owner_id, after, limit),
        'polling': 'Rescan enquiry pages from after=0 to find updates to existing conversations. GET never marks messages read.'})


@inquiries_bp.route('/api/agent/v1/inquiries/<int:inquiry_id>', methods=['GET'])
def api_inquiry(inquiry_id):
    connection = agent(['inquiries:read'])
    after, limit = pagination()
    inquiry = participant_inquiry(inquiry_id, connection.owner_id)
    return jsonify(room_context(inquiry, connection.owner_id, after, limit))


@inquiries_bp.route('/api/agent/v1/listings/<kind>/<int:listing_id>/inquiries', methods=['POST'])
def api_start(kind, listing_id):
    connection = agent(['inquiries:write'])
    result, created = start_inquiry(kind, listing_id, connection.owner_id,
        request.headers.get('Idempotency-Key'), write_body(), connection=connection)
    db.session.commit()
    return jsonify({**result, 'created': created}), 201 if created else 200


@inquiries_bp.route('/api/agent/v1/inquiries/<int:inquiry_id>/messages', methods=['POST'])
def api_reply(inquiry_id):
    connection = agent(['inquiries:write'])
    result, created = reply_inquiry(inquiry_id, connection.owner_id,
        request.headers.get('Idempotency-Key'), write_body(), connection=connection)
    db.session.commit()
    return jsonify({**result, 'created': created}), 201 if created else 200
