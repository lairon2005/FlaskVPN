// webapp/static/js/site.js
// Поведение публичных страниц на Tailwind-базе: мобильное меню и аккордеон FAQ.
// Платежи, промокоды и слоты устройств живут в scripts.js — он подключается
// на тех страницах, где действительно нужен.

// ============================================================
// Мобильное меню
// ============================================================

(function initNav() {
    const toggle = document.getElementById('navToggle');
    const panel = document.getElementById('navPanel');
    if (!toggle || !panel) return;

    const iconOpen = document.getElementById('navIconOpen');
    const iconClose = document.getElementById('navIconClose');

    function setOpen(open) {
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
        if (iconOpen) iconOpen.hidden = open;
        if (iconClose) iconClose.hidden = !open;
    }

    toggle.addEventListener('click', () => setOpen(panel.hidden));

    // Клик по пункту меню закрывает панель: якорные ссылки ведут на эту же
    // страницу, иначе меню осталось бы висеть поверх контента.
    panel.addEventListener('click', (e) => {
        if (e.target.closest('a')) setOpen(false);
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !panel.hidden) {
            setOpen(false);
            toggle.focus();
        }
    });
})();

// ============================================================
// FAQ
// ============================================================

(function initFaq() {
    document.querySelectorAll('[data-faq-toggle]').forEach((button) => {
        const answer = document.getElementById(button.getAttribute('aria-controls'));
        if (!answer) return;

        button.addEventListener('click', () => {
            const open = button.getAttribute('aria-expanded') === 'true';
            button.setAttribute('aria-expanded', String(!open));
            answer.hidden = open;
            const chevron = button.querySelector('[data-faq-chevron]');
            if (chevron) chevron.classList.toggle('rotate-180', !open);
        });
    });
})();

// ============================================================
// Модальные окна
// ============================================================

(function initModals() {
    document.querySelectorAll('[data-modal-open]').forEach((trigger) => {
        trigger.addEventListener('click', () => {
            const dialog = document.getElementById(trigger.dataset.modalOpen);
            if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
        });
    });

    document.querySelectorAll('dialog.modal').forEach((dialog) => {
        dialog.querySelectorAll('[data-modal-close]').forEach((btn) => {
            btn.addEventListener('click', () => dialog.close());
        });

        // Клик по затемнению закрывает окно. Проверяем именно координаты:
        // событие click с backdrop приходит с target самого <dialog>, а клик
        // внутри содержимого — с target вложенного элемента.
        dialog.addEventListener('click', (e) => {
            if (e.target !== dialog) return;
            const box = dialog.getBoundingClientRect();
            const inside = e.clientX >= box.left && e.clientX <= box.right &&
                           e.clientY >= box.top && e.clientY <= box.bottom;
            if (!inside) dialog.close();
        });
    });
})();
