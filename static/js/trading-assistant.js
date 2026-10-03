/* Optional enhancement only: the server validates every choice and limit. */
(function () {
  'use strict';
  const form = document.getElementById('trading-assistant-form');
  if (!form) return;
  form.querySelectorAll('[data-trading-side]').forEach(function (panel) {
    const selected = panel.querySelector('[data-publish-side]');
    const fields = panel.querySelector('[data-side-fields]');
    const replies = panel.querySelector('[data-side-replies]');
    function update() {
      fields.hidden = !selected.checked;
      fields.querySelectorAll('input, select').forEach(function (input) {
        input.disabled = !selected.checked;
        if (!selected.checked) {
          if (input.type === 'checkbox') input.checked = false;
          else input.value = '';
        }
      });
      fields.querySelectorAll('[data-policy-required]').forEach(function (input) {
        input.required = selected.checked;
      });
      fields.querySelectorAll('[data-reply-limit]').forEach(function (input) {
        input.disabled = !selected.checked || !replies.checked;
        input.required = selected.checked && replies.checked;
        if (input.disabled) input.value = '';
      });
    }
    selected.addEventListener('change', update);
    replies.addEventListener('change', update);
    update();
  });
}());
