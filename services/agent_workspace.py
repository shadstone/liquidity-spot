"""Owner-scoped permissions; maker writes require a separate reviewed SSO grant."""
from datetime import datetime, timedelta
import hashlib
import json
import re
import secrets

from sqlalchemy import update
from models import db, User
from services.payment_assets import get_payment_asset, parse_offer_amounts, format_decimal
from services.agent_sso import AgentSSOGrant, authenticate_sso_connection


SCOPE = 'drafts:read drafts:write'
PROFILES = {
    'offer-drafts': {
        'label': 'Prepare offer drafts (review first)',
        'scopes': ['drafts:read', 'drafts:write'],
    },
    'trade-assistant': {
        'label': 'Watch trades (read-only)',
        'scopes': ['events:read', 'trades:read'],
        'optional_scope': 'trade_messages:read',
    },
    'maker-assistant': {
        'label': 'Manage offers & reply (within my limits)',
        'scopes': ['events:read', 'trades:read', 'offers:read', 'maker:write'],
        'optional_scope': 'trade_messages:read',
    },
}
VALID_SCOPE_SETS = (
    frozenset(PROFILES['offer-drafts']['scopes']),
    frozenset(PROFILES['trade-assistant']['scopes']),
    frozenset([*PROFILES['trade-assistant']['scopes'], 'trade_messages:read']),
    frozenset(PROFILES['maker-assistant']['scopes']),
    frozenset([*PROFILES['maker-assistant']['scopes'], 'trade_messages:read']),
)
LIMITS = {'pending': 50, 'hourly': 100, 'body_bytes': 8192,
          'connections': 5, 'token_days': 7, 'connections_hourly': 20}
TOKEN_PATTERN = re.compile(r'ls_agent_[A-Za-z0-9_-]{43}')
IDEMPOTENCY_PATTERN = re.compile(r'[A-Za-z0-9_.:-]{1,128}')


class AgentConnection(db.Model):
    __tablename__ = 'agent_connections'
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    label = db.Column(db.String(80), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    scope = db.Column(db.String(80), nullable=False, default=SCOPE)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)
    last_used_at = db.Column(db.DateTime)
    owner = db.relationship('User')

    @property
    def is_active(self):
        return self.revoked_at is None and self.expires_at > datetime.utcnow()

    @property
    def scope_list(self):
        return self.scope.split()

    @property
    def profile_id(self):
        scopes = set(self.scope_list) - {'trade_messages:read'}
        return next((key for key, profile in PROFILES.items() if scopes == set(profile['scopes'])), None)

    @property
    def authentication_method(self):
        return 'gfavip-sso' if self.sso_grant is not None else 'scoped-token'


