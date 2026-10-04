#!/usr/bin/env python3
"""
Снимки панели менеджера и сборка PDF-руководства.

Запускается Python'ом, где установлен playwright (браузер — установленный Google Chrome):

    <python с playwright> tools/manager_guide/capture.py --base http://127.0.0.1:8765 --data /tmp/guide

Ожидает, что стенд (stand.py) уже запущен и записал в --data файлы bot.json, stand.json, qr.png.
Результат — docs/manager-guide.pdf. Порядок целиком — в build.sh.
"""
import argparse
import base64
import html
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from content import build_html

ROOT = Path(__file__).resolve().parents[2]
PHONE = {"width": 390, "height": 844}


# --- Экраны бота: макет чата Telegram из настоящих текстов и кнопок --------------------------

def _demo_links(text: str) -> str:
    """Тестовые домены стенда → вид боевых ссылок (секреты не настоящие)."""
    text = text.replace("https://example.com", "https://flaskvpn.ru")
    text = re.sub(r"https://sub\.example/[^\s<]+", "https://sub.flaskvpn.ru/Xk3fQ9vLm2…", text)
    text = re.sub(r"https://pay\.example/[^\s<]+", "https://yoomoney.ru/checkout/payments/v2/contract?orderId=2f1c…", text)
    text = re.sub(r"(https://flaskvpn\.ru/c/)[\w-]+", r"\1Qm7Zt1yW…", text)
    text = re.sub(r"(https://flaskvpn\.ru/manager/password\?t=)[\w-]+", r"\1h3Kd9…", text)
    return text


BOT_CSS = """
body { margin: 0; background: transparent; font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }
.chat { width: 390px; padding: 14px 10px 16px; box-sizing: border-box; border-radius: 0;
        background: #cfe0c4 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='40'%3E%3Ccircle cx='20' cy='20' r='1.6' fill='%23b7cba9'/%3E%3C/svg%3E"); }
.head { display: flex; align-items: center; gap: 10px; margin: -14px -10px 12px; padding: 10px 14px;
        background: #fff; border-bottom: 1px solid #e5e5e5; }
.ava { width: 34px; height: 34px; border-radius: 50%; background: #2457C5; color: #fff; font-weight: 800;
       display: flex; align-items: center; justify-content: center; font-size: 15px; }
.name { font-weight: 700; font-size: 15px; } .sub { font-size: 12px; color: #8a8a8a; }
.msg { max-width: 330px; margin: 0 0 6px; }
.bubble { background: #fff; border-radius: 14px 14px 14px 4px; padding: 8px 11px 9px; font-size: 14.5px;
          line-height: 1.38; color: #111; box-shadow: 0 1px 1px rgba(0,0,0,.08); white-space: pre-wrap; word-wrap: break-word; }
.photo { border-radius: 14px 14px 0 0; overflow: hidden; background: #fff; padding: 12px 0 4px; }
.photo img { display: block; width: 190px; margin: 0 auto; }
.photo.full { padding: 0; } .photo.full img { width: 100%; }
.photo + .bubble { border-radius: 0 0 14px 4px; }
.user { margin-left: auto; } .user .bubble { background: #e1fec6; border-radius: 14px 14px 4px 14px; }
.kb { display: grid; gap: 4px; margin-top: 4px; }
.row { display: flex; gap: 4px; }
.btn { flex: 1; background: rgba(48, 74, 50, .32); color: #fff; font-size: 13.5px; font-weight: 600; text-align: center;
       padding: 9px 6px; border-radius: 9px; text-shadow: 0 1px 1px rgba(0,0,0,.15); }
code { font-family: Menlo, monospace; font-size: 13px; color: #b5373a; }
"""


