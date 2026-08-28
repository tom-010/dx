# see settings.HEALTH_CHECK
curl --fail-with-body -X GET -H "Accept: application/json" http://localhost:8000/ht/healthz/ && echo ok 