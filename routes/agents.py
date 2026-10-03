"""Human-managed connections and a strictly draft-only machine API."""
from datetime import datetime
import hmac
import json
import re
import secrets

from flask import Blueprint, Response, jsonify, make_response, redirect, render_template, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

from models import db, User
from routes.auth import login_required
from services.agent_workspace import (
    AgentConnection, AgentDraft, LIMITS, SCOPE, WorkspaceError,
    authenticate_bearer, create_draft, issue_connection, lock_owner, serialize_draft,
)
from services.payment_assets import PAYMENT_ASSETS


agents_bp = Blueprint('agents', __name__)


@agents_bp.before_request
def bound_request_size():
    request.max_content_length = LIMITS['body_bytes']


@agents_bp.after_request
def private_responses(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@agents_bp.errorhandler(WorkspaceError)
def workspace_error(error):
    db.session.rollback()
    if request.path.startswith('/api/agent/'):
        response = jsonify({'error': str(error)})
    else:
        # Plain text, never HTML interpretation of user/agent content.
        response = Response(str(error), mimetype='text/plain')
    response.status_code = error.status
    if error.status == 401 and request.path.startswith('/api/agent/'):
        response.headers['WWW-Authenticate'] = 'Bearer realm="agent-drafts"'
    if error.status == 429:
        response.headers['Retry-After'] = '3600'
    return response


@agents_bp.errorhandler(RequestEntityTooLarge)
def oversized_request(error):
    return workspace_error(WorkspaceError('Request body exceeds the 8 KB limit.', 413))


@agents_bp.errorhandler(SQLAlchemyError)
def database_error(error):
    # Never echo request contents, credentials or database connection details.
    return workspace_error(WorkspaceError('The workspace is temporarily unavailable. Retry safely with the same Idempotency-Key.', 503))


def current_owner():
    user = db.session.get(User, session.get('user_id'))
    if not user:
        raise WorkspaceError('Please sign in again before managing agent access.', 401)
    return user


def csrf_token():
    if session.get('agent_workspace_csrf_owner') != session.get('user_id') or not session.get('agent_workspace_csrf'):
        session['agent_workspace_csrf'] = secrets.token_urlsafe(32)
        session['agent_workspace_csrf_owner'] = session.get('user_id')
    return session['agent_workspace_csrf']


def require_csrf():
    supplied = request.form.get('csrf_token', '')
    expected = session.get('agent_workspace_csrf', '')
    if (session.get('agent_workspace_csrf_owner') != session.get('user_id')
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied)
            or not expected or not hmac.compare_digest(supplied, expected)):
        raise WorkspaceError('Reload the workspace and submit the form again.', 400)


def workspace_context(owner):
    connections = AgentConnection.query.filter_by(owner_id=owner.id).order_by(AgentConnection.created_at.desc()).limit(50).all()
    pending = AgentDraft.query.filter_by(owner_id=owner.id, status='pending').order_by(AgentDraft.created_at.desc()).limit(LIMITS['pending']).all()
    history = AgentDraft.query.filter_by(owner_id=owner.id, status='dismissed').order_by(AgentDraft.dismissed_at.desc()).limit(20).all()
    return dict(connections=connections, drafts=pending + history, csrf_token=csrf_token(),
                limits=LIMITS, issued_token=None, issued_connection=None)


@agents_bp.route('/agents')
@login_required
def workspace():
    return render_template('agent_workspace.html', **workspace_context(current_owner()))


@agents_bp.route('/agents/connections', methods=['POST'])
@login_required
def create_connection():
    owner = current_owner()
    require_csrf()
    connection, raw_token = issue_connection(owner.id, request.form.get('label', ''))
    db.session.commit()
    # One-time response only: no token in a URL, flash, server session, or cookie.
    session['agent_workspace_csrf'] = secrets.token_urlsafe(32)
    context = workspace_context(owner)
    context.update(issued_token=raw_token, issued_connection=connection)
    response = make_response(render_template('agent_workspace.html', **context))
    # The issued-token branch is standalone: no external scripts can read it.
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    return response