def render_bot(screen: list[dict], qr_uri: str, data: Path) -> str:
    parts = ['<div class="chat"><div class="head"><div class="ava">F</div>'
             '<div><div class="name">FlaskVPN</div><div class="sub">бот</div></div></div>']
    for m in screen:
        if m["from"] == "user":
            parts.append(f'<div class="msg user"><div class="bubble">{html.escape(m["text"])}</div></div>')
            continue
        text = _demo_links(m["text"])
        photo = ""
        if m["kind"] == "photo":
            image = m.get("image")
            if image:   # настоящая картинка сообщения (чек во всю ширину пузыря)
                uri = "data:image/png;base64," + base64.b64encode((data / image).read_bytes()).decode()
                photo = f'<div class="photo full"><img src="{uri}"></div>'
                # Подпись чека повторяет картинку — в макете хватит первых строк.
                lines = text.split("\n")
                if len(lines) > 3:
                    text = "\n".join(lines[:3]) + "\n…"
            else:
                photo = f'<div class="photo"><img src="{qr_uri}"></div>'
        kb = ""
        if m["buttons"]:
            rows = "".join('<div class="row">' + "".join(f'<div class="btn">{html.escape(b)}</div>' for b in row)
                           + "</div>" for row in m["buttons"])
            kb = f'<div class="kb">{rows}</div>'
        parts.append(f'<div class="msg">{photo}<div class="bubble">{text}</div>{kb}</div>')
    parts.append("</div>")
    return f"<html><head><meta charset='utf-8'><style>{BOT_CSS}</style></head><body>{''.join(parts)}</body></html>"


def shoot_bot(browser, data: Path, img: Path) -> None:
    screens = json.loads((data / "bot.json").read_text(encoding="utf-8"))
    # data: URI — страница из set_content не может грузить file://
    qr_uri = "data:image/png;base64," + base64.b64encode((data / "qr.png").read_bytes()).decode()
    page = browser.new_page(device_scale_factor=2, viewport={"width": 420, "height": 900})
    for name, screen in screens.items():
        page.set_content(render_bot(screen, qr_uri, data))
        page.wait_for_load_state("load")
        page.locator(".chat").screenshot(path=str(img / f"bot_{name}.jpg"), type="jpeg", quality=82)
    page.close()


# --- Сайт ---------------------------------------------------------------------------------