class AgentDraft(db.Model):
    __tablename__ = 'agent_drafts'
    __table_args__ = (
        db.UniqueConstraint('connection_id', 'idempotency_key', name='uq_agent_draft_connection_key'),
        db.Index('idx_agent_draft_owner_created', 'owner_id', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    connection_id = db.Column(db.Integer, db.ForeignKey('agent_connections.id'), nullable=False)
    actor = db.Column(db.String(20), nullable=False, default='agent')
    idempotency_key = db.Column(db.String(128), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    terms = db.Column(db.JSON, nullable=False)
    notes = db.Column(db.Text, nullable=False, default='')
    status = db.Column(db.String(20), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    dismissed_at = db.Column(db.DateTime)
    dismissed_by_user_id = db.Column(db.String(36), db.ForeignKey('users.id'))
    owner = db.relationship('User', foreign_keys=[owner_id])
    connection = db.relationship('AgentConnection')


class WorkspaceError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest_token(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def lock_owner(owner_id):
    """Serialize owner quotas, connection revocation, and draft creation."""
    if db.engine.dialect.name == 'sqlite':
        # SQLite ignores FOR UPDATE. A no-op write takes its writer lock while
        # retaining the user's values; it is never a wallet/balance mutation.
        db.session.execute(update(User).where(User.id == owner_id).values(last_sync=User.last_sync)
                           .execution_options(synchronize_session=False))
    # PostgreSQL NO KEY UPDATE still serializes owner operations without
    # deadlocking against KEY SHARE checks when room events reference users.
    return User.query.filter_by(id=owner_id).with_for_update(key_share=True).first()


def issue_connection(owner_id, label, profile='offer-drafts', include_messages=False,
                     maker_policy=None, *, _reviewed_sso=False):
    if profile not in PROFILES:
        raise WorkspaceError('Choose an available agent permission profile.')
    if type(include_messages) is not bool:
        raise WorkspaceError('Message access requires an explicit yes/no choice.')
    if include_messages and profile not in ('trade-assistant', 'maker-assistant'):
        raise WorkspaceError('Message access is only available for trade watching or maker assistance.')
    canonical_policy = None
    if profile == 'maker-assistant':
        from services.agent_maker import maker_enabled, validate_maker_policy
        if not _reviewed_sso:
            raise WorkspaceError('Maker access requires a reviewed GFAVIP username connection.', 403)
        if not maker_enabled():
            raise WorkspaceError('Agent offer management is not enabled.', 503)
        canonical_policy = validate_maker_policy(maker_policy)
    elif maker_policy is not None:
        raise WorkspaceError('Maker limits are only available for the maker-assistant profile.')
    scopes = list(PROFILES[profile]['scopes'])
    if include_messages:
        scopes.append('trade_messages:read')
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
        raise WorkspaceError('Give the connection a name of 1–80 characters.')
    if any(ord(char) < 32 or ord(char) == 127 for char in label):
        raise WorkspaceError('Connection names cannot contain control characters.')
    if not lock_owner(owner_id):
        raise WorkspaceError('Your account is no longer available.', 401)
    now = datetime.utcnow()
    active = AgentConnection.query.filter(
        AgentConnection.owner_id == owner_id, AgentConnection.revoked_at.is_(None),
        AgentConnection.expires_at > now,
    ).count()
    if active >= LIMITS['connections']:
        raise WorkspaceError('Revoke an existing connection before adding another (maximum 5 active).', 429)
    issued_recently = AgentConnection.query.filter(
        AgentConnection.owner_id == owner_id, AgentConnection.created_at > now - timedelta(hours=1)
    ).count()
    if issued_recently >= LIMITS['connections_hourly']:
        raise WorkspaceError('Connection creation limit reached. Try again in an hour.', 429)
    raw_token = 'ls_agent_' + secrets.token_urlsafe(32)
    connection = AgentConnection(owner_id=owner_id, label=label.strip(), token_hash=digest_token(raw_token),
                                 scope=' '.join(scopes), created_at=now, expires_at=now + timedelta(days=LIMITS['token_days']))
    db.session.add(connection)
    db.session.flush()
    if canonical_policy is not None:
        from services.agent_maker import AgentMakerPolicy, policy_digest
        db.session.add(AgentMakerPolicy(connection_id=connection.id, policy=canonical_policy,
                                       policy_hash=policy_digest(canonical_policy)))
        db.session.flush()
    return connection, raw_token


def require_scopes(connection, required_scopes):
    if isinstance(required_scopes, str):
        required_scopes = required_scopes.split()
    if not set(required_scopes or ()).issubset(connection.scope_list):
        raise WorkspaceError('This credential does not grant the required permission.', 403)


def authenticate_bearer(header, required_scopes=None, connection_id=None):
    if not isinstance(header, str) or len(header) > 250:
        raise WorkspaceError('A valid scoped agent Bearer credential is required.', 401)
    parts = header.split(' ')
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise WorkspaceError('A valid scoped agent Bearer credential is required.', 401)
    if parts[1].startswith('gfavip-session-'):
        connection = authenticate_sso_connection(parts[1], connection_id)
    else:
        if len(header) > 100 or not TOKEN_PATTERN.fullmatch(parts[1]):
            raise WorkspaceError('A valid scoped agent Bearer credential is required.', 401)
        connection = AgentConnection.query.filter_by(token_hash=digest_token(parts[1])).first()
        # SSO grants deliberately discard the generated legacy secret. Even if
        # it were accidentally retained, it must not bypass Wallet verification.
        if connection and (connection.sso_grant is not None or 'maker:write' in connection.scope_list):
            raise WorkspaceError('A valid scoped agent Bearer credential is required.', 401)
    if not connection or not connection.is_active or frozenset(connection.scope_list) not in VALID_SCOPE_SETS:
        raise WorkspaceError('A valid scoped agent Bearer credential is required.', 401)
    require_scopes(connection, required_scopes)
    return connection


def validate_draft(payload):
    if not isinstance(payload, dict):
        raise WorkspaceError('The request body must be a JSON object.')
    required = {'side', 'payment_asset', 'amount_hns', 'price'}
    if not required.issubset(payload) or set(payload) - (required | {'notes'}):
        raise WorkspaceError('Use only side, payment_asset, amount_hns, price, and optional notes.')
    if any(not isinstance(payload[key], str) for key in required):
        raise WorkspaceError('Side, payment asset, amount_hns, and price must be strings; monetary JSON numbers are not accepted.')
    if payload['side'] not in ('buy', 'sell'):
        raise WorkspaceError('Side must be buy or sell HNS.')
    notes = payload.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 1000:
        raise WorkspaceError('Notes must be text of at most 1,000 characters.')
    if '\x00' in notes:
        raise WorkspaceError('Notes cannot contain null characters.')
    try:
        amount, price, total = parse_offer_amounts(payload['amount_hns'], payload['price'], payload['payment_asset'])
        asset = get_payment_asset(payload['payment_asset'])
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc
    canonical = {**payload, 'notes': notes}
    payload_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    terms = {
        'version': 1, 'side': payload['side'], 'amount_hns': format_decimal(amount),
        'price': format_decimal(price), 'total': format_decimal(total), 'payment_asset': asset,
        'rounding': 'nearest atomic unit, ties up',
        'fees': 'Sender pays network fees separately; recipient receives the agreed total.',
        'settlement': 'Draft only. Manual P2P; no escrow, bridge, or automatic verification.',
    }
    return terms, notes, payload_hash


def create_draft(connection, key, payload):
    if not isinstance(key, str) or not IDEMPOTENCY_PATTERN.fullmatch(key):
        raise WorkspaceError('Provide an Idempotency-Key of 1–128 letters, digits, dots, hyphens, underscores, or colons.')
    terms, notes, payload_hash = validate_draft(payload)
    if not lock_owner(connection.owner_id):
        raise WorkspaceError('A valid draft-only Bearer credential is required.', 401)
    # Revocation and create share the owner lock; recheck after waiting for it.
    db.session.refresh(connection)
    if not connection.is_active or frozenset(connection.scope_list) not in VALID_SCOPE_SETS:
        raise WorkspaceError('A valid draft-only Bearer credential is required.', 401)
    require_scopes(connection, ['drafts:write'])
    previous = AgentDraft.query.filter_by(connection_id=connection.id, idempotency_key=key).first()
    if previous:
        if previous.payload_hash != payload_hash:
            raise WorkspaceError('That Idempotency-Key is already bound to different draft content.', 409)
        connection.last_used_at = datetime.utcnow()
        return previous, False
    if AgentDraft.query.filter_by(owner_id=connection.owner_id, status='pending').count() >= LIMITS['pending']:
        raise WorkspaceError('Maximum 50 pending drafts. Review or dismiss drafts before creating more.', 429)
    now = datetime.utcnow()
    if AgentDraft.query.filter(AgentDraft.owner_id == connection.owner_id,
                              AgentDraft.created_at > now - timedelta(hours=1)).count() >= LIMITS['hourly']:
        raise WorkspaceError('Maximum 100 new drafts per hour. Try again later.', 429)
    draft = AgentDraft(owner_id=connection.owner_id, connection_id=connection.id,
                       actor='agent', idempotency_key=key, payload_hash=payload_hash,
                       terms=terms, notes=notes, status='pending', created_at=now)
    db.session.add(draft)
    connection.last_used_at = now
    db.session.flush()
    return draft, True


def serialize_draft(draft):
    return {'id': draft.id, 'status': draft.status, 'terms': draft.terms, 'notes': draft.notes,
            'actor': draft.actor, 'connection_id': draft.connection_id,
            'created_at': draft.created_at.isoformat() + 'Z',
            'dismissed_at': draft.dismissed_at.isoformat() + 'Z' if draft.dismissed_at else None,
            'funds_verified': False, 'published': False}
