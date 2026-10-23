(() => {
    'use strict';

    const rows = document.getElementById('shakescape-offer-rows');
    const status = document.getElementById('shakescape-offer-status');
    if (!rows || !status) return;

    const cell = (row, value, className = 'p-2') => {
        const td = document.createElement('td');
        td.className = className;
        td.textContent = value;
        row.appendChild(td);
        return td;
    };

    const addOffer = (offer) => {
        const row = document.createElement('tr');
        row.className = 'border-b border-gfa-light/20 bg-gfa-light/5';
        row.id = `shakescape-offer-${offer.offer_id}`;
        const source = cell(row, 'ShakeScape', 'p-2 text-sm text-gfa-light');
        const detail = document.createElement('div');
        detail.className = 'text-xs text-gfa-grey';
        detail.textContent = `Signer ${offer.signer_fingerprint}`;
        source.appendChild(detail);
        cell(row, offer.side.toUpperCase(), 'p-2 uppercase');
        cell(row, offer.amount_hns);
        const price = cell(row, offer.price_btc_per_hns);
        const total = document.createElement('div');
        total.className = 'text-xs text-gfa-grey';
        total.textContent = `${offer.total_btc} BTC total · one whole offer`;
        price.appendChild(total);
        cell(row, '—');
        const action = cell(row, '', 'p-2 text-xs');
        if (offer.offer_uri) {
            const link = document.createElement('a');
            link.href = offer.offer_uri;
            link.className = 'font-bold text-gfa-light underline';
            link.textContent = 'Open in ShakeScape';
            link.rel = 'noopener noreferrer';
            action.appendChild(link);
        }
        const id = document.createElement('div');
        id.className = 'mt-1 text-gfa-grey break-all';
        id.textContent = `Offer ID: ${offer.offer_id}`;
        action.appendChild(id);
        if (navigator.clipboard?.writeText) {
            const copy = document.createElement('button');
            copy.type = 'button';
            copy.className = 'mt-1 text-gfa-light underline';
            copy.textContent = 'Copy offer ID';
            copy.addEventListener('click', () => {
                navigator.clipboard.writeText(offer.offer_id)
                    .then(() => { copy.textContent = 'Copied'; })
                    .catch(() => { copy.textContent = 'Select the ID above'; });
            });
            action.appendChild(copy);
        }
        rows.appendChild(row);
    };

    const refresh = async () => {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 4000);
        try {
            const response = await fetch(rows.dataset.feedUrl, {
                cache: 'no-store', signal: controller.signal,
            });
            if (!response.ok) throw new Error('feed unavailable');
            const snapshot = await response.json();
            if (snapshot.status !== 'available' || !Array.isArray(snapshot.offers)) {
                throw new Error('invalid feed');
            }
            rows.replaceChildren();
            snapshot.offers.forEach(addOffer);
            status.textContent = snapshot.offers.length
                ? `${snapshot.offers.length} ShakeScape offer${snapshot.offers.length === 1 ? '' : 's'} advertised. Check current terms in ShakeScape before acting.`
                : 'No ShakeScape offers are currently advertised.';
        } catch {
            rows.replaceChildren();
            status.textContent = 'ShakeScape offers are temporarily unavailable. Liquidity Spot orders are unaffected.';
        } finally {
            clearTimeout(timeout);
            setTimeout(refresh, 10_000);
        }
    };
    refresh();
})();
