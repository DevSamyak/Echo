from fastapi import HTTPException,Header
import jwt
import os
from dotenv import load_dotenv
from fastapi import HTTPException, Header
import jwt

load_dotenv()
JWT_SECRET = os.getenv("JWT_SECRET_KEY")

def AuthMiddleware(x_auth_token: str = Header()):
    try:
        if not x_auth_token:
            raise HTTPException(401, 'No auth token,access denied!')

        verified_token = jwt.decode(x_auth_token, JWT_SECRET, algorithms=['HS256'])

        if not verified_token:
            raise HTTPException(401, 'Token verification failed,authorization denied')

        uid = verified_token.get('id')
        return {'uid': uid, 'token': verified_token}

    except jwt.PyJWTError:
        raise HTTPException(401, "Token is not valid,authorisation failed")