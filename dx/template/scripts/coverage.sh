set -e
coverage run --source=. -m pytest -s "${@:1}"
coverage report --show-missing --skip-covered
coverage html # to htmlcov/index.html
coverage xml