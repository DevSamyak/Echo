# Flow is from top to bottom in file structure in case you forget
from fastapi import FastAPI 
from models.base import Base
from routes import auth, song, saavn
from database import engine
app = FastAPI()

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    # The keep-alive ping also tops up the home rows when they are stale
    # (memory check only, so the ping never touches the database).
    saavn.warm_stale()
    return {"status": "ok"}
app.include_router(auth.router,prefix='/auth')
app.include_router(song.router,prefix='/song')
app.include_router(saavn.router,prefix='/saavn')
Base.metadata.create_all(engine)
# stroring all the databases in Base.metadata and for creating .create_all