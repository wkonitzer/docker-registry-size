#!/usr/bin/env python3

import concurrent.futures
import logging
import sys
import argparse
import signal
import requests
import csv
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from requests.exceptions import ReadTimeout

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

HEADERS = {
    "accept": "application/json",
    "Content-Type": "application/json"
}

# Define a global flag to signal worker threads to exit
exit_flag = False


class FetchTagsError(Exception):
    def __init__(self, message):
        super().__init__(message)

       
def handle_interrupt(signum, frame):
    global exit_flag
    exit_flag = True
    print("\nOperation interrupted by the user. Exiting...")


def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"


def fetch_page(base_url, username, token, pagesize, page_start=None,
                return_headers=False):
    params = {
        "pageSize": pagesize,
        "count": True,
    }

    if page_start:
        params["pageStart"] = page_start

    try:
        response = requests.get(f"{base_url}/repositories", headers=HEADERS,
                                auth=(username, token),
                                verify=not args.insecure, timeout=5,
                                params=params)
        # Raise an HTTPError if the HTTP request returned an unsuccessful
        # status code
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Error fetching repositories: {e}")
        return []

    if return_headers:
        return response.json().get("repositories", []), response.headers
    return response.json().get("repositories", [])


def list_repositories(base_url, username, token, pagesize, workers):
    repos = []

    print(f"Fetching count of repositories", end='', flush=True)

    # Fetch the first page to get started
    repos_first_page, first_response_headers = fetch_page(base_url, username,
                                        token, pagesize, return_headers=True)
    repos.extend(repos_first_page)

    # Get the X-Resource-Count from the first request
    total_repos_count = int(first_response_headers.get("X-Resource-Count", 0))

    next_page_start = first_response_headers.get("X-Next-Page-Start")

    while next_page_start:
        page_repos, next_page_headers = fetch_page(base_url, username, token, 
                                                   pagesize,
                                                   page_start=next_page_start,
                                                   return_headers=True)
        
        if not page_repos:
            break  # Break if we get an empty page, indicating no more data

        repos.extend(page_repos)
        next_page_start = next_page_headers.get("X-Next-Page-Start")

        # Display a dot for each page of tags fetched as a progress indicator
        print('.', end='', flush=True)

    # Validate fetched repos count against X-Resource-Count
    if len(repos) != total_repos_count:
        logging.warning(
            f"Discrepancy detected. Expected {total_repos_count} repos, "
            f"but fetched {len(repos)} repos."
        )

    total_repos = len(repos) 

    return repos, total_repos


def fetch_tags_page(namespace, repo_name, base_url, username, token,
                    page_start=None, pagesize=100, return_headers=False):
    params = {
        "pageSize": pagesize,
        "count": True,
        "includeManifests": True
    }

    if page_start:
        params["pageStart"] = page_start

    url = f"{base_url}/repositories/{namespace}/{repo_name}/tags"

    try:
        response = requests.get(url, headers=HEADERS, auth=(username, token),
                                params=params, verify=not args.insecure,
                                timeout=5)
        
        # Extract the next page start from the headers
        next_page_start = response.headers.get('X-Next-Page-Start')

        logging.debug(f"Fetched page with pageStart {page_start}. "
                     f"Next page start: {next_page_start}")

        # Check for errors and raise FetchTagsError with a descriptive message
        if response.status_code != 200:
            raise FetchTagsError(
                f"Failed to fetch tags for repository {repo_name}: "
                f"HTTP Status Code: {response.status_code}, "
                f"Response: {response.text}"
            )

        tags = response.json()
        for tag in tags:
            logging.info(f"Collected tag for {repo_name}: {tag['name']}")            
        
        # Return both the tags and the next page start value
        if return_headers:
            return response.json(), next_page_start, response.headers
        else:
            return response.json(), next_page_start
    except Exception as err:
        # Log the URL that failed and the specific error message
        logging.error(
            f"Failed to fetch tags for repository {repo_name}: "
            f"URL: {url}, Error: {err}"
        )
        raise FetchTagsError(
            f"Failed to fetch tags for repository {repo_name}: {err}"
        ) from err


def process_manifest(tag):
    """Helper function to process a single manifest."""
    unique_layers_local = set()
    tag_size = 0
    try:
        for layer in tag['manifest']['dockerfile']:
            if layer['layerDigest'] not in unique_layers_local:
                unique_layers_local.add(layer['layerDigest'])
                layer_size = layer['size']
                tag_size += layer_size
    except KeyError:
        logging.error("Unexpected structure in server response: %s",
                        tag['manifest'])
    return unique_layers_local, tag_size


