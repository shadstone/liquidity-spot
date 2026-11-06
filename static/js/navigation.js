(() => {
    'use strict';
    const button = document.getElementById('mobile-menu-btn');
    const menu = document.getElementById('mobile-menu');
    if (!button || !menu) return;
    // With no JavaScript the mobile links stay visible and usable.
    const setOpen = (open) => {
        menu.hidden = !open;
        button.setAttribute('aria-expanded', String(open));
        button.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    };
    setOpen(false);
    button.hidden = false;
    button.addEventListener('click', () => setOpen(menu.hidden));
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        const active = document.activeElement;
        if (!menu.hidden && (menu.contains(active) || active === button)) {
            setOpen(false);
            button.focus();
        }
        document.querySelectorAll('.site-navigation details[open]').forEach((details) => {
            details.open = false;
            if (details.contains(active) && !menu.contains(active)) details.querySelector('summary').focus();
        });
    });
    window.matchMedia('(min-width: 1024px)').addEventListener('change', () => setOpen(false));
})();
