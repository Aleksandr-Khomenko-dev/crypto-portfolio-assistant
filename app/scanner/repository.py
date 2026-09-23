from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.scanner_models import MarketSetup, ScannerRun, ScannerSnapshot
from app.research.repository import start_outcome
from app.scanner.domain import Candle, Direction, ScannerResult, SetupRead, SignalState


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ScannerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def last_run(self) -> ScannerRun | None:
        return self.session.scalar(
            select(ScannerRun).order_by(ScannerRun.started_at.desc()).limit(1)
        )

    def expire(self, now: datetime) -> None:
        for setup in self.session.scalars(
            select(MarketSetup).where(
                MarketSetup.lifecycle == "ACTIVE", MarketSetup.expires_at <= now
            )
        ):
            setup.lifecycle = "EXPIRED"
            setup.updated_at = now
        self.session.flush()

    def save(
        self,
        run_id: UUID,
        result: ScannerResult,
        settings: Settings,
        closed_bars: list[Candle] | None = None,
    ) -> None:
        snapshot = ScannerSnapshot(
            run_id=run_id,
            symbol=result.symbol,
            exchange=result.exchange,
            created_at=result.created_at,
            candle_closed_at=result.candle_closed_at,
            long_score=result.long_score,
            short_score=result.short_score,
            data=result.model_dump(mode="json"),
        )
        self.session.add(snapshot)
        self.session.flush()
        for candidate in result.setups:
            previous = self.session.scalar(
                select(MarketSetup)
                .where(
                    MarketSetup.exchange == result.exchange,
                    MarketSetup.symbol == result.symbol,
                    MarketSetup.direction == candidate.direction,
                )
                .order_by(MarketSetup.created_at.desc())
                .limit(1)
            )
            if previous and previous.lifecycle == "ACTIVE":
                level = previous.initial_invalidation
                observed = (
                    [(b.close_time, b.close) for b in closed_bars]
                    if closed_bars is not None
                    else [(result.candle_closed_at, result.price)]
                )
                broken = level is not None and any(
                    utc(previous.created_at)
                    < at
                    <= min(result.created_at, utc(previous.expires_at))
                    and (
                        price <= level
                        if candidate.direction == Direction.LONG
                        else price >= level
                    )
                    for at, price in observed
                )
                if broken or utc(previous.expires_at) <= result.created_at:
                    previous.lifecycle = "INVALIDATED" if broken else "EXPIRED"
                    previous.updated_at = result.created_at
                    previous.snapshot_id = snapshot.id
                    continue
                if previous.initial_invalidation is None:
                    previous.initial_invalidation = candidate.risk.invalidation
                previous.snapshot_id = snapshot.id
                previous.score, previous.state, previous.readiness = (
                    candidate.score,
                    candidate.state,
                    candidate.readiness,
                )
                previous.price, previous.data, previous.updated_at = (
                    result.price,
                    candidate.model_dump(mode="json"),
                    result.created_at,
                )
                start_outcome(self.session, previous.id, candidate, result, snapshot.id)
            elif candidate.score >= settings.scanner_watch_score:
                # A terminal episode must not immediately resurrect from the same closed bar.
                if previous and result.candle_closed_at <= utc(previous.updated_at):
                    continue
                setup_id = uuid4()
                self.session.add(
                    MarketSetup(
                        id=setup_id,
                        snapshot_id=snapshot.id,
                        symbol=result.symbol,
                        exchange=result.exchange,
                        direction=candidate.direction,
                        score=candidate.score,
                        state=candidate.state,
                        readiness=candidate.readiness,
                        price=result.price,
                        initial_invalidation=candidate.risk.invalidation,
                        created_at=result.created_at,
                        updated_at=result.created_at,
                        expires_at=result.expires_at,
                        data=candidate.model_dump(mode="json"),
                        last_notified_at=previous.last_notified_at
                        if previous
                        else None,
                    )
                )
                start_outcome(self.session, setup_id, candidate, result, snapshot.id)
        self.session.flush()

    def setups(
        self,
        *,
        now: datetime,
        minimum_score: int = 0,
        direction: Direction | None = None,
        state: SignalState | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[SetupRead]:
        query = select(MarketSetup).where(
            MarketSetup.lifecycle == "ACTIVE",
            MarketSetup.expires_at > now,
            MarketSetup.score >= minimum_score,
        )
        if direction:
            query = query.where(MarketSetup.direction == direction)
        if state:
            query = query.where(MarketSetup.state == state)
        if symbol:
            query = query.where(MarketSetup.symbol == symbol.upper())
        rows = self.session.scalars(
            query.order_by(
                MarketSetup.score.desc(), MarketSetup.symbol, MarketSetup.direction
            ).limit(limit)
        )
        return [
            SetupRead(
                **row.data,
                id=row.id,
                price=row.price,
                created_at=utc(row.updated_at),
                expires_at=utc(row.expires_at),
                lifecycle=row.lifecycle,
                episode_invalidation=row.initial_invalidation,
                exchange=row.exchange,
            )
            for row in rows
        ]

    def latest_results(
        self,
        symbol: str | None = None,
        limit: int = 100,
        exchange: str | None = None,
    ) -> list[ScannerResult]:
        latest_per_symbol = select(
            ScannerSnapshot.id,
            func.row_number()
            .over(
                partition_by=ScannerSnapshot.symbol,
                order_by=ScannerSnapshot.created_at.desc(),
            )
            .label("rank"),
        )
        if exchange:
            latest_per_symbol = latest_per_symbol.where(
                ScannerSnapshot.exchange == exchange
            )
        ranked = latest_per_symbol.subquery()
        query = (
            select(ScannerSnapshot)
            .join(ranked, ranked.c.id == ScannerSnapshot.id)
            .where(ranked.c.rank == 1)
        )
        if symbol:
            query = query.where(ScannerSnapshot.symbol == symbol.upper())
        rows = self.session.scalars(
            query.order_by(
                ScannerSnapshot.created_at.desc(), ScannerSnapshot.symbol
            ).limit(limit)
        )
        results = [ScannerResult.model_validate(row.data) for row in rows]
        # Expose live episode status alongside immutable research snapshots.
        latest: dict[tuple[str, str, str], MarketSetup] = {}
        for setup in self.session.scalars(
            select(MarketSetup)
            .where(MarketSetup.symbol.in_([r.symbol for r in results]))
            .order_by(MarketSetup.created_at.desc())
        ):
            latest.setdefault((setup.exchange, setup.symbol, setup.direction), setup)
        now = datetime.now(UTC)
        for result in results:
            for candidate in result.setups:
                episode = latest.get(
                    (result.exchange, result.symbol, candidate.direction)
                )
                state = "NOT_TRACKED"
                if episode is not None:
                    state = (
                        "EXPIRED"
                        if episode.lifecycle == "ACTIVE"
                        and utc(episode.expires_at) <= now
                        else episode.lifecycle
                    )
                result.setup_lifecycles[candidate.direction] = state
        return results
