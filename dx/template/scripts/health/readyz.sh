# the trailing / is important in readyz/
curl --fail-with-body -X GET -H "Accept: application/json" http://localhost:8000/ht/readyz/ && echo ok 
