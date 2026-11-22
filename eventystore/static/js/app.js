/* EventyStore — JavaScript d'amélioration progressive (le site reste utilisable sans JS pour les parcours principaux). */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var csrf = ($('meta[name=csrf-token]') || {}).content || '';
  var fmt = function (n) { return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' FCFA'; };
  var esc = function (s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
  function api(url, opts) {
    opts = opts || {}; opts.headers = Object.assign({ 'X-CSRFToken': csrf, 'X-Requested-With': 'fetch' }, opts.headers || {});
    opts.credentials = 'same-origin';
    return fetch(url, opts).then(function (r) { return r.json(); });
  }

  // Menu mobile + menus déroulants
  var mt = $('.menu-toggle'), mn = $('.mobile-nav');
  if (mt && mn) mt.addEventListener('click', function () { var o = mn.classList.toggle('open'); mt.setAttribute('aria-expanded', o); });
  $$('.dropdown > [data-dropdown]').forEach(function (b) {
    b.addEventListener('click', function (e) { e.stopPropagation(); var d = b.parentNode, o = !d.classList.contains('open'); $$('.dropdown.open').forEach(function (x) { x.classList.remove('open'); }); d.classList.toggle('open', o); b.setAttribute('aria-expanded', o); if (o && b.dataset.dropdown === 'notifs') loadNotifs(); });
  });
  document.addEventListener('click', function () { $$('.dropdown.open').forEach(function (x) { x.classList.remove('open'); }); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') $$('.dropdown.open').forEach(function (x) { x.classList.remove('open'); }); });

  function loadNotifs() {
    var box = $('#notif-list'); if (!box) return;
    api('/api/v1/notifications').then(function (d) {
      if (!d.items) return;
      box.innerHTML = d.items.length ? d.items.map(function (n) { return '<a class="notif-item ' + (n.read ? '' : 'unread') + '" href="' + esc(n.url) + '"><strong>' + esc(n.title) + '</strong><br><span class="muted">' + esc(n.body || '') + '</span></a>'; }).join('') : '<p class="muted small" style="padding:.6rem">Aucune notification.</p>';
    });
  }
  if ($('#badge-notif')) setInterval(function () {
    if (document.hidden) return;
    api('/api/v1/me/unread').then(function (d) { [['badge-notif', d.notifications], ['badge-msg', d.messages]].forEach(function (p) { var el = document.getElementById(p[0]); if (el) { el.textContent = p[1]; el.hidden = !p[1]; } }); });
  }, 45000);

  // Confirmations
  $$('form[data-confirm]').forEach(function (f) { f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); }); });
  // Évite les doubles envois
  $$('form').forEach(function (f) { f.addEventListener('submit', function () { $$('button[type=submit]:not([data-multi])', f).forEach(function (b) { setTimeout(function () { b.disabled = true; }, 0); }); }); });

  // Autocomplétion de recherche
  $$('[data-suggest]').forEach(function (inp) {
    var box = inp.closest('.pos-rel').querySelector('.suggest'), t;
    inp.setAttribute('autocomplete', 'off');
    inp.addEventListener('input', function () {
      clearTimeout(t); var v = inp.value.trim();
      if (v.length < 2) { box.classList.remove('open'); return; }
      t = setTimeout(function () {
        api('/api/v1/suggest?q=' + encodeURIComponent(v)).then(function (d) {
          var s = d.suggestions || []; if (!s.length) { box.classList.remove('open'); return; }
          box.innerHTML = s.map(function (x) { return x.url ? '<a href="' + esc(x.url) + '"><small class="muted">' + ({ category: 'Catégorie', vendor: 'Prestataire', offer: 'Offre' }[x.type] || '') + '</small> ' + esc(x.label) + '</a>' : '<button type="button" data-place="' + esc(x.value) + '"><small class="muted">Lieu</small> ' + esc(x.label) + '</button>'; }).join('');
          box.classList.add('open');
        });
      }, 200);
    });
    box.addEventListener('click', function (e) { var b = e.target.closest('[data-place]'); if (b) { var p = $('[name=place]', inp.form); if (p) p.value = b.dataset.place; inp.value = ''; box.classList.remove('open'); } });
    document.addEventListener('click', function (e) { if (!box.contains(e.target) && e.target !== inp) box.classList.remove('open'); });
  });

  // Dates : fin = début par défaut
  $$('input[name=start],input[name=start_date]').forEach(function (s) {
    var e = $('input[name=' + (s.name === 'start' ? 'end' : 'end_date') + ']', s.form); if (!e) return;
    s.addEventListener('change', function () { if (!e.value || e.value < s.value) e.value = s.value; e.min = s.value; });
  });
  // Préremplissage depuis un événement
  $$('select[data-event-select]').forEach(function (sel) {
    sel.addEventListener('change', function () {
      var o = sel.options[sel.selectedIndex], f = sel.form;
      [['start', 'start'], ['end', 'end'], ['start_date', 'start'], ['end_date', 'end'], ['guests', 'guests'], ['location', 'location'], ['quantity', 'guests']].forEach(function (p) {
        var el = $('[name=' + p[0] + ']', f); if (el && o.dataset[p[1]] && (p[0] !== 'quantity' || el.dataset.fillGuests)) { el.value = o.dataset[p[1]]; el.dispatchEvent(new Event('change', { bubbles: true })); }
      });
    });
  });

  // Galerie
  $$('.thumbs button').forEach(function (b) { b.addEventListener('click', function () { var m = $('.gallery .main img'); m.src = b.dataset.src; m.alt = b.dataset.alt || ''; $$('.thumbs button').forEach(function (x) { x.classList.remove('on'); }); b.classList.add('on'); }); });

  // Calendrier de disponibilité public
  var calEl = $('[data-offer-calendar]');
  if (calEl) {
    var oid = calEl.dataset.offerCalendar, now = new Date(), y = now.getFullYear(), m = now.getMonth() + 1;
    var head = $('.cal-title', calEl), grid = $('.cal-grid', calEl);
    var names = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre'];
    var draw = function () {
      api('/api/v1/offers/' + oid + '/calendar?year=' + y + '&month=' + m).then(function (d) {
        head.textContent = names[m - 1] + ' ' + y;
        var first = (new Date(y, m - 1, 1).getDay() + 6) % 7, days = new Date(y, m, 0).getDate(), h = ['L', 'M', 'M', 'J', 'V', 'S', 'D'].map(function (x) { return '<div class="dow">' + x + '</div>'; }).join('');
        for (var i = 0; i < first; i++) h += '<div></div>';
        for (var k = 1; k <= days; k++) { var key = y + '-' + String(m).padStart(2, '0') + '-' + String(k).padStart(2, '0'), st = d.days[key] || 'past', lbl = { free: 'disponible', busy: 'complet', blocked: 'indisponible', partial: 'partiellement réservé', past: 'non réservable' }[st]; h += '<button type="button" class="d ' + st + '" data-date="' + key + '" title="' + k + ' : ' + lbl + '" aria-label="' + k + ' ' + names[m - 1] + ' : ' + lbl + '"' + (st === 'free' || st === 'partial' ? '' : ' disabled') + ' style="border:0;font:inherit;cursor:' + (st === 'free' || st === 'partial' ? 'pointer' : 'default') + '">' + k + '</button>'; }
        grid.innerHTML = h;
      });
    };
    $('.cal-prev', calEl).addEventListener('click', function () { m--; if (m < 1) { m = 12; y--; } draw(); });
    $('.cal-next', calEl).addEventListener('click', function () { m++; if (m > 12) { m = 1; y++; } draw(); });
    grid.addEventListener('click', function (e) { var b = e.target.closest('[data-date]'); if (!b || b.disabled) return; var f = $('form[data-estimator]'); if (!f) return; var s = $('[name=start]', f), en = $('[name=end]', f); s.value = b.dataset.date; if (!en.value || en.value < s.value) en.value = s.value; s.dispatchEvent(new Event('change', { bubbles: true })); });
    draw();
  }

  // Estimation de prix (calcul serveur)
  var est = $('form[data-estimator]');
  if (est) {
    var out = $('#estimate'), tm;
    var run = function () {
      var fd = new FormData(est), p = new URLSearchParams();
      ['start', 'end', 'quantity', 'guests', 'event_id', 'promo'].forEach(function (k) { if (fd.get(k)) p.append(k, fd.get(k)); });
      fd.getAll('options').forEach(function (o) { p.append('options', o); });
      if (!fd.get('start')) { out.innerHTML = '<span class="muted">Choisissez une date pour voir le détail du prix et la disponibilité.</span>'; return; }
      api('/api/v1/offers/' + est.dataset.estimator + '/price?' + p.toString()).then(function (d) {
        if (d.error) { out.textContent = ''; return; }
        var h = d.lines.map(function (l) { return '<div class="line"><span>' + esc(l.label) + (l.detail ? ' <small class="muted">(' + esc(l.detail) + ')</small>' : '') + '</span><span>' + fmt(l.total_xof) + '</span></div>'; }).join('');
        if (d.discount_xof) h += '<div class="line diff-added"><span>Remise</span><span>-' + fmt(d.discount_xof) + '</span></div>';
        h += '<div class="line total"><span>Total</span><span>' + fmt(d.total_xof) + '</span></div>';
        if (d.deposit_xof) h += '<div class="line small muted"><span>Acompte (' + d.deposit_percent + ' %)</span><span>' + fmt(d.deposit_xof) + '</span></div>';
        if (d.caution_xof) h += '<div class="line small muted"><span>Caution (restituée)</span><span>' + fmt(d.caution_xof) + '</span></div>';
        if (d.promo_error) h += '<div class="small" style="color:var(--err)">' + esc(d.promo_error) + '</div>';
        var cls = d.availability.status;
        h += '<div class="avail avail-' + cls + '" style="margin-top:.5rem">' + ({ available: '✔ ', unavailable: '✖ ', to_confirm: '⚠ ' }[cls] || '') + esc(d.availability.label) + (d.availability.reason ? ' — ' + esc(d.availability.reason) : '') + '</div>';
        if (d.alternatives && d.alternatives.length) {
          h += '<div class="small" style="margin-top:.5rem">Dates proches disponibles :</div><div class="chips" style="margin-top:.3rem">' + d.alternatives.map(function (x) { return '<button type="button" class="badge badge-info" data-alt-date="' + x + '" style="cursor:pointer;border:0">' + x.split('-').reverse().join('/') + '</button>'; }).join('') + '</div>';
        }
        out.innerHTML = h;
        var sb = $('[data-submit-book]', est); if (sb) sb.disabled = cls !== 'available';
      });
    };
    out.addEventListener('click', function (e) {
      var b = e.target.closest('[data-alt-date]'); if (!b) return;
      var s = $('[name=start]', est), en = $('[name=end]', est); s.value = b.dataset.altDate; en.value = b.dataset.altDate;
      s.dispatchEvent(new Event('change', { bubbles: true }));
    });
    est.addEventListener('input', function () { clearTimeout(tm); tm = setTimeout(run, 250); });
    est.addEventListener('change', function () { clearTimeout(tm); tm = setTimeout(run, 150); });
    run();
  }

  // Constructeur de lignes de devis
  $$('[data-lines]').forEach(function (box) {
    var body = $('tbody', box), tot = $('[data-total]', box.closest('form'));
    var calc = function () {
      var sum = 0, fees = 0;
      $$('tr', body).forEach(function (tr) {
        var q = parseFloat(($('[name=line_qty]', tr).value || '1').replace(',', '.')) || 0, p = parseFloat($('[name=line_price]', tr).value) || 0, d = parseFloat($('[name=line_discount]', tr).value) || 0, k = $('[name=line_kind]', tr).value;
        var t = Math.max(0, Math.round(q * p) - d); $('.lt', tr).textContent = fmt(t);
        if (k !== 'caution') sum += t;
      });
      if (tot) tot.textContent = fmt(sum);
      var dep = $('[name=deposit_percent]', box.closest('form')), dv = $('[data-deposit]', box.closest('form')); if (dep && dv) dv.textContent = fmt(sum * (parseFloat(dep.value) || 0) / 100);
    };
    box.addEventListener('input', calc);
    box.closest('form').addEventListener('input', function (e) { if (e.target.name === 'deposit_percent') calc(); });
    var add = $('[data-add-line]', box);
    if (add) add.addEventListener('click', function () { var tpl = $('template', box).content.cloneNode(true); body.appendChild(tpl); calc(); });
    box.addEventListener('click', function (e) { var b = e.target.closest('[data-del-line]'); if (b && $$('tr', body).length > 1) { b.closest('tr').remove(); calc(); } });
    calc();
  });
  // Options d'offre (formulaire prestataire)
  var oa = $('[data-add-option]');
  if (oa) { oa.addEventListener('click', function () { $('#opt-rows').appendChild($('#opt-tpl').content.cloneNode(true)); renumber(); }); document.addEventListener('click', function (e) { var b = e.target.closest('[data-del-option]'); if (b) { b.closest('.opt-row').remove(); renumber(); } }); }
  function renumber() { $$('#opt-rows .opt-row').forEach(function (r, i) { var c = $('[name=opt_required]', r); if (c) c.value = i; }); }

  // Messagerie : rafraîchissement
  var chat = $('[data-chat]');
  if (chat) {
    var last = parseInt(chat.dataset.last || '0', 10), id = chat.dataset.chat;
    chat.scrollTop = chat.scrollHeight;
    setInterval(function () {
      if (document.hidden) return;
      api('/messages/' + id + '/poll?after=' + last).then(function (d) {
        (d.messages || []).forEach(function (m) {
          last = Math.max(last, m.id);
          if (document.querySelector('[data-mid="' + m.id + '"]')) return;
          var el = document.createElement('div'); el.className = 'msg' + (m.mine ? ' mine' : ''); el.dataset.mid = m.id;
          el.innerHTML = esc(m.body) + (m.attachment ? '<div><a href="' + esc(m.att_url) + '">📎 ' + esc(m.attachment) + '</a></div>' : '') + '<div class="meta">' + esc(m.author) + ' · ' + esc(m.at) + '</div>';
          chat.appendChild(el); chat.scrollTop = chat.scrollHeight;
        });
      });
    }, 8000);
  }
  $$('[data-template]').forEach(function (b) { b.addEventListener('click', function () { var t = $('textarea[name=body]'); t.value = b.dataset.template; t.focus(); }); });
  // Étoiles de notation
  $$('.rate input').forEach(function (i) { i.addEventListener('change', function () { var g = i.closest('.rate'); $$('label', g).forEach(function (l) { l.classList.toggle('on', parseInt(l.dataset.v, 10) <= parseInt(i.value, 10)); }); }); });
  // Comptes de démonstration : préremplit le formulaire de connexion
  $$('[data-fill]').forEach(function (b) {
    b.addEventListener('click', function () {
      var f = b.closest('.card').querySelector('form');
      $('[name=identifier]', f).value = b.dataset.fill;
      $('[name=password]', f).value = 'Demo1234';
      $('[name=password]', f).focus();
    });
  });
  // Filtres de recherche : repliés sur mobile
  var fm = $('details.filters-m');
  if (fm && window.innerWidth < 960) fm.removeAttribute('open');
  // Impression
  $$('[data-print]').forEach(function (b) { b.addEventListener('click', function () { window.print(); }); });
  // Afficher/masquer champ société
  var acct = $('select[name=account_type]');
  if (acct) { var co = $('[data-company]'); var t = function () { co.hidden = acct.value !== 'company'; }; acct.addEventListener('change', t); t(); }
})();
