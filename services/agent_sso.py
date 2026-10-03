"""Verified GFAVIP identity plus explicit, owner-issued Liquidity permissions.

Wallet POST /api/auth/validate returns ``valid`` and ``user.id``. No other
Wallet claims (including agent_context.owner_id or tier) confer permissions.
"""
import re
from uuid import UUID

import requests

from models import db
from services.http_client import post as http_post


WALLET_VALIDATE_URL = 'https://wallet.gfavip.com/api/auth/validate'
# Opaque session credential: never decode the suffix as a user identity.
SSO_TOKEN_PATTERN = re.compile(r'gfavip-session-[A-Za-z0-9_-]{16,200}')
UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
CONNECTION_ID_PATTERN = re.compile(r'[1-9][0-9]{0,9}')


class AgentSSOGrant(db.Model):
    __tablename__ = 'agent_sso_grants'
    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey('agent_connections.id'), nullable=False, unique=True)
    agent_gfavip_user_id = db.Column(db.String(36), nullable=False, index=True)
    connection = db.relationship('AgentConnection', backref=db.backref('sso_grant', uselist=False))


def canonical_wallet_id(value):
    """Accept only a permanent UUID, not an email, handle, token, or PL key."""
    if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
        return None
    parsed = UUID(value)
    return str(parsed) if parsed.int else None


def create_sso_grant(owner_id, label, agent_gfavip_user_id,
                     profile='offer-drafts', include_messages=False,
                     maker_policy=None, reviewed_username=False):
    """Create, but do not commit, an explicit human-authorized connection.

    The supplied ID is a binding chosen by the owner, not proof of identity.
    Every use must independently verify the session with Wallet and match it.
    The unused legacy token is discarded and cannot authenticate this grant.
    """
    from services.agent_workspace import WorkspaceError, issue_connection

    agent_id = canonical_wallet_id(agent_gfavip_user_id)
    if agent_id is None:
        raise WorkspaceError('Enter the agent’s permanent GFAVIP Wallet UUID, not a token, email, or handle.')
    if profile == 'maker-assistant' and reviewed_username is not True:
        raise WorkspaceError('Maker access requires reviewing the matched GFAVIP username.', 403)
    connection, unused_token = issue_connection(owner_id, label, profile, include_messages,
                                                maker_policy=maker_policy,
                                                _reviewed_sso=reviewed_username is True)
    del unused_token
    db.session.add(AgentSSOGrant(connection=connection, agent_gfavip_user_id=agent_id))
    db.session.flush()
    return connection


def validate_agent_identity(token):
    """Return only Wallet-verified identity. Never persist or echo the token."""
    from services.agent_workspace import WorkspaceError

    if not isinstance(token, str) or not SSO_TOKEN_PATTERN.fullmatch(token):
        raise WorkspaceError('A valid GFAVIP session Bearer credential is required.', 401)
    try:
        response = http_post(WALLET_VALIDATE_URL,
                             headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
                             timeout=5, allow_redirects=False)
    except requests.RequestException:
        raise WorkspaceError('GFAVIP identity verification is temporarily unavailable. Try again later.', 503) from None
    if response.status_code in (401, 403):
        raise WorkspaceError('The GFAVIP session is invalid or expired. Re-authenticate with Wallet.', 401)
    if response.status_code != 200:
        raise WorkspaceError('GFAVIP identity verification is temporarily unavailable. Try again later.', 503)
    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        raise WorkspaceError('GFAVIP identity verification is temporarily unavailable. Try again later.', 503) from None
    if not isinstance(payload, dict) or payload.get('valid') is not True:
        raise WorkspaceError('The GFAVIP session could not be verified. Re-authenticate with Wallet.', 401)
    user = payload.get('user')
    verified_id = canonical_wallet_id(user.get('id')) if isinstance(user, dict) else None
    if verified_id is None:
        raise WorkspaceError('GFAVIP identity verification is temporarily unavailable. Try again later.', 503)
    return {'gfavip_user_id': verified_id}


def authenticate_sso_connection(token, connection_id):
    """Require an exact connection selection and matching verified principal."""
    from services.agent_workspace import AgentConnection, WorkspaceError

    if (not isinstance(connection_id, str) or not CONNECTION_ID_PATTERN.fullmatch(connection_id)
            or int(connection_id) > 2147483647):
        raise WorkspaceError('Provide the exact connection ID in X-Liquidity-Connection.', 400)
    identity = validate_agent_identity(token)
    connection = AgentConnection.query.join(
        AgentSSOGrant, AgentSSOGrant.connection_id == AgentConnection.id,
    ).filter(AgentConnection.id == int(connection_id),
             AgentSSOGrant.agent_gfavip_user_id == identity['gfavip_user_id']).first()
    if connection is None:
        raise WorkspaceError('A matching active Liquidity agent connection is required.', 401)
    return connection
