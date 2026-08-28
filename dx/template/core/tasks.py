import celery
import logging
import time

from celery_progress.backend import ProgressRecorder
from config.celery import WithRetry

logger = logging.getLogger(__name__)
logger.level = logging.INFO


@celery.shared_task(bind=True, base=WithRetry, track_started=True)
def slow_count(self, n: int):
    import time
    total = 0
    for i in range(n):
        logger.info(f"current count: {i}")
        time.sleep(1)
    return total


@celery.shared_task(bind=True, track_started=True)
def process_data_task(self, items_count: int, task_name: str = "Data Processing"):
    """
    Example task that demonstrates progress tracking with celery-progress.
    Simulates processing a number of items with progress updates.
    """
    progress_recorder = ProgressRecorder(self)
    result = {"processed_items": [], "errors": []}

    logger.info(f"Starting {task_name} task with {items_count} items")

    for i in range(items_count):
        try:
            # Simulate some work
            time.sleep(0.5)  # Simulate processing time

            # Simulate processing an item
            item_result = {
                "item_id": i + 1,
                "processed_at": time.time(),
                "status": "success"
            }
            result["processed_items"].append(item_result)

            # Update progress
            progress_recorder.set_progress(
                current=i + 1,
                total=items_count,
                description=f"Processing item {i + 1} of {items_count}"
            )

            logger.info(f"Processed item {i + 1}/{items_count}")

        except Exception as e:
            error_info = {
                "item_id": i + 1,
                "error": str(e),
                "timestamp": time.time()
            }
            result["errors"].append(error_info)
            logger.error(f"Error processing item {i + 1}: {e}")

    logger.info(f"Completed {task_name} task. Processed: {len(result['processed_items'])}, Errors: {len(result['errors'])}")
    return result


@celery.shared_task(bind=True, track_started=True)
def file_upload_simulation_task(self, file_size_mb: int, task_name: str = "File Upload"):
    """
    Simulates a file upload process with progress tracking.
    """
    progress_recorder = ProgressRecorder(self)

    # Simulate upload in chunks
    chunk_size_mb = 1  # 1MB chunks
    total_chunks = max(1, file_size_mb // chunk_size_mb)

    logger.info(f"Starting {task_name} task - uploading {file_size_mb}MB in {total_chunks} chunks")

    uploaded_chunks = 0
    for chunk in range(total_chunks):
        # Simulate network delay
        time.sleep(0.3)

        uploaded_chunks += 1
        uploaded_mb = uploaded_chunks * chunk_size_mb

        progress_recorder.set_progress(
            current=uploaded_chunks,
            total=total_chunks,
            description=f"Uploaded {uploaded_mb}MB of {file_size_mb}MB"
        )

        logger.info(f"Uploaded chunk {uploaded_chunks}/{total_chunks}")

    result = {
        "file_size_mb": file_size_mb,
        "chunks_uploaded": uploaded_chunks,
        "status": "completed",
        "upload_time": total_chunks * 0.3
    }

    logger.info(f"Completed {task_name} task")
    return result
