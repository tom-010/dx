from ninja import NinjaAPI
from config.auth_api import jwt_auth

api = NinjaAPI(csrf=False, auth=jwt_auth)