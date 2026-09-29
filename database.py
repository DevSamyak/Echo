import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Neon closes idle connections. Without these two options SQLAlchemy can hand
# out a dead pooled connection ("SSL connection has been closed unexpectedly").
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # test each connection before use, replace it if dead
    pool_recycle=300,    # never reuse a connection older than 5 minutes
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()