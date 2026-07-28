"""vibecheck: проверка запросов к Agent API «Вайб-Маркетолог» по их же каталогу.

Две команды:
  validate  проверить тело запроса до отправки, по правилам из GET /capabilities
  audit     сверить каталог с прайсом (без токена); опционально со сметой (нужен токен)

Валидатор не знает ни одной модели: все правила берутся из каталога, который
платформа отдаёт наружу. Новая модель в каталоге даёт новые правила без правки кода.
Исключение: имена полей-списков перечислены ниже в LIST_PARAMS.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

try:  # системное хранилище сертификатов вместо бандла из дистрибутива
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # ponytail: без truststore тоже поедет, если бандл свежий
    pass

BASE = os.environ.get("VIBE_API_BASE", "https://lk.vibemarketolog.ru/api/agent")

# поля, которые принимает любая модель: в каталоге они не перечислены
COMMON_PARAMS = {"type", "model", "prompt", "callback_url", "strict", "idempotency_key"}

# поля-списки: каталог задаёт для них максимум элементов, а не максимум значения
LIST_PARAMS = {
    "image_urls", "image_input", "reference_image_urls", "reference_video_urls",
    "reference_audio_urls", "character_ids", "audio_ids", "descriptions", "video_list",
}


class ApiError(RuntimeError):
    pass


def call(path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(f"{e.code}: {raw[:200]}") from None


# --------------------------------------------------------------------------- #
# правила из каталога
# --------------------------------------------------------------------------- #

@dataclass
class Problem:
    code: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.field}: {self.message}"


@dataclass
class ModelRules:
    """Правила одной модели, вычитанные из каталога. Кода про конкретные модели нет."""
    name: str
    type: str
    price: float | None = None
    price_varies: str | None = None   # tier_prices / price_by_quality / price_formula
    required: set[str] = field(default_factory=set)
    allowed: set[str] = field(default_factory=set)
    enums: dict[str, list] = field(default_factory=dict)
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    max_len: dict[str, int] = field(default_factory=dict)
    max_items: dict[str, int] = field(default_factory=dict)
    exclusive: list[list[str]] = field(default_factory=list)
    hint: str | None = None


_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*([a-zа-я]*)\s*$", re.I)


def _parse_limits(spec: dict, rules: ModelRules) -> None:
    """limits в каталоге записаны тремя способами, разбираем каждый по-своему."""
    for key, val in spec.get("limits", {}).items():
        if key == "mutually_exclusive":
            # группы = взаимоисключающие сценарии: внутри группы поля совместимы
            # (first_frame_url + last_frame_url), между группами нет.
            # Семантика выведена из hint модели: документация её не описывает.
            rules.exclusive = [list(g) for g in val]
        elif key.endswith("_max") and isinstance(val, (int, float)):
            rules.max_len[key[:-4]] = int(val)          # prompt_max → длина prompt
        elif isinstance(val, str) and (m := _RANGE.match(val)):
            rules.ranges[key] = (float(m.group(1)), float(m.group(2)))
        elif isinstance(val, (int, float)) and key in LIST_PARAMS:
            rules.max_items[key] = int(val)             # число элементов списка
        elif isinstance(val, (int, float)):
            rules.ranges[key] = (0, float(val))


def build_rules(catalog: dict) -> dict[str, ModelRules]:
    out: dict[str, ModelRules] = {}
    for typ, models in catalog.get("models", {}).items():
        for name, spec in models.items():
            varies = next((k for k in ("tier_prices", "price_by_quality", "price_formula")
                           if k in spec), None)
            r = ModelRules(name=name, type=typ, price=spec.get("price"),
                           price_varies=varies, hint=spec.get("hint"))
            r.required = set(spec.get("required", []))
            r.allowed = COMMON_PARAMS | r.required | set(spec.get("optional", []))
            r.enums = dict(spec.get("enums", {}))
            _parse_limits(spec, r)
            out[name] = r
    return out


# --------------------------------------------------------------------------- #
# валидация
# --------------------------------------------------------------------------- #

def validate(body: dict, rules: dict[str, ModelRules]) -> list[Problem]:
    problems: list[Problem] = []
    model = body.get("model")
    if not model:
        return [Problem("missing_model", "model", "поле обязательно")]
    if model not in rules:
        return [Problem("unknown_model", "model",
                        f"нет в каталоге; ближайшие: {', '.join(_similar(model, rules))}")]
    r = rules[model]

    if body.get("type") and body["type"] != r.type:
        problems.append(Problem("type_mismatch", "type",
                                f"каталог относит {model} к типу «{r.type}», передан «{body['type']}»"))

    for name in sorted(r.required - set(body)):
        problems.append(Problem("required_missing", name, "обязательное поле не передано"))

    for name in sorted(set(body) - r.allowed):
        problems.append(Problem("unknown_param", name,
                                f"модель {model} такого поля не принимает"))

    for name, value in body.items():
        if name not in r.allowed:
            continue
        if (allowed := r.enums.get(name)) and value not in allowed:
            problems.append(Problem("enum_violation", name,
                                    f"«{value}» вне допустимых: {', '.join(map(str, allowed))}"))
        if (rng := r.ranges.get(name)) and isinstance(value, (int, float)):
            lo, hi = rng
            if not lo <= value <= hi:
                problems.append(Problem("range_violation", name,
                                        f"{value} вне диапазона {lo:g}-{hi:g}"))
        if (cap := r.max_len.get(name)) and isinstance(value, str) and len(value) > cap:
            problems.append(Problem("too_long", name, f"{len(value)} символов при пределе {cap}"))
        if (cap := r.max_items.get(name)) and isinstance(value, list) and len(value) > cap:
            problems.append(Problem("too_many", name, f"{len(value)} элементов при пределе {cap}"))
        if name in LIST_PARAMS and isinstance(value, str):
            problems.append(Problem("wrong_shape", name, "ожидается список, передана строка"))

    used = [(g, [f for f in g if f in body]) for g in r.exclusive]
    engaged = [(g, present) for g, present in used if present]
    if len(engaged) > 1:
        names = " и ".join("/".join(present) for _, present in engaged)
        problems.append(Problem("mutually_exclusive", names,
                                "это разные сценарии: в одном запросе допустим только один"))
    return problems


def _similar(name: str, rules: dict) -> list[str]:
    stem = re.split(r"[-_]", name)[0]
    return [m for m in rules if m.startswith(stem)][:3] or list(rules)[:3]


def price_hint(body: dict, rules: dict[str, ModelRules]) -> str:
    """Цена по каталогу. Для моделей с переменной ценой каталог сам велит идти в смету."""
    r = rules.get(body.get("model", ""))
    if r is None:
        return ""
    if r.price_varies:
        return (f"цена зависит от параметров (в каталоге {r.price_varies}); "
                f"точную даёт POST /generate/estimate")
    return f"ориентировочно {r.price} ₽" if r.price is not None else ""


# --------------------------------------------------------------------------- #
# сверка публичных описаний
# --------------------------------------------------------------------------- #

def audit_catalog_vs_prices(catalog: dict, prices: dict) -> list[Problem]:
    """Каталог и прайс: два публичных источника правды. Сверяем состав и типы."""
    problems = []
    cat = {m: t for t, ms in catalog.get("models", {}).items() for m in ms}
    pri = {m: t for t, ms in prices.get("prices", {}).items() for m in ms}

    specs = {m: s for ms in catalog.get("models", {}).values() for m, s in ms.items()}
    for m in sorted(set(cat) - set(pri)):
        how = ("формулой price_formula" if "price_formula" in specs[m]
               else f"{specs[m].get('price')} ₽")
        problems.append(Problem("price_missing", m,
                                f"в каталоге цена есть ({how}), в /prices модели нет"))
    for m in sorted(set(pri) - set(cat)):
        problems.append(Problem("catalog_missing", m,
                                "есть в /prices, в каталоге нет: параметры узнать неоткуда"))
    for m in sorted(set(cat) & set(pri)):
        if cat[m] != pri[m]:
            problems.append(Problem("type_conflict", m,
                                    f"каталог: {cat[m]}, прайс: {pri[m]}; какой слать в поле type?"))
    for m, spec in ((m, s) for ms in catalog.get("models", {}).values() for m, s in ms.items()):
        if m in pri and (p := spec.get("price")) is not None:
            listed = prices["prices"][pri[m]][m]
            if listed != p:
                problems.append(Problem("price_conflict", m,
                                        f"каталог: {p} ₽, прайс: {listed} ₽"))
    declared = set(catalog.get("types", []))
    for t in sorted(declared - set(catalog.get("models", {}))):
        problems.append(Problem("empty_type", t, "тип объявлен, но моделей этого типа в каталоге нет"))
    return problems


def audit_against_estimate(rules: dict[str, ModelRules], token: str,
                           models: list[str] | None = None) -> list[Problem]:
    """Сверка каталога с ответом бесплатной сметы. Денег не тратит."""
    import time

    problems = []
    for name in models or list(rules):
        r = rules[name]
        resp = call("/generate/estimate",
                    {"type": r.type, "model": name, "prompt": "проверка сметы"}, token)
        time.sleep(1.1)  # ponytail: лимит 60 запросов/мин; убрать, если появится /limits
        if "valid_params" not in resp:
            problems.append(Problem("estimate_failed", name, str(resp)[:120]))
            continue
        server = set(resp["valid_params"])
        if extra := server - r.allowed:
            problems.append(Problem("param_undocumented", name,
                                    f"смета принимает, каталог не публикует: {', '.join(sorted(extra))}"))
        if missing := r.allowed - server:
            problems.append(Problem("param_phantom", name,
                                    f"каталог публикует, смета не принимает: {', '.join(sorted(missing))}"))
        cost = resp.get("estimated_cost_rub")
        if cost is not None and r.price is not None and cost != r.price and not r.price_varies:
            problems.append(Problem("price_drift", name,
                                    f"каталог: {r.price} ₽, смета: {cost} ₽"))
    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_catalog(path: str | None = None) -> dict:
    if path:
        return json.load(open(path, encoding="utf-8"))
    return call("/capabilities")


def _token() -> str:
    tok = os.environ.get("SPA_ACCESS_TOKEN")
    if not tok and os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            if line.startswith("SPA_ACCESS_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        sys.exit("нужен SPA_ACCESS_TOKEN: в окружении или в .env")
    return tok


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd == "validate":
        body = json.load(open(rest[0], encoding="utf-8")) if rest else json.load(sys.stdin)
        rules = build_rules(load_catalog(os.environ.get("VIBE_CATALOG")))
        problems = validate(body, rules)
        if not problems:
            hint = price_hint(body, rules)
            print(f"замечаний нет; {hint}" if hint else "замечаний нет")
            return 0
        print(f"замечаний: {len(problems)}, запрос отправлять нельзя")
        for p in problems:
            print(" ", p)
        return 1

    if cmd == "audit":
        catalog = load_catalog(os.environ.get("VIBE_CATALOG"))
        prices = call("/prices")
        problems = audit_catalog_vs_prices(catalog, prices)
        print(f"=== каталог против прайса: {len(problems)} расхождений ===")
        for p in problems:
            print(" ", p)
        if "--with-estimate" in rest:
            rules = build_rules(catalog)
            live = audit_against_estimate(rules, _token())
            print(f"\n=== каталог против сметы: {len(live)} расхождений ===")
            for p in live:
                print(" ", p)
        return 0

    sys.exit(f"неизвестная команда: {cmd}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
