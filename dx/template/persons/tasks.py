import celery
import logging

from config.celery import WithRetry
from . import models

logger = logging.getLogger(__name__)
logger.level = logging.INFO


@celery.shared_task(bind=True, base=WithRetry, track_started=True)
def process_persons_batch(self, batch_size: int = 100):
    """
    Example batch processing task for Persons items
    """
    items = models.Persons.objects.filter(is_active=True)[:batch_size]
    processed_count = 0

    for item in items:
        logger.info(f"Processing Persons {item.id}: {item.title}")
        # Add your processing logic here
        processed_count += 1

    logger.info(f"Processed {processed_count} Persons items")
    return {"processed": processed_count}


@celery.shared_task(bind=True, base=WithRetry, track_started=True)
def cleanup_old_persons(self, days_old: int = 30):
    """
    Example cleanup task for old Persons items
    """
    from datetime import datetime, timedelta
    from django.utils import timezone

    cutoff_date = timezone.now() - timedelta(days=days_old)
    deleted_count = models.Persons.objects.filter(
        created__lt=cutoff_date,
        is_active=False
    ).delete()[0]

    logger.info(f"Deleted {deleted_count} old Persons items")
    return {"deleted": deleted_count}