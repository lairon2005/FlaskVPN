# webapp/templating.py
"""Единственный на приложение Jinja2Templates.

Раньше каждый роутер создавал свой экземпляр, и фильтр `timestamp_to_date`
приходилось регистрировать дважды — в main.py и в dashboard.py. Любой новый
глобал (ссылки сайта, реквизиты, год) пришлось бы дублировать в четырёх
местах, а забытая регистрация падала бы только на конкретной странице.
"""
import inspect
import re
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from webapp.core.site import load_site_info

templates = Jinja2Templates(directory="webapp/templates")


def timestamp_to_date(value):
    """Unix-время из панели → строка для шаблона."""
    if value:
        try:
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            return "Err"
    return "Неограниченно"


templates.env.filters['timestamp_to_date'] = timestamp_to_date


def bytes_to_gb(value):
    """Байты из панели → число ГБ (гибибайты, как везде в проекте) с одним знаком."""
    try:
        gb = float(value or 0) / (1024 ** 3)
    except (TypeError, ValueError):
        return 0
    return round(gb, 1) if gb < 100 else round(gb)


templates.env.filters['bytes_to_gb'] = bytes_to_gb

# Ссылки, канонический адрес и реквизиты — из .env, в рантайме не меняются.
templates.env.globals['site'] = load_site_info()
# Год вызывается на рендере, а не фиксируется при импорте: контейнер живёт
# месяцами и после Нового года показывал бы в подвале прошлый год.
templates.env.globals['now_year'] = lambda: datetime.now().year

_STATIC_ROOT = Path("webapp/static")


def has_static(rel_path: str) -> bool:
    """Есть ли файл в webapp/static. Нужен коллажу на лендинге: картинки
    бренда докладываются отдельно, и до их появления шаблон не должен
    рисовать битые <img>. Проверка на рендере, а не на старте — статику
    заливают через `docker cp` без перезапуска контейнера."""
    return (_STATIC_ROOT / rel_path.lstrip("/")).is_file()


templates.env.globals['has_static'] = has_static


def static_url(rel_path: str) -> str:
    """
    /static/<путь>?v=<время изменения>. Статика отдаётся без Cache-Control, и браузер
    кэширует её эвристически: после релиза менеджер получал новую страницу со старым JS.
    Версия в адресе меняется вместе с файлом — кэш сбрасывается сам.
    """
    rel = rel_path.lstrip("/")
    try:
        version = int((_STATIC_ROOT / rel).stat().st_mtime)
    except OSError:
        return f"/static/{rel}"
    return f"/static/{rel}?v={version}"


templates.env.globals['static_url'] = static_url

# Эмодзи в общих с ботом текстах (памятка менеджера, названия разделов): на сайте вместо
# них — SVG-иконки, сами символы убираем. Диапазоны: пиктограммы, символы, стрелки-часы, VS16, ZWJ.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D]")


def no_emoji(value) -> str:
    text = _EMOJI.sub("", str(value or ""))
    text = re.sub(r"«\s+", "«", text)
    return re.sub(r"\s{2,}", " ", text).strip()


templates.env.filters['no_emoji'] = no_emoji


# Сигнатура TemplateResponse менялась: раньше (name, context), с Starlette 0.29 —
# (request, name, context), а в новых версиях старая форма удалена совсем. Старый
# вызов встречается по всему проекту; новый код зовёт render(), который работает
# на любой версии.
_NEW_STYLE = next(iter(list(inspect.signature(Jinja2Templates.TemplateResponse).parameters)[1:2]), "") == "request"


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"request": request, **(context or {})}
    if _NEW_STYLE:
        response = templates.TemplateResponse(request, name, ctx)
    else:
        response = templates.TemplateResponse(name, ctx)
    response.status_code = status_code
    return response
