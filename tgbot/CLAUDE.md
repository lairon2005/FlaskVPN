# tgbot/ — Документация модуля бота

## Структура директорий

```
tgbot/
├── handlers/
│   ├── user/          — пользовательские сценарии
│   ├── admin/         — административные сценарии
│   ├── support.py     — чат поддержки
│   └── webhook_handlers.py — входящие вебхуки от YooKassa
├── services/          — бизнес-логика (сервисный слой)
├── keyboards/         — inline-клавиатуры
├── states/            — FSM-состояния
├── filters/           — фильтры (IsAdmin)
└── middlewares/       — антиспам, таймаут поддержки
```

---

## Handlers — пользовательские

### `handlers/user/start.py` — `start_router`
Центральный файл меню. Все точки входа в главное меню идут через `_show_main_menu()`.

| Функция / хендлер | Триггер | Что делает |
|---|---|---|
| `_show_main_menu(target, user_id, full_name)` | вызывается из других хендлеров | Вызывает `profile_service.get_profile()`, строит текст со статусом подписки + трафиком, показывает `main_menu_keyboard()` |
| `process_start_command` | `/start` | Регистрирует нового / показывает меню существующему; запускает онбординг для новых |
| `_start_onboarding` | вызывается из `process_start_command` | Показывает список каналов для подписки (шаг 1 онбординга) |
| `onboarding_check_subscription` | `onboarding_check_sub` | Проверяет подписку на каналы → активирует триал → шаг 2 |
| `_activate_and_show_download` | вызывается из онбординга | Активирует триал/реферальный бонус, показывает скачивание Happ |
| `onboarding_app_installed` | `onboarding_app_installed` | Шаг 3 онбординга: показывает импорт-ссылку в Happ |
| `show_referral_info` | вызывается из хендлеров | Формирует реферальную ссылку и статистику |
| `referral_command_handler` | `/referral` | → `show_referral_info` |
| `referral_program_handler` | `referral_program` | → `show_referral_info` |
| `back_to_main_menu_handler` | `back_to_main_menu` | Очищает FSM, → `_show_main_menu` |

**Текст главного меню** (активная подписка):
```
👋 Добро пожаловать, <b>Иван</b>!

📋 <b>Ваша подписка:</b>
🟢 Статус: active
📅 Активна до: 15.04.2026 (осталось 24 дн.)
📊 Трафик: 12.3 GB / Безлимит
```

---

### `handlers/user/profile.py` — `profile_router`
Профиль и ключи.

| Функция / хендлер | Триггер | Что делает |
|---|---|---|
| `show_profile_logic(event, remnawave, bot)` | вызывается из payment.py и webhook_handlers | Показывает профиль с QR-кодом и sub_url; используется после оплаты. `remnawave: RemnawaveClient` приезжает из workflow data диспетчера (`Dispatcher(remnawave=...)`), в вебхуках — из `request.app['remnawave']` |
| `profile_command_handler` | `/profile` | → `show_profile_logic` |
| `my_profile_callback_handler` | `my_profile` | → `show_profile_logic` |
| `my_keys_handler` | `my_keys` | Показывает sub_url для копирования + кнопку импорта в Happ |
| `my_payments_handler` | `my_payments` | История последних 10 платежей пользователя |

**`my_keys_handler`** — упрощённый экран: берёт `subscription_url` из Remnawave через `profile_service` (уже полный URL) и показывает `keys_screen_keyboard(full_sub_url)` — кнопка ведёт на sub-страницу панели.

---

### `handlers/user/devices.py` — `devices_router`
Управление привязанными устройствами — пара к `hwidDeviceLimit` в панели: без
этого экрана упёршийся в лимит пользователь не подключит новый телефон.

| Хендлер | Триггер | Что делает |
|---|---|---|
| `devices_command_handler` | `/devices` | → `_show_devices` |
| `devices_callback_handler` | `my_devices`, `my_devices:<page>` | Список устройств, 8 на страницу |
| `device_delete_confirm` | `dev_del:<key>` | Карточка устройства + подтверждение |
| `device_delete_apply` | `dev_del_ok:<key>` | Отвязывает и возвращает к списку |

