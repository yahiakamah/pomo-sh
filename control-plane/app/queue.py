import redis
from rq import Queue

from .config import settings

redis_conn = redis.from_url(settings.redis_url)

task_queue = Queue("provisioning", connection=redis_conn, default_timeout=1800)
