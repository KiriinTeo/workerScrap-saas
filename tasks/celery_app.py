from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "bet_saas_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=["tasks.worker"] 
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1, 
)

celery_app.conf.beat_schedule = {
    "rodar-scraping-a-cada-15-min": {
        "task": "tasks.worker.run_all_scrapers",
        "schedule": crontab(minute="*/15"), 
    },
}