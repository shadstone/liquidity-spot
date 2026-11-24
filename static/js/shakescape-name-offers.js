(() => {
    'use strict';

    const rows = document.getElementById('shakescape-name-offer-rows');
    const status = document.getElementById('shakescape-name-offer-status');
    if (!rows || !status) return;

    const cell = (row, value) => {
        const td = document.createElement('td');
        td.className = 'p-2';
        td.textContent = value;
        row.appendChild(td);
        return td;
    };

    const addOffer = (offer) => {
        const row = document.createElement('tr');
        row.className = 'border-b border-gfa-light/20 bg-gfa-light/5';
        const name = cell(row, offer.name);
        const signer = document.createElement('div');
        signer.className = 'text-xs text-gfa-grey';
        signer.textContent = `Seller ${offer.seller_fingerprint}`;
        name.appendChild(signer);
        cell(row, offer.price_hns);
        cell(row, offer.marketplace_fee_hns);
        const expiry = new Date(offer.expires_at * 1000);
        cell(row, Number.isNaN(expiry.getTime())
            ? String(offer.expires_at) : expiry.toLocaleString());
        const action = cell(row, '');
        if (offer.offer_uri) {
            const link = document.createElement('a');
            link.href = offer.offer_uri;
            link.className = 'font-bold text-gfa-light underline';
            link.textContent = 'Open in ShakeScape';
            link.rel = 'noopener noreferrer';
            action.appendChild(link);
        }
        const id = document.createElement('div');
        id.className = 'mt-1 text-xs text-gfa-grey break-all';
        id.textContent = `Listing ID: ${offer.offer_id}`;
        action.appendChild(id);
        if (navigator.clipboard?.writeText) {
            const copy = (label, value) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'mt-1 mr-3 text-xs text-gfa-light underline';
                button.textContent = label;
                button.addEventListener('click', () => {
                    navigator.clipboard.writeText(value)
                        .then(() => { button.textContent = 'Copied'; })
                        .catch(() => { button.textContent = 'Copy failed'; });
                });
                action.appendChild(button);
            };
            if (offer.offer_uri) copy('Copy app link', offer.offer_uri);
            copy('Copy listing ID', offer.offer_id);
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
                ? `${snapshot.offers.length} signed name offer${snapshot.offers.length === 1 ? '' : 's'} advertised. Check current terms in ShakeScape.`
                : 'No name offers are currently advertised.';
        } catch {
            rows.replaceChildren();
            status.textContent = 'Name offers are temporarily unavailable. Liquidity Spot orders are unaffected.';
        } finally {
            clearTimeout(timeout);
            setTimeout(refresh, 10_000);
        }
    };
    refresh();
})();
