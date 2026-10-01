// Панель менеджера (/manager). Весь динамический текст вставляется через textContent —
// ничего из ответов сервера не попадает в innerHTML.
(function () {
    'use strict';

    var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    function $(id) { return document.getElementById(id); }

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
        return text.replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' ₽';
    }

    function loadQr(img, url) {
        if (!img || !url) return;
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

    // ------------------------------------------------------------------ копирование
    document.addEventListener('click', function (event) {
        var btn = event.target.closest('#copyLinkBtn, #copyTempBtn');
        if (!btn) return;
        copy($(btn.id === 'copyLinkBtn' ? 'linkValue' : 'tempLink'));
    });

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
                    loadQr($('linkQr'), r.data.subscription_url);
                });
            });
        }
        $('cabinetResetBtn').addEventListener('click', function () {
            if (!confirm('Перевыпустить ссылку кабинета? Старая перестанет работать.')) return;
            api('/cabinet-reset', { client_code: code }).then(function (r) {
                if (!r.ok) { toast(r.data.message || 'Ошибка', 'error'); return; }
                $('cabinetBox').classList.remove('hidden');
                $('cabinetValue').value = r.data.cabinet_url;
            });
        });
        $('noAutoBtn').addEventListener('click', function () {
            if (!confirm('Выключить автопродление у клиента?')) return;
            api('/noauto', { client_code: code }).then(function (r) {
                toast(r.ok ? (r.data.disabled ? 'Автопродление выключено' : 'У клиента оно не включено') : (r.data.message || 'Ошибка'),
                      r.ok ? 'ok' : 'error');
            });
        });
    }

    // ------------------------------------------------------------------ временный ключ
    var tempBtn = $('issueTempBtn');
    if (tempBtn) {
        var tempNonce = nonce();
        tempBtn.addEventListener('click', function () {
            tempBtn.classList.add('btn-loading');
            api('/temp', { nonce: tempNonce }).then(function (r) {
                tempBtn.classList.remove('btn-loading');
                if (!r.ok) { toast(r.data.message || 'Не удалось выдать ключ', 'error'); return; }
                $('tempResult').classList.remove('hidden');
                $('tempOp').textContent = 'чек № M-' + String(r.data.operation_id).padStart(6, '0');
                $('tempUntil').textContent = r.data.expires_at;
                $('tempLink').value = r.data.subscription_url;
                $('tempConvert').href = '/manager/issue?temp=' + encodeURIComponent(r.data.key_id);
                loadQr($('tempQr'), r.data.subscription_url);
                tempNonce = nonce(); // следующий тап — уже новый ключ
                tempBtn.disabled = true;
            });
        });
    }

    // ------------------------------------------------------------------ выдача
    var app = $('issueApp');
    if (!app) return;

    var tempKeyId = parseInt(app.dataset.tempKey || '0', 10);
    var maxDays = parseInt(app.dataset.maxDays || '365', 10);
    var state = { clientCode: null, quote: null, nonce: nonce(), busy: false };

    function selected(name) {
        var el = document.querySelector('input[name="' + name + '"]:checked');
        return el ? el.value : null;
    }

    function currentClient() {
        if (tempKeyId) return null;
        var who = selected('who');
        if (who === 'mine') { var sel = $('clientSelect'); return sel && sel.value ? sel.value : null; }
        if (who === 'code') return state.clientCode;
        return null;
    }

    function clientReady() {
        if (tempKeyId) return true;
        var who = selected('who');
        if (who === 'code') return !!state.clientCode;
        if (who === 'mine') return !!currentClient();
        return true;
    }

    function request() {
        var product = selected('product');
        var body = { product: product, client_code: currentClient() };
        if (product === 'tariff') { body.tariff_id = parseInt($('tariffSelect').value, 10); }
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
        line(body, 'К оплате: ' + money(q.total), 'font-display font-black text-2xl mt-2');
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
        if (!clientReady()) {
            $('previewBody').textContent = 'Сначала подтвердите код клиента.';
            return;
        }
        var product = selected('product');
        $('productTariff').classList.toggle('hidden', product !== 'tariff');
        $('productCustom').classList.toggle('hidden', product !== 'custom');
        if (product === 'custom') {
            var d = parseInt($('daysInput').value, 10);
            if (!d || d < 1 || d > maxDays) { $('previewBody').textContent = 'Введите число дней от 1 до ' + maxDays + '.'; return; }
        }
        api('/quote', request()).then(function (r) {
            if (!r.ok) { $('previewBody').textContent = '❌ ' + (r.data.message || 'Не удалось посчитать'); return; }
            state.quote = r.data;
            state.nonce = nonce(); // новая цена — новое подтверждение
            renderPreview(r.data);
        });
    }

    function showResult(data, method) {
        var box = $('resultBox');
        box.classList.remove('hidden');
        box.textContent = '';
        $('previewBox').classList.add('hidden');

        if (data.replayed) {
            line(box, 'ℹ️ Эта операция уже оформлена (чек № M-' + String(data.operation_id).padStart(6, '0') + '). Проверьте историю.');
            return;
        }
        var head = data.status === 'completed' ? '✅ Ключ выдан' : '💳 Счёт выставлен';
        line(box, head + ' · чек № M-' + String(data.operation_id).padStart(6, '0'), 'font-display font-extrabold text-lg');
        line(box, 'Клиент ' + data.client_code + ' · ' + money(data.price) + (method === 'cash' ? ' · наличные' : ''), 'text-sm text-subtle');

        function linkField(label, url, withQr, qrAlt) {
            line(box, label, 'mt-3 text-sm font-bold');
            var wrap = document.createElement('div');
            wrap.className = 'copy-field mt-1';
            var input = document.createElement('input');
            input.type = 'text'; input.readOnly = true; input.value = url;
            var btn = document.createElement('button');
            btn.className = 'copy-btn'; btn.type = 'button'; btn.textContent = '⧉'; btn.setAttribute('aria-label', 'Скопировать');
            btn.addEventListener('click', function () { copy(input); });
            wrap.appendChild(input); wrap.appendChild(btn);
            box.appendChild(wrap);
            if (withQr) {
                var img = document.createElement('img');
                img.className = 'mt-3 w-48 h-48 rounded-xl border-2 border-blue/20'; img.alt = qrAlt;
                box.appendChild(img);
                loadQr(img, url);
            }
        }

        if (data.status === 'completed') {
            linkField('Ссылка для установки', data.subscription_url, true, 'QR для установки');
            if (data.expires_at) line(box, 'Подписка действует до ' + data.expires_at + ' МСК', 'mt-2 text-sm');
        } else {
            linkField('Ссылка на оплату — клиент сканирует QR или открывает ссылку', data.payment_url, true, 'QR для оплаты');
            var status = line(box, '⏳ Ждём оплату…', 'mt-3 font-bold');
            var cancelBtn = document.createElement('button');
            cancelBtn.className = 'btn-pop btn-pop-outline !min-h-0 !py-2 mt-2'; cancelBtn.type = 'button';
            cancelBtn.textContent = 'Отменить счёт';
            cancelBtn.addEventListener('click', function () {
                api('/op/' + data.operation_id + '/cancel', {}).then(function (r) {
                    status.textContent = r.ok && r.data.cancelled ? '🚫 Счёт отменён. Не оплачивайте старую ссылку.' : 'Счёт уже оплачен или отменён.';
                    clearInterval(poll);
                });
            });
            box.appendChild(cancelBtn);
            var poll = setInterval(function () {
                api('/op/' + data.operation_id).then(function (r) {
                    if (!r.ok) return;
                    if (r.data.status === 'completed') {
                        clearInterval(poll);
                        status.textContent = '✅ Оплата получена — ключ выдан. Чек придёт в бота; ссылка — в разделе «Клиенты».';
                        cancelBtn.remove();
                    } else if (r.data.status === 'cancelled' || r.data.status === 'failed') {
                        clearInterval(poll);
                        status.textContent = '🚫 Счёт отменён или не оплачен.';
                    }
                });
            }, 3000);
        }
        if (data.cabinet_url) {
            linkField('Кабинет клиента — покажите один раз, потом ссылку не восстановить', data.cabinet_url, false);
        }
        var again = document.createElement('a');
        again.className = 'btn-pop btn-pop-outline mt-4'; again.href = '/manager/issue'; again.textContent = 'Выдать ещё';
        box.appendChild(again);
    }

    function submit(method, button) {
        if (state.busy || !state.quote) return;
        state.busy = true;
        button.classList.add('btn-loading');
        var body = request();
        body.method = method;
        body.nonce = state.nonce;
        body.expected_total = state.quote.total;
        if (tempKeyId) { body.temp_key_id = tempKeyId; body.client_code = null; }
        api('/issue', body).then(function (r) {
            state.busy = false;
            button.classList.remove('btn-loading');
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
            var who = selected('who');
            $('whoMine').classList.toggle('hidden', who !== 'mine');
            $('whoCode').classList.toggle('hidden', who !== 'code');
            state.clientCode = null;
            refresh();
        });
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="product"]'), function (el) {
        el.addEventListener('change', refresh);
    });
    if ($('tariffSelect')) $('tariffSelect').addEventListener('change', refresh);
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
