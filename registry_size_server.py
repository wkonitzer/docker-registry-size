from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response
from flask_caching import Cache
from prometheus_client import generate_latest, REGISTRY, Gauge
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
metrics.info('app_info', 'Application info', version='1.0.0')

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
repository_total_size = Gauge('repository_total_size', 'Total size of a repository in bytes', ['id', 'namespace', 'name'])
repository_total_tags = Gauge('repository_total_tags', 'Total number of tags in the repository', ['id', 'namespace', 'name'])

# New Metrics
total_repositories = Gauge('total_repositories', 'Total number of repositories')
total_size_all_repos = Gauge('total_size_all_repos', 'Total size of all repositories in bytes')

TEST_REPOS = ["dtr", "dtr-api", "dtr-astronaut-cypress"]


def update_cache():
    compute_metrics()


# Custom cache key function
def metrics_cache_key():
    return "metrics_cache_key"


def fetch_tags_for_repo(repo):
    delay = randint(1, 5)  # generates a random integer between 1 and 5 (inclusive)
    sleep(delay)

    try:
        repo_total_size, total_tags, _ = docker_registry_size.get_tags_for_repository(
            repo['namespace'], repo['name'], base_url, username, token, pagesize, workers, insecure
        )

        # Update total size metric
        repository_total_size.labels(
            id=repo['id'],
            namespace=repo['namespace'],
            name=repo['name']
        ).set(repo_total_size)

        if total_tags == "error":
            logging.error("Error fetching tags for repository: {}".format(repo))
            total_tags = -1

        # Update total tags metric
        repository_total_tags.labels(
            id=repo['id'],
            namespace=repo['namespace'],
            name=repo['name']
        ).set(total_tags)

        return repo_total_size  # Return size for aggregation
    except Exception as e:
        logging.error(f"Error processing repository {repo['name']}: {e}")
        return 0  # Return 0 size for error cases

@app.route('/metrics', methods=['GET'])
def metrics():
    # First, get your custom metrics
    custom_metrics = compute_metrics()

    # Then, get the Flask exporter's metrics
    flask_exporter_metrics = generate_latest(metrics.registry)

    # Combine both sets of metrics
    all_metrics = custom_metrics + flask_exporter_metrics

    return Response(all_metrics, content_type="text/plain")


@cache.cached(timeout=1200, key_prefix=metrics_cache_key)  # Use the custom cache key
def compute_metrics():
    repos, total_repo_count = docker_registry_size.list_repositories(base_url, username, token, pagesize, workers, insecure)
    #repos = [repo for repo in repos if repo['name'] in TEST_REPOS]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        sizes = list(executor.map(fetch_tags_for_repo, repos))

    # Update new metrics
    total_repositories.set(total_repo_count)
    total_size_all_repos.set(sum(sizes))
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
