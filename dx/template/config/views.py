from django.http import JsonResponse
from django.views.defaults import server_error, page_not_found


def custom_500_handler(request, exception=None):
    """Return JSON for API requests, HTML for browser requests on 500 errors."""
    if 'application/json' in request.META.get('HTTP_ACCEPT', ''):
        return JsonResponse({'detail': 'Internal Server Error'}, status=500)
    return server_error(request)  # Fallback to Django's default HTML 500 page


def custom_404_handler(request, exception):
    """Return JSON for API requests, HTML for browser requests on 404 errors."""
    if 'application/json' in request.META.get('HTTP_ACCEPT', ''):
        return JsonResponse({'detail': 'Not Found'}, status=404)
    return page_not_found(request, exception)  # Fallback to Django's default HTML 404 page


def custom_400_handler(request, exception):
    """Return JSON for API requests, HTML for browser requests on 400 errors."""
    from django.views.defaults import bad_request
    if 'application/json' in request.META.get('HTTP_ACCEPT', ''):
        return JsonResponse({'detail': 'Bad Request'}, status=400)
    return bad_request(request, exception)


def custom_403_handler(request, exception):
    """Return JSON for API requests, HTML for browser requests on 403 errors."""
    from django.views.defaults import permission_denied
    if 'application/json' in request.META.get('HTTP_ACCEPT', ''):
        return JsonResponse({'detail': 'Permission Denied'}, status=403)
    return permission_denied(request, exception)