from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Position, Transaction, TransactionSide
from app.schemas.portfolio import TransactionCreate


class TransactionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_transactions(self, position_id: UUID) -> list[Transaction]:
        statement = (
            select(Transaction)
            .where(Transaction.position_id == position_id)
            .order_by(Transaction.executed_at.desc())
        )
        return list(self.session.scalars(statement))

    def get_transaction(self, transaction_id: UUID) -> Transaction | None:
        return self.session.get(Transaction, transaction_id)

    def add_transaction(self, position: Position, payload: TransactionCreate) -> Transaction:
        tx = Transaction(
            portfolio_id=position.portfolio_id,
            asset_id=position.asset_id,
            position_id=position.id,
            side=payload.side,
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            fee_amount=payload.fee_amount,
            executed_at=payload.executed_at or datetime.now(timezone.utc),
            notes=payload.notes,
        )
        self.session.add(tx)
        self._recalculate_position(position, payload)
        self.session.commit()
        return tx

    def delete_transaction(self, transaction: Transaction) -> None:
        position_id = transaction.position_id
        self.session.delete(transaction)
        self.session.flush()
        if position_id is not None:
            position = self.session.get(Position, position_id)
            if position is not None:
                self._recalculate_from_history(position)
        self.session.commit()

    def _recalculate_position(self, position: Position, payload: TransactionCreate) -> None:
        old_quantity = Decimal(position.quantity)
        old_cost_basis = Decimal(position.cost_basis)
        tx_quantity = payload.quantity
        tx_price = payload.unit_price
        fee = payload.fee_amount or Decimal("0")

        if payload.side == TransactionSide.BUY:
            new_quantity = old_quantity + tx_quantity
            new_cost_basis = old_cost_basis + (tx_quantity * tx_price) + fee
            new_avg_price = new_cost_basis / new_quantity
        else:
            if tx_quantity >= old_quantity:
                # Full close — zero out the position
                new_quantity = Decimal("0")
                new_cost_basis = Decimal("0")
                new_avg_price = Decimal(position.average_entry_price)
            else:
                new_quantity = old_quantity - tx_quantity
                # Proportionally reduce cost basis
                new_cost_basis = old_cost_basis * (new_quantity / old_quantity)
                new_avg_price = Decimal(position.average_entry_price)

        position.quantity = new_quantity
        position.average_entry_price = new_avg_price
        position.cost_basis = new_cost_basis
        self.session.add(position)

    def _recalculate_from_history(self, position: Position) -> None:
        """Recalculate position from scratch using all remaining transactions (used after delete)."""
        transactions = list(
            self.session.scalars(
                select(Transaction)
                .where(Transaction.position_id == position.id)
                .order_by(Transaction.executed_at.asc())
            )
        )

        quantity = Decimal("0")
        cost_basis = Decimal("0")
        avg_price = Decimal("0")

        for tx in transactions:
            tx_qty = Decimal(tx.quantity)
            tx_price = Decimal(tx.unit_price)
            fee = Decimal(tx.fee_amount) if tx.fee_amount is not None else Decimal("0")

            if tx.side == TransactionSide.BUY:
                new_quantity = quantity + tx_qty
                cost_basis = cost_basis + (tx_qty * tx_price) + fee
                avg_price = cost_basis / new_quantity
                quantity = new_quantity
            else:
                if tx_qty >= quantity:
                    quantity = Decimal("0")
                    cost_basis = Decimal("0")
                else:
                    new_quantity = quantity - tx_qty
                    cost_basis = cost_basis * (new_quantity / quantity)
                    quantity = new_quantity

        position.quantity = quantity
        position.cost_basis = cost_basis
        position.average_entry_price = avg_price
        self.session.add(position)
