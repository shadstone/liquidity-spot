"""Resolve an exact Wallet username; never authenticate or grant access."""
import re
from uuid import UUID

import requests
from flask import current_app

from services.agent_sso import canonical_wallet_id
from services.agent_workspace import WorkspaceError
from services.http_client import get as http_get


WALLET_LOOKUP_URL = 'https://wallet.gfavip.com/api/external/users/lookup'
USERNAME_PATTERN = re.compile(r'[A-Za-z0-9_.-]{1,200}')
SECRET_PREFIXES = ('gfavip-session-', 'gfavip_', 'ls_agent_', 'ls-guest-',
                   'pl_live_', 'pl_test_', 'pl_api_', 'pl_sk_', 'sk-', 'sk_', 'pk_')
UNAVAILABLE_MESSAGE = (
    'Username lookup is not enabled or is temporarily unavailable. '
    'Use Advanced Wallet UUID with the agent’s verified ID, or try again later.'
)


def _lookup_key():
    for setting in ('GFAVIP_WALLET_LOOKUP_API_KEY', 'GFAVIP_WALLET_API_KEY'):
        value = current_app.config.get(setting)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def lookup_available():
    """Configured key presence only; does not prove Wallet permission or health."""
    return bool(_lookup_key())


def _username(raw):
    if not isinstance(raw, str):
        raise WorkspaceError('Enter the agent’s exact Wallet username, optionally starting with @.')
    name = raw.strip()
    if name.startswith('@'):
        name = name[1:]
    if name.lower().startswith(SECRET_PREFIXES):
        raise WorkspaceError('Enter a Wallet username, never an API key, session token or recovery key.')
    try:
        UUID(name)
    except ValueError:
        pass
    else:
        raise WorkspaceError('For a permanent Wallet UUID, choose Advanced Wallet UUID instead of username lookup.')
    if not USERNAME_PATTERN.fullmatch(name):
        raise WorkspaceError('Use an exact Wallet username of 1–200 letters, numbers, dots, hyphens or underscores, optionally starting with @.')
    return name


def lookup_agent_username(raw):
    """Return a minimal verified directory match for explicit owner confirmation.

    Matching a directory entry is not proof of ownership or a permission grant.
    Callers must bind the returned immutable ID, not re-resolve the name later.
    The service key belongs only in the fixed Wallet request's auth header.
    """
    name = _username(raw)
    key = _lookup_key()
    if key is None:
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503)
    try:
        response = http_get(WALLET_LOOKUP_URL, params={'username': name},
                            headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
                            timeout=5, allow_redirects=False)
    except requests.RequestException:
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503) from None
    if response.status_code == 404:
        raise WorkspaceError('No matching Wallet account was found. Check the exact username or use Advanced Wallet UUID.', 404)
    if response.status_code == 409:
        raise WorkspaceError('That username matches more than one Wallet account. Use Advanced Wallet UUID with the agent’s verified ID.', 409)
    if response.status_code == 429:
        raise WorkspaceError('Username lookup is busy. Wait a minute and try again, or use Advanced Wallet UUID.', 429)
    if response.status_code != 200:
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503)
    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503) from None
    if not isinstance(payload, dict) or set(payload) != {'id', 'username', 'accountKind'}:
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503)
    wallet_id = canonical_wallet_id(payload['id'])
    matched_name = payload['username']
    if (wallet_id is None or not isinstance(matched_name, str)
            or not USERNAME_PATTERN.fullmatch(matched_name) or matched_name.lower() != name.lower()):
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503)
    if payload['accountKind'] == 'human':
        raise WorkspaceError('This is a human Wallet account, not an AI agent. Ask your agent for its own Wallet username or verified UUID.')
    if payload['accountKind'] != 'ai_agent':
        raise WorkspaceError(UNAVAILABLE_MESSAGE, 503)
    return {'id': wallet_id, 'username': matched_name, 'accountKind': 'ai_agent'}
