#!/usr/bin/env python3
"""One polling step for a user-owned routine. Never trades or sends messages.

Linux/macOS, Python 3.10+, standard library only. Fetch retains a private pending
batch until the caller explicitly acknowledges it after successful handling.
Credentials come only from LIQUIDITY_AGENT_TOKEN, never command-line arguments.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class PollError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer credential to a redirect destination.
        return None


def validate_base_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise PollError('Use https://liquidity.spot or an explicit loopback development URL.') from None
    local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or parsed.scheme not in ('https', 'http')
            or (not local and (parsed.scheme != 'https' or parsed.hostname != 'liquidity.spot'
                               or port not in (None, 443)))):
        raise PollError('Use https://liquidity.spot or an explicit loopback development URL.')
    return value.rstrip('/')


def fetch_events(base_url, token, after):
    if not token or '\n' in token or '\r' in token:
        raise PollError('Set LIQUIDITY_AGENT_TOKEN privately to a trade-assistant credential.')
    request = Request(base_url + '/api/agent/v1/events?' + urlencode({'after': after, 'limit': 50}),
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with build_opener(NoRedirects()).open(request, timeout=20) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise PollError('Response exceeded the safety limit; cursor unchanged.')
            payload = json.loads(data)
    except HTTPError as error:
        # Do not print remote response bodies or exception URLs.
        if error.code in (401, 403):
            raise PollError('Access denied. Check permissions, expiry or revocation; cursor unchanged.') from None
        raise PollError(f'Event endpoint returned HTTP {error.code}; cursor unchanged.') from None
    except (URLError, TimeoutError, ValueError, OSError):
        raise PollError('Could not fetch a valid event response; cursor unchanged.') from None
    return validate_batch(payload, after)


def valid_cursor(value):
    return type(value) is int and 0 <= value <= 9223372036854775807


def validate_batch(payload, after):
    if not isinstance(payload, dict):
        raise PollError('Invalid event response; cursor unchanged.')
    events = payload.get('events')
    stream = payload.get('stream_id')
    cursor = payload.get('next_cursor')
    if (not isinstance(stream, str) or not 1 <= len(stream) <= 128
            or not isinstance(events, list) or len(events) > 100
            or not valid_cursor(cursor) or type(payload.get('has_more')) is not bool):
        raise PollError('Invalid event response; cursor unchanged.')
    previous = after
    for event in events:
        if not isinstance(event, dict) or not valid_cursor(event.get('id')) or event['id'] <= previous:
            raise PollError('Events are not in cursor order; cursor unchanged.')
        previous = event['id']
    if cursor != previous or (not events and payload['has_more']):
        raise PollError('Invalid continuation cursor; cursor unchanged.')
    return {'stream_id': stream, 'events': events, 'next_cursor': cursor, 'has_more': payload['has_more']}


@contextmanager
def locked_state(path):
    try:
        import fcntl
    except ImportError:
        raise PollError('This helper requires Linux/macOS; other clients can use the documented HTTP API.') from None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(str(path) + '.lock', flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PollError('Another polling step is running; try again later.') from None
        yield
    finally:
        os.close(descriptor)


def read_state(path, base_url):
    if path.is_symlink():
        raise PollError('State must be a regular private file, not a symlink.')
    if not path.exists():
        return {'version': 1, 'base_url': base_url, 'stream_id': None, 'cursor': 0, 'pending': None}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise PollError('State file is invalid; it was not overwritten.')
    try:
        state = json.loads(path.read_text())
    except (ValueError, OSError):
        raise PollError('State file cannot be read; it was not overwritten.') from None
    if (not isinstance(state, dict) or state.get('version') != 1
            or state.get('base_url') != base_url or not valid_cursor(state.get('cursor'))
            or not (state.get('stream_id') is None or isinstance(state['stream_id'], str))):
        raise PollError('State file belongs to a different endpoint or format; use a separate file.')
    if state.get('pending') is not None:
        batch = validate_batch(state['pending'], state['cursor'])
        if batch['stream_id'] != state['stream_id'] or not batch['events']:
            raise PollError('Pending batch is invalid; state was not changed.')
    return state


def write_state(path, state):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(state, output, separators=(',', ':'))
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fetch_step(path, base_url, token):
    with locked_state(path):
        state = read_state(path, base_url)
        # Authenticate on every fetch, including when replaying a saved batch.
        batch = fetch_events(base_url, token, state['cursor'])
        if state['stream_id'] not in (None, batch['stream_id']):
            raise PollError('This state file belongs to another owner. Use a separate file; cursor unchanged.')
        state['stream_id'] = batch['stream_id']
        if state.get('pending') is None and batch['events']:
            state['pending'] = batch
        write_state(path, state)
        pending = state.get('pending') or batch
        return {**pending, 'ack_required': bool(pending['events']),
                'instruction': 'Handle and deduplicate events by ID, then ack next_cursor. No payment is verified by this feed.'}


def acknowledge(path, base_url, cursor):
    with locked_state(path):
        state = read_state(path, base_url)
        batch = state.get('pending')
        if not batch or cursor != batch['next_cursor']:
            raise PollError('Acknowledge only the exact next_cursor of the saved pending batch.')
        state['cursor'] = cursor
        state['pending'] = None
        write_state(path, state)
        return {'acknowledged_cursor': cursor}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('fetch', 'ack'))
    parser.add_argument('--state', type=Path, required=True, help='Private cursor file outside the repository; separate file per owner.')
    parser.add_argument('--base-url', default='https://liquidity.spot')
    parser.add_argument('--cursor', type=int, help='For ack only: last successfully handled next_cursor.')
    args = parser.parse_args()
    try:
        base_url = validate_base_url(args.base_url)
        if args.action == 'fetch':
            if args.cursor is not None:
                raise PollError('Fetch resumes the saved cursor; --cursor is only for ack.')
            result = fetch_step(args.state.expanduser().absolute(), base_url, os.environ.get('LIQUIDITY_AGENT_TOKEN', ''))
        else:
            if not valid_cursor(args.cursor):
                raise PollError('Ack requires a nonnegative --cursor from a successfully handled batch.')
            result = acknowledge(args.state.expanduser().absolute(), base_url, args.cursor)
        print(json.dumps(result, ensure_ascii=True))
    except (PollError, OSError) as error:
        print(str(error) if isinstance(error, PollError) else 'Private state could not be accessed; no events acknowledged.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
