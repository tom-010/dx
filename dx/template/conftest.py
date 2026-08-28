import pytest
from django.contrib.auth import get_user_model
from persons.models import Person
from django.test import RequestFactory

User = get_user_model()

def create_user(username, email, password):
    user = User.objects.create_user(
        username=username,
        email=email,
        password=password,
    )
    Person.objects.create(
        user=user,
        name=username
    )
    return user

@pytest.fixture
def user_a(db):
    return create_user(
        username="user_a",
        email="user_a@example.com",
        password="password_a"
    )


@pytest.fixture
def user_b(db):
    return create_user(
        username="user_b",
        email="user_b@example.com",
        password="password_b"
    )



def clone_request(r, mapping, method='GET'):
    request = RequestFactory().generic(method.upper(), '/')
    request.user = r.user
    for key, value in mapping.items():
        setattr(request, key, value)
    return request

@pytest.fixture
def post_request(user_a):
    request = RequestFactory().post('/')
    request.user = user_a
    request.clone = lambda **kwargs: clone_request(request, kwargs, method='POST')
    return request

@pytest.fixture
def get_request(user_a):
    request = RequestFactory().get('/')
    request.user = user_a
    request.clone = lambda **kwargs: clone_request(request, kwargs, method='GET')
    return request

@pytest.fixture
def put_request(user_a):
    request = RequestFactory().put('/')
    request.user = user_a
    request.clone = lambda **kwargs: clone_request(request, kwargs, method='PUT')
    return request

@pytest.fixture
def delete_request(user_a):
    request = RequestFactory().delete('/')
    request.user = user_a
    request.clone = lambda **kwargs: clone_request(request, kwargs, method='DELETE')
    return request