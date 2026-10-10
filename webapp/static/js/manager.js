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

    // SVG-иконка из спрайта _icons.html (<symbol id="i-…">) — тот же вид, что в шаблонах.
    var SVG_NS = 'http://www.w3.org/2000/svg';
    function svgIcon(name, cls) {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('class', cls || 'w-5 h-5 shrink-0');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        var use = document.createElementNS(SVG_NS, 'use');
        use.setAttribute('href', '#i-' + name);
        svg.appendChild(use);
        return svg;
    }

    // Содержимое элемента: [иконка] текст. Текст — только через textContent.
    function fill(el, text, icon, iconCls) {
        el.textContent = '';
        if (icon) {
            el.classList.add('flex', 'items-center', 'gap-2');
            el.appendChild(svgIcon(icon, iconCls));
            var span = document.createElement('span');
            span.textContent = text;
            el.appendChild(span);
        } else {
            el.classList.remove('flex', 'items-center', 'gap-2');
            el.textContent = text;
        }
        return el;
    }

    function line(parent, text, cls, icon, iconCls) {
        var p = document.createElement('p');
        if (cls) p.className = cls;
        fill(p, text, icon, iconCls);
        parent.appendChild(p);
        return p;
    }

    function button(parent, text, cls, onClick, icon) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = cls;
        if (icon) b.appendChild(svgIcon(icon));
        b.appendChild(document.createTextNode(text));
        b.addEventListener('click', onClick);
        parent.appendChild(b);
        return b;
    }

    function linkButton(parent, text, cls, href, icon) {
        var a = document.createElement('a');
        a.className = cls;
        a.href = href;
        if (icon) a.appendChild(svgIcon(icon));
        a.appendChild(document.createTextNode(text));
        parent.appendChild(a);
        return a;
    }

    // ------------------------------------------------------------------ чек картинкой
    // Тот же чек, что в Telegram: картинку можно скачать или переслать клиенту (Web Share с файлом).
    function receiptBlock(parent, operationId) {
        var src = '/manager/receipt/' + encodeURIComponent(operationId) + '.png';
        var name = 'check-' + receipt(operationId) + '.png';
        var wrap = document.createElement('div');
        wrap.className = 'mt-5';
        parent.appendChild(wrap);
        line(wrap, 'Чек № ' + receipt(operationId), 'font-display font-extrabold', 'receipt', 'w-5 h-5 shrink-0 text-blue');
        var img = document.createElement('img');
        img.className = 'mt-2 w-full max-w-sm rounded-2xl border-2 border-blue/15';
        img.alt = 'Чек № ' + receipt(operationId);
        img.loading = 'lazy';
        img.src = src + '?t=' + Date.now(); // статус в чеке меняется — без кэша
        wrap.appendChild(img);
        var actions = document.createElement('div');
        actions.className = 'mt-2 grid gap-2 sm:flex sm:flex-wrap';
        wrap.appendChild(actions);
        var download = linkButton(actions, 'Скачать чек', 'btn-pop btn-pop-outline w-full sm:w-auto', src + '?download=1', 'download');
        download.setAttribute('download', name);
        if (navigator.share && window.File) {
            button(actions, 'Отправить клиенту', 'btn-pop btn-pop-outline w-full sm:w-auto', function () {
                fetch(src, { credentials: 'same-origin' }).then(function (response) {
                    if (!response.ok) throw new Error('receipt');
                    return response.blob();
                }).then(function (blob) {
                    var file = new File([blob], name, { type: 'image/png' });
                    if (navigator.canShare && !navigator.canShare({ files: [file] })) throw new Error('share');
                    return navigator.share({ files: [file], title: 'Чек ' + receipt(operationId) });
                }).catch(function (error) {
                    if (error && error.name === 'AbortError') return;
                    toast('Не получилось отправить — скачайте чек и перешлите его', 'error');
                });
            }, 'share');
        }
        return wrap;
    }

    // ------------------------------------------------------------------ «Покажите клиенту»
    // Экран на весь дисплей: большой QR и шаги. Вкладки — когда показать нужно два QR
    // (установка и личный кабинет нового клиента).
    var overlay = $('showClient');
    var lastFocus = null;

    function installView(url, title) {
        return { tab: 'Установка', icon: 'smartphone', title: title || 'Покажите клиенту этот QR', url: url, steps: INSTALL_STEPS, note: '' };
    }

    function cabinetView(url) {
        return { tab: 'Личный кабинет', icon: 'link', title: 'Личный кабинет клиента', url: url, steps: [], note: CABINET_HINT };
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
                var b = button(tabs, view.tab, 'chip gap-1.5', function () {
                    Array.prototype.forEach.call(tabs.children, function (c) { c.setAttribute('aria-pressed', 'false'); });
                    b.setAttribute('aria-pressed', 'true');
                    renderView(view);
                }, view.icon);
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
                var receiptBox = $('tempReceipt');
                if (receiptBox) { receiptBox.textContent = ''; receiptBlock(receiptBox, r.data.operation_id); }
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
    // slots / packs: null — «как у клиента сейчас» (новому — ничего); число — выбрал менеджер.
    var state = { clientCode: null, quote: null, nonce: nonce(), busy: false, slots: null, packs: null };

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
        if (state.slots !== null) body.slots = state.slots;
        if (state.packs !== null) body.packs = state.packs;
        return body;
    }

    var timer = null;
    function refreshSoon() { clearTimeout(timer); timer = setTimeout(refresh, 250); }

    function plural(n, one, few, many) {
        var m10 = n % 10, m100 = n % 100;
        if (m10 === 1 && m100 !== 11) return one;
        if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
        return many;
    }

    // Счётчики «Устройства» и «Трафик»: значения и границы — из ответа сервера.
    function renderExtras(q) {
        $('extrasBox').classList.remove('hidden');
        var devices = q.base_devices + q.slots;
        $('slotsValue').textContent = String(devices);
        $('slotsHint').textContent = q.max_slots
            ? q.base_devices + ' ' + plural(q.base_devices, 'входит', 'входят', 'входят') + ' в тариф · каждое следующее +' +
              money(q.slot_price) + '/мес · до ' + (q.base_devices + q.max_slots)
            : q.base_devices + ' ' + plural(q.base_devices, 'устройство', 'устройства', 'устройств') + ' по тарифу';
        setStep('slots', -1, q.slots > 0);
        setStep('slots', 1, q.slots < q.max_slots);

        var traffic = $('trafficStepper');
        traffic.classList.toggle('hidden', !q.max_packs);
        if (q.max_packs) {
            $('packsValue').textContent = (q.quota_gb + q.extra_traffic_gb) + ' ГБ';
            $('packsHint').textContent = q.quota_gb + ' ГБ в тарифе · +' + q.pack_gb + ' ГБ за ' + money(q.pack_price) + '/мес';
            setStep('packs', -1, q.packs > 0);
            setStep('packs', 1, q.packs < q.max_packs);
        }
    }

    function setStep(kind, delta, enabled) {
        var btn = document.querySelector('[data-step="' + kind + '"][data-delta="' + delta + '"]');
        if (btn) btn.disabled = !enabled;
    }

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
        if (q.service_fee) {
            line(body, '+ ваша услуга (подключение и настройка) — ' + money(q.service_fee), 'font-semibold text-blue',
                 'briefcase', 'w-4 h-4 shrink-0');
        }
        line(body, 'К оплате: ' + money(q.total), 'font-display font-black text-3xl mt-2');
        if (q.service_fee) {
            line(body, 'По QR: онлайн ' + money(q.total - q.service_fee) + ' за подписку, услугу ' +
                       money(q.service_fee) + ' клиент отдаёт вам наличными.', 'text-sm text-subtle', 'qr', 'w-4 h-4 shrink-0 self-start mt-0.5');
        }
        if (q.hint) {
            line(body, 'Выгоднее стандартный тариф «' + q.hint.name + '» — ' + q.hint.days + ' дн. за ' +
                       money(q.hint.price) + ' (на ' + money(q.hint.saving) + ' дешевле) и с автопродлением.',
                 'mt-2 text-orange font-semibold', 'lightbulb', 'w-5 h-5 shrink-0 self-start');
        }
        if (q.renew_text) line(body, q.renew_text, 'mt-2 text-muted', 'rotate', 'w-4 h-4 shrink-0 self-start mt-0.5');
        $('previewActions').classList.remove('hidden');
        renderExtras(q);
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
            if (!r.ok) { fill($('previewBody'), r.data.message || 'Не удалось посчитать', 'alert', 'w-5 h-5 shrink-0 text-danger'); return; }
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
        button(actions, 'Показать клиенту QR', 'btn-pop w-full sm:w-auto', function () { openShow(views); }, 'qr');
        linkButton(actions, 'Карточка клиента', 'btn-pop btn-pop-outline w-full sm:w-auto',
                   '/manager/clients/' + encodeURIComponent(data.client_code), 'user');
        linkButton(actions, 'Новая продажа', 'btn-pop btn-pop-outline w-full sm:w-auto', '/manager/', 'zap');
        if (data.cabinet_url) {
            line(box, 'У нового клиента есть личный кабинет — в «Показать клиенту» вкладка «Личный кабинет». ' +
                      'Ссылка показывается один раз.', 'mt-3 text-sm text-subtle', 'link', 'w-4 h-4 shrink-0 self-start mt-0.5');
        }
        openShow(views);
    }

    function showResult(data, method) {
        var box = $('resultBox');
        box.classList.remove('hidden');
        box.textContent = '';
        lockForm();

        if (data.replayed) {
            line(box, 'Эта продажа уже оформлена (чек № ' + receipt(data.operation_id) + '). Проверьте историю.', '', 'info');
            return;
        }

        if (data.status === 'completed') {
            line(box, 'Готово · чек № ' + receipt(data.operation_id), 'font-display font-extrabold text-xl', 'check-circle', 'w-6 h-6 shrink-0 text-success');
            line(box, money(data.price) + (method === 'cash' ? ' наличными' : '') +
                      (data.expires_at ? ' · подписка до ' + data.expires_at + ' МСК' : ''), 'text-sm text-subtle');
            showDone(box, data, data.subscription_url);
            receiptBlock(box, data.operation_id);
            return;
        }

        line(box, 'Счёт на ' + money(data.price) + ' · чек № ' + receipt(data.operation_id), 'font-display font-extrabold text-xl', 'card', 'w-6 h-6 shrink-0 text-blue');
        if (data.fee_cash) {
            line(box, 'Возьмите с клиента наличными вашу услугу — ' + money(data.fee_cash) + '. В счёт по QR она не входит.',
                 'mt-1 font-bold text-orange', 'banknote', 'w-5 h-5 shrink-0 self-start');
        }
        line(box, 'Разверните телефон к клиенту: он наводит камеру на QR и платит картой или через СБП.', 'mt-1 text-sm');
        var img = document.createElement('img');
        img.className = 'mt-3 w-64 h-64 max-w-full rounded-2xl border-2 border-blue/20 bg-white'; img.alt = 'QR для оплаты';
        box.appendChild(img);
        loadQr(img, data.payment_url);
        var link = document.createElement('a');
        link.href = data.payment_url; link.target = '_blank'; link.rel = 'noopener';
        link.className = 'block mt-2 text-sm font-bold text-blue break-all'; link.textContent = 'Открыть ссылку на оплату';
        box.appendChild(link);
        var status = line(box, 'Ждём оплату… страница обновится сама.', 'mt-4 font-bold', 'hourglass');
        var cancelBtn = button(box, 'Отменить счёт', 'btn-pop btn-pop-outline !min-h-0 !py-2 mt-2', function () {
            if (!confirm('Отменить счёт? Клиент не сможет по нему заплатить.')) return;
            api('/op/' + data.operation_id + '/cancel', {}).then(function (r) {
                fill(status, r.ok && r.data.cancelled ? 'Счёт отменён. Не оплачивайте старую ссылку.' : 'Счёт уже оплачен или отменён.', 'ban');
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
                    status.className = 'mt-4 font-display font-extrabold text-xl text-success';
                    fill(status, 'Оплачено! Подписка выдана.', 'check-circle', 'w-6 h-6 shrink-0');
                    api('/link', { client_code: r.data.client_code || data.client_code }).then(function (l) {
                        if (l.ok) { showDone(box, data, l.data.subscription_url); }
                        else { line(box, 'QR для установки — в карточке клиента.', 'mt-2 text-sm'); }
                        receiptBlock(box, data.operation_id);
                    });
                } else if (r.data.status === 'cancelled' || r.data.status === 'failed') {
                    clearInterval(poll);
                    cancelBtn.remove();
                    fill(status, 'Счёт отменён или не оплачен. Если клиент всё ещё хочет купить — начните продажу заново.', 'ban');
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
            state.slots = state.packs = null; // у другого клиента — свои устройства и трафик
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
    if ($('clientSelect')) $('clientSelect').addEventListener('change', function () {
        state.slots = state.packs = null;
        refresh();
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-step]'), function (btn) {
        btn.addEventListener('click', function () {
            if (!state.quote) return;
            var kind = btn.dataset.step;
            var current = state[kind] !== null ? state[kind] : state.quote[kind];
            state[kind] = Math.max(0, current + parseInt(btn.dataset.delta, 10));
            refresh();
        });
    });
    if ($('useCodeBtn')) {
        $('useCodeBtn').addEventListener('click', function () {
            api('/client-code', { code: $('accessCode').value }).then(function (r) {
                if (!r.ok) { toast(r.data.message || 'Неверный код', 'error'); return; }
                state.clientCode = r.data.client_code;
                state.slots = state.packs = null;
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
