"""gunicorn configuration for the production image (docker/Dockerfile).

Sizing comes from the environment: WEB_CONCURRENCY = worker processes, GUNICORN_THREADS =
threads per worker. Threads matter here: the task event stream (`GET /api/tasks/{id}/events`,
Server-Sent Events) holds a request open for minutes, so with plain sync workers two open
streams would block the whole API.
"""

import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
# Import the app once in the master and fork: faster start, shared memory, and a broken
# configuration fails at boot instead of on the first request.
preload_app = True
# Recycle workers now and then (guards against slow leaks); the jitter staggers the restarts.
max_requests = 1000
max_requests_jitter = 100
# Heartbeat files on tmpfs — on a disk-backed /tmp a slow disk can make workers look hung.
worker_tmp_dir = "/dev/shm"
timeout = 60
graceful_timeout = 30
# Longer than the proxy's idle timeout so Caddy can reuse upstream connections.
keepalive = 5
# Logs to stdout/stderr for the container runtime; the proxy's X-Forwarded-For is the client.
accesslog = "-"
errorlog = "-"
access_log_format = (
    '%({x-forwarded-for}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)ss'
)
