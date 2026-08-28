#!/bin/bash

curl -X "GET" \
    -H "Authorization: Bearer $(poetry run python3 manage.py token 2> /dev/null)" \
    -H "Content-Type: application/json" \
    "localhost:8000/api/auth/user"
