from sqlalchemy import TEXT, Column, ForeignKey

from models.base import Base
from sqlalchemy.orm import relationship

#why seperate favourite model because we follow normalistion first principle
#ie to seperate such entities which have same value in a column thats why diff table
class Favourite(Base):
    __tablename__ = 'favourites'
    id = Column(TEXT,primary_key=True)
    song_id = Column(TEXT,ForeignKey("songs.id"))
    user_id = Column(TEXT,ForeignKey("users.id"))

    song = relationship('Song')
    user = relationship('User',back_populates='favourites')
