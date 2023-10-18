import concurrent.futures
import logging
import sys
import argparse
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

HEADERS = {
    "accept": "application/json",
    "Content-Type": "application/json"
}


def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"


def list_repositories(base_url, username, token):
    response = requests.get(f"{base_url}/repositories", headers=HEADERS,
                            auth=(username, token), verify=not args.insecure,
                            timeout=5)
    
    if response.status_code != 200:
        logging.error(f"Error fetching repositories: {response.text}")
        return []

    repos = response.json().get("repositories", [])
    for repo in repos:
        logging.info(f"Repository ID: {repo['id']}, Name: {repo['name']}, "
                     f"Namespace: {repo['namespace']}")

    total_repos = len(repos)  

    return repos, total_repos


def fetch_tags_page(namespace, repo_name, base_url, username, token,
                    page_start=None, pagesize=100):
    params = {
        "pageSize": pagesize,
        "count": True,
        "includeManifests": True
    }
    if page_start:
        params["pageStart"] = page_start

    url = f"{base_url}/repositories/{namespace}/{repo_name}/tags"
    response = requests.get(url, headers=HEADERS, auth=(username, token),
                            params=params, verify=not args.insecure,
                            timeout=5)
    
    # Extract the next page start from the headers
    next_page_start = response.headers.get('X-Next-Page-Start')

    logging.debug(f"Fetched page with pageStart {page_start}. "
                 f"Next page start: {next_page_start}")

    # Return both the tags and the next page start value
    if response.status_code == 200:
        return response.json(), next_page_start
    else:
        return [], next_page_start   


def get_tags_for_repository(namespace, repo_name, base_url, username,
                            token, pagesize):
    unique_layers = set()
    repo_total_size = 0
    total_tags = 0
    next_pages = [None]  # Start with no pageStart for the first request
    processed_pages = set()

    print(f"Fetching tags for repository: {repo_name}...", end='', flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        while next_pages:
            logging.debug(f"Next pages to process: {next_pages}")

            # Only submit new pages that haven't been processed
            future_to_page = {executor.submit(fetch_tags_page, namespace, 
                                                repo_name, base_url, username,
                                                token, page_start, pagesize):
                                        page_start for page_start in next_pages}
            next_pages = []

            for future in concurrent.futures.as_completed(future_to_page):
                tags, next_page_start = future.result()

                if not tags:
                    continue

                for tag in tags:
                    tag_size = 0
                    for layer in tag['manifest']['dockerfile']:
                        if layer['layerDigest'] not in unique_layers:
                            unique_layers.add(layer['layerDigest'])
                            layer_size = layer['size']
                            repo_total_size += layer_size
                            tag_size += layer_size
                    total_tags += 1

                    logging.debug(f"Tag Name: {tag['name']}, "
                                f"Total Size: {human_readable_size(tag_size)}")

                # Check if the next page value exists and if it 
                # hasn't been processed yet
                if next_page_start and next_page_start not in processed_pages:
                    next_pages.append(next_page_start)
                    processed_pages.add(next_page_start)

            # Display a dot for each page of tags fetched 
            # as a progress indicator
            print('.', end='', flush=True)                    

    print(f"\nTotal tags for repository {repo_name}: {total_tags}")
    print(f"Total size for repository {repo_name} "
        f"(considering unique layers): {human_readable_size(repo_total_size)}")
    print("-----------------------------------------")

    return repo_total_size


def main(args):
    # Use args.url, args.username, args.token inside the main function
    # Append "api/v0" and ensure there's no trailing slash
    BASE_URL = f"{args.url.rstrip('/')}/api/v0"
    USERNAME = args.username
    TOKEN = args.token
    PAGESIZE = args.pagesize
    
    repositories, total_repos = list_repositories(BASE_URL, USERNAME, TOKEN)

    print(f"\nTotal number of repositories: {total_repos}")


    overall_total_size = 0
    for repo in repositories:
        namespace = repo['namespace']
        repo_name = repo['name']
        repo_size = get_tags_for_repository(namespace, repo_name, 
                                            BASE_URL, USERNAME, TOKEN, PAGESIZE)
        overall_total_size += repo_size

    print(f"\nOverall total size of all repositories: "
          f"{human_readable_size(overall_total_size)}")   


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Process Docker registry data.')

    parser.add_argument('--url', required=True, 
                        help='Base URL of the Docker registry API.')

    parser.add_argument('-u', '--username', required=True, 
                        help='Username for authentication.')

    parser.add_argument('-t', '--token', required=True, 
                        help='Token for authentication.')

    parser.add_argument('--trace-level', 
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR',
                                 'CRITICAL'],
                        default='WARNING', 
                        help='Logging level.')

    parser.add_argument('--pagesize', type=int, default=100,
                        help='Number of tags fetched per request.')

    parser.add_argument('-k', '--insecure', action='store_true',
                        help='Ignore SSL certificate verification.')


    args = parser.parse_args()

    # Initialize logging configuration
    logging.basicConfig(format='%(levelname)s: %(message)s')
    logger = logging.getLogger()
    logger.setLevel(level=args.trace_level)

    try:
        main(args)
    except KeyboardInterrupt:
        print("\nOperation interrupted by the user. Exiting...")
    except Exception as e:
        logging.error(f"\nAn error occurred: {e}", exc_info=True)
