(() => {
    'use strict';

    const message = document.getElementById('offer-share-text');
    const link = document.getElementById('offer-share-url');
    const copyMessage = document.getElementById('copy-offer-text');
    const copyLink = document.getElementById('copy-offer-link');
    const shareMessage = document.getElementById('share-offer-text');
    const status = document.getElementById('share-offer-status');
    if (!status) return;

    if (message) {
        // Keep the entire paragraph visible, including the link, on phones.
        const fitMessage = () => {
            message.style.height = 'auto';
            message.style.height = `${message.scrollHeight + 2}px`;
        };
        fitMessage();
        window.addEventListener('resize', fitMessage);
    }

    function bindCopy(button, field, success, fallback) {
        if (!button || !field) return;
        button.addEventListener('click', async () => {
            try {
                if (!navigator.clipboard || !navigator.clipboard.writeText) {
                    throw new Error('Clipboard unavailable');
                }
                await navigator.clipboard.writeText(field.value);
                status.textContent = success;
            } catch (_) {
                field.focus();
                field.select();
                status.textContent = fallback;
            }
        });
    }

    bindCopy(copyMessage, message,
        'Full offer message copied. Paste the whole paragraph into your group.',
        'Automatic copy is unavailable. The full message is selected: copy it manually, then paste it into your group.');
    bindCopy(copyLink, link, 'Offer link copied.',
        'Automatic copy is unavailable. The link is selected: copy it manually.');

    if (shareMessage && message && typeof navigator.share === 'function') {
        shareMessage.hidden = false;
        shareMessage.addEventListener('click', async () => {
            status.textContent = '';
            try {
                // The visible paragraph already includes the permalink. Keep
                // native sharing identical to copying, with no duplicate URL.
                await navigator.share({title: 'Liquidity.spot offer', text: message.value});
                status.textContent = 'Share action completed.';
            } catch (error) {
                if (!error || error.name !== 'AbortError') {
                    status.textContent = 'Could not open the share menu. Use Copy full message instead.';
                }
            }
        });
    }
})();