`key` — не сырой hwid, а `blake2s(hwid, 6)` (см. `device_service.device_key`):
callback_data ограничена 64 байтами, а длина hwid зависит от клиента.

Кнопка «➕ Докупить устройство» показывается, только если
`device_slot_service.quote()` вернул `ok` (активная оплаченная подписка,
потолок не выбран) — сам сценарий живёт в `device_slots.py`.

---

### `handlers/user/device_slots.py` — `device_slots_router`
Докупка дополнительных устройств в середине оплаченного периода. Пара к «Моим
устройствам»: там слот освобождают удалением, здесь — покупают новый.

| Хендлер | Триггер | Что делает |
|---|---|---|
| `buy_slots_handler` | `buy_slots` | Карточка докупки: текущий лимит, будущий, цена по остатку дней |
| `change_slots_quantity` | `slots_qty:<n>` | Степпер количества (пересчёт на сервере через `device_slot_service.quote`) |
| `pay_slots_handler` | `slots_pay:<card\|sbp>:<n>` | Создаёт счёт `kind='devices'` (tariff_id=NULL) и отдаёт ссылку на оплату |

Цена пересчитывается на сервере при нажатии «Оплатить»: между показом экрана и
нажатием могли пройти сутки или админ мог поменять цену.

### `handlers/user/revoke_key.py` — `revoke_key_router`
Перевыпуск ключа подписки. Пара к «Моим устройствам», но лечит другое:
отвязка освобождает слот, а перевыпуск отбирает доступ у того, кто скопировал
саму ссылку (иначе он просто зарегистрируется заново).

| Хендлер | Триггер | Что делает |
|---|---|---|
| `revoke_key_confirm` | `revoke_key`, `revoke_key:dev` | Предупреждение; суффикс `:dev` = вернуться по отмене на экран устройств |
| `revoke_key_apply` | `revoke_key_ok` | Перевыпускает и показывает новую ссылку |

---

### `handlers/user/payment.py` — `payment_router`
Оплата и промокоды.

| Функция / хендлер | Триггер | Что делает |
|---|---|---|
| `show_tariffs_logic(event, state)` | вызывается внутри | Показывает список тарифов, учитывает скидку из FSM |
| `payment_command_handler` | `/payment` | → `show_tariffs_logic` |
| `buy_subscription_callback_handler` | `buy_subscription` | → `show_tariffs_logic` |
| `apply_promo_from_broadcast` | `apply_promo_<code>` | Применяет промокод из рассылки, → тарифы со скидкой |
| `enter_promo_callback_handler` | `enter_promo_code` | Запускает FSM ввода промокода (из тарифной клавиатуры) |
| `process_promo_code` | `PromoApplyFSM.awaiting_code` | Валидирует промокод: бонусные дни → extend + показ профиля; скидка → тарифы |
| `select_tariff_handler` | `select_tariff_<id>` | Шаг «сколько устройств»: сумма тарифа + слотов, степпер |
| `change_tariff_slots_handler` | `tslots_<tariff_id>_<n>` | Степпер доп. устройств на чекауте |
| `tariff_to_payment_handler` | `tpay_<tariff_id>_<n>` | Переход к оплате (способ оплаты или сразу счёт) |
| `select_payment_method_handler` | `paymethod_<card\|sbp>_<tariff_id>_<slots>` | Создаёт платёж в YooKassa, сохраняет в БД, отправляет ссылку оплаты |

**Доп. устройства на чекауте:** количество по умолчанию = `user.extra_devices` —
при продлении уже оплаченные слоты должны сохраняться без лишних нажатий. Оно
едет в `callback_data`, а не в FSM: экран могли открыть из старого сообщения,
когда состояние уже очищено. Скидка промокода на слоты не распространяется.

**FSM скидки**: `state.update_data(discount=N, promo_code='CODE')` — подхватывается в `show_tariffs_logic` и `select_tariff_handler`.

