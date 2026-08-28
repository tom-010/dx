import pytest
from django.http.response import Http404, HttpResponseForbidden
from abc import ABC, abstractmethod

@pytest.mark.django_db
class ApiTestCase:
    ...

def expect_404(func):
    with pytest.raises(Http404):
        func()




class CrudTestCase(ApiTestCase, ABC):

    def test_crud(self, get_request, put_request, post_request, delete_request, user_b):
        # inititally no trips
        assert len(self.get_all(get_request)) == 0

        # create a trip
        res = self.create(post_request)
        id = res.id
        assert id is not None

        # now one trip
        assert len(self.get_all(get_request)) == 1
        self.check_created(self.get_one(get_request, id))

        # but still no trips for others
        assert len(self.get_all(get_request.clone(user=user_b))) == 0
        assert len(self.get_all(get_request)) == 1
        expect_404(lambda: self.get_one(get_request.clone(user=user_b), id))

        # update it
        expect_404(lambda: self.update(put_request.clone(user=user_b), id))
        assert self.update(put_request, id).success is True
        self.check_updated(self.get_one(get_request, id))

        # delete it
        expect_404(lambda: self.delete(delete_request.clone(user=user_b), id))
        assert self.delete(delete_request, id).success is True


    @abstractmethod
    def get_all(self, request):
        pass

    @abstractmethod
    def get_one(self, request, id):
        pass


    def check_created(self, item):
        pass

    @abstractmethod
    def update(self, request, id):
        pass

    @abstractmethod
    def check_updated(self, item):
        pass

    @abstractmethod
    def delete(self, request, id):
        pass

