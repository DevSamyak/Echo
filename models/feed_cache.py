from sqlalchemy import Column, String, Text, DateTime
from models.base import Base

class FeedCache(Base):
    __tablename__ = "feed_cache"
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime(timezone=True))