---

### `handlers/user/trial_sub.py` — `trial_sub_router`
Повторная активация триала для уже зарегистрированных пользователей (кнопка "Бесплатная подписка" в меню).

| Хендлер | Триггер | Что делает |
|---|---|---|
| `start_trial_process` | `start_trial_process` | Проверяет, не получал ли уже триал; показывает каналы для подписки |
| `check_subscription_handler` | `check_subscription` | Проверяет подписку → `give_trial_subscription` |
| `give_trial_subscription` | вызывается внутри | Активирует триал через `subscription_service.activate_trial()` |

---

### `handlers/user/link_email.py` — `link_email_router`
Привязка email к аккаунту (для доступа к web-dashboard).

FSM: `EmailLinkFSM` → запрашивает email → отправляет код верификации → подтверждает.

---

### `handlers/user/instruction.py` — `instruction_router`
Инструкция по подключению VPN. Триггер: `instruction_info` (из `keys_screen_keyboard`).

---

### `handlers/webhook_handlers.py`
Входящий вебхук от YooKassa (POST /yookassa).

```
Вебхук → parse_webhook_notification → payment_service.process_payment_succeeded()
  → subscription extended → notify user (Telegram) + web user (email)
  → show_profile_logic (для Telegram-пользователей)
```

---

### `handlers/support.py` — `support_router`
Чат с поддержкой (FSM). Пользователь ↔ Администраторы.

---

## Handlers — администраторские (`handlers/admin/`)

| Файл | Что делает |
|---|---|
| `main.py` | Точка входа `/admin`, главное меню, статистика |
| `users.py` | Поиск пользователя, добавление дней, сброс ключа, удаление |
| `tariffs.py` | CRUD тарифов (название, цена, срок, вкл/выкл, удаление) |
| `promocodes.py` | CRUD промокодов (бонусные дни или скидка %) |
| `channels.py` | CRUD каналов для онбординга |
| `broadcast.py` | Рассылка: выбор аудитории → текст → опциональный промокод → подтверждение → отправка |
| `cancel.py` | Универсальная отмена FSM для admin-сценариев |
| `device_settings.py` | Настройки доп. устройств: цена слота, базовый лимит, потолок докупки (таблица `app_settings`) |

Все admin-хендлеры защищены фильтром `IsAdmin` (см. `filters/admin.py`, применяется на уровне `admin_router` в `admin/__init__.py`).

---

## Services — сервисный слой

Все сервисы инициализируются в `services/__init__.py` как синглтоны и передаются через DI или импортируются напрямую.

| Объект | Класс | Зависимости | Ключевые методы |
|---|---|---|---|
| `subscription_service` | `SubscriptionService` | `user_repo`, `remnawave_client` | `activate_trial(user_id, days)`, `extend(user_id, days, data_limit_gb)` |
| `referral_service` | `ReferralService` | `user_repo`, `subscription_service` | `activate_new_user_referral(user_id, referrer_id, days)` |
| `promo_service` | `PromoCodeService` | `promo_repo`, `user_repo` | `validate(code, user_id)`, `apply(user_id, promo)` |
| `user_service` | `UserService` | `user_repo`, `stats_repo` | `register_or_get(...)`, `get_user(user_id)`, `get_referral_info(user_id)` |
| `profile_service` | `ProfileService` | `user_repo`, `remnawave_client` | `get_profile(user_id) → ProfileData(db_user, vpn_user, error)` |
| `device_service` | `DeviceService` | `user_repo`, `remnawave_client` | `list_devices(user_id) → DeviceList`, `delete_device(user_id, key)` |
| `device_slot_service` | `DeviceSlotService` | `user_repo`, `settings_repo`, `remnawave_client` | `quote(user_id, slots) → SlotQuote`, `add_slots` / `set_slots`, `sync_limit(user_id) → SyncResult`, `expire_slots` |
| `key_service` | `KeyService` | `user_repo`, `remnawave_client` | `revoke(user_id) → RevokeResult` — новая ссылка подписки + отвязка всех устройств, кулдаун 10 мин по `subRevokedAt` |
| `payment_service` | `PaymentService` | `subscription_service`, `referral_service`, `user_repo`, `tariff_repo`, `payment_repo`, `payment_method_service`, `device_slot_service` | `create_payment_record(...)`, `process_successful_payment(...)`, `process_stars_payment(...)`, `process_refund(...)`, `cancel_pending_payment(user_id)`, `cancel_stale_payments(minutes)`, `charge_renewal(user_id)` |
| `admin_stats_service` | `AdminStatsService` | `stats_repo`, `remnawave_client`, `payment_repo`, `lifecycle_repo` | `get_dashboard_stats()`, `get_cohort_metrics()` |
| `support_service` | `SupportService` | `user_repo` | управление состоянием чата поддержки |

