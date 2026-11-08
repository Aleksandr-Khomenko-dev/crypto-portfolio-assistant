"""Every main scanner signal carries a one-tap TradingView chart button."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from app.scanner.notifications import deliver_notifications
from app.telegram.fast import tradingview_url
from tests.test_scanner_service import save_ready


async def test_setup_alert_has_a_tradingview_button(session, monkeypatch):
    settings, _, _, row = save_ready(
        session, scanner_telegram_chat_id="123", telegram_bot_token="123456:secret"
    )
    sender = AsyncMock(return_value=[7])
    monkeypatch.setattr(
        "app.services.telegram_service.TelegramService.send_scanner", sender
    )
    assert await deliver_notifications(session, settings, datetime.now(UTC)) == 1
    buttons = sender.call_args.kwargs["buttons"]
    assert buttons == [("📈 TradingView", tradingview_url(row.symbol, row.exchange))]


def test_gold_opens_the_free_oanda_chart():
    assert tradingview_url("NCCOGOLD2USDUSDT").endswith("symbol=OANDA:XAUUSD")
    assert tradingview_url("ETHUSDT").endswith("symbol=BINGX:ETHUSDT.P")
