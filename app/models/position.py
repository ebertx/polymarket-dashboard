from sqlalchemy import Column, Integer, String, Numeric, DateTime, Text, ForeignKey, func, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import ENUM
from app.database import Base

# Define PostgreSQL enum types to match existing schema
position_direction = ENUM('yes', 'no', name='position_direction', create_type=False)
position_status = ENUM('open', 'closed', 'pending', name='position_status', create_type=False)
thesis_status_enum = ENUM('intact', 'strengthened', 'weakened', 'degraded', 'invalidated', name='thesis_status', create_type=False)


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
