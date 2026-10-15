#this contains business logic 
import os
import uuid
import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Header
from sqlalchemy.orm import Session, joinedload

from database import get_db
from middleware.auth_middleware import AuthMiddleware
from models.user import User
from pydantic_schemas import user_create
from pydantic_schemas.firebase_auth import FirebaseAuth
from pydantic_schemas.user_login import UserLogin

load_dotenv()

router = APIRouter()

JWT_SECRET = os.getenv("JWT_SECRET_KEY")

# Firebase Admin SDK imports & initialization
try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth, credentials
except ImportError:
    firebase_admin = None
    firebase_auth = None
    credentials = None

# Initialize Firebase Admin (do this once at module level)
if firebase_admin is not None and not firebase_admin._apps:
    service_account_path = os.getenv('FIREBASE_SERVICE_ACCOUNT_PATH', 'firebase-service-account.json')
    try:
        if os.path.exists(service_account_path):
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
        else:
            try:
                # Attempt default credentials if service account file is not found
                firebase_admin.initialize_app()
            except Exception as default_err:
                print(
                    f"Warning: Firebase service account file '{service_account_path}' not found "
                    f"and default credentials unavailable ({default_err}). "
                    "Firebase authentication will not work until credentials are provided."
                )
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK with '{service_account_path}': {e}")


@router.post('/firebase')
def firebase_login(auth_data: FirebaseAuth, db: Session = Depends(get_db)):
    """Authenticate or register a user using a Firebase ID token.
    Returns app JWT token and user profile."""
    if firebase_admin is None or firebase_auth is None:
        raise HTTPException(
            status_code=500,
            detail="firebase-admin is not installed on the server. Please run 'pip install firebase-admin'."
        )

    if not firebase_admin._apps:
        raise HTTPException(
            status_code=500,
            detail="Firebase Admin SDK is not initialized. Please configure FIREBASE_SERVICE_ACCOUNT_PATH or credentials."
        )

    try:
        decoded_token = firebase_auth.verify_id_token(auth_data.firebase_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired Firebase token: {str(e)}")

    uid = decoded_token.get('uid')
    if not uid:
        raise HTTPException(status_code=400, detail="Invalid Firebase token: missing uid")

    email = decoded_token.get('email')
    phone = decoded_token.get('phone_number')
    name = decoded_token.get('name', '') or decoded_token.get('phone_number', 'User')

    # Look up user by firebase_uid
    user_db = db.query(User).filter(User.firebase_uid == uid).options(joinedload(User.favourites)).first()

    if not user_db:
        # Check if an existing account with the same email exists to link
        if email:
            existing_user = db.query(User).filter(User.email == email).options(joinedload(User.favourites)).first()
            if existing_user and not existing_user.firebase_uid:
                existing_user.firebase_uid = uid
                if not existing_user.phone and phone:
                    existing_user.phone = phone
                if (not existing_user.name or existing_user.name == '') and name:
                    existing_user.name = name
                db.commit()
                db.refresh(existing_user)
                user_db = existing_user

        if not user_db:
            user_db = User(
                id=str(uuid.uuid4()),
                firebase_uid=uid,
                name=name,
                email=email,
                phone=phone,
                password=None,
            )
            db.add(user_db)
            db.commit()
            db.refresh(user_db)
            # Reload with favourites joined if available
            reloaded_user = db.query(User).filter(User.id == user_db.id).options(joinedload(User.favourites)).first()
            if reloaded_user:
                user_db = reloaded_user
    else:
        # If user exists, optionally update name/email/phone if they were previously empty
        updated = False
        if not user_db.name and name:
            user_db.name = name
            updated = True
        if not user_db.email and email:
            user_db.email = email
            updated = True
        if not user_db.phone and phone:
            user_db.phone = phone
            updated = True

        if updated:
            db.commit()
            db.refresh(user_db)
            reloaded_user = db.query(User).filter(User.id == user_db.id).options(joinedload(User.favourites)).first()
            if reloaded_user:
                user_db = reloaded_user

    token = jwt.encode(payload={'id': user_db.id}, key=JWT_SECRET)
    return {'token': token, 'user': user_db}


# DEPRECATED: Email/password signup is deprecated. Use POST /auth/firebase instead.
@router.post('/signup', status_code=201, deprecated=True)
def signup_user(user: user_create.UserCreate, db: Session = Depends(get_db)):
    # extract data that coming from req jo ki aa rhi hai Usercreate class se means user se
    user_db = db.query(User).filter(User.email == user.email).first()
    # check if data already exists in database
    if user_db:
        raise HTTPException(400, 'User with same email id already exists')
    # add the user to db
    hash_pw = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt())
    #bcrypt is a password hashing algorithm.
    #why encode because it understands only bytes the bcrypt 
    #why salt two user same passwords but diff hashcodes due to salt
    user_db = User(id=str(uuid.uuid4()), name=user.name, email=user.email, password=hash_pw)
    #now we will be hashing the password bcuz if someone gets acces to db we dont want him to access the password
    #of users directly  
    db.add(user_db)
    db.commit()
    db.refresh(user_db)
    return user_db
    # print(user.name)
    # print(user.email)
    # print(user.password)


# DEPRECATED: Email/password login is deprecated. Use POST /auth/firebase instead.
@router.post('/login', deprecated=True)
def login_user(user: UserLogin, db: Session = Depends(get_db)):
    user_db = db.query(User).filter(User.email == user.email).first()

    if not user_db:
        raise HTTPException(400, 'User with this email does not exist!')
    
    #password matching if it exits
    is_match = bcrypt.checkpw(user.password.encode(), user_db.password)

    if not is_match:
        raise HTTPException(400, 'Incorrect Password!')

    
    token = jwt.encode(payload={'id': user_db.id}, key=JWT_SECRET)
    return {'token': token, 'user': user_db}
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