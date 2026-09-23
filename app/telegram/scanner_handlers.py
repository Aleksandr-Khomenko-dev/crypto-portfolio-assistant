from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import get_settings
from app.db.session import get_db_session
from app.scanner.domain import Direction
from app.scanner.repository import ScannerRepository
from app.telegram.scanner import format_ranking, format_setup

router = Router(name="market_scanner")


@router.message(Command("scanner", "toplong", "topshort", "watchlist", "setup"))
async def scanner_handler(message: Message, command: CommandObject) -> None:
    settings = get_settings()
    with get_db_session() as session:
        repo = ScannerRepository(session)
        if command.command == "scanner":
            run = repo.last_run()
            text = (
                "Scanner enabled" if settings.scanner_enabled else "Scanner disabled"
            ) + "\n"
            text += (
                f"Last run: {run.status}; analyzed {run.analyzed}/{run.universe_size}; failed {run.failed}"
                if run
                else "No scanner runs yet."
            )
            text += "\n/toplong · /topshort · /setup SYMBOL · /watchlist\nScores are model confluence points."
        elif command.command == "setup":
            symbol = (command.args or "").strip().upper()
            if symbol and not symbol.endswith("USDT"):
                symbol += "USDT"
            results = repo.latest_results(symbol, limit=1) if symbol else []
            if not results:
                text = "No analysis found. Usage: /setup BTCUSDT"
            else:
                result = results[0]
                for setup in result.setups:
                    await message.answer(
                        format_setup(
                            setup,
                            result,
                            result.setup_lifecycles.get(setup.direction, "SNAPSHOT"),
                        ),
                        parse_mode="HTML",
                    )
                return
        else:
            direction = (
                Direction.LONG
                if command.command == "toplong"
                else Direction.SHORT
                if command.command == "topshort"
                else None
            )
            setups = repo.setups(
                now=datetime.now(UTC),
                direction=direction,
                minimum_score=settings.scanner_watch_score,
                limit=10,
            )
            text = format_ranking(setups, f"Top {direction or 'watchlist'} setups")
    await message.answer(text, parse_mode="HTML")