def get_tags_for_repository(namespace, repo_name, base_url, username,
                            token, pagesize, workers):
    unique_layers = set()
    repo_total_size = 0
    total_tags = 0
    total_tags_count = 0
    next_pages = [None]  # Start with no pageStart for the first request
    processed_pages = set()
    first_page = True
    output = []

    if not exit_flag:
        print(f"\nFetching tags for repository: {repo_name}", end='',
                                                              flush=True)
        output.append(f"Repository: {repo_name}...")   

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        try:
            while next_pages and not exit_flag:
                logging.debug(f"Next pages to process: {next_pages}")

                # Only submit new pages that haven't been processed
                future_to_page = {executor.submit(fetch_tags_page, namespace, 
                                                    repo_name, base_url,
                                                    username, token, page_start,
                                                    pagesize,
                                                    return_headers=first_page):
                                        page_start for page_start in next_pages}
                next_pages = []

                for future in concurrent.futures.as_completed(future_to_page):
                    try:
                        if first_page:
                            tags, next_page_start, headers = future.result()
                            total_tags_count = int(
                                headers.get("X-Resource-Count", 0)
                            )
                            first_page = False
                        else:
                            tags, next_page_start = future.result()
                    except FetchTagsError as error:
                        # Handle the custom exception here
                        logging.error(str(error))
                        total_tags = "error"
                        continue                                          

                    if not tags:
                        continue

                    for tag in tags:
                        tag_size_increment = 0
                        media_type = tag['manifest'].get('mediaType', '')

                        # Handle OCI
                        oci_media_type = (
                            "application/vnd.oci.image.index.v1+json"
                        )
                        if media_type == oci_media_type:
                            manifests = tag['manifest'].get('manifests', [])
                            for manifest in manifests:

                                # Handle potential FetchTagsError here
                                try:
                                    nested_tags, _ = fetch_tags_page(
                                        namespace, repo_name, base_url,
                                        username, token,
                                        page_start=manifest['digest'],
                                        pagesize=pagesize
                                    )
                                    for nested_tag in nested_tags:
                                        result = process_manifest(nested_tag)
                                        tag_unique_layers = result[0]
                                        tag_size_increment = result[1]
                                        unique_layers.update(tag_unique_layers)
                                        repo_total_size += tag_size_increment
                                except FetchTagsError as error:
                                    logging.error(str(error))
                        else:
                            (tag_unique_layers, 
                             tag_size_increment) = process_manifest(tag)
                            unique_layers.update(tag_unique_layers)
                            repo_total_size += tag_size_increment

                        total_tags += 1

                        logging.debug(
                            f"Tag Name: {tag['name']}, "
                            f"Total Size: "
                            f"{human_readable_size(tag_size_increment)}"
                        )

                    # Check if the next page value exists and if it hasn't been
                    # processed yet
                    if (next_page_start and 
                        next_page_start not in processed_pages):
                        next_pages.append(next_page_start)
                        processed_pages.add(next_page_start)                     

                # Display a dot for each page of tags fetched as a progress
                # indicator
                if not exit_flag:
                    print('.', end='', flush=True)             

        except Exception as err:
            # Handle other exceptions here if needed
            logging.error(f"An error occurred: {err}", exc_info=True)               

    output.append(f"Total tags for repository {repo_name}: {total_tags}")
    output.append(f"Total size for repository {repo_name} "
        f"(considering unique layers): {human_readable_size(repo_total_size)}")

    # Validate fetched tags count against X-Resource-Count
    if total_tags != total_tags_count:
        warning_msg = ("WARNING: Discrepancy detected. Expected %s tags, "
                       "but fetched %s tags." % (total_tags_count, total_tags))
        output.append(warning_msg)
        total_tags = "error"    

    output.append("-----------------------------------------")

    return repo_total_size, total_tags, "\n".join(output)


def write_to_csv(repo_details, csv_filename):
    with open(csv_filename, 'w', newline='') as csvfile:
        fieldnames = ['Namespace', 'Repo Name', 'Tag Count', 'Size']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        # Manually write the headers
        csvfile.write('Namespace,Repo Name,Tag Count,Size (bytes)\n')

        for detail in repo_details:
            writer.writerow(detail)


def main(args):
    # Use args.url, args.username, args.token inside the main function
    # Append "api/v0" and ensure there's no trailing slash
    BASE_URL = f"{args.url.rstrip('/')}/api/v0"
    USERNAME = args.username
    TOKEN = args.token
    PAGESIZE = args.pagesize
    WORKERS = args.workers
    REPO = args.repo

    # Initialize a list to store repository details
    repo_details = []    
    
    repositories, total_repos = list_repositories(BASE_URL, USERNAME, TOKEN,
                                                  PAGESIZE, WORKERS)

    if REPO:
        repositories = [repo for repo in repositories if repo['name'] == REPO]
        if not repositories:
            logging.error(f"Error: Repository named '{REPO}' not found.")
            return    

    print(f"\nTotal number of repositories: {total_repos}")
    print("-----------------------------------------")    

    overall_total_size = 0

    # Parallelizing the processing of each repository
    outputs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_to_repo = {executor.submit(get_tags_for_repository, 
                                          repo['namespace'], repo['name'],
                                          BASE_URL, USERNAME, TOKEN, PAGESIZE, 
                              min(10, WORKERS)): repo for repo in repositories}

        for future in concurrent.futures.as_completed(future_to_repo):
            repo_size, tag_count, repo_output = future.result()
            overall_total_size += repo_size
            outputs.append(repo_output)

            # Get the repository details from the mapping
            repo = future_to_repo[future]
            
            # Add repository details to the list
            repo_details.append({
                'Namespace': repo['namespace'],
                'Repo Name': repo['name'],
                'Tag Count': tag_count,
                'Size': repo_size
            })

    # Now print the outputs
    if not exit_flag:
        print("\n-----------------------------------------") 
        for output in outputs:
            print(output)            

        if args.csv:
            write_to_csv(repo_details, args.csv)

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

    parser.add_argument('--workers', type=int, default=5,
                        help='Number of worker threads for fetching tags.')

    parser.add_argument('--csv', type=str,
                        help='Name of the CSV file to save results.')

    parser.add_argument('--repo', type=str,
                        help='Name of the specific repository to fetch. If not'
                             ' provided, all repositories are fetched.')

    args = parser.parse_args()

    # Initialize logging configuration
    logging.basicConfig(format='%(levelname)s: %(message)s')
    logger = logging.getLogger()
    logger.setLevel(level=args.trace_level)

    # Set up a signal handler for Ctrl-C
    signal.signal(signal.SIGINT, handle_interrupt)

    try:
        main(args)
    except KeyboardInterrupt:
        print("\nOperation interrupted by the user. Exiting...")
    except Exception as error:
        logging.error(f"\nAn error occurred: {error}", exc_info=True)
