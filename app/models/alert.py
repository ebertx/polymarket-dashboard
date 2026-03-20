from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Text, Boolean,
    ForeignKey, func, JSON,
)
from sqlalchemy.orm import relationship
from app.database import Base


class AlertDefinition(Base):
    """Alert rule definition — synced from alerts.yaml or managed via API."""
    __tablename__ = "alert_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(255), nullable=False, unique=True)  # e.g. "greenland-acquisition-no:drawdown:30"
    market_slug = Column(String(255))  # NULL for global alerts
    market_name = Column(String(500))
    direction = Column(String(10))  # yes/no, NULL for global
    alert_type = Column(String(50), nullable=False)  # drawdown, price_below, price_above, catalyst, resolution_approaching, etc.
    threshold = Column(Numeric(10, 4))  # numeric threshold if applicable
    catalyst_date = Column(DateTime(timezone=True))  # for catalyst-type alerts
    catalyst_description = Column(Text)
    days_before = Column(Integer)  # for catalyst/resolution alerts
    action = Column(Text, nullable=False)  # recommended action text
    severity = Column(String(20), nullable=False, default="WARNING")  # INFO, WARNING, CRITICAL
    entry_price = Column(Numeric(10, 4))  # stored from alerts.yaml for drawdown calc
    is_global = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    events = relationship("AlertEvent", back_populates="definition")

    def __repr__(self):
        return f"<AlertDefinition(id={self.id}, slug={self.slug}, type={self.alert_type})>"


class AlertEvent(Base):
    """Record of a triggered alert — used for deduplication and history."""
    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    definition_id = Column(Integer, ForeignKey("alert_definitions.id"), nullable=False)
    market_slug = Column(String(255))
    market_name = Column(String(500))
    alert_type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    action = Column(Text)
    details = Column(JSON)  # extra context (price, drawdown %, etc.)
    notified = Column(Boolean, default=False)  # whether notification was sent
    notification_error = Column(Text)  # error if notification failed
    triggered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    cleared_at = Column(DateTime(timezone=True))  # when condition cleared
    acknowledged_at = Column(DateTime(timezone=True))  # when user acknowledged via ntfy button

    definition = relationship("AlertDefinition", back_populates="events")

    def __repr__(self):
        return f"<AlertEvent(id={self.id}, type={self.alert_type}, severity={self.severity})>"