### `services/payment.py` (не класс, а модуль)
Низкоуровневая работа с YooKassa API:
- `create_payment(user_id, amount, description, return_url, metadata, shop_id, secret_key)` → `(payment_url, yookassa_payment_id)`
- `get_payment_url(yookassa_payment_id, shop_id, secret_key)` → `str | None`
- `parse_webhook_notification(data)` → распаковывает входящий вебхук

### `services/scheduler.py`
APScheduler задачи:
- Проверка истекающих подписок (за N дней до конца) → `send_reminder(bot, user, text)`
- Проверка просроченных pending-платежей → отмена через 30 мин
- `sync_device_limits` (каждый час в :20) — сверка `hwidDeviceLimit` с БД,
  гашение слотов вместе с истёкшей подпиской и удаление устройств сверх лимита
  (панель режет только регистрацию нового hwid, старые записи живут дальше).
  Предупреждение за сутки идёт через `lifecycle_repo`, серия `device_slots`.

### `services/device_pricing.py` / `services/device_slot_service.py`
Ценообразование доп. устройств и работа со слотами — см. раздел
«Дополнительные устройства» в корневом CLAUDE.md.

### `services/utils.py`
- `format_traffic(bytes) → str` — форматирование трафика (KB/MB/GB)
- `format_traffic` и `get_user_attribute` — реэкспорт из `utils/formatting.py`; `get_user_attribute(vpn_user, key, default)` безопасно достаёт поле из dict-профиля Remnawave
- `decline_word(n, forms)` — склонение числительных

### `services/qr_generator.py`
- `create_qr_code(url) → BytesIO` — генерация QR-кода для subscription_url

### `services/subscription.py` (утилита, не класс)
- `check_subscription(bot, user_id) → bool` — проверяет подписку пользователя на все каналы из БД

---

## Keyboards (`keyboards/inline.py`)

| Функция | Где используется |
|---|---|
| `main_menu_keyboard(has_active_sub, has_email)` | `start.py/_show_main_menu` |
| `tariffs_keyboard(tariffs, promo_procent)` | `payment.py`, `scheduler.py` |
| `keys_screen_keyboard(import_url)` | `profile.py/my_keys_handler` |
| `devices_keyboard(devices, page, can_buy_slots)` | `devices.py` — список с пагинацией + докупка |
| `tariff_slots_keyboard(tariff_id, slots, max_slots, base_limit)` | `payment.py` — шаг «сколько устройств» |
| `slot_purchase_keyboard(slots, max_slots, total_limit)` | `device_slots.py` — степпер докупки |
| `slot_invoice_keyboard(payment_url)` | `device_slots.py` — счёт на слоты |
| `device_settings_keyboard()` | `admin/device_settings.py` — цена/база/потолок |
| `device_delete_confirm_keyboard(key, page)` | `devices.py` — подтверждение отвязки |
| `revoke_key_confirm_keyboard(back)` | `revoke_key.py` — подтверждение перевыпуска |
| `revoked_key_keyboard(subscription_url)` | `revoke_key.py` — экран с новой ссылкой |
| `profile_keyboard(subscription_url)` | `profile.py/show_profile_logic` |
| `onboarding_subscribe_keyboard(channels)` | `start.py/_start_onboarding` |
| `onboarding_download_app_keyboard()` | `start.py/_activate_and_show_download` |
| `onboarding_import_keyboard(subscription_url)` | `start.py/onboarding_app_installed` |
| `channels_subscribe_keyboard(channels)` | `trial_sub.py` |
| `back_to_main_menu_keyboard()` | везде как fallback |
| `cancel_fsm_keyboard(back_callback_data)` | admin FSM |
| `admin_main_menu_keyboard()` | `admin/main.py` |
| `user_manage_keyboard(user_id)` | `admin/users.py` |
| `tariffs_list_keyboard / single_tariff_manage_keyboard` | `admin/tariffs.py` |
| `promo_codes_list_keyboard / promo_type_keyboard` | `admin/promocodes.py` |
| `broadcast_audience_keyboard / broadcast_promo_keyboard / confirm_broadcast_keyboard` | `admin/broadcast.py` |
| `close_support_chat_keyboard()` | `support.py` |