@agents_bp.route('/agents/connections/<int:connection_id>/revoke', methods=['POST'])
@login_required
def revoke_connection(connection_id):
    owner = current_owner()
    require_csrf()
    lock_owner(owner.id)
    connection = AgentConnection.query.filter_by(id=connection_id, owner_id=owner.id).first()
    if not connection:
        raise WorkspaceError('Connection not found.', 404)
    if connection.revoked_at is None:
        connection.revoked_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('agents.workspace'))


@agents_bp.route('/agents/drafts/<int:draft_id>/dismiss', methods=['POST'])
@login_required
def dismiss_draft(draft_id):
    owner = current_owner()
    require_csrf()
    lock_owner(owner.id)
    draft = AgentDraft.query.filter_by(id=draft_id, owner_id=owner.id).first()
    if not draft:
        raise WorkspaceError('Draft not found.', 404)
    if draft.status == 'pending':
        draft.status = 'dismissed'
        draft.dismissed_at = datetime.utcnow()
        draft.dismissed_by_user_id = owner.id
    db.session.commit()
    return redirect(url_for('agents.workspace'))


@agents_bp.route('/api/agent/v1/capabilities', methods=['GET'])
def capabilities():
    return jsonify({
        'version': 1, 'mode': 'draft-only', 'scope': SCOPE,
        'payment_assets': list(PAYMENT_ASSETS.values()),
        'limits': LIMITS, 'required_headers': {'Authorization': 'Bearer <credential>', 'Idempotency-Key': '<unique key for each POST>'},
        'draft_schema': {
            'type': 'object', 'additionalProperties': False,
            'required': ['side', 'payment_asset', 'amount_hns', 'price'],
            'properties': {'side': {'type': 'string', 'enum': ['buy', 'sell']},
                           'payment_asset': {'type': 'string', 'enum': list(PAYMENT_ASSETS)},
                           'amount_hns': {'type': 'string', 'description': 'Positive decimal HNS amount, maximum 6 decimal places.'},
                           'price': {'type': 'string', 'description': 'Positive payment-asset units per HNS; BTC max 12 decimal places, other assets max 18.'},
                           'notes': {'type': 'string', 'maxLength': 1000}},
        },
        'idempotency': 'Per credential; reusing a key with different content returns 409. Successful replays do not create another draft.',
        'supported_actions': ['list-own-drafts', 'create-own-draft'],
        'forbidden_actions': ['publish', 'accept', 'trade-action', 'message', 'fund', 'sign', 'withdraw'],
        'settlement': 'No offers or trades are created. No custody, escrow, bridging, or payment verification.',
    })


def strict_json_body():
    if not request.is_json:
        raise WorkspaceError('Use Content-Type: application/json.', 415)
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field.')
            result[key] = value
        return result
    try:
        return json.loads(request.get_data(cache=False), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise WorkspaceError('Provide valid JSON without duplicate fields.')


@agents_bp.route('/api/agent/v1/drafts', methods=['GET', 'POST'])
def api_drafts():
    # Deliberately never uses login_required or the browser session for API auth.
    connection = authenticate_bearer(request.headers.get('Authorization'))
    if request.method == 'POST':
        draft, created = create_draft(connection, request.headers.get('Idempotency-Key'), strict_json_body())
        db.session.commit()
        return jsonify({'draft': serialize_draft(draft), 'created': created}), 201 if created else 200
    before = request.args.get('before_id')
    if before is not None and (not re.fullmatch(r'[0-9]{1,18}', before) or int(before) <= 0):
        raise WorkspaceError('before_id must be a positive draft ID.')
    query = AgentDraft.query.filter_by(owner_id=connection.owner_id)
    if before:
        query = query.filter(AgentDraft.id < int(before))
    drafts = query.order_by(AgentDraft.id.desc()).limit(101).all()
    has_more = len(drafts) > 100
    drafts = drafts[:100]
    connection.last_used_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'drafts': [serialize_draft(draft) for draft in drafts],
                    'next_before_id': drafts[-1].id if has_more else None})
