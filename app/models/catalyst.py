from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, func
from sqlalchemy.orm import relationship
from app.database import Base


class Catalyst(Base):
    __tablename__ = "catalysts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    description = Column(Text)
    event_date = Column(DateTime(timezone=True), nullable=False)
    affected_cluster_id = Column(Integer, ForeignKey("clusters.id"))
    risk_direction = Column(String(50))  # e.g., 'bullish', 'bearish', 'neutral'
    recommended_action = Column(String(255))
    action_taken = Column(Boolean, default=False)
    reminder_sent = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cluster = relationship("Cluster")

    def __repr__(self):
        return f"<Catalyst(id={self.id}, title={self.title})>"
