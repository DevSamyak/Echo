from pydantic import BaseModel


class FavouriteSongs(BaseModel):
    song_id:str
    