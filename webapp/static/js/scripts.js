// webapp/static/js/scripts.js

// ============================================================
// Toast System
// ============================================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast-item ${type}`;
    toast.innerHTML = `<span>${message}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================================
// State
// ============================================================

let activePromo = null;

// ============================================================
// Payment
// ============================================================

// Доп. устройства, выбранные на экране тарифов. Цена слотов не зависит от
// тарифа — только от его длительности, поэтому одно значение на все карточки.
let selectedSlots = 0;

function billingMonths(durationDays) {
    // Тот же расчёт, что на сервере (device_pricing.billing_months):
    // 365 дней → 12 месяцев, минимум один месяц.
    return Math.max(1, Math.round(durationDays / 30));
}

function initSlotSelector() {
    // Стартуем с уже оплаченных слотов: продление, в котором человек не трогал
    // счётчик, не должно снимать его доп. устройства.
    const box = document.getElementById('slotSelector');
    if (!box) return;
    selectedSlots = parseInt(box.dataset.currentSlots || '0', 10);
    updateSlotCount(0);
}

function updateSlotCount(delta) {
    const box = document.getElementById('slotSelector');
    if (!box) return;

    const max = parseInt(box.dataset.slotMax || '0', 10);
    const baseLimit = parseInt(box.dataset.baseLimit || '0', 10);

    selectedSlots = Math.max(0, Math.min(max, selectedSlots + delta));

    const label = document.getElementById('slotTotalLabel');
    if (label) label.textContent = baseLimit + selectedSlots;

    renderTariffPrices(activePromo ? activePromo.discount_percent : 0);
}

// Пока счёт в статусе pending, сервер не даёт создать второй (409) — иначе
// вебхук по старому начислил бы оплаченное дважды. Сама YooKassa отменяет счёт
// только через ~30 минут, поэтому передумавшему насчёт способа оплаты нужна
// ручная отмена, иначе он заперт до таймаута.
async function offerCancelPending(retry) {
    const ok = confirm(
        'У вас уже есть неоплаченный счёт.\n\n' +
        'Отменить его и выставить новый? Старую ссылку на оплату после этого ' +
        'открывать не нужно.'
    );
    if (!ok) return;

    try {
        const response = await fetch('/payment/cancel-pending', { method: 'POST' });
        if (response.ok) {
            showToast('Счёт отменён, выставляю новый…', 'info');
            await retry();
        } else if (response.status === 401) {
            window.location.href = '/login';
        } else {
            showToast('Не удалось отменить счёт. Обновите страницу и попробуйте снова.', 'error');
        }
    } catch (error) {
        console.error('Cancel pending error:', error);
        showToast('Ошибка соединения с сервером.', 'error');
    }
}

async function initPayment(tariffName, price, btn, allowCancelRetry = true) {
    if (!btn) return;
    btn.classList.add('btn-loading');

    try {
        const isIntro = btn.dataset.intro === '1';
        const payload = { tariff_name: tariffName, price: price, extra_devices: isIntro ? 0 : selectedSlots };
        if (btn.dataset.tariffId) payload.tariff_id = parseInt(btn.dataset.tariffId, 10);
        // Вводный тариф: цена фиксированная, промокод и доп. устройства не применяются.
        if (activePromo && !isIntro) {
            payload.promo_code = activePromo.code;
            payload.discount_percent = activePromo.discount_percent;
        }

        const response = await fetch('/payment/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            const data = await response.json();
            window.location.href = data.payment_url;
        } else if (response.status === 401) {
            window.location.href = '/login';
        } else if (response.status === 409) {
            // Один повтор: если и после отмены 409, дальше молча уходим в toast.
            if (allowCancelRetry) {
                await offerCancelPending(() => initPayment(tariffName, price, btn, false));
            } else {
                showToast("У вас уже есть неоплаченный счёт. Завершите оплату или попробуйте позже.", "warning");
            }
        } else if (response.status === 400) {
            const data = await response.json().catch(() => ({}));
            showToast(data.detail || "Тариф недоступен.", "warning");
        } else {
            showToast("Ошибка при создании платежа. Попробуйте позже.", "error");
        }
    } catch (error) {
        console.error('Payment error:', error);
        showToast("Ошибка соединения с сервером.", "error");
    } finally {
        btn.classList.remove('btn-loading');
    }
}

// ============================================================
// Device slots (докупка устройств в середине периода)
// ============================================================

let buySlotsCount = 1;

function updateBuySlots(delta) {
    const box = document.getElementById('buySlotsBox');
    if (!box) return;

    const available = parseInt(box.dataset.slotAvailable || '1', 10);
    const unitPrice = parseInt(box.dataset.slotUnitPrice || '0', 10);

    buySlotsCount = Math.max(1, Math.min(available, buySlotsCount + delta));

    const countEl = document.getElementById('buySlotsCount');
    if (countEl) countEl.textContent = buySlotsCount;

    const priceEl = document.getElementById('buySlotsPrice');
    if (priceEl) priceEl.textContent = unitPrice * buySlotsCount;
}

async function buyDeviceSlots(btn, allowCancelRetry = true) {
    if (!btn) return;
    btn.classList.add('btn-loading');

    try {
        const response = await fetch('/payment/device-slots', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slots: buySlotsCount })
        });

        if (response.ok) {
            const data = await response.json();
            window.location.href = data.payment_url;
        } else if (response.status === 401) {
            window.location.href = '/login';
        } else if (response.status === 409 && allowCancelRetry) {
            await offerCancelPending(() => buyDeviceSlots(btn, false));
        } else {
            // Сервер присылает готовый текст (нет подписки, потолок, занятый счёт).
            const data = await response.json().catch(() => ({}));
            showToast(data.detail || "Не удалось создать счёт. Попробуйте позже.", "error");
        }
    } catch (error) {
        console.error('Device slots error:', error);
        showToast("Ошибка соединения с сервером.", "error");
    } finally {
        btn.classList.remove('btn-loading');
    }
}

// ============================================================
// Promo Code
// ============================================================

async function applyPromo() {
    const input = document.getElementById('promoInput');
    const resultDiv = document.getElementById('promoResult');
    const code = input.value.trim();

    if (!code) {
        resultDiv.innerHTML = '<span class="text-danger-custom">Введите промокод</span>';
        return;
    }

    resultDiv.innerHTML = '<span class="text-muted">Проверяю…</span>';

    try {
        const response = await fetch('/payment/validate-promo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code })
        });

        const data = await response.json();

        if (data.valid) {
            if (data.type === 'discount') {
                activePromo = { code: data.code, discount_percent: data.discount_percent };
                resultDiv.innerHTML = `<span class="text-success-custom">Скидка ${data.discount_percent}% применена!</span>`;
                showToast(`Промокод применён: скидка ${data.discount_percent}%`, 'success');
                updateTariffPrices(data.discount_percent);
            } else if (data.type === 'bonus_days') {
                resultDiv.innerHTML = '<span class="text-muted">Применяю бонусные дни…</span>';
                const applyResp = await fetch('/payment/apply-bonus-promo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: code })
                });
                if (applyResp.ok) {
                    const applyData = await applyResp.json();
                    resultDiv.innerHTML = `<span class="text-success-custom">Начислено ${applyData.bonus_days} бонусных дней!</span>`;
                    showToast(`+${applyData.bonus_days} бонусных дней!`, 'success');
                    setTimeout(() => location.reload(), 2000);
                } else {
                    resultDiv.innerHTML = '<span class="text-danger-custom">Ошибка при применении промокода</span>';
                }
            }
        } else {
            activePromo = null;
            resultDiv.innerHTML = `<span class="text-danger-custom">${data.error}</span>`;
            resetTariffPrices();
        }
    } catch (error) {
        console.error('Promo error:', error);
        resultDiv.innerHTML = '<span class="text-danger-custom">Ошибка соединения</span>';
    }
}

// Галочка из SVG-спрайта (_icons.html). Раньше здесь подставлялся
// `<i class="fas fa-check">` — Font Awesome с новой базы убран.
const CHECK_ICON =
    '<svg class="w-4 h-4 shrink-0 mt-0.5 text-success" aria-hidden="true"><use href="#i-check"></use></svg>';

// Цена карточки зависит сразу от двух вещей — промокода и количества доп.
// устройств, — поэтому рисуется в одном месте. Иначе промокод затирал бы
// разметку, на которую опирается счётчик устройств, и брал бы за «исходную»
// цену уже посчитанную сумму со слотами (а скидка на слоты не действует).
function renderTariffPrices(discountPercent) {
    const cards = document.querySelectorAll('#tariffCards .tariff-card[data-tariff-price]');
    if (!cards.length) {
        // Лендинг и прочие экраны без счётчика устройств — прежнее поведение.
        legacyUpdateTariffPrices(discountPercent);
        return;
    }

    const box = document.getElementById('slotSelector');
    const slotPrice = box ? parseInt(box.dataset.slotPrice || '0', 10) : 0;
    const baseLimit = box ? parseInt(box.dataset.baseLimit || '0', 10) : 0;

    cards.forEach((card) => {
        const basePrice = parseInt(card.dataset.tariffPrice || '0', 10);
        const days = parseInt(card.dataset.tariffDays || '30', 10);
        const slotsCost = slotPrice * billingMonths(days) * selectedSlots;
        const tariffPart = discountPercent
            ? Math.round(basePrice * (1 - discountPercent / 100))
            : basePrice;
        const total = tariffPart + slotsCost;

        const priceEl = card.querySelector('.tariff-price');
        if (priceEl) {
            const struck = discountPercent
                ? `<s class="text-muted text-[0.45em] font-semibold">${basePrice + slotsCost} ₽</s> `
                : '';
            priceEl.innerHTML =
                `${struck}<span class="tariff-price-value">${total}</span>` +
                `<span class="text-[0.5em]"> ₽</span>`;
        }

        const devicesEl = card.querySelector('.tariff-devices-line');
        if (devicesEl) {
            const extra = selectedSlots
                ? ` <span class="text-muted">(+${slotsCost} ₽)</span>`
                : '';
            devicesEl.innerHTML = CHECK_ICON +
                `<span>${baseLimit + selectedSlots} устройств${extra}</span>`;
        }
    });
}

function legacyUpdateTariffPrices(discountPercent) {
    document.querySelectorAll('.tariff-price').forEach(el => {
        const originalPrice = parseInt(el.dataset.originalPrice || el.textContent);
        if (!el.dataset.originalPrice) {
            el.dataset.originalPrice = originalPrice;
        }
        if (!discountPercent) {
            el.innerHTML = `${originalPrice}<span class="text-[0.5em]"> ₽</span>`;
            return;
        }
        const newPrice = Math.round(originalPrice * (1 - discountPercent / 100));
        el.innerHTML = `<s class="text-muted text-[0.45em] font-semibold">${originalPrice} ₽</s> ${newPrice}<span class="text-[0.5em]"> ₽</span>`;
    });
}

function updateTariffPrices(discountPercent) {
    renderTariffPrices(discountPercent);
}

function resetTariffPrices() {
    renderTariffPrices(0);
}

// ============================================================
// Copy Functions
// ============================================================

function copyLink() {
    const input = document.getElementById("subLink");
    if (!input) return;
    navigator.clipboard.writeText(input.value).then(() => {
        showToast("Ключ скопирован!", "success");
    });
}

function initRefLink() {
    const input = document.getElementById("refLink");
    if (!input) return;
    const ref = input.dataset.ref;
    if (ref) input.value = window.location.origin + "/register?ref=" + ref;
}

function copyRefLink() {
    const input = document.getElementById("refLink");
    if (!input) return;
    navigator.clipboard.writeText(input.value).then(() => {
        showToast("Реферальная ссылка скопирована!", "success");
    });
}

// ============================================================
// OS Detection
// ============================================================

function detectOSAndSetLink() {
    const userAgent = navigator.userAgent || navigator.vendor || window.opera;
    const downloadBtn = document.getElementById("downloadBtn");
    const appDesc = document.getElementById("appDescription");
    const osIcon = document.getElementById("osIcon");
    const btnTextEl = document.getElementById("btnText");

    if (!downloadBtn) return;

    // Раньше здесь переписывался className классами Font Awesome. Со спрайтом
    // меняется только ссылка на символ — иконка остаётся тем же <svg>.
    const setIcon = (name) => {
        const use = osIcon && osIcon.querySelector('use');
        if (use) use.setAttribute('href', '#i-' + name);
    };

    const INCY_ANDROID = "https://play.google.com/store/apps/details?id=llc.itdev.incy";
    const INCY_IOS = "https://apps.apple.com/ru/app/incy/id6756943388";
    const INCY_WIN = "https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-windows-setup.exe";

    let url = INCY_ANDROID, desc = "Выберите приложение для вашей ОС.", icon = "download", label = "Скачать";

    if (/android/i.test(userAgent)) {
        url = INCY_ANDROID; desc = "Для Android рекомендуем INCY."; icon = "android"; label = "Google Play";
    } else if (/iPad|iPhone|iPod/.test(userAgent) && !window.MSStream) {
        url = INCY_IOS; desc = "Для iOS рекомендуем INCY."; icon = "apple"; label = "App Store";
    } else if (/Win/i.test(userAgent)) {
        url = INCY_WIN; desc = "Для Windows рекомендуем INCY."; icon = "windows"; label = "Скачать для Windows";
    } else if (/Mac/i.test(userAgent)) {
        url = INCY_IOS; desc = "Для macOS рекомендуем INCY."; icon = "apple"; label = "Скачать для macOS";
    }

    downloadBtn.href = url;
    if (appDesc) appDesc.textContent = desc;
    if (btnTextEl) btnTextEl.textContent = label;
    setIcon(icon);
}

// ============================================================
// Password Strength Indicator
// ============================================================

function initPasswordStrength() {
    const passwordInput = document.getElementById('registerPassword');
    const strengthBar = document.getElementById('passwordStrengthBar');
    if (!passwordInput || !strengthBar) return;

    passwordInput.addEventListener('input', function() {
        const val = this.value;
        strengthBar.className = 'bar';

        if (val.length === 0) {
            strengthBar.style.width = '0';
            return;
        }
        if (val.length < 6) {
            strengthBar.classList.add('weak');
        } else if (val.length < 10 || !/[A-Z]/.test(val) || !/[0-9]/.test(val)) {
            strengthBar.classList.add('medium');
        } else {
            strengthBar.classList.add('strong');
        }
    });
}

// ============================================================
// Form Loading State
// ============================================================

function initFormLoading() {
    document.querySelectorAll('form').forEach(form => {
        form.addEventListener('submit', function() {
            const btn = form.querySelector('button[type="submit"]');
            if (btn && !btn.classList.contains('btn-link')) {
                btn.classList.add('btn-loading');
            }
        });
    });
}

// ============================================================
// Scroll Animations (IntersectionObserver)
// ============================================================

function initScrollAnimations() {
    // Без IntersectionObserver анимации просто отыгрывают сразу: паузить их
    // здесь означало бы оставить контент с opacity: 0 навсегда.
    if (!('IntersectionObserver' in window)) return;

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.style.animationPlayState = 'running';
                observer.unobserve(entry.target);
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('.animate-in').forEach(el => {
        el.style.animationPlayState = 'paused';
        observer.observe(el);
    });
}

// ============================================================
// Init
// ============================================================

document.addEventListener("DOMContentLoaded", function() {
    detectOSAndSetLink();
    initRefLink();
    initPasswordStrength();
    initFormLoading();
    initScrollAnimations();
    initSlotSelector();
});
