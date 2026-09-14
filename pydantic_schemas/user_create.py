# why pydantic schemas this is for incoming requests, different from models
#Schemas validate
# Incoming JSON
# ↓
# Python Object
from pydantic import BaseModel

#question is why inherit from BaseModel beacuse it converts incoming json into user create class
# JSON → Python Object

class UserCreate(BaseModel):
    name: str
    email: str
    password: str