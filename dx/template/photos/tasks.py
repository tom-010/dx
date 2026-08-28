import celery
import logging

from config.celery import WithRetry
from . import models

logger = logging.getLogger(__name__)
logger.level = logging.INFO


@celery.shared_task(bind=True, base=WithRetry, track_started=True)
def process_photos_batch(self, batch_size: int = 100):
    """
    Example batch processing task for Photos items
    """
    items = models.Photos.objects.filter(is_active=True)[:batch_size]
    processed_count = 0

    for item in items:
        logger.info(f"Processing Photos {item.id}: {item.title}")
        # Add your processing logic here
        processed_count += 1

    logger.info(f"Processed {processed_count} Photos items")
    return {"processed": processed_count}


@celery.shared_task(bind=True, base=WithRetry, track_started=True)
def cleanup_old_photos(self, days_old: int = 30):
    """
    Example cleanup task for old Photos items
    """
    from datetime import datetime, timedelta
    from django.utils import timezone

    cutoff_date = timezone.now() - timedelta(days=days_old)
    deleted_count = models.Photos.objects.filter(
        created__lt=cutoff_date,
        is_active=False
    ).delete()[0]

    logger.info(f"Deleted {deleted_count} old Photos items")
    return {"deleted": deleted_count}