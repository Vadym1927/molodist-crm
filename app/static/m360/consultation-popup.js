(() => {
  const ENDPOINT = '/api/lead', T0 = Date.now();   // заявка → звернення в CRM (як форми сайту), без сторонніх сервісів
  const TURNSTILE_SITEKEY = '0x4AAAAAAFMuSXEOwAuueMGz';
  const selector = '.js-consultation-popup';
  let widgetId = null;

  const modal = document.createElement('div');
  modal.className = 'consultation-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="consultation-modal__backdrop" data-consultation-close></div>
    <section class="consultation-modal__dialog" role="dialog" aria-modal="true" aria-labelledby="consultation-title">
      <button class="consultation-modal__close" type="button" aria-label="Закрити" data-consultation-close>×</button>
      <p class="consultation-modal__eyebrow">MOLODIST 360</p>
      <h2 id="consultation-title">Записатись на консультацію</h2>
      <p class="consultation-modal__intro">Залиште контакти — очікуйте повідомлення від адміністратора MOLODIST для погодження часу.<br><span class="consultation-modal__hours">Графік роботи адміністратора: Вт–Сб, 10:00–20:00.</span></p>
      <form class="consultation-form" novalidate>
        <label>Ім’я<input name="name" autocomplete="name" required maxlength="120"></label>
        <label>Телефон<span class="consultation-phone"><span class="consultation-phone__prefix" aria-hidden="true">+380</span><input name="phone" type="tel" autocomplete="tel-national" inputmode="numeric" required minlength="9" maxlength="9" pattern="[0-9]{9}" aria-label="Номер телефону після +380"></span></label>
        <label>Email <span class="consultation-form__optional">необов’язково</span><input name="email" type="email" autocomplete="email" maxlength="160"></label>
        <label>Коментар <span class="consultation-form__optional">необов’язково</span><textarea name="message" rows="3" maxlength="2000" placeholder="Зручний час або ваш запит"></textarea></label>
        <label class="consultation-form__honeypot" aria-hidden="true">Website<input name="website" tabindex="-1" autocomplete="off"></label>
        <label class="consultation-form__consent"><input type="checkbox" required> <span>Погоджуюсь з <a href="/molodist-360/privacy" target="_blank" rel="noopener">політикою конфіденційності</a>.</span></label>
        <p class="consultation-form__status" aria-live="polite"></p>
        <button class="btn btn-primary consultation-form__submit" type="submit">Надіслати заявку</button>
      </form>
    </section>`;
  document.body.appendChild(modal);

  const form = modal.querySelector('form');
  const phone = form.elements.phone;
  const status = modal.querySelector('.consultation-form__status');
  const submit = modal.querySelector('[type="submit"]');
  const close = () => { modal.hidden = true; document.body.classList.remove('consultation-modal-open'); };
  const renderTurnstile = () => {
    if (!window.turnstile || widgetId !== null) return;
    widgetId = window.turnstile.render(modal.querySelector('.consultation-turnstile'), { sitekey: TURNSTILE_SITEKEY, theme: 'light' });
  };
  const loadTurnstile = () => {
    return;                                   // захист від спаму — на сервері CRM (приховане поле, час, ліміт)
    if (window.turnstile) return renderTurnstile();
    if (document.querySelector('script[data-consultation-turnstile]')) return;
    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true;
    script.defer = true;
    script.dataset.consultationTurnstile = 'true';
    script.onload = renderTurnstile;
    document.head.appendChild(script);
  };
  const open = () => {
    modal.hidden = false;
    document.body.classList.add('consultation-modal-open');
    loadTurnstile();
    // Focusing an input immediately makes iOS Safari scroll the dialog past
    // its heading. Keep the complete form visible on phones.
    if (!window.matchMedia('(max-width: 720px)').matches) modal.querySelector('[name="name"]').focus();
  };

  const normalizePhone = value => {
    let digits = String(value || '').replace(/\D/g, '');
    if (digits.startsWith('380')) digits = digits.slice(3);
    else if (digits.startsWith('0')) digits = digits.slice(1);
    return digits.slice(0, 9);
  };
  phone.addEventListener('input', () => { phone.value = normalizePhone(phone.value); });
  phone.addEventListener('paste', event => {
    event.preventDefault();
    phone.value = normalizePhone(event.clipboardData?.getData('text') || '');
    phone.dispatchEvent(new Event('input', { bubbles: true }));
  });

  document.querySelectorAll(selector).forEach(button => button.addEventListener('click', event => { event.preventDefault(); open(); }));
  modal.querySelectorAll('[data-consultation-close]').forEach(button => button.addEventListener('click', close));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !modal.hidden) close(); });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const fields = new FormData(form);
    submit.disabled = true;
    submit.textContent = 'Надсилаємо…';
    status.textContent = '';
    try {
      const response = await fetch(ENDPOINT, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        name: fields.get('name'), phone: `+380${normalizePhone(fields.get('phone'))}`, email: fields.get('email'), comment: fields.get('message'), website: fields.get('website'), page: window.location.href, formname: 'molodist-360:consultation', interest: 'Консультація після тесту «Молодість 360»', t: Math.round((Date.now() - T0) / 1000)
      }) });
      if (!response.ok) throw new Error('request failed');
      form.reset();
      status.textContent = 'Дякуємо! Заявку надіслано. Ми зв’яжемося з вами найближчим часом.';
      status.dataset.state = 'success';
    } catch (_) {
      status.textContent = 'Не вдалося надіслати заявку. Спробуйте ще раз або зателефонуйте нам.';
      status.dataset.state = 'error';
    } finally {
      submit.disabled = false;
      submit.textContent = 'Надіслати заявку';
    }
  });
})();
