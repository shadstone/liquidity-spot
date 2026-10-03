(() => {
    'use strict';

    const noticeStack = document.getElementById('trade-notice-stack');
    if (noticeStack) {
        // Recovery and active-trade notices can make the sticky bar taller on
        // mobile. Keep the guide's section headings below the real stack.
        const updateAnchorOffset = () => {
            document.documentElement.style.setProperty('--liquidity-sticky-offset',
                `${Math.ceil(noticeStack.getBoundingClientRect().height) + 16}px`);
        };
        updateAnchorOffset();
        window.addEventListener('resize', updateAnchorOffset);
        if (window.ResizeObserver) {
            new window.ResizeObserver(updateAnchorOffset).observe(noticeStack);
        }
    }

    const banner = document.getElementById('agent-help-banner');
    const dismiss = document.getElementById('dismiss-agent-help');
    const dismissalKey = 'liquidity-agent-help-hidden-v1';
    if (banner && dismiss) {
        // No cookies, credentials or server changes. Storage may be unavailable.
        try {
            banner.hidden = window.sessionStorage.getItem(dismissalKey) === 'yes';
        } catch (_) {
            // The guide still works if the browser disallows storage.
        }
        dismiss.hidden = false;
        dismiss.addEventListener('click', () => {
            banner.hidden = true;
            try {
                window.sessionStorage.setItem(dismissalKey, 'yes');
            } catch (_) {
                // Hiding for this page is still useful without persistence.
            }
        });
    }

    const prompt = document.getElementById('agent-setup-prompt');
    const copy = document.getElementById('copy-agent-prompt');
    const status = document.getElementById('copy-agent-status');
    if (prompt && copy && status) {
        copy.addEventListener('click', async () => {
            try {
                if (!navigator.clipboard || !navigator.clipboard.writeText) {
                    throw new Error('Clipboard unavailable');
                }
                await navigator.clipboard.writeText(prompt.value);
                status.textContent = 'Prompt copied. Paste it into your agent. Do not add API keys to the conversation.';
            } catch (_) {
                prompt.focus();
                prompt.select();
                status.textContent = 'Automatic copy is unavailable. The prompt is selected: use your browser’s Copy command, then paste it into your agent.';
            }
        });
    }
})();
