// Панель менеджера (/manager). Весь динамический текст вставляется через textContent —
// ничего из ответов сервера не попадает в innerHTML.
(function () {
    'use strict';

    var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    function $(id) { return document.getElementById(id); }

    function readJson(id, fallback) {
        try { return JSON.parse(($(id) || {}).textContent || 'null') || fallback; } catch (e) { return fallback; }
    }
    var INSTALL_STEPS = readJson('installSteps', []);
    var CABINET_HINT = readJson('cabinetHint', '');

    function toast(message, kind) {
        var box = $('toast-container');
        if (!box) return;
        var el = document.createElement('div');
        el.className = 'alert ' + (kind === 'error' ? 'alert-error' : 'alert-success') + ' shadow-lg';
        el.textContent = message;
        box.appendChild(el);
        setTimeout(function () { el.remove(); }, 4500);
    }

    function api(path, body) {
        return fetch('/manager/api' + path, {
            method: body === undefined ? 'GET' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: body === undefined ? undefined : JSON.stringify(body),
            credentials: 'same-origin',
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (response.status === 401) { window.location.href = '/manager/login'; }
                return { ok: response.ok, status: response.status, data: data };
            });
        });
    }

    function nonce() {
        var bytes = new Uint8Array(12);
        (window.crypto || window.msCrypto).getRandomValues(bytes);
        return Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
    }

    function money(value) {
        var n = Number(value);
        var text = Number.isInteger(n) ? String(n) : n.toFixed(2).replace('.', ',');
        return text.replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' ₽';
    }

    function receipt(id) { return 'M-' + String(id).padStart(6, '0'); }

    function loadQr(img, url) {
        if (!img || !url) return;
        img.removeAttribute('src');
        api('/qr', { text: url }).then(function (r) { if (r.ok) img.src = r.data.data_url; });
    }

    function copy(input) {
        input.select();
        if (navigator.clipboard) { navigator.clipboard.writeText(input.value); } else { document.execCommand('copy'); }
        toast('Скопировано');
    }

    function line(parent, text, cls) {
        var p = document.createElement('p');
        if (cls) p.className = cls;
        p.textContent = text;
        parent.appendChild(p);
        return p;
    }

    function button(parent, text, cls, onClick) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = cls;
        b.textContent = text;
        b.addEventListener('click', onClick);
        parent.appendChild(b);
        return b;
    }

    // ------------------------------------------------------------------ «Покажите клиенту»
    // Экран на весь дисплей: большой QR и шаги. Вкладки — когда показать нужно два QR
    // (установка и личный кабинет нового клиента).
    var overlay = $('showClient');
    var lastFocus = null;

    function installView(url, title) {
        return { tab: '📲 Установка', title: title || 'Покажите клиенту этот QR', url: url, steps: INSTALL_STEPS, note: '' };
    }

    function cabinetView(url) {
        return { tab: '🔗 Личный кабинет', title: 'Личный кабинет клиента', url: url, steps: [], note: CABINET_HINT };
    }

    function renderView(view) {
        $('showClientTitle').textContent = view.title;
        loadQr($('showClientQr'), view.url);
        var steps = $('showClientSteps');
        steps.textContent = '';
        view.steps.forEach(function (step, i) {
            var li = document.createElement('li');
            li.className = 'flex gap-3';
            var num = document.createElement('span');
            num.className = 'shrink-0 w-7 h-7 rounded-full bg-blue text-paper font-bold flex items-center justify-center';
            num.textContent = String(i + 1);
            var text = document.createElement('span');
            text.textContent = step;
            li.appendChild(num); li.appendChild(text);
            steps.appendChild(li);
        });
        $('showClientNote').textContent = view.note || '';
    }

    function openShow(views) {
        if (!overlay || !views.length) return;
        var tabs = $('showClientTabs');
        tabs.textContent = '';
        tabs.classList.toggle('hidden', views.length < 2);
        if (views.length > 1) {
            views.forEach(function (view, i) {
                var b = button(tabs, view.tab, 'chip', function () {
                    Array.prototype.forEach.call(tabs.children, function (c) { c.setAttribute('aria-pressed', 'false'); });
                    b.setAttribute('aria-pressed', 'true');
                    renderView(view);
                });
                b.setAttribute('aria-pressed', i === 0 ? 'true' : 'false');
            });
        }
        renderView(views[0]);
        lastFocus = document.activeElement;
        overlay.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        var close = overlay.querySelector('[data-close-show]');
        if (close) close.focus();
    }

    function closeShow() {
        if (!overlay || overlay.classList.contains('hidden')) return;
        overlay.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
        if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    if (overlay) {
        overlay.addEventListener('click', function (event) { if (event.target.closest('[data-close-show]')) closeShow(); });
        document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeShow(); });
    }

    // ------------------------------------------------------------------ обратный отсчёт пробных ключей
    function tickCountdowns() {
        var now = Date.now() / 1000;
        Array.prototype.forEach.call(document.querySelectorAll('[data-countdown]'), function (el) {
            var left = Math.round((parseInt(el.dataset.countdown, 10) - now) / 60);
            el.textContent = left > 0 ? left + ' мин' : 'меньше минуты';
        });
    }
    if (document.querySelector('[data-countdown]')) { tickCountdowns(); setInterval(tickCountdowns, 30000); }

    // ------------------------------------------------------------------ копирование
    document.addEventListener('click', function (event) {
        var btn = event.target.closest('#copyLinkBtn, #copyTempBtn');
        if (!btn) return;
        copy($(btn.id === 'copyLinkBtn' ? 'linkValue' : 'tempLink'));
    });

    // ------------------------------------------------------------------ список клиентов: поиск
    var search = $('clientSearch');
    if (search) {
        search.addEventListener('input', function () {
            var q = search.value.trim().toLowerCase();
            var shown = 0;
            Array.prototype.forEach.call(document.querySelectorAll('#clientList [data-search]'), function (li) {
                var hit = !q || li.dataset.search.indexOf(q) !== -1;
                li.classList.toggle('hidden', !hit);
                if (hit) shown += 1;
            });
            $('clientEmpty').classList.toggle('hidden', shown > 0);
        });
    }

    // ------------------------------------------------------------------ добавить клиента по коду
    var addForm = $('addByCode');
    if (addForm) {
        addForm.addEventListener('submit', function (event) {
            event.preventDefault();
            api('/client-code', { code: $('codeInput').value }).then(function (r) {
                if (r.ok) { window.location.href = '/manager/clients/' + encodeURIComponent(r.data.client_code); }
                else { toast(r.data.message || 'Не удалось добавить клиента', 'error'); }
            });
        });
    }

    // ------------------------------------------------------------------ карточка клиента
    var card = $('clientCard');
    if (card) {
        var code = card.dataset.code;
        var showBtn = $('showLinkBtn');
        if (showBtn) {
            showBtn.addEventListener('click', function () {
                api('/link', { client_code: code }).then(function (r) {
                    if (!r.ok) { toast(r.data.message || 'Ошибка', 'error'); return; }
                    $('linkBox').classList.remove('hidden');
                    $('linkValue').value = r.data.subscription_url;
                    openShow([installView(r.data.subscription_url)]);
                });
            });
        }
        $('labelBtn').addEventListener('click', function () {
            var value = window.prompt('Пометка о клиенте (например «Анна, кофейня»). Пусто — убрать пометку.', card.dataset.label || '');
            if (value === null) return;
            api('/label', { client_code: code, label: value }).then(function (r) {
                if (!r.ok) { toast(r.data.message || 'Ошибка', 'error'); return; }
                card.dataset.label = r.data.label || '';
                $('clientTitle').textContent = r.data.label || 'Без пометки';
                toast('Пометка сохранена');
            });
        });
        $('cabinetResetBtn').addEventListener('click', function () {
            if (!confirm('Выдать клиенту новую ссылку на кабинет? Старая перестанет работать.')) return;
            api('/cabinet-reset', { client_code: code }).then(function (r) {
                if (!r.ok) { toast(r.data.message || 'Ошибка', 'error'); return; }
                $('cabinetBox').classList.remove('hidden');
                $('cabinetValue').value = r.data.cabinet_url;
                openShow([cabinetView(r.data.cabinet_url)]);
            });
        });
        $('cabinetShowBtn').addEventListener('click', function () { openShow([cabinetView($('cabinetValue').value)]); });
        $('noAutoBtn').addEventListener('click', function () {
            if (!confirm('Выключить автопродление у клиента?')) return;
            api('/noauto', { client_code: code }).then(function (r) {
                toast(r.ok ? (r.data.disabled ? 'Автопродление выключено' : 'У клиента оно не включено') : (r.data.message || 'Ошибка'),
                      r.ok ? 'ok' : 'error');
            });
        });
    }

    // ------------------------------------------------------------------ пробный ключ
    var tempBtn = $('issueTempBtn');
    if (tempBtn) {
        var tempNonce = nonce();
        tempBtn.addEventListener('click', function () {
            tempBtn.classList.add('btn-loading');
            api('/temp', { nonce: tempNonce }).then(function (r) {
                tempBtn.classList.remove('btn-loading');
                if (!r.ok) { toast(r.data.message || 'Не удалось выдать ключ', 'error'); return; }
                $('tempResult').classList.remove('hidden');
                $('tempOp').textContent = 'чек № ' + receipt(r.data.operation_id);
                $('tempUntil').textContent = r.data.expires_at;
                $('tempLink').value = r.data.subscription_url;
                $('tempConvert').href = '/manager/issue?temp=' + encodeURIComponent(r.data.key_id);
                var view = installView(r.data.subscription_url, 'Пробный ключ: покажите клиенту QR');
                $('tempShowBtn').onclick = function () { openShow([view]); };
                openShow([view]);
                tempNonce = nonce(); // следующий тап — уже новый ключ
                tempBtn.disabled = true;
            });
        });
    }

    // ------------------------------------------------------------------ продажа
    var app = $('issueApp');
    if (!app) return;

    var tempKeyId = parseInt(app.dataset.tempKey || '0', 10);
    var maxDays = parseInt(app.dataset.maxDays || '365', 10);
    var state = { clientCode: null, quote: null, nonce: nonce(), busy: false };

    function selected(name) {
        var el = document.querySelector('input[name="' + name + '"]:checked');
        return el ? el.value : null;
    }

    function plan() {
        var value = selected('plan');
        if (!value) return null;
        if (value === 'custom') return { product: 'custom' };
        return { product: 'tariff', tariffId: parseInt(value.slice(1), 10) };
    }

    function who() { return tempKeyId ? 'new' : (selected('who') || 'new'); }

    function currentClient() {
        if (tempKeyId) return null;
        if (who() === 'mine') { var sel = $('clientSelect'); return sel && sel.value ? sel.value : null; }
        if (who() === 'code') return state.clientCode;
        return null;
    }

    function clientReady() {
        if (tempKeyId || who() === 'new') return true;
        return !!currentClient();
    }

    function label() {
        var input = $('labelInput');
        return input && who() === 'new' ? input.value.trim() : '';
    }

    function request() {
        var p = plan();
        var body = { product: p.product, client_code: currentClient() };
        if (p.product === 'tariff') { body.tariff_id = p.tariffId; }
        else { body.days = parseInt($('daysInput').value, 10); }
        return body;
    }

    var timer = null;
    function refreshSoon() { clearTimeout(timer); timer = setTimeout(refresh, 250); }

    function renderPreview(q) {
        var body = $('previewBody');
        body.textContent = '';
        body.className = 'mt-2 text-sm';
        line(body, q.tariff_name + ' · ' + q.days + ' дн.', 'font-bold');
        line(body, (q.quota_gb === 0 ? 'Безлимитный трафик' : (q.quota_gb + q.extra_traffic_gb) + ' ГБ/мес') +
                   ' · до ' + q.devices_limit + ' устройств');
        if (q.breakdown) line(body, q.breakdown, 'text-muted italic');
        if (q.slots) line(body, '+ доп. устройства: ' + q.slots + ' шт. — ' + money(q.slots_cost));
        if (q.packs) line(body, '+ доп. трафик: +' + q.extra_traffic_gb + ' ГБ — ' + money(q.traffic_cost));
        line(body, 'К оплате: ' + money(q.total), 'font-display font-black text-3xl mt-2');
        if (q.hint) {
            line(body, '💡 Выгоднее стандартный тариф «' + q.hint.name + '» — ' + q.hint.days + ' дн. за ' +
                       money(q.hint.price) + ' (на ' + money(q.hint.saving) + ' дешевле) и с автопродлением.',
                 'mt-2 text-orange font-semibold');
        }
        if (q.renew_text) line(body, '♻️ ' + q.renew_text, 'mt-2 text-muted');
        $('previewActions').classList.remove('hidden');
    }

    function refresh() {
        state.quote = null;
        $('previewActions').classList.add('hidden');
        var p = plan();
        $('productCustom').classList.toggle('hidden', !p || p.product !== 'custom');
        if (!clientReady()) {
            $('previewBody').textContent = who() === 'code' ? 'Сначала подтвердите код клиента.' : 'Выберите клиента.';
            return;
        }
        if (!p) { $('previewBody').textContent = 'Выберите тариф — цена появится здесь.'; return; }
        if (p.product === 'custom') {
            var d = parseInt($('daysInput').value, 10);
            Array.prototype.forEach.call(document.querySelectorAll('[data-days]'), function (chip) {
                chip.setAttribute('aria-pressed', String(parseInt(chip.dataset.days, 10) === d));
            });
            if (!d || d < 1 || d > maxDays) { $('previewBody').textContent = 'Введите число дней от 1 до ' + maxDays + '.'; return; }
        }
        api('/quote', request()).then(function (r) {
            if (!r.ok) { $('previewBody').textContent = '❌ ' + (r.data.message || 'Не удалось посчитать'); return; }
            state.quote = r.data;
            state.nonce = nonce(); // новая цена — новое подтверждение
            renderPreview(r.data);
        });
    }

    function lockForm() {
        Array.prototype.forEach.call(app.querySelectorAll('section:not(#resultBox)'), function (s) { s.classList.add('hidden'); });
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function showDone(box, data, installUrl) {
        var views = [installView(installUrl)];
        if (data.cabinet_url) views.push(cabinetView(data.cabinet_url));
        var actions = document.createElement('div');
        actions.className = 'mt-4 grid gap-2 sm:flex sm:flex-wrap';
        box.appendChild(actions);
        button(actions, '📲 Показать клиенту QR', 'btn-pop w-full sm:w-auto', function () { openShow(views); });
        var cardLink = document.createElement('a');
        cardLink.className = 'btn-pop btn-pop-outline w-full sm:w-auto';
        cardLink.href = '/manager/clients/' + encodeURIComponent(data.client_code);
        cardLink.textContent = '👤 Карточка клиента';
        actions.appendChild(cardLink);
        var again = document.createElement('a');
        again.className = 'btn-pop btn-pop-outline w-full sm:w-auto';
        again.href = '/manager/'; again.textContent = '⚡ Новая продажа';
        actions.appendChild(again);
        if (data.cabinet_url) {
            line(box, '🔗 У нового клиента есть личный кабинет — в «Показать клиенту» вкладка «Личный кабинет». ' +
                      'Ссылка показывается один раз.', 'mt-3 text-sm text-subtle');
        }
        openShow(views);
    }

    function showResult(data, method) {
        var box = $('resultBox');
        box.classList.remove('hidden');
        box.textContent = '';
        lockForm();

        if (data.replayed) {
            line(box, 'ℹ️ Эта продажа уже оформлена (чек № ' + receipt(data.operation_id) + '). Проверьте историю.');
            return;
        }

        if (data.status === 'completed') {
            line(box, '✅ Готово · чек № ' + receipt(data.operation_id), 'font-display font-extrabold text-xl');
            line(box, money(data.price) + (method === 'cash' ? ' наличными' : '') +
                      (data.expires_at ? ' · подписка до ' + data.expires_at + ' МСК' : ''), 'text-sm text-subtle');
            showDone(box, data, data.subscription_url);
            return;
        }

        line(box, '💳 Счёт на ' + money(data.price) + ' · чек № ' + receipt(data.operation_id), 'font-display font-extrabold text-xl');
        line(box, 'Разверните телефон к клиенту: он наводит камеру на QR и платит картой или через СБП.', 'mt-1 text-sm');
        var img = document.createElement('img');
        img.className = 'mt-3 w-64 h-64 max-w-full rounded-2xl border-2 border-blue/20 bg-white'; img.alt = 'QR для оплаты';
        box.appendChild(img);
        loadQr(img, data.payment_url);
        var link = document.createElement('a');
        link.href = data.payment_url; link.target = '_blank'; link.rel = 'noopener';
        link.className = 'block mt-2 text-sm font-bold text-blue break-all'; link.textContent = 'Открыть ссылку на оплату';
        box.appendChild(link);
        var status = line(box, '⏳ Ждём оплату… страница обновится сама.', 'mt-4 font-bold');
        var cancelBtn = button(box, 'Отменить счёт', 'btn-pop btn-pop-outline !min-h-0 !py-2 mt-2', function () {
            if (!confirm('Отменить счёт? Клиент не сможет по нему заплатить.')) return;
            api('/op/' + data.operation_id + '/cancel', {}).then(function (r) {
                status.textContent = r.ok && r.data.cancelled ? '🚫 Счёт отменён. Не оплачивайте старую ссылку.' : 'Счёт уже оплачен или отменён.';
                clearInterval(poll);
                cancelBtn.remove();
            });
        });
        var poll = setInterval(function () {
            api('/op/' + data.operation_id).then(function (r) {
                if (!r.ok) return;
                if (r.data.status === 'completed') {
                    clearInterval(poll);
                    cancelBtn.remove();
                    img.remove(); link.remove();
                    status.textContent = '✅ Оплачено! Подписка выдана.';
                    status.className = 'mt-4 font-display font-extrabold text-xl text-success';
                    api('/link', { client_code: r.data.client_code || data.client_code }).then(function (l) {
                        if (l.ok) { showDone(box, data, l.data.subscription_url); }
                        else { line(box, 'QR для установки — в карточке клиента.', 'mt-2 text-sm'); }
                    });
                } else if (r.data.status === 'cancelled' || r.data.status === 'failed') {
                    clearInterval(poll);
                    cancelBtn.remove();
                    status.textContent = '🚫 Счёт отменён или не оплачен. Если клиент всё ещё хочет купить — начните продажу заново.';
                }
            });
        }, 3000);
    }

    function submit(method, btn) {
        if (state.busy || !state.quote) return;
        if (method === 'cash' && !confirm('Подтвердите: вы получили от клиента ' + money(state.quote.total) + ' наличными?')) return;
        state.busy = true;
        btn.classList.add('btn-loading');
        var body = request();
        body.method = method;
        body.nonce = state.nonce;
        body.expected_total = state.quote.total;
        if (label()) body.label = label();
        if (tempKeyId) { body.temp_key_id = tempKeyId; body.client_code = null; }
        api('/issue', body).then(function (r) {
            state.busy = false;
            btn.classList.remove('btn-loading');
            if (!r.ok) {
                toast(r.data.message || 'Не удалось оформить', 'error');
                if (r.data.error === 'price_changed') refresh();
                return;
            }
            showResult(r.data, method);
        });
    }

    // события
    Array.prototype.forEach.call(document.querySelectorAll('input[name="who"]'), function (el) {
        el.addEventListener('change', function () {
            $('whoNew').classList.toggle('hidden', who() !== 'new');
            $('whoMine').classList.toggle('hidden', who() !== 'mine');
            $('whoCode').classList.toggle('hidden', who() !== 'code');
            state.clientCode = null;
            refresh();
        });
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="plan"]'), function (el) {
        el.addEventListener('change', refresh);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-days]'), function (chip) {
        chip.addEventListener('click', function () { $('daysInput').value = chip.dataset.days; refresh(); });
    });
    if ($('daysInput')) $('daysInput').addEventListener('input', refreshSoon);
    if ($('clientSelect')) $('clientSelect').addEventListener('change', refresh);
    if ($('useCodeBtn')) {
        $('useCodeBtn').addEventListener('click', function () {
            api('/client-code', { code: $('accessCode').value }).then(function (r) {
                if (!r.ok) { toast(r.data.message || 'Неверный код', 'error'); return; }
                state.clientCode = r.data.client_code;
                $('accessCode').value = '';
                toast('Клиент ' + r.data.client_code + ' добавлен');
                refresh();
            });
        });
    }
    $('payOnlineBtn').addEventListener('click', function () { submit('online', this); });
    if ($('payCashBtn')) $('payCashBtn').addEventListener('click', function () { submit('cash', this); });

    refresh();
})();
