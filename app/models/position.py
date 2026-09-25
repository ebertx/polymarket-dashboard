from sqlalchemy import Column, Integer, String, Numeric, DateTime, Text, ForeignKey, func, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import ENUM
from app.database import Base

# Define PostgreSQL enum types to match existing schema
position_direction = ENUM('yes', 'no', name='position_direction', create_type=False)
position_status = ENUM('open', 'closed', 'pending', name='position_status', create_type=False)
thesis_status_enum = ENUM('intact', 'strengthened', 'weakened', 'degraded', 'invalidated', name='thesis_status', create_type=False)

# Attribution tags (beads pm-rfz.5). Plain VARCHAR columns, not PG enums, so the
# vocabulary can grow without a migration. Kept in sync by tests with the copy
# in scripts/backfill_attribution_tags.py and the desk's post_order_alerts.py.
STRATEGY_TAGS = frozenset({
    "value-midband",       # 35-85c entry on a forecast/judgment thesis
    "carry-definitional",  # >=85c entry whose thesis is a resolution-rule reading
    "carry-forecast",      # >=85c entry on a forecast thesis
    "fast-track-data",     # 35-85c entry on a Grade A/B data-resolved market via the Fast Track
    "longshot",            # <35c entry (blocked since Sep 2026; historical rows only)
    "override",            # user-directed entry outside the pipeline's recommendation
})
PIPELINE_TAGS = frozenset({
    "full",   # full 7-9 agent pipeline
    "fast",   # Fast Track (4 agents)
    "gate",   # gate-check only
    "none",   # no analysis folder
})


class Recommendation(Base):
    """Stub model to register the recommendations table in ORM metadata.

    The recommendations table exists in the database (created by polybot).
    This stub prevents NoReferencedTableError when SQLAlchemy's mapper
    encounters the FK constraint on positions.recommendation_id during
    table sorting at flush time.
    """
    __tablename__ = "recommendations"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"))
    direction = Column(position_direction, nullable=False)
    shares = Column(Numeric(18, 6), nullable=False)
    entry_price = Column(Numeric(10, 4), nullable=False)
    entry_date = Column(DateTime(timezone=True), nullable=False)
    exit_price = Column(Numeric(10, 4))
    exit_date = Column(DateTime(timezone=True))
    current_price = Column(Numeric(10, 4))
    current_value = Column(Numeric(18, 6))
    unrealized_pnl = Column(Numeric(18, 6))
    realized_pnl = Column(Numeric(18, 6))
    cost_basis = Column(Numeric(18, 6), nullable=False)
    status = Column(position_status)
    thesis_status = Column(thesis_status_enum)
    recommendation_id = Column(Integer, ForeignKey("recommendations.id"))
    analysis_folder = Column(String(255))
    entry_reasoning = Column(Text)
    exit_reasoning = Column(Text)
    # Consecutive sync cycles this position has been absent from the Data API.
    # Persisted (rather than held in a module-level dict) so a container restart
    # doesn't reset progress toward AUTO_CLOSE_MISS_THRESHOLD. Added by the
    # idempotent ALTER in app/main.py's lifespan.
    api_miss_count = Column(Integer, nullable=False, server_default="0", default=0)
    # Attribution (pm-rfz.5): trade shape / research depth. Nullable — set by
    # the desk's post_order_alerts.py after a BUY, or by
    # scripts/backfill_attribution_tags.py. Ensured by the same idempotent
    # ALTER pattern as api_miss_count in app/main.py's lifespan.
    strategy_tag = Column(String(32))
    pipeline_tag = Column(String(16))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    market = relationship("Market", back_populates="positions")
    snapshots = relationship("PositionSnapshot", back_populates="position")

    def __repr__(self):
        return f"<Position(id={self.id}, direction={self.direction}, shares={self.shares})>"


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id"))
    timestamp = Column(DateTime(timezone=True), nullable=False)
    price = Column(Numeric(10, 4), nullable=False)
    value = Column(Numeric(18, 6), nullable=False)
    bid = Column(Numeric(10, 4))
    ask = Column(Numeric(10, 4))
    spread = Column(Numeric(10, 4))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    position = relationship("Position", back_populates="snapshots")

    def __repr__(self):
        return f"<PositionSnapshot(id={self.id}, price={self.price})>"
