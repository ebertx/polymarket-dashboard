from sqlalchemy import Column, Integer, String, Numeric, DateTime, Text, ForeignKey, func
from sqlalchemy.orm import relationship
from app.database import Base


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    max_exposure_pct = Column(Numeric(5, 2))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    markets = relationship("Market", back_populates="cluster")

    def __repr__(self):
        return f"<Cluster(id={self.id}, name={self.name})>"


class Market(Base):
    __tablename__ = "markets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(255), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text)
    resolution_criteria = Column(Text)
    condition_id = Column(String(100))
    clob_token_id_yes = Column(String(100))
    clob_token_id_no = Column(String(100))
    end_date = Column(DateTime(timezone=True))
    resolved_at = Column(DateTime(timezone=True))
    resolution_outcome = Column(String(20))
    volume_24h = Column(Numeric(18, 2))
    liquidity = Column(Numeric(18, 2))
    cluster_id = Column(Integer, ForeignKey("clusters.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    cluster = relationship("Cluster", back_populates="markets")
    positions = relationship("Position", back_populates="market")

    def __repr__(self):
        return f"<Market(id={self.id}, title={self.title[:50]}...)>"
