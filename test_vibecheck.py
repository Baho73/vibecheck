"""Тесты валидатора. Офлайн: работают на сохранённом каталоге, сеть не нужна.

Каждый случай из группы «сервер это принимает» проверен на живом API 28.07.2026:
смета вернула valid: true и деньги списались бы. Валидатор ловит их до отправки.

Запуск: python test_vibecheck.py  (или pytest test_vibecheck.py)
"""
import json
import pathlib

from vibecheck import audit_catalog_vs_prices, build_rules, validate

FIX = pathlib.Path(__file__).parent / "fixtures"
CATALOG = json.load(open(FIX / "capabilities.json", encoding="utf-8"))
PRICES = json.load(open(FIX / "prices.json", encoding="utf-8"))
RULES = build_rules(CATALOG)


def codes(body):
    return {p.code for p in validate(body, RULES)}


# --- то, что сервер принимает и оплачивает, а валидатор ловит ----------------

def test_enum_violation_server_accepts_this():
    """seedance-2: каталог объявляет resolution 480p/720p/1080p. Смета приняла «4k»."""
    assert "enum_violation" in codes(
        {"type": "video", "model": "seedance-2", "prompt": "тест", "resolution": "4k"})


def test_range_violation_server_accepts_this():
    """seedance-2: каталог объявляет duration 4-15s. Смета приняла 30 — общий предел 60."""
    assert "range_violation" in codes(
        {"type": "video", "model": "seedance-2", "prompt": "тест", "duration": 30})


def test_mutually_exclusive_server_accepts_this():
    """Каталог объявляет пару взаимоисключающих полей. Смета не возражает."""
    assert "mutually_exclusive" in codes(
        {"type": "video", "model": "seedance-2", "prompt": "тест",
         "first_frame_url": "https://x/a.png",
         "reference_image_urls": ["https://x/b.png"]})


def test_type_mismatch_server_accepts_this():
    """gemini-omni-character: каталог относит к image, прайс — к text.
    Смета принимает оба варианта, то есть поле type ни с чем не сверяется."""
    assert "type_mismatch" in codes(
        {"type": "text", "model": "gemini-omni-character", "prompt": "кот",
         "image_urls": ["https://x/a.png"]})


def test_too_many_items():
    """reference_image_urls: каталог задаёт максимум 9."""
    body = {"type": "video", "model": "seedance-2", "prompt": "тест",
            "reference_image_urls": [f"https://x/{i}.png" for i in range(12)]}
    assert "too_many" in codes(body)


def test_prompt_too_long():
    assert "too_long" in codes(
        {"type": "image", "model": "z-image", "prompt": "а" * 20001})


# --- то, что сервер и так ловит, — валидатор должен ловить тоже -------------

def test_unknown_param():
    assert "unknown_param" in codes(
        {"type": "image", "model": "z-image", "prompt": "тест", "nonexistent": 1})


def test_required_missing():
    assert "required_missing" in codes(
        {"type": "video", "model": "motion-control-720p", "prompt": "тест"})


def test_unknown_model_from_price_list():
    """grok-ttv-10 есть в /prices, но в каталоге его нет — параметры взять неоткуда."""
    problems = validate({"type": "video", "model": "grok-ttv-10", "prompt": "тест"}, RULES)
    assert problems[0].code == "unknown_model"
    assert "grok-ttv" in problems[0].message  # подсказывает существующее имя


def test_list_param_as_string():
    assert "wrong_shape" in codes(
        {"type": "video", "model": "seedance-2", "prompt": "тест",
         "reference_image_urls": "https://x/a.png"})


# --- корректные запросы не должны давать ложных срабатываний ----------------

def test_valid_request_is_silent():
    assert validate({"type": "image", "model": "z-image", "prompt": "баннер кофейни",
                     "aspect_ratio": "1:1"}, RULES) == []


def test_valid_video_with_allowed_values():
    assert validate({"type": "video", "model": "seedance-2", "prompt": "город ночью",
                     "resolution": "1080p", "duration": 8, "aspect_ratio": "9:16"},
                    RULES) == []


def test_every_catalog_model_accepts_its_own_minimal_request():
    """Ни одна модель не должна ругаться на запрос из своих же обязательных полей."""
    for name, r in RULES.items():
        body = {"type": r.type, "model": name}
        body.update({f: "https://x/a.png" for f in r.required if f != "prompt"})
        if "prompt" in r.required:
            body["prompt"] = "тест"
        assert [p for p in validate(body, RULES)
                if p.code not in ("wrong_shape",)] == [], f"ложное срабатывание на {name}"


# --- сверка публичных описаний ---------------------------------------------

def test_audit_finds_known_discrepancies():
    problems = audit_catalog_vs_prices(CATALOG, PRICES)
    found = {p.code for p in problems}
    assert {"price_missing", "catalog_missing", "type_conflict", "empty_type"} <= found
    by_code = lambda c: {p.field for p in problems if p.code == c}
    assert "seedream-5-pro" in by_code("price_missing")
    assert "grok-ttv-10" in by_code("catalog_missing")
    assert "gemini-omni-character" in by_code("type_conflict")
    assert "text" in by_code("empty_type")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  ok   {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL {name}: {e}")
    print(f"\n{passed} прошло, {failed} упало")
    raise SystemExit(1 if failed else 0)
