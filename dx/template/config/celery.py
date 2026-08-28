import os

import celery
from celery import Celery
from celery.signals import worker_process_init
from django.conf import settings
from dotenv import load_dotenv
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


@worker_process_init.connect(weak=False)
def init_worker(**kwargs):
    pass


# this code copied from manage.py
# set the default Django settings module for the 'celery' app.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
load_dotenv()

# you can change the name here
app = Celery(
    settings.PROJECT_NAME,
    broker_connection_retry_on_startup=True,
)
app.conf.update(
    # Adjust this number based on your task size and memory availability
    # worker_max_tasks_per_child=5,
    # worker_max_memory_per_child=5*(1024**3)  # Memory in GiB
)


# read config from Django settings, the CELERY namespace would make celery
# config keys has `CELERY` prefix
app.config_from_object("django.conf:settings", namespace="CELERY")

# Set a rate limit like this:
# app.control.rate_limit('machine.tasks.find_customer_folder', '100/m')

# discover and load tasks.py from from all registered Django apps
app.autodiscover_tasks(lambda: settings.MODULES)


class WithRetry(celery.Task):
    autoretry_for = (Exception, KeyError)
    retry_kwargs = {"max_retries": 5}
    retry_backoff = True
    retry_jitter = True
