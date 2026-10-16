#this contains business logic
import json
import os
import uuid

import bcrypt
import firebase_admin
import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth as firebase_auth, credentials
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


# ---------------------------------------------------------------------------
# Firebase Admin SDK setup (runs once when the server starts)
# ---------------------------------------------------------------------------
# Credentials are read from, in this order:
#   1. FIREBASE_SERVICE_ACCOUNT_JSON  -> the whole JSON pasted into an env var
#   2. FIREBASE_SERVICE_ACCOUNT_PATH  -> path to the JSON file
#                                        (Render Secret File: /etc/secrets/firebase-service-account.json)
#                                        default: firebase-service-account.json next to main.py
# If neither exists the server stops at startup with a clear message,
# instead of starting and failing on the first login.
def _init_firebase():
    if firebase_admin._apps:
        return
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "firebase-service-account.json")
    if raw:
        cred = credentials.Certificate(json.loads(raw))
    elif os.path.exists(path):
        cred = credentials.Certificate(path)
    else:
        raise RuntimeError(
            f"Firebase credentials not found. Set FIREBASE_SERVICE_ACCOUNT_PATH "
            f"(looked at '{path}') or FIREBASE_SERVICE_ACCOUNT_JSON."
        )
    firebase_admin.initialize_app(cred)


_init_firebase()


# ---------------------------------------------------------------------------
# Response shape: never send the password hash (or firebase_uid) to the app
# ---------------------------------------------------------------------------
def _user_out(u: User) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "phone": u.phone,
        "favourites": [
            {"id": f.id, "song_id": f.song_id, "user_id": f.user_id}
            for f in (u.favourites or [])
        ],
    }


def _load_with_favourites(db: Session, user_id: str):
    return (
        db.query(User)
        .filter(User.id == user_id)
        .options(joinedload(User.favourites))
        .first()
    )


@router.post('/firebase')
def firebase_login(auth_data: FirebaseAuth, db: Session = Depends(get_db)):
    """Authenticate or register a user using a Firebase ID token.
    Sign up and sign in are the same call: the user is created on first login.
    Returns app JWT token and user profile."""
    try:
        # clock_skew_seconds avoids random "Token used too early" errors
        # when the phone's clock is a few seconds ahead of the server's.
        decoded_token = firebase_auth.verify_id_token(
            auth_data.firebase_token, clock_skew_seconds=10
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired Firebase token: {str(e)}")

    uid = decoded_token.get('uid')
    if not uid:
        raise HTTPException(status_code=400, detail="Invalid Firebase token: missing uid")

    email = decoded_token.get('email')
    phone = decoded_token.get('phone_number')
    email_verified = bool(decoded_token.get('email_verified'))
    # Name: Firebase display name, else the part of the email before '@'
    name = (
        decoded_token.get('name')
        or (email.split('@')[0] if email else None)
        or phone
        or 'User'
    )

    # Look up user by firebase_uid
    user_db = db.query(User).filter(User.firebase_uid == uid).options(joinedload(User.favourites)).first()

    if not user_db:
        # Link an existing account with the same email ONLY if Firebase says the
        # email is verified. Google sign-in is always verified; a fresh
        # email/password sign-up is not, and linking on an unverified email would
        # let anyone take over someone else's account by signing up with their email.
        if email and email_verified:
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
            user_db = _load_with_favourites(db, user_db.id) or user_db
    else:
        # If user exists, fill in name/email/phone if they were previously empty
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
            user_db = _load_with_favourites(db, user_db.id) or user_db

    token = jwt.encode(payload={'id': user_db.id}, key=JWT_SECRET)
    return {'token': token, 'user': _user_out(user_db)}


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
    return _user_out(user_db)


# DEPRECATED: Email/password login is deprecated. Use POST /auth/firebase instead.
@router.post('/login', deprecated=True)
def login_user(user: UserLogin, db: Session = Depends(get_db)):
    user_db = db.query(User).filter(User.email == user.email).first()

    if not user_db:
        raise HTTPException(400, 'User with this email does not exist!')

    # Google / phone users have no password, so they can't use this endpoint.
    if not user_db.password:
        raise HTTPException(400, 'This account uses Google or phone login.')

    #password matching if it exits
    is_match = bcrypt.checkpw(user.password.encode(), user_db.password)

    if not is_match:
        raise HTTPException(400, 'Incorrect Password!')

    token = jwt.encode(payload={'id': user_db.id}, key=JWT_SECRET)
    return {'token': token, 'user': _user_out(user_db)}


@router.get('/')
def get_current_user_data(db: Session = Depends(get_db),
                          user_dic: dict = Depends(AuthMiddleware)):
    #get the user token from headers
    # This is another FastAPI feature.
    # Instead of reading JSON,
    # it reads an HTTP Header.
    user = _load_with_favourites(db, user_dic['uid'])

    if not user:
        raise HTTPException(401, "User invalid!")

    return _user_out(user)