"""Russian scanner alert presentation. Formatting only; alert rules are tested unchanged."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from app.config import Settings
from app.scanner.domain import Derivatives, Direction, Readiness, SignalState
from app.telegram.scanner import format_ranking, format_setup, price
from app.telegram.scanner_smoke_test import synthetic_ready_result

CLOSED = datetime(2026, 9, 24, 12, 59, 59, 999000, tzinfo=UTC)
TELEGRAM_LIMIT = 4096


class TelegramHTML(HTMLParser):
    """Telegram accepts only a small tag set; every tag must be closed in order."""

    ALLOWED = frozenset({"b", "i", "code"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED or attrs:
            self.errors.append(f"tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")


def valid_markup(text):
    parser = TelegramHTML()
    parser.feed(text)
    parser.close()
    return not parser.errors and not parser.stack


def result(score=85, direction=Direction.LONG, readiness=Readiness.READY, **setup):
    base = synthetic_ready_result(
        CLOSED + timedelta(minutes=7), CLOSED, score, Settings(scanner_provider="bingx")
    )
    candidate = base.setups[0].model_copy(
        update={"direction": direction, "readiness": readiness, **setup}
    )
    if direction == Direction.SHORT:
        candidate.risk.invalidation = Decimal("0.5200")
    return base.model_copy(update={"setups": [candidate]}), candidate


def render(score=85, lifecycle="ACTIVE", mode="LIVE", **kwargs):
    res, setup = result(score, **kwargs)
    return format_setup(setup, res, lifecycle, mode=mode)


@pytest.mark.parametrize(
    ("score", "state", "header"),
    [
        (62, SignalState.WATCH, "👀 <b>НАБЛЮДЕНИЕ</b>"),
        (74, SignalState.SETUP_FORMING, "🟡 <b>СЕТАП ФОРМИРУЕТСЯ</b>"),
        (85, SignalState.HIGH_CONFLUENCE, "🔥 <b>СИЛЬНЫЙ СИГНАЛ</b>"),
        (
            92,
            SignalState.EXTREME_CONFLUENCE,
            "🚨 <b>ОЧЕНЬ ВЫСОКАЯ СОВОКУПНОСТЬ ФАКТОРОВ</b>",
        ),
    ],
)
def test_state_headers_in_russian(score, state, header):
    text = render(score, state=state)
    assert text.startswith(header + " · 🟢 LONG")
    assert f"⭐ Оценка: <b>{score} / 100</b>" in text
    assert (
        "Это совокупность факторов модели,\nа не вероятность успешной сделки." in text
    )
    assert valid_markup(text) and len(text) < TELEGRAM_LIMIT


def test_russian_long_alert_structure():
    text = render(85, state=SignalState.HIGH_CONFLUENCE)
    sections = [
        "📊 <b>СТРУКТУРА РЫНКА</b>",
        "💹 <b>ДЕРИВАТИВЫ</b>",
        "🌐 <b>КОНТЕКСТ BTC/ETH</b>",
        "🎯 <b>КЛЮЧЕВЫЕ УРОВНИ</b>",
        "💡 <b>Что это значит:</b>",
        "🧭 <b>ЧТО СМОТРЕТЬ ДАЛЬШЕ</b>",
    ]
    positions = [text.index(section) for section in sections]
    assert positions == sorted(positions)
    assert "🪙 <b>ARBUSDT</b> · BingX" in text
    assert "🟢 Направление: LONG" in text
    assert "🟢 Тренд 4H: бычий" in text and "🟢 Структура 15m: BOS вверх" in text
    assert "📈 RSI: 58 · 💪 ADX: 28 · 🔊 RVOL: 1.80x" in text
    assert "🛑 Инвалидация: <code>0.4800</code>" in text
    assert "🟢 Поддержка: <code>0.4850 – 0.4900</code>" in text
    assert "⚖️ R:R: <b>2.00</b> ✅" in text
    assert "следить за удержанием цены выше <code>0.4800</code>" in text
    assert "🕐 Свеча закрыта: 24.09.2026 · 13:00 UTC" in text
    assert "📡 Режим: LIVE" in text and "ТЕСТОВЫЙ" not in text


def test_russian_short_alert_colours_and_wording():
    text = render(85, direction=Direction.SHORT)
    assert "· 🔴 SHORT" in text.splitlines()[0]
    assert "🔴 Направление: SHORT" in text
    # Bullish structure opposes a SHORT: shown red, never green.
    assert "🔴 Тренд 4H: бычий" in text
    assert "следить за удержанием цены ниже <code>0.5200</code>" in text
    assert "Сигнал направлен против старшего тренда" in text
    assert valid_markup(text)


def test_ready_versus_waiting_status():
    ready = render(85)
    assert "✅ <b>Статус: УСЛОВИЯ ПОДТВЕРЖДЕНЫ</b>" in ready
    waiting = render(85, readiness=Readiness.WAIT_RETEST)
    assert "🟡 Статус: наблюдать — ждать ретеста уровня" in waiting
    assert "• проверить повторный тест уровня пробоя" in waiting
    assert "УСЛОВИЯ ПОДТВЕРЖДЕНЫ" not in waiting


def test_invalidated_has_distinct_design():
    res, setup = result(92)
    text = format_setup(
        setup, res, "INVALIDATED", episode_invalidation=Decimal("0.4750")
    )
    assert text.startswith("❌ <b>СЦЕНАРИЙ ОТМЕНЁН</b>")
    assert "📉 LONG setup больше не активен." in text
    assert "⭐ Последняя оценка: 92 / 100" in text
    # The fixed episode level that was actually broken, not the latest plan's level.
    assert "🛑 Нарушен уровень инвалидации: <code>0.4750</code>" in text
    assert "🚫 Не использовать старый сценарий." in text
    assert "🔄 Ждать нового анализа." in text
    assert "СТРУКТУРА РЫНКА" not in text and valid_markup(text)


def test_expired_does_not_imply_failure():
    text = render(80, lifecycle="EXPIRED")
    assert text.startswith("⌛ <b>СЦЕНАРИЙ ИСТЁК</b>")
    assert "за отведённое время жизни" in text
    assert "не означает, что он оказался неверным" in text
    assert "отменён" not in text.lower() and valid_markup(text)


@pytest.mark.parametrize(
    ("mode", "prefix", "footer"),
    [
        (
            "TEST",
            "🧪 <b>ТЕСТОВЫЙ СИГНАЛ</b>\nЭто тест интеграции.\nЭто НЕ реальный торговый сигнал.",
            "🧪 Режим: ТЕСТ",
        ),
        ("DRY_RUN", "🧪 <b>DRY RUN</b>", "🧪 Режим: DRY RUN"),
    ],
)
@pytest.mark.parametrize("lifecycle", ["ACTIVE", "INVALIDATED", "EXPIRED"])
def test_test_and_dry_run_prefix_on_every_message(mode, prefix, footer, lifecycle):
    text = render(85, lifecycle=lifecycle, mode=mode)
    assert text.startswith(prefix) and footer in text and "Режим: LIVE" not in text


def test_unavailable_oi_and_funding_never_leak_raw_values():
    text = render(85)
    assert "📊 OI 15m: нет данных" in text and "📊 OI 4H: нет данных" in text
    for raw in ("unavailable", "None", "nan", "%%"):
        assert raw not in text
    assert "💰 Funding: нет данных" in text


@pytest.mark.parametrize(
    ("interpretation", "sentence"),
    [
        (
            "NEW_LONG_PARTICIPATION_POSSIBLE",
            "Рост цены и OI может указывать на приток новых позиций.",
        ),
        (
            "SHORT_BUILD_POSSIBLE",
            "Снижение цены при росте OI может указывать на наращивание short-позиций.",
        ),
        (
            "SHORT_COVERING_POSSIBLE",
            "Рост цены при снижении OI может быть связан с закрытием short-позиций.",
        ),
        (
            "DELEVERAGING_POSSIBLE",
            "Снижение цены и OI может указывать на сокращение позиций / deleveraging.",
        ),
    ],
)
def test_oi_values_and_conditional_interpretation(interpretation, sentence):
    res, setup = result(85)
    res.derivatives = Derivatives(
        funding_rate=Decimal("0.000075"),
        funding_interval_hours=4,
        funding_state="NEUTRAL",
        interpretation=interpretation,
        oi_change_by_horizon={"15m": 2.4, "1h": -5.1, "4h": None},
    )
    text = format_setup(setup, res)
    assert "📊 OI 15m: +2.40%" in text and "📊 OI 1H: -5.10%" in text
    assert "📊 OI 4H: нет данных" in text
    assert "💰 Funding: 0.0075%/4ч · нейтральный" in text
    assert f"<i>{sentence}</i>" in text


def test_no_interpretation_without_known_directions():
    res, setup = result(85)
    res.derivatives = Derivatives(interpretation="unavailable")
    assert "может указывать" not in format_setup(setup, res)


def test_meaning_mentions_only_present_conditions():
    res, setup = result(62, state=SignalState.WATCH)
    setup.blocks = dict.fromkeys(setup.blocks, 0)
    for frame in res.frames.values():
        frame.technical.alignment = "MIXED"
        frame.macro.trend = "MIXED"
        frame.micro.breaks = []
    text = format_setup(setup, res)
    meaning = text.split("💡 <b>Что это значит:</b>\n")[1].split("\n\n")[0]
    assert (
        meaning
        == "Совокупность факторов пока ограниченная — сценарий в стадии наблюдения."
    )


def test_invalid_or_missing_rr_is_explicit():
    res, setup = result(85)
    setup.risk = setup.risk.model_copy(
        update={"rr": 1.1, "valid": False, "reason": "NO_ROOM"}
    )
    assert "⚖️ R:R: <b>1.10</b> ⚠️ мало пространства до цели" in format_setup(setup, res)
    setup.risk = setup.risk.model_copy(
        update={
            "rr": None,
            "invalidation": None,
            "valid": False,
            "reason": "NO_STRUCTURAL_STOP",
        }
    )
    text = format_setup(setup, res)
    assert "⚖️ R:R: нет данных ⚠️ нет структурного стопа" in text
    assert "🛑 Инвалидация: нет данных" in text


def test_html_escaping_of_provider_text():
    res, setup = result(85)
    setup.symbol = "<b>EVIL&CO</b>"
    res = res.model_copy(update={"exchange": "<script>"})
    text = format_setup(setup, res)
    assert "&lt;b&gt;EVIL&amp;CO&lt;/b&gt;" in text and "&lt;script&gt;" in text
    assert valid_markup(text)
    assert not valid_markup("<b>broken")  # the validator itself rejects bad markup


def test_no_trade_instructions_or_probability_claims():
    for lifecycle in ("ACTIVE", "INVALIDATED", "EXPIRED"):
        text = render(92, lifecycle=lifecycle).lower()
        for banned in (
            "buy",
            "sell",
            "enter now",
            "guaranteed",
            "high probability",
            "купить",
            "продать",
            "гарант",
        ):
            assert banned not in text


def test_worst_case_length_stays_below_telegram_limit():
    res, setup = result(99, readiness=Readiness.WAIT_STRUCTURE)
    setup.symbol = "1000000MOGUSDT" * 3
    res.context.state = "RISK_OFF"
    res.derivatives = Derivatives(
        funding_rate=Decimal("-0.00123456"),
        funding_interval_hours=1,
        funding_state="CROWDED_SHORT",
        interpretation="SHORT_BUILD_POSSIBLE",
        oi_history_source="SELF_RECORDED",
        oi_change_by_horizon={"15m": -12.3456, "1h": 123.4, "4h": -99.9},
    )
    text = format_setup(setup, res, mode="DRY_RUN")
    assert len(text) < 2000 < TELEGRAM_LIMIT and valid_markup(text)


def test_price_formatting_never_scientific():
    assert price(Decimal("84372.5"), "-") == "84372.5"
    assert price(Decimal("0.4850"), "-") == "0.4850"
    assert price(Decimal("0.0000123"), "-") == "0.00001230"
    assert "e" not in price(Decimal("1E-9"), "-").lower()


def test_russian_ranking():
    from app.scanner.domain import SetupRead

    _, setup = result(85)
    row = SetupRead(
        **setup.model_dump(),
        id="00000000-0000-0000-0000-000000000001",
        price=Decimal(1),
        created_at=CLOSED,
        expires_at=CLOSED,
        lifecycle="ACTIVE",
    )
    text = format_ranking([row], Direction.LONG)
    assert text.startswith("<b>Лучшие LONG-сетапы</b>")
    assert "1. 🟢 <b>ARBUSDT</b> · 85/100 · ✅ условия подтверждены" in text
    assert valid_markup(text)
