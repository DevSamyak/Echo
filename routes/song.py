import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session,joinedload
from database import get_db
from middleware import auth_middleware
import cloudinary
import cloudinary.uploader

from models.favourite import Favourite
from models.song import Song
from pydantic_schemas.favourite_song import FavouriteSongs
import os
from dotenv import load_dotenv
load_dotenv()
router=APIRouter()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

@router.post('/upload',status_code=201)
async def upload_song(song: UploadFile = File(...),
                thumbnail: UploadFile = File(...),
                artist: str = Form(...),
                song_name: str = Form(...),
                hex_code: str = Form(...),
                db: Session = Depends(get_db),
                auth_dict = Depends(auth_middleware.AuthMiddleware)):
    
    song_id = str(uuid.uuid4())
    
    # 1. Read files into memory to avoid pointer blocking
    song_bytes = song.file.read()
    thumbnail_bytes = thumbnail.file.read()

    # 2. Upload audio as 'video'
    # Upload both files concurrently instead of one after another
    song_res, thumbnail_res = await asyncio.gather(
        asyncio.to_thread(
            cloudinary.uploader.upload,
            song_bytes,
            resource_type='video',
            folder=f'song/{song_id}'
        ),
        asyncio.to_thread(
            cloudinary.uploader.upload,
            thumbnail_bytes,
            resource_type='image',
            folder=f'song/{song_id}'
        ),
    )
    print("Song URL:", song_res['url'])
    print("Thumbnail URL:", thumbnail_res['url'])


    new_song = Song(
        id=song_id,
        song_name=song_name,
        artist = artist,
        hex_code=hex_code,
        song_url=song_res['url'],
        thumbnail_url=thumbnail_res['url'],
        user_id=auth_dict['uid'],   
    )
    db.add(new_song)
    db.commit()
    db.refresh(new_song)
    return new_song

@router.get('/list')
def list_songs(db: Session=Depends(get_db),auth_details=Depends(auth_middleware.AuthMiddleware)):
    user_id = auth_details['uid']
    songs = db.query(Song).filter(Song.user_id == user_id).all()
    return songs

@router.post('/favourite')
def favourite_songs(
    song:FavouriteSongs,
    db: Session=Depends(get_db),
    auth_details=Depends(auth_middleware.AuthMiddleware)):

    #song is already favourited check
    user_id = auth_details['uid']
    fav_song = db.query(Favourite).filter(Favourite.song_id == song.song_id,Favourite.user_id==user_id).first()

    if(fav_song):
        db.delete(fav_song)
        db.commit()
        return {'message':False}
    else:
        new_fav=Favourite(id=str(uuid.uuid4()),song_id=song.song_id,user_id=user_id)
        db.add(new_fav)
        db.commit()
        return {'message':True}
    #if fav the unfav it
    #if unfav fav it
@router.get('/list/favourites')
def list_fav_songs(db: Session=Depends(get_db),auth_details=Depends(auth_middleware.AuthMiddleware)):
    user_id = auth_details['uid']

    fav_songs = db.query(Favourite).filter(Favourite.user_id==user_id).options(joinedload(Favourite.song)).all()
    return fav_songs