**`main_menu_keyboard` логика кнопок:**
- Всегда: Оплатить, Мои ключи, Рефералы, Поддержка (adjust 1,1,2)
- `not has_active_sub` → + Бесплатная подписка
- `not has_email` → + Привязать Email

---

## States (FSM)

| Файл | Класс | Состояния |
|---|---|---|
| `payment_states.py` | `PromoApplyFSM` | `awaiting_code` |
| `email_link_states.py` | `EmailLinkFSM` | `awaiting_email`, `awaiting_code` |
| `support_states.py` | `SupportFSM` | `in_chat` |
| `admin_states.py` | `AdminUserFSM` | `awaiting_user_id`, `awaiting_days` |
| `tariff_states.py` | `TariffFSM` | `awaiting_name`, `awaiting_price`, `awaiting_duration`, `editing_*` |
| `promo_states.py` | `PromoFSM` | `awaiting_code`, `awaiting_value`, `awaiting_uses` |
| `broadcast_states.py` | `BroadcastFSM` | `awaiting_text`, `awaiting_promo` |
| `channel_states.py` | `ChannelFSM` | `awaiting_channel_id`, `awaiting_title`, `awaiting_invite_link` |
| `device_settings_states.py` | `DeviceSettingsFSM` | `edit_value` |

---

## Filters & Middlewares

| Файл | Что делает |
|---|---|
| `filters/admin.py` — `IsAdmin` | Проверяет `event.from_user.id in config.tg_bot.admin_ids` |
| `middlewares/flood.py` — `ThrottlingMiddleware` | Антиспам: L1=0.5s, L2=1.5s, предупреждение "Не спамь!" |
| `middlewares/support_timeout.py` | Автоматически закрывает чат поддержки по таймауту |
| `middlewares/callback_answer.py` | Автоматически отвечает на callback queries |

---

## Паттерны, используемые повсеместно

### Отправка / редактирование сообщений
```python
try:
    await call.message.edit_text(text, reply_markup=kb)
except TelegramBadRequest:
    await call.message.delete()
    await call.message.answer(text, reply_markup=kb)
```

### Получение ссылки подписки
```python
# Remnawave отдаёт в subscriptionUrl уже полный URL — домен и порт не приклеиваем.
profile_data = await profile_service.get_profile(user_id)
if profile_data.error or not profile_data.vpn_user:
    ...  # показать profile_data.error
connection = build_connection_view(profile_data.vpn_user)  # utils/subscription_view.py
full_sub_url = connection.subscription_url
```

### Кнопка подключения
```python
# Ведём прямо на subscription_url — Remnawave отдаёт по нему sub-страницу
# с импортом под каждое приложение. happ:// deeplink и хелперы utils/url.py
# удалены; статическая страница /import в nginx осталась только для старых ссылок.
keys_screen_keyboard(full_sub_url)
```

### FSM скидки в flow оплаты
```python
# Сохраняем
await state.update_data(discount=percent, promo_code='CODE')
# Читаем
fsm_data = await state.get_data()
discount = fsm_data.get("discount")
```
