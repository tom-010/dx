from tests.helpers import ApiTestCase, expect_404, CrudTestCase
from trips import api

class TestTripCrud(CrudTestCase):

    def get_all(self, request):
        return api.list_trips(request)

    def get_one(self, request, id):
        return api.get_trip(request, id)

    def create(self, request):
        return api.create_trip(request, api.s.TripIn(
            name="Test Trip",
            synopsis="A trip"
        ))

    def check_created(self, item):
        assert item.name == "Test Trip"

    def update(self, request, id):
        return api.update_trips(request, id, api.s.TripIn(
            name="Updated Trip",
            synopsis="An updated trip"
        ))

    def check_updated(self, item):
        assert item.name == "Updated Trip"

    def delete(self, request, id):
        return api.delete_trips(request, id)


    
