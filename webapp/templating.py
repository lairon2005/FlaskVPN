# webapp/templating.py
"""Единственный на приложение Jinja2Templates.

Раньше каждый роутер создавал свой экземпляр, и фильтр `timestamp_to_date`
приходилось регистрировать дважды — в main.py и в dashboard.py. Любой новый
глобал (ссылки сайта, реквизиты, год) пришлось бы дублировать в четырёх
местах, а забытая регистрация падала бы только на конкретной странице.
"""
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
