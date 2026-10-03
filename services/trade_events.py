"""Transactional, private P2P event journal; no delivery or trade authority.

Readers advance a cursor only through the returned owner's committed events.
PostgreSQL writers serialize on participant-specific advisory locks before
allocating event IDs. A later event for an owner cannot commit ahead of an
earlier one and make an ID-based reader skip it. Locks last until commit or
rollback; unrelated owners can write independently. Sequence gaps are normal.
"""
from datetime import datetime
import hashlib

from sqlalchemy import text
from models import db, P2PTrade


EVENT_LOCK_NAMESPACE = 0x4C535445  # LSTE; distinct from single-key schema locks.
EVENT_KINDS = frozenset({
    'p2p.offer_accepted', 'p2p.message_added', 'p2p.trade_updated',
    'p2p.trade_canceled', 'p2p.dispute_opened',
})
SAFE_STATUSES = frozenset({'matched', 'completed', 'canceled', 'disputed', 'no_show'})
SAFE_MILESTONES = frozenset({'matched', 'payment_sent', 'payment_received', 'released', 'completed'})


class AgentTradeEvent(db.Model):
    __tablename__ = 'agent_trade_events'
    __table_args__ = (
        db.Index('idx_agent_trade_event_owner_cursor', 'owner_id', 'id'),
        {'sqlite_autoincrement': True},
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    trade_id = db.Column(db.Integer, db.ForeignKey('p2p_trades.id'), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    actor_user_id = db.Column(db.String(36), db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    payload = db.Column(db.JSON, nullable=False)


def lock_trade_message_writes(trade):
    """Serialize room message IDs before constructing or flushing a message.

    NO KEY UPDATE serializes trade writers without conflicting with foreign-key
    KEY SHARE checks. Hold the lock through commit, then take event locks only
    after business writes. SQLite already serializes allocation in its writer.
    The before-payment cancellation UPDATE acquires this row lock itself.
    """
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text("SET LOCAL lock_timeout = '10s'"))
        db.session.refresh(trade, with_for_update={'key_share': True})


def lock_trade_bond_writes(trade):
    """Before mutations, acquire trade then offer locks ahead of event locks.

    Refund handlers update both rows after the external wallet call. Preloading
    and locking them avoids adding a lazy load or new row-lock wait after credit.
    This does not make the external wallet call atomic with the database commit.
    """
    lock_trade_message_writes(trade)
    offer = trade.offer
    if db.engine.dialect.name == 'postgresql':
        db.session.refresh(offer, with_for_update={'key_share': True})


def _lock_participant_events(participant_ids):
    if db.engine.dialect.name != 'postgresql':
        # SQLite allocates IDs only while holding its transaction writer lock.
        return
    # Stable hashes (not Python's randomized hash). Sort lock keys, not just
    # names: even a hash collision can only add contention, not reverse order.
    lock_keys = sorted({int.from_bytes(hashlib.sha256(owner_id.encode('utf-8')).digest()[:4],
                                      'big', signed=True) for owner_id in participant_ids})
    # Do not leave a trade request waiting indefinitely behind another writer.
    # SET LOCAL lasts only for this transaction; timeout fails the entire write.
    db.session.execute(text("SET LOCAL lock_timeout = '10s'"))
    for key in lock_keys:
        db.session.execute(text('SELECT pg_advisory_xact_lock(:namespace, :owner_key)'),
                           {'namespace': EVENT_LOCK_NAMESPACE, 'owner_key': key})


def emit_trade_event(trade, kind, actor_user_id, message_id=None):
    """Append one private event per participant, never commit or send anything.

    Flush business-row writes first to keep their row-lock order ahead of the
    advisory locks across all callers. Event IDs are allocated only afterward.
    A rollback rolls back the business change and both events together.
    """
    if not isinstance(trade, P2PTrade):
        raise TypeError('Only manual P2P trades are supported by this journal.')
    if kind not in EVENT_KINDS:
        raise ValueError('Unsupported P2P event kind.')
    if message_id is not None and (type(message_id) is not int or message_id <= 0):
        raise ValueError('message_id must be a positive integer.')
    participants = {trade.creator_id, trade.counterparty_id}
    if any(not isinstance(owner_id, str) or not owner_id for owner_id in participants):
        raise ValueError('A P2P event requires both trade participants.')
    participants = sorted(participants)

    db.session.flush()
    if trade.id is None:
        raise ValueError('Trade must belong to the database session before emitting events.')
    _lock_participant_events(participants)

    # Never copy arbitrary status text, notes, chat, addresses or transaction IDs.
    metadata = {
        'status': trade.status if trade.status in SAFE_STATUSES else 'unknown',
        'milestone': trade.milestone if trade.milestone in SAFE_MILESTONES else 'unknown',
    }
    if message_id is not None:
        metadata['message_id'] = message_id
    now = datetime.utcnow()
    events = [AgentTradeEvent(owner_id=owner_id, trade_id=trade.id, kind=kind,
                             actor_user_id=actor_user_id, created_at=now, payload=dict(metadata))
              for owner_id in participants]
    db.session.add_all(events)
    db.session.flush()
    return events


def serialize_event(event):
    # Keep the serialization allowlist too: future stored metadata must not
    # accidentally expand the read-only event scope to include private text.
    stored = event.payload or {}
    status, milestone = stored.get('status'), stored.get('milestone')
    metadata = {
        'status': status if isinstance(status, str) and status in SAFE_STATUSES else 'unknown',
        'milestone': milestone if isinstance(milestone, str) and milestone in SAFE_MILESTONES else 'unknown',
    }
    if type(stored.get('message_id')) is int and stored['message_id'] > 0:
        metadata['message_id'] = stored['message_id']
    return {
        'id': event.id, 'type': event.kind, 'trade_id': event.trade_id,
        'actor_user_id': event.actor_user_id,
        'actor': ('system' if event.actor_user_id is None else
                  'self' if event.actor_user_id == event.owner_id else 'counterparty'),
        'occurred_at': event.created_at.isoformat() + 'Z',
        'room_url': f'/p2p/trades/{event.trade_id}',
        'metadata': metadata,
    }
