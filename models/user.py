from sqlalchemy import TEXT, VARCHAR, Column, LargeBinary
from models.base import Base
from sqlalchemy.orm import relationship
class User(Base):
    __tablename__ = 'users'
    id = Column(TEXT, primary_key=True)
    name = Column(VARCHAR(100))
    email = Column(VARCHAR(100), nullable=True)
    password = Column(LargeBinary, nullable=True)
    firebase_uid = Column(VARCHAR(128), unique=True, nullable=True, index=True)
    phone = Column(VARCHAR(20), nullable=True)

    favourites = relationship('Favourite', back_populates='user')