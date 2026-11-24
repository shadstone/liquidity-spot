"""Public, read-only projection of external ShakeScape offers."""

from flask import Blueprint, current_app, jsonify

from services.shakescape_board import BoardUnavailable, board_snapshot
from services.shakescape_name_board import name_board_snapshot


shakescape_bp = Blueprint('shakescape', __name__)


@shakescape_bp.get('/api/external-offers/shakescape')
def external_offers():
    try:
        snapshot = board_snapshot(
            current_app.config['SHAKESCAPE_BOARD_FEED_URL'],
            current_app.config['SHAKESCAPE_BOARD_NETWORK'],
            current_app.config['SHAKESCAPE_OFFER_URI_TEMPLATE'],
        )
    except BoardUnavailable:
        response = jsonify({'status': 'unavailable', 'offers': []})
        response.status_code = 503
    else:
        response = jsonify({'status': 'available', **snapshot})
    response.headers['Cache-Control'] = 'no-store'
    return response


@shakescape_bp.get('/api/external-offers/shakescape/names')
def external_name_offers():
    try:
        snapshot = name_board_snapshot(
            current_app.config['SHAKESCAPE_NAME_BOARD_FEED_URL'],
            current_app.config['SHAKESCAPE_BOARD_NETWORK'],
            current_app.config['SHAKESCAPE_NAME_OFFER_URI_TEMPLATE'],
        )
    except BoardUnavailable:
        response = jsonify({'status': 'unavailable', 'offers': []})
        response.status_code = 503
    else:
        response = jsonify({'status': 'available', **snapshot})
    response.headers['Cache-Control'] = 'no-store'
    return response
