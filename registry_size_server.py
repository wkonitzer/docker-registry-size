from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response
from flask_caching import Cache
from prometheus_client import generate_latest, REGISTRY, Gauge, Counter, Histogram
from prometheus_flask_exporter import PrometheusMetrics
import os
import atexit
import docker_registry_size
import logging
from random import randint
from time import sleep

app = Flask(__name__)

# Set up metrics
metrics = PrometheusMetrics(app)

# Expose some default metrics
metrics.info('app_info', 'Application info', version='1.0.1')

# Retrieve the desired logging level from an environment variable, defaulting to "INFO" if not set
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# Validate the provided logging level
if LOG_LEVEL not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
    logging.warning(f'Invalid LOG_LEVEL: {LOG_LEVEL}. Defaulting to INFO.')
    LOG_LEVEL = 'INFO'

logging.basicConfig(level=getattr(logging, LOG_LEVEL))

base_url = os.environ['DOCKER_REGISTRY_URL'].rstrip('/') + '/api/v0'
username = os.environ['DOCKER_REGISTRY_USERNAME']
token = os.environ['DOCKER_REGISTRY_TOKEN']
pagesize = int(os.environ.get('DOCKER_REGISTRY_PAGESIZE', 100))
workers = int(os.environ.get('DOCKER_REGISTRY_WORKERS', 5))
insecure = os.environ.get('DOCKER_REGISTRY_INSECURE', 'false').lower() == "true"

# Set up caching config
app.config['CACHE_TYPE'] = 'simple'  # Using simple in-memory cache.
cache = Cache(app)

# Scheduler interval
SCHEDULER_INTERVAL = int(os.environ.get('SCHEDULER_INTERVAL', 15))

# Define metrics
repository_total_size = Gauge('repository_total_size', 'Total size of a repository in bytes', ['namespace', 'name'])
repository_total_tags = Gauge('repository_total_tags', 'Total number of tags in the repository', ['namespace', 'name'])

# New Metrics
total_repositories = Gauge('total_repositories', 'Total number of repositories')
total_size_all_repos = Gauge('total_size_all_repos', 'Total size of all repositories in bytes')

# Cache metrics
cache_hits = Counter('cache_hits', 'Number of cache hits')
cache_misses = Counter('cache_misses', 'Number of cache misses')

# Update scheduler metrics
update_duration = Histogram('update_duration_seconds', 'Time spent updating cache')
update_failures = Counter('update_failures', 'Number of update failures')

# Metrics generation
feed_requests = Counter('feed_requests', 'Number of metrics requests')
feed_generation_duration = Histogram('feed_generation_duration_seconds', 'Time spent generating metrics')


@update_duration.time()
def update_cache():
    try:
        compute_metrics()
    except Exception as e:
        update_failures.inc()
        logging.error(f"Failed to update cache: {str(e)}")


# Custom cache key function
def metrics_cache_key():
    return "metrics_cache_key"


def update_repository_metrics(repo_details):
    for repo in repo_details:
        # Update total size metric
        repository_total_size.labels(
            namespace=repo['namespace'],
            name=repo['name']
        ).set(int(repo['size']))

        # Update total tags metric
        repository_total_tags.labels(
            namespace=repo['namespace'],
            name=repo['name']
        ).set(int(repo['count']))


@app.route('/metrics', methods=['GET'])
def metrics():
    feed_requests.inc()
    with feed_generation_duration.time():    
        # First, get your custom metrics
        custom_metrics = compute_metrics()

        # Then, get the Flask exporter's metrics
        flask_exporter_metrics = generate_latest(metrics.registry)

        # Combine both sets of metrics
        all_metrics = custom_metrics + flask_exporter_metrics

        return Response(all_metrics, content_type="text/plain")


@cache.cached(timeout=1200, key_prefix=metrics_cache_key)  # Use the custom cache key
def compute_metrics():
    # This function execution means a cache miss occurred because it had to compute the result.
    cache_misses.inc() 
        
    repo_details, total_repo_count, _ = docker_registry_size.fetch_and_process_repositories(base_url, username, token, pagesize, workers, insecure)

    # Update Prometheus metrics for each repository
    update_repository_metrics(repo_details)    

    # Process repository details to calculate the total size and update metrics
    total_size = sum(repo['size'] for repo in repo_details)

    # Update new metrics
    total_repositories.set(total_repo_count)
    total_size_all_repos.set(total_size)
    return generate_latest(REGISTRY)


@app.route('/health')
def health_check():
    """
    A simple health check endpoint returning 'OK' with a 200 status code.
    Can be used as a liveness and readiness probe in Kubernetes.
    """
    return 'OK', 200    


update_cache()
scheduler = BackgroundScheduler()
scheduler.add_job(update_cache, 'interval', minutes=SCHEDULER_INTERVAL)
logging.info(
    'Scheduler started with job to update cache every %s minutes.',
    SCHEDULER_INTERVAL
)
scheduler.start()

# Ensure the scheduler is shut down properly on application exit
atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    logging.info('Starting application...')
    is_debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host="0.0.0.0", port=8000, debug=is_debug_mode)
    logging.info('Application stopped.')
