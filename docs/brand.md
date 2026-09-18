# Бренд-бук FLASK в коде

Как лого-бук разложен по файлам проекта. Источник истины для цветов и шрифтов —
`webapp/static/css/styles.css` (блок `:root`); остальные поверхности повторяют те же
значения вручную, потому что живут вне сайта (письма, статика nginx, QR).

## Палитра

| Цвет | HEX | Роль по бренд-буку | CSS-переменная |
|---|---|---|---|
| Глубокий синий | `#2457C5` | основной цвет, логотип, акценты | `--brand-blue` (`--primary`) |
| Светлый голубой | `#73B9F5` | фоновые формы, доп. элементы | `--brand-sky` |
| Яркий оранжевый | `#FF7627` | акценты, элементы графики | `--brand-orange` (`--warning`) |
| Тёплый кремовый | `#F7F1E8` | фон, основа композиции | `--brand-cream` (`--bg-body`) |

Производные: `--brand-blue-deep #1B44A0` (hover), `--brand-sky-soft #DCEDFD` и
`--brand-orange-soft #FFE6D5` (подложки иконок), `--brand-paper #FFFCF6` (карточки),
`--brand-ink #17203A` (текст).

Оранжевый — **только акцент**: бейдж «Популярный», активная вкладка TMA, щит в hero,
искры, hover ссылок. Заливать им блоки не нужно.

## Типографика

| Роль | Шрифт | Где |
|---|---|---|
| Дисплей (заголовки, цены, логотип) | **Nunito** 800/900, трекинг −0.03em | `--font-display` |
| Текст интерфейса | **Inter** 400–700 | `--font-body` |
| Подписи-надзаголовки, коды, таблицы | **JetBrains Mono** капсом, трекинг 0.12–0.18em | `--font-mono` |

Все три с кириллицей — подключаются одним `<link>` в `base.html`, `tma/base.html`
и `tma/_auth_splash.html`. В письмах и `etc/nginx/static/import.html` веб-шрифтов нет
(почтовые клиенты их режут) — там системные аналоги.

## Графические элементы

| Элемент бренд-бука | Реализация |
|---|---|
| Зерно бумаги | `--grain` (SVG `feTurbulence`) → `body::before`, `opacity .05`, `mix-blend-mode: multiply` |
| Голубые органические формы | `.deco.deco-blob` + `.tariff-card::after` (несимметричный `border-radius`) |
| Полутон (halftone) | `.deco.deco-halftone` (`--halftone` + radial-маска), в hero-SVG — `<pattern id="hv-dots">` |
| Оранжевая «искра»-астериск | `.sparkle` (CSS-маска) и `.hv-spark` в hero-SVG |
| Рукописное подчёркивание | `.brand-underline::after` — оранжевая плашка с наклоном −0.8° |
| Печатный офсет вместо теней | `--shadow-pop` (`4px 4px 0`), кнопки «вдавливаются» на `:active` |

## Поверхности

| Файл | Что в нём фирменного |
|---|---|
| `webapp/static/css/styles.css` | вся дизайн-система: токены, кнопки, карточки, бейджи, hero |
| `webapp/static/css/tma.css` | светлый таб-бар, шторка «Ещё», компактные карточки тарифов |
| `webapp/static/js/tma.js` | `setHeaderColor`/`setBackgroundColor`/`setBottomBarColor` + цвет `MainButton` |
| `webapp/static/img/logo.svg` | иконка: синий скруглённый квадрат, круглая белая «F», оранжевые акценты |
| `webapp/templates/index.html` | hero-SVG глобуса в плоской брендовой графике |
| `webapp/core/mail.py` | `_brand_code_email()` — письма на таблицах с инлайн-цветами |
| `etc/nginx/static/import.html` | страница-редирект deeplink, стили инлайн (отдаётся мимо flask_site) |
| `tgbot/services/qr_generator.py` | QR синим по кремовому (в сером ~157/255 — сканеры читают) |

Слово-марка везде набирается одинаково: `Flask` синим + `VPN` оранжевым
(`.brand-wordmark` / `.vpn`). Тэглайн — `Freedom connects people` моно-капсом.
