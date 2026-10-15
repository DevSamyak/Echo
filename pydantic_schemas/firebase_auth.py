from pydantic import BaseModel

class FirebaseAuth(BaseModel):
    firebase_token: str
