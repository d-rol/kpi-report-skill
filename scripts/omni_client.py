"""Тонкий клиент Omnidesk API: авторизация из .env, пагинация, безопасные повторы.

Только стандартная библиотека (urllib) — чтобы не тянуть зависимости.
Все времена Omnidesk отдаёт по-разному: лидерборд — в секундах,
детальный тикет (first_response_speed) — в минутах. Учитывать на месте.
"""
import os
import json
import time
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse


class OmniClient:
    # Omnidesk сам сообщает лимит в заголовках ответа:
    #   rate_limit_per_minute — потолок запросов/мин на весь аккаунт;
    #   api_calls_left        — сколько осталось в текущем окне;
    #   retry_after           — через сколько секунд окно сбросится (на 429).
    # Поэтому не угадываем интервал, а держим темп по факту из заголовков.
    # Пример: у аккаунта на Базовом тарифе (2-3 сотрудника) лимит обычно 20/мин.
    def __init__(self, env_path=None, safety=1.15, cache=False, cache_dir=None):
        cfg = _load_env(env_path)
        self.email = cfg["EMAIL"]
        self.token = cfg["OMNIDESK_API"]
        self.subdomain = cfg["SUBDOMAIN"]
        self.base = f"https://{self.subdomain}.omnidesk.ru/api"
        auth = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        self._auth_header = f"Basic {auth}"
        self.safety = safety
        self.rate_limit = 20            # уточняется из первого ответа
        self.min_interval = 60.0 / self.rate_limit * safety
        self._last_request = 0.0
        # Дисковый кэш GET-ответов: тесты гоняются по одним и тем же тикетам,
        # незачем каждый раз дёргать rate-limited API. Только для чтения (GET).
        # Ключ = хэш от path+params, значение — JSON ответа. Освежить = удалить
        # файл/папку cache или создать клиент с cache=False.
        self.cache = cache
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        self.cache_dir = cache_dir

    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _apply_headers(self, headers):
        limit = headers.get("rate_limit_per_minute")
        if limit:
            try:
                self.rate_limit = int(limit)
                self.min_interval = 60.0 / max(1, self.rate_limit) * self.safety
            except ValueError:
                pass
        left = headers.get("api_calls_left")
        retry = headers.get("retry_after")
        # Почти исчерпали окно — подождём его сброса, чтобы не ловить 429.
        if left is not None:
            try:
                if int(left) <= 1:
                    time.sleep(float(retry) if retry else self.min_interval * 2)
            except ValueError:
                pass

    def _cache_path(self, url):
        key = hashlib.sha1(url.encode()).hexdigest()
        return os.path.join(self.cache_dir, key + ".json")

    def get(self, path, params=None, retries=8):
        url = f"{self.base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cache_file = self._cache_path(url) if self.cache else None
        if cache_file and os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth_header)
        for attempt in range(retries):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    self._apply_headers(resp.headers)
                    if cache_file:
                        os.makedirs(self.cache_dir, exist_ok=True)
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False)
                    return data
            except urllib.error.HTTPError as e:
                # 429 — окно исчерпано; ждём retry_after из заголовка и повторяем.
                if e.code == 429 and attempt < retries - 1:
                    retry_after = e.headers.get("retry_after") or e.headers.get("Retry-After")
                    try:
                        pause = float(retry_after)
                    except (TypeError, ValueError):
                        pause = self.min_interval * (attempt + 2)
                    time.sleep(pause)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                    continue
                raise

    def iter_cases(self, from_time=None, to_time=None, status=None,
                   show_first_response_time=False, sort="created_at_asc"):
        """Итерирует тикеты за период (по created_at), листая страницы по 100.

        from_time/to_time — строки 'YYYY-MM-DD HH:MM:SS' (МСК) либо unix ts.
        show_first_response_time=True добавляет в каждый тикет поле
        first_response_speed (минуты) прямо в списке — избавляет от запроса
        деталей по каждому тикету (см. документацию, параметр списка обращений).
        Отдаёт по одному словарю case за раз.
        """
        page = 1
        while True:
            params = {"limit": 100, "page": page, "sort": sort}
            if from_time is not None:
                params["from_time"] = from_time
            if to_time is not None:
                params["to_time"] = to_time
            if status is not None:
                params["status"] = status
            if show_first_response_time:
                params["show_first_response_time"] = "true"
            data = self.get("cases.json", params)
            batch = [data[k]["case"] for k in data if k.isdigit()]
            if not batch:
                break
            for case in batch:
                yield case
            if len(batch) < 100:
                break
            page += 1

    def case_detail(self, case_id, pause=0.0):
        """Детальный тикет — содержит first_response_speed (в минутах), в отличие от списка."""
        data = self.get(f"cases/{case_id}.json")
        if pause:
            time.sleep(pause)
        return data.get("case", data)

    def custom_fields_map(self):
        """field_id (строкой) -> описание кастомного поля Омнидеска.

        Нужен, чтобы превратить сырое значение выпадающего списка (в обращении
        лежит КЛЮЧ варианта, например "3") в читаемое название темы. Устроен не
        как справочники групп и меток: id лежит в `field_id`, варианты — в
        `field_data`, уровень поля (обращение или клиент) — в `field_level`.
        """
        out = {}
        page = 1
        while True:
            data = self.get("custom_fields.json", {"page": page, "limit": 100})
            batch = [k for k in data if str(k).isdigit()]
            for k in batch:
                f = data[k].get("custom_field")
                if f:
                    out[str(f.get("field_id"))] = f
            if len(batch) < 100:
                break
            page += 1
        return out

    def staff_map(self):
        """staff_id -> имя, из лидерборда статистики (за 7 дней достаточно для маппинга)."""
        data = self.get("stats_leaderboard.json", {"period": "last_7_days"})
        out = {}
        for k in data:
            if k.isdigit():
                s = data[k]["staff"]
                out[s["staff_id"]] = s["staff_name"]
        return out


def _load_env(env_path=None):
    if env_path is None:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    cfg = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg
