from sqlalchemy import Column, Integer, Numeric, DateTime, String, func
from app.database import Base


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    cash_balance = Column(Numeric(18, 6), nullable=False)
    position_value = Column(Numeric(18, 6), nullable=False)
    total_value = Column(Numeric(18, 6), nullable=False)
    daily_pnl = Column(Numeric(18, 6))
    daily_pnl_pct = Column(Numeric(10, 4))
    granularity = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<PortfolioSnapshot(id={self.id}, total={self.total_value})>"
