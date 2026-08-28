import traceback
from django.conf import settings
from django.http import JsonResponse


class JsonDebugErrorHandlerMiddleware:
    """
    Catches exceptions when DEBUG=True and returns a JSON response with a
    traceback if the client accepts JSON.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
        except Exception as e:
            # Re-raise if not in debug or if the client doesn't want JSON.
            # This allows the standard Django debug page or production handler to run.
            if not settings.DEBUG or 'application/json' not in request.META.get('HTTP_ACCEPT', ''):
                raise

            # Format the exception and traceback for the JSON response.
            tb_str = traceback.format_exc()
            response_data = {
                'error': f"{type(e).__name__}: {e}",
                'traceback': tb_str.split('\n')
            }
            return JsonResponse(response_data, status=500)

        return response