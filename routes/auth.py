#this contains business logic 
import uuid
import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Header
from models.user import User
from pydantic_schemas import user_create
from fastapi import APIRouter
from database import get_db
from sqlalchemy.orm import Session
from pydantic_schemas.user_login import UserLogin
from middleware.auth_middleware import AuthMiddleware
from sqlalchemy.orm import joinedload
import jwt
router = APIRouter()
import os
from dotenv import load_dotenv
load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET_KEY")

@router.post('/signup',status_code=201)
def signup_user(user: user_create.UserCreate,db:Session=Depends(get_db)):
    # extract data that coming from req jo ki aa rhi hai Usercreate class se means user se
    user_db = db.query(User).filter(User.email == user.email).first()
    # check if data already exists in database
    if user_db:
        raise HTTPException(400,'User with same email id already exists')
    # add the user to db
    hash_pw=bcrypt.hashpw(user.password.encode(),bcrypt.gensalt())
    #bcrypt is a password hashing algorithm.
    #why encode because it understands only bytes the bcrypt 
    #why salt two user same passwords but diff hashcodes due to salt
    user_db = User(id=str(uuid.uuid4()),name=user.name,email=user.email,password=hash_pw)
    #now we will be hashing the password bcuz if someone gets acces to db we dont want him to access the password
    #of users directly  
    db.add(user_db)
    db.commit()
    db.refresh(user_db)
    return user_db
    # print(user.name)
    # print(user.email)
    # print(user.password)
@router.post('/login')
def login_user(user:UserLogin,db:Session=Depends(get_db)):
    user_db = db.query(User).filter(User.email==user.email).first()

    if not user_db:
        raise HTTPException(400,'User with this email does not exist!')
    
    #password matching if it exits
    is_match = bcrypt.checkpw(user.password.encode(),user_db.password)

    if not is_match:
        raise HTTPException(400,'Incorrect Password!')

    
    token = jwt.encode(payload={'id': user_db.id}, key=JWT_SECRET)
    return {'token':token,'user':user_db}
    pass

@router.get('/')
def get_current_user_data(db:Session = Depends(get_db),
                          user_dic:dict = Depends(AuthMiddleware)):
    #get the user token from headers
    # This is another FastAPI feature.
    # Instead of reading JSON,
    # it reads an HTTP Header.
    user = db.query(User).filter(User.id==user_dic['uid']).options(
        joinedload(User.favourites)).first()

    if not user:
        raise HTTPException(401,"User invalid!")

    return user
    
    pass