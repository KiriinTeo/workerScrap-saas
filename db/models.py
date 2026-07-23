from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from config.database import Base

class Bookmaker(Base):
    __tablename__ = "bookmakers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    base_url = Column(String, nullable=False)

    odds = relationship("OddHistory", back_populates="bookmaker")

class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, index=True)
    home_team = Column(String, nullable=False)
    away_team = Column(String, nullable=False)
    league = Column(String, nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)

    odds = relationship("OddHistory", back_populates="match")

    __table_args__ = (
        UniqueConstraint('home_team', 'away_team', 'start_time', name='uix_match_event'),
    )

class OddHistory(Base):
    __tablename__ = "odds_history"

    id = Column(Integer, primary_key=True, index=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False)
    bookmaker_id = Column(Integer, ForeignKey("bookmakers.id"), nullable=False)
    market = Column(String, nullable=False)
    selection = Column(String, nullable=False)
    odd_value = Column(Float, nullable=False)
    collected_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0), index=True)

    match = relationship("Match", back_populates="odds")
    bookmaker = relationship("Bookmaker", back_populates="odds")