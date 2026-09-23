from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import TransactionSide
from app.schemas.portfolio import PortfolioCreate, PositionCreate, TransactionCreate
from app.services.portfolio_service import PortfolioService
from app.services.transaction_service import TransactionService


def _make_portfolio(session, symbol="BTC", qty="1", price="50000"):
    svc = PortfolioService(session)
    return svc.create_portfolio(
        PortfolioCreate(
            name=f"Test {symbol}",
            strategy_profile_code="main",
            positions=[
                PositionCreate(
                    quantity=Decimal(qty),
                    average_entry_price=Decimal(price),
                    asset={"symbol": symbol, "name": symbol, "coingecko_id": symbol.lower()},
                )
            ],
        )
    )


def test_buy_transaction_updates_position(session) -> None:
    portfolio = _make_portfolio(session, qty="1", price="50000")
    position = portfolio.positions[0]

    tx_svc = TransactionService(session)
    tx_svc.add_transaction(
        position,
        TransactionCreate(side=TransactionSide.BUY, quantity=Decimal("1"), unit_price=Decimal("60000")),
    )

    session.refresh(position)
    assert Decimal(position.quantity) == Decimal("2")
    assert Decimal(position.cost_basis) == Decimal("110000")
    assert Decimal(position.average_entry_price) == Decimal("55000")


def test_buy_transaction_includes_fee(session) -> None:
    portfolio = _make_portfolio(session, qty="1", price="50000")
    position = portfolio.positions[0]

    TransactionService(session).add_transaction(
        position,
        TransactionCreate(
            side=TransactionSide.BUY,
            quantity=Decimal("1"),
            unit_price=Decimal("60000"),
            fee_amount=Decimal("100"),
        ),
    )

    session.refresh(position)
    assert Decimal(position.cost_basis) == Decimal("110100")


def test_partial_sell_reduces_position_proportionally(session) -> None:
    portfolio = _make_portfolio(session, qty="4", price="25000")
    position = portfolio.positions[0]
    original_cost = Decimal(position.cost_basis)

    TransactionService(session).add_transaction(
        position,
        TransactionCreate(side=TransactionSide.SELL, quantity=Decimal("1"), unit_price=Decimal("30000")),
    )

    session.refresh(position)
    assert Decimal(position.quantity) == Decimal("3")
    expected_cost = original_cost * Decimal("3") / Decimal("4")
    assert Decimal(position.cost_basis) == expected_cost


def test_sell_does_not_change_average_entry_price(session) -> None:
    portfolio = _make_portfolio(session, qty="2", price="40000")
    position = portfolio.positions[0]
    original_avg = Decimal(position.average_entry_price)

    TransactionService(session).add_transaction(
        position,
        TransactionCreate(side=TransactionSide.SELL, quantity=Decimal("1"), unit_price=Decimal("50000")),
    )

    session.refresh(position)
    assert Decimal(position.average_entry_price) == original_avg


def test_full_close_zeroes_position(session) -> None:
    portfolio = _make_portfolio(session, qty="2", price="40000")
    position = portfolio.positions[0]

    TransactionService(session).add_transaction(
        position,
        TransactionCreate(side=TransactionSide.SELL, quantity=Decimal("2"), unit_price=Decimal("50000")),
    )

    session.refresh(position)
    assert Decimal(position.quantity) == Decimal("0")
    assert Decimal(position.cost_basis) == Decimal("0")


def test_delete_transaction_recalculates_from_history(session) -> None:
    """After deleting a transaction, position is recalculated from remaining transaction records.

    Note: the initial position seed (created without a transaction) is not part of
    the transaction history, so recalculation replays only explicit transactions.
    """
    portfolio = _make_portfolio(session, qty="1", price="50000")
    position = portfolio.positions[0]
    tx_svc = TransactionService(session)

    tx1 = tx_svc.add_transaction(
        position,
        TransactionCreate(side=TransactionSide.BUY, quantity=Decimal("1"), unit_price=Decimal("60000")),
    )
    tx2 = tx_svc.add_transaction(  # noqa: F841
        position,
        TransactionCreate(side=TransactionSide.BUY, quantity=Decimal("1"), unit_price=Decimal("70000")),
    )

    session.refresh(position)
    assert Decimal(position.quantity) == Decimal("3")

    tx_svc.delete_transaction(tx1)

    session.refresh(position)
    # Recalculates from tx records only (tx2: BUY 1 @ 70k) — initial seed is not a transaction
    assert Decimal(position.quantity) == Decimal("1")
    assert Decimal(position.cost_basis) == Decimal("70000")


def test_list_transactions_returns_sorted_by_date(session) -> None:
    portfolio = _make_portfolio(session, qty="2", price="50000")
    position = portfolio.positions[0]
    tx_svc = TransactionService(session)

    tx_svc.add_transaction(
        position,
        TransactionCreate(side=TransactionSide.BUY, quantity=Decimal("1"), unit_price=Decimal("55000")),
    )
    tx_svc.add_transaction(
        position,
        TransactionCreate(side=TransactionSide.BUY, quantity=Decimal("1"), unit_price=Decimal("60000")),
    )

    txs = tx_svc.list_transactions(position.id)
    assert len(txs) == 2
    assert txs[0].executed_at >= txs[1].executed_at


def test_transaction_api_endpoint(client) -> None:
    portfolio_resp = client.post(
        "/api/portfolios",
        json={
            "name": "TX Test Portfolio",
            "strategy_profile_code": "main",
            "positions": [
                {
                    "quantity": "1",
                    "average_entry_price": "50000",
                    "asset": {"symbol": "BTC", "name": "Bitcoin", "coingecko_id": "bitcoin"},
                }
            ],
        },
    )
    assert portfolio_resp.status_code == 201
    portfolio_id = portfolio_resp.json()["id"]
    position_id = portfolio_resp.json()["positions"][0]["id"]

    tx_resp = client.post(
        f"/api/portfolios/{portfolio_id}/positions/{position_id}/transactions",
        json={"side": "buy", "quantity": "0.5", "unit_price": "60000"},
    )
    assert tx_resp.status_code == 201
    tx_data = tx_resp.json()
    assert tx_data["side"] == "buy"
    assert Decimal(tx_data["quantity"]) == Decimal("0.5")

    list_resp = client.get(f"/api/portfolios/{portfolio_id}/positions/{position_id}/transactions")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    tx_id = tx_data["id"]
    del_resp = client.delete(f"/api/portfolios/{portfolio_id}/positions/{position_id}/transactions/{tx_id}")
    assert del_resp.status_code == 204

    list_resp2 = client.get(f"/api/portfolios/{portfolio_id}/positions/{position_id}/transactions")
    assert list_resp2.status_code == 200
    assert len(list_resp2.json()) == 0
