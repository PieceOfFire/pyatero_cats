import random
import re
from collections import defaultdict
from datetime import timedelta

RARITY_WEIGHTS = {"COM": 60, "UCOM": 25, "RARE": 10, "EPIC": 4, "LEG": 1}
POINTS = {"COM": 1, "UCOM": 3, "RARE": 7, "EPIC": 20, "LEG": 50}
LABELS = {
    "COM": "⚪ Обычный",
    "UCOM": "🟢 Необычный",
    "RARE": "🔵 Редкий",
    "EPIC": "🟣 Эпический",
    "LEG": "🟠 Легендарный",
}


def choose_cat(cats, rng=None):
    """Preserve the legacy rarity draw and fallback among unowned cards."""
    rng = rng or random.SystemRandom()
    if not cats:
        raise ValueError("Каталог пуст")
    by_rarity = defaultdict(list)
    for cat in cats:
        by_rarity[cat["rarity"]].append(cat)
    rarity = rng.choices(list(RARITY_WEIGHTS), weights=list(RARITY_WEIGHTS.values()), k=1)[0]
    return rng.choice(by_rarity[rarity] or cats)


def validate_cat(cat_id: str, description: str, rarity: str) -> tuple[str, str, str]:
    cat_id, description, rarity = cat_id.strip(), description.strip(), rarity.strip().upper()
    if not cat_id or len(cat_id) > 100 or any(c.isspace() or c == "|" for c in cat_id):
        raise ValueError("ID: 1–100 символов без пробелов и |")
    if not description or len(description) > 700:
        raise ValueError("Описание: от 1 до 700 символов")
    if rarity not in POINTS:
        raise ValueError("Редкость: COM, UCOM, RARE, EPIC или LEG")
    return cat_id, description, rarity


def daily_terms(last_daily, streak: int, today, fixed_bonus: int, mode: str):
    if last_daily and last_daily >= today:
        return 0, streak
    streak = streak + 1 if last_daily == today - timedelta(days=1) else 1
    bonus = (1 if streak <= 3 else 2 if streak <= 6 else 3) if mode == "streak" else fixed_bonus
    return bonus, streak


def promo_code(raw: str) -> str:
    code = raw.strip().upper()
    if not re.fullmatch(r"[A-ZА-ЯЁ0-9_-]{1,64}", code):
        raise ValueError("Промокод: 1–64 буквы, цифры, _ или -, без пробелов")
    return code


def parse_promo(raw: str, max_spins: int):
    parts = [part.strip() for part in raw.split("|", 2)]
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("Формат: КОД | количество спинов | описание")
    code, bonus = promo_code(parts[0]), int(parts[1])
    description = parts[2] if len(parts) == 3 else ""
    if not 1 <= bonus <= max_spins or len(description) > 300:
        raise ValueError(f"Бонус: 1–{max_spins}; описание: до 300 символов")
    return code, bonus, description
