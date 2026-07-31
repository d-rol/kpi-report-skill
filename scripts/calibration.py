"""Этап 2, п.2: калибровочная грация для новых продуктов/направлений.

Зачем. Когда запускается новое направление (новая группа в Омнидеске — новый
продукт или новый канал), первые недели метрики по нему систематически хуже:
операторы не знают типовых вопросов, нет шаблонов, база знаний пустая, поток
непредсказуем. Штрафовать за это — штрафовать за сам факт запуска. Поэтому
обращения из «молодой» группы считаются отдельно и не идут в строгий зачёт
первые `grace_weeks` недель.

Как определяется «молодая». В плане грация задумывалась ручным флагом, но
`groups.json` отдаёт `created_at` у каждой группы — возраст можно взять из
данных, а не с чьих-то слов. Поэтому:

  1. авто-детект по `created_at` группы (основной путь, ничего не надо вести
     руками — новая группа подхватится сама в день создания);
  2. `calibration.json` — ручные переопределения поверх авто-детекта, когда
     бизнес знает то, чего не знает API (например, направление формально новое,
     но это переезд старого потока в отдельную группу — грация не нужна).

Переопределение всегда с датой и автором решения: грация money-adjacent,
через полгода должно быть видно, кто и когда решил не давать её конкретной
группе.

Важно: грация НЕ вычитает нарушения из личного зачёта задним числом. Она
показывает их отдельной строкой — «вот столько нарушений пришло из молодых
направлений». Решение, что с ними делать, остаётся за руководителем; правила
pass/fail в этом проекте сознательно нет.

Запуск (посмотреть, какие группы сейчас в грации):
  python calibration.py --cache
  python calibration.py --at "2026-07-18 23:59:59" --cache --json
"""
import os
import sys
import json
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime

from omni_client import OmniClient

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

MSK = dt.timezone(dt.timedelta(hours=3))
FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_GRACE_WEEKS = 4
CONFIG_NAME = "calibration.json"


def config_path(path=None):
    return path or os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def load_config(path=None):
    """Читает calibration.json. Отсутствие файла — не ошибка: значит правим
    только авто-детектом, с настройками по умолчанию."""
    p = config_path(path)
    if not os.path.exists(p):
        return {"grace_weeks": DEFAULT_GRACE_WEEKS, "auto_detect": True, "overrides": {}}
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("grace_weeks", DEFAULT_GRACE_WEEKS)
    cfg.setdefault("auto_detect", True)
    cfg.setdefault("overrides", {})
    return cfg


def _parse_created(value):
    """created_at группы — RFC 2822, как и у обращений."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(MSK)
    except (TypeError, ValueError):
        return None


def groups(client):
    """{group_id: {title, created_at}} из groups.json."""
    raw = client.get("groups.json") or {}
    out = {}
    for item in raw.values():
        g = item.get("group") if isinstance(item, dict) else None
        if not g:
            continue
        out[int(g["group_id"])] = {
            "title": g.get("group_title") or f"группа {g['group_id']}",
            "created_at": _parse_created(g.get("created_at")),
            "active": g.get("active", True),
        }
    return out


def grace_status(client, at=None, cfg=None):
    """Кто сейчас в грации. `at` — момент оценки (конец отчётного периода).

    Возраст группы считается на КОНЕЦ периода отчёта, а не на «сейчас»: отчёт
    за июль, собранный в сентябре, должен видеть июльскую картину, иначе
    грация задним числом исчезнет и старый отчёт перестанет воспроизводиться.
    """
    cfg = cfg if cfg is not None else load_config()
    at = at or dt.datetime.now(MSK)
    if at.tzinfo is None:
        at = at.replace(tzinfo=MSK)
    weeks = cfg.get("grace_weeks", DEFAULT_GRACE_WEEKS)
    horizon = dt.timedelta(weeks=weeks)
    overrides = cfg.get("overrides", {})
    auto = cfg.get("auto_detect", True)

    out = {}
    for gid, g in groups(client).items():
        created = g["created_at"]
        age_days = round((at - created).total_seconds() / 86400, 1) if created else None
        in_grace = bool(auto and created and (at - created) < horizon)
        source = "auto"
        note = None
        ov = overrides.get(str(gid)) or overrides.get(gid)
        if ov is not None:
            # Ручное решение бьёт авто-детект — и в ту, и в другую сторону.
            in_grace = bool(ov.get("in_grace", False))
            source = "override"
            decided = ov.get("decided")
            by = ov.get("by")
            note = ov.get("note")
            if note and (decided or by):
                note = f"{note} (решение {by or '?'}, {decided or 'без даты'})"
        out[gid] = {
            "group_id": gid,
            "title": g["title"],
            "created_at": created.strftime(FMT) if created else None,
            "age_days": age_days,
            "in_grace": in_grace,
            "source": source,
            "note": note,
            # Сколько ещё осталось грации — чтобы руководитель видел, когда
            # направление выйдет в обычный зачёт, и не гадал.
            "grace_until": ((created + horizon).strftime(FMT)
                            if created and in_grace and source == "auto" else None),
        }
    return {"grace_weeks": weeks, "auto_detect": auto, "at": at.strftime(FMT), "groups": out}


def graced_group_ids(status):
    return {gid for gid, g in status["groups"].items() if g["in_grace"]}


def split_cases(cases, status, key="group_id"):
    """Делит список размеченных обращений на (обычные, из молодых направлений)."""
    graced = graced_group_ids(status)
    normal, in_grace = [], []
    for c in cases:
        (in_grace if c.get(key) in graced else normal).append(c)
    return normal, in_grace


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", help="момент оценки 'YYYY-MM-DD HH:MM:SS' (МСК); по умолчанию сейчас")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--config", help="путь к calibration.json")
    args = ap.parse_args()

    at = dt.datetime.strptime(args.at, FMT).replace(tzinfo=MSK) if args.at else None
    client = OmniClient(cache=args.cache)
    status = grace_status(client, at=at, cfg=load_config(args.config))

    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return

    print(f"\nКалибровочная грация на {status['at']} "
          f"(окно {status['grace_weeks']} нед, авто-детект: "
          f"{'вкл' if status['auto_detect'] else 'выкл'})\n")
    rows = sorted(status["groups"].values(), key=lambda g: g["age_days"] or 1e9)
    width = max((len(g["title"]) for g in rows), default=10)
    for g in rows:
        mark = "ГРАЦИЯ" if g["in_grace"] else "  —   "
        age = f"{g['age_days']:.0f} дн" if g["age_days"] is not None else "?"
        line = f"  {mark}  {g['title']:<{width}}  {age:>7}  (создана {g['created_at']})"
        if g["source"] == "override":
            line += "  [ручное решение]"
        print(line)
        if g["note"]:
            print(f"          {g['note']}")
        if g["grace_until"]:
            print(f"          в обычный зачёт с {g['grace_until']}")
    n = len(graced_group_ids(status))
    print(f"\nВ грации сейчас: {n} из {len(rows)}.")
    if not n:
        print("Все направления в обычном зачёте.")


if __name__ == "__main__":
    main()