def shoot_web(browser, base: str, stand: dict, img: Path) -> None:
    def shot(page, name, **kw):
        page.wait_for_timeout(350)
        page.screenshot(path=str(img / f"web_{name}.jpg"), type="jpeg", quality=82, **kw)

    def element(locator, name):
        # Фрагмент страницы — без нижней панели вкладок, она закрыла бы низ фрагмента.
        page = locator.page
        page.add_style_tag(content=".tabbar{display:none!important}")
        locator.screenshot(path=str(img / f"web_{name}.jpg"), type="jpeg", quality=82)

    ctx = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True, locale="ru-RU")
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{base}/manager/login")
    shot(page, "login")
    page.fill("#mgrLogin", stand["login"])
    page.fill("#mgrPassword", stand["password"])
    page.check("input[name=remember]")
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/manager/")
    shot(page, "dashboard")

    # Продажа: тариф → устройства и трафик → оплата → наличные → «Покажите клиенту»
    page.goto(f"{base}/manager/issue?tariff=2")
    page.wait_for_selector("#previewActions:not(.hidden)")
    page.fill("#labelInput", "Мария, пекарня у вокзала")
    shot(page, "issue_top")
    page.click('[data-step="slots"][data-delta="1"]'); page.wait_for_timeout(500)
    page.click('[data-step="packs"][data-delta="1"]'); page.wait_for_timeout(500)
    page.locator("#extrasBox").scroll_into_view_if_needed()
    page.evaluate("document.getElementById('extrasBox').scrollIntoView({block: 'start'}); window.scrollBy(0, -90)")
    shot(page, "issue_extras")
    element(page.locator("#previewBox"), "issue_pay")
    page.click("#payCashBtn")
    page.wait_for_selector("#showClient:not(.hidden)")
    page.wait_for_timeout(700)
    shot(page, "show_install")
    page.locator("#showClientTabs button").nth(1).click()
    page.wait_for_timeout(700)
    shot(page, "show_cabinet")
    page.click("[data-close-show]")
    shot(page, "issue_done")

    # Вкладки «Кому»: по коду
    page.goto(f"{base}/manager/issue")
    page.wait_for_selector("#previewActions:not(.hidden)")
    page.click("label.choice:has(input[value=code])")
    element(page.locator("section").first, "who_code")

    # Любой срок
    page.goto(f"{base}/manager/issue?custom=1")
    page.wait_for_selector("#productCustom:not(.hidden)")
    page.click('[data-days="45"]')
    page.wait_for_timeout(700)
    page.evaluate("document.getElementById('productCustom').scrollIntoView({block: 'start'}); window.scrollBy(0, -90)")
    shot(page, "custom")

    # Оплата по QR: ждём оплату
    page.goto(f"{base}/manager/issue?tariff=3")
    page.wait_for_selector("#previewActions:not(.hidden)")
    page.click("#payOnlineBtn")
    page.wait_for_selector("#resultBox:not(.hidden) img[src^='data:']")
    page.wait_for_timeout(500)
    shot(page, "invoice")

    # Клиенты
    page.goto(f"{base}/manager/clients")
    shot(page, "clients")
    page.goto(f"{base}/manager/clients/{stand['client_code']}")
    shot(page, "client_card")
    page.evaluate("document.querySelector('details').open = true; window.scrollTo(0, document.body.scrollHeight)")
    shot(page, "client_more")

    # Пробный ключ
    page.goto(f"{base}/manager/temp")
    shot(page, "temp")
    page.click("#issueTempBtn")
    page.wait_for_selector("#showClient:not(.hidden)")
    page.click("[data-close-show]")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
        const link = document.getElementById('tempLink');
        if (link) link.value = 'https://sub.flaskvpn.ru/Xk3fQ9vLm2…';   // тестовый адрес стенда → вид боевого
    }""")
    shot(page, "temp_done")
    page.reload()
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    shot(page, "temp_list")

    for path, name in (("/manager/help", "help"), ("/manager/history", "history"), ("/manager/account", "account")):
        page.goto(f"{base}{path}")
        shot(page, name)

    # Компьютер
    state = ctx.storage_state()
    desk = browser.new_context(viewport={"width": 1280, "height": 820}, device_scale_factor=1.5, storage_state=state)
    dpage = desk.new_page()
    dpage.goto(f"{base}/manager/")
    dpage.wait_for_timeout(400)
    dpage.screenshot(path=str(img / "web_desktop.jpg"), type="jpeg", quality=82)
    desk.close()

    # Страница «Задать пароль» — без входа
    anon = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
    apage = anon.new_page()
    apage.goto(f"{base}{stand['password_link']}")
    apage.wait_for_timeout(400)
    apage.screenshot(path=str(img / "web_password.jpg"), type="jpeg", quality=82)
    anon.close()
    ctx.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "manager-guide.pdf")
    args = parser.parse_args()
    img = args.data / "img"
    img.mkdir(parents=True, exist_ok=True)
    stand = json.loads((args.data / "stand.json").read_text(encoding="utf-8"))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        shoot_bot(browser, args.data, img)
        shoot_web(browser, args.base, stand, img)

        guide = args.data / "guide.html"
        guide.write_text(build_html(img), encoding="utf-8")
        page = browser.new_page()
        page.goto(guide.resolve().as_uri())
        page.wait_for_load_state("networkidle")
        page.pdf(
            path=str(args.out), format="A4", print_background=True,
            margin={"top": "16mm", "bottom": "18mm", "left": "15mm", "right": "15mm"},
            display_header_footer=True, header_template="<span></span>",
            footer_template=(
                "<div style='width:100%;font-size:8px;color:#8a93a8;padding:0 15mm;display:flex;"
                "justify-content:space-between;font-family:sans-serif'><span>FlaskVPN · Руководство менеджера</span>"
                "<span><span class='pageNumber'></span> / <span class='totalPages'></span></span></div>"
            ),
        )
        browser.close()
    print(f"PDF: {args.out}")


if __name__ == "__main__":
    main()
