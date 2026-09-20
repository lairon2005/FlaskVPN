# Бренд-бук FLASK в коде

Как лого-бук разложен по файлам проекта. Стиль называется **дофаминовым**:
тёплая бумага с зерном, пузырьковая типографика, коллаж из ретро-техники,
скотч и рваные края, оранжевые «искры».

Источник истины для цветов и шрифтов — **`webapp/static/css/src/app.css`** (блок
`@theme`). Сайт и Mini App собираются из него одним файлом; остальные поверхности
повторяют значения вручную, потому что живут вне сайта (письма, статика nginx, QR).

**Градус дофамина по поверхностям:** лендинг — на максимум (коллаж, леттеринг,
крупная графика); кабинет и Mini App — сдержанно, только палитра, формы и
типографика: там работают с ключом и платежами.

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
| Леттеринг (слово-марка, английские слоганы) | **Matcha World** — `webapp/static/fonts/matcha-world.woff2` | `--font-lettering` |
| Дисплей (заголовки, цены, логотип) | **Nunito** 800/900, трекинг −0.03em | `--font-display` |
| Текст интерфейса | **Inter** 400–700 | `--font-body` |
| Подписи-надзаголовки, коды, таблицы | **JetBrains Mono** капсом, трекинг 0.12–0.18em | `--font-mono` |

**В Matcha World нет кириллицы** — 128 глифов, латиница и цифры (Khurasan Studio,
свободен для коммерческого использования). Поэтому в `@font-face` у него
`unicode-range` только на латиницу: слово-марка `FlaskVPN` и английские слоганы
(`FREEDOM CONNECTS PEOPLE`, `SAME INTERNET JUST FREER`, `SMALL TOOLS BIGGER FREEDOM`)
набираются леттерингом, а русские заголовки браузер сразу отдаёт Nunito 900 —
единственному из кандидатов (Comfortaa, Unbounded, Rubik), который держит ту же
округлую тяжесть. Шрифт caps-only: строчные отображаются капителью, как в гайде.

Nunito, Inter и JetBrains Mono — подключаются одним `<link>` в `base.html`, `tma/base.html`
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
| `webapp/static/css/src/app.css` | дизайн-система на Tailwind: `@theme`-токены, `.sticker`, `.btn-pop`, `.halftone`, `.blob`, `.marker-underline`, `.tape` |
| `webapp/templates/_icons.html` | SVG-спрайт Lucide (38 символов) вместо Font Awesome |
| `webapp/static/img/og-cover.png` | обложка ссылки 1200×630, генерится `python3 tools/make_og.py` |
| `webapp/static/fonts/matcha-world.woff2` | фирменный леттеринг, 23 КБ |
| `webapp/static/img/collage/` | ретро-коллаж (монитор, раскладушка, скотч, бумага) — см. README в папке |
| `webapp/static/css/src/app.css` (секция `.tma-*`) | светлый таб-бар, шторка «Ещё», компактные карточки тарифов |
| `webapp/static/js/tma.js` | `setHeaderColor`/`setBackgroundColor`/`setBottomBarColor` + цвет `MainButton` |
| `webapp/static/img/logo.svg` | иконка: синий скруглённый квадрат, круглая белая «F», оранжевые акценты |
| `webapp/templates/index.html` | hero-SVG глобуса в плоской брендовой графике |
| `webapp/core/mail.py` | `_brand_code_email()` — письма на таблицах с инлайн-цветами |
| `etc/nginx/static/import.html` | страница-редирект deeplink, стили инлайн (отдаётся мимо flask_site) |
| `tgbot/services/qr_generator.py` | QR синим по кремовому (в сером ~157/255 — сканеры читают) |

Слово-марка везде набирается одинаково: `Flask` синим + `VPN` оранжевым
(`.brand-wordmark` / `.vpn`). Тэглайн — `Freedom connects people` моно-капсом.
