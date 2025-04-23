import os
import json
import logging
import sys
import yaml
import time
import requests
from flask import Flask, render_template, request, Response, redirect, url_for, send_from_directory, abort, jsonify, send_file
from urllib.parse import urlparse

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

repository_url = os.environ.get('REPOSITORY_URL', 'https://charts.helm.sh/stable')
repository_source = os.environ.get('REPOSITORY_SOURCE', None)
index_path = '/app/static/index_cache'
cache_path = '/app/static/chart_cache'
index_ttl = 600 # 10 minutes
chart_ttl = 604800 # 7 days

index_url = f"{repository_url}/index.yaml"
upstream_index_path = f"{index_path}/upstream_index.yaml"
downstream_index_path = f"{index_path}/index.yaml"

@app.route('/upstream_index.yaml')
def upstream_index():
    yaml_output = return_index(upstream_index_path)

    # Fetch and process failed
    if yaml_output is None:
        return jsonify({"error": "Failed to retreive or process YAML data."}), 500

    # Set mimetype based on if client is a browser
    accept_header = request.headers.get('Accept', '')
    if 'text/html' in accept_header.lower():
        mimetype = 'text/plain'
    else:
        mimetype = 'application/x-yaml'

    return Response(yaml_output, mimetype=mimetype)

@app.route('/index.yaml')
def downstream_index():
    yaml_output = return_index(downstream_index_path)

    # Fetch and process failed
    if yaml_output is None:
        return jsonify({"error": "Failed to retreive or process YAML data."}), 500

    # Set mimetype based on if client is a browser
    accept_header = request.headers.get('Accept', '')
    if 'text/html' in accept_header.lower():
        mimetype = 'text/plain'
    else:
        mimetype = 'application/x-yaml'

    return Response(yaml_output, mimetype=mimetype)

def return_index(path_to_index):
    yaml_output = None
    cache_valid = False

    # Check cache
    try:
        if os.path.exists(path_to_index):
            file_mod_time = os.path.getmtime(path_to_index)
            current_time = time.time()
            file_age = current_time - file_mod_time

            if file_age < index_ttl:
                cache_valid = True
                logger.info(f"Cache hit! Using cached file (age: {file_age:.2f}s < TTL: {index_ttl}s) {path_to_index}")
                try:
                    with open(path_to_index, 'r') as f:
                        yaml_output = f.read()
                except (IOError, OSError) as e:
                    logger.error(f"Error reading cache file {path_to_index}: {e}")
                    cache_valid = False
            else:
                logger.info(f"Cache expired. File (age: {file_age:.2f}s >= TTL: {index_ttl}s) {path_to_index}")
        else:
            logger.info(f"Cache file not found. File {path_to_index}")
    except Exception as e:
        logger.error(f"Error during cache check: {e}")
        cache_valid = False

    # Cache invalid or read failed
    if not cache_valid:
        yaml_output = fetch_and_process_index_yaml(path_to_index)

    return yaml_output

def fetch_and_process_index_yaml(path_to_index):
    logger.info(f"Fetching upstream index: {index_url}")
    try:
        response = requests.get(index_url, timeout=10)
        response.raise_for_status()
        yaml_data = yaml.safe_load(response.text)
        
        upstream_yaml_output = yaml.dump(yaml_data, default_flow_style=False)
        try:
            with open(upstream_index_path, 'w') as f:
                f.write(upstream_yaml_output)
            logger.info(f"Successfully fetched upstream index to {upstream_index_path}")
        except (IOError, OSError) as e:
            logger.error(f"Error saving upstream index to {upstream_index_path}: {e}")

        modified_data = yaml_data
        if isinstance(modified_data, dict) and 'entries' in modified_data:
            if isinstance(modified_data['entries'], dict):
                for entry_name, releases_list in modified_data['entries'].items():
                    if isinstance(releases_list, list):
                        for release in releases_list:
                            if isinstance(release, dict):
                                version = release.get('version', 'null')
                                release['urls'] = [f"charts/{entry_name}-{version}.tgz"]

        downstream_yaml_output = yaml.dump(modified_data, default_flow_style=False)
        try:
            with open(downstream_index_path, 'w') as f:
                f.write(downstream_yaml_output)
            logger.info(f"Successfully generated and saved downstream index to {downstream_index_path}")
        except (IOError, OSError) as e:
            logger.error(f"Error saving downstream index to {downstream_index_path}: {e}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching upstream index from {upstream_index_url}: {e}")
        return None
    except yaml.YAMLError as e:
        logger.error(f"Error parsing fetched YAML: {e}")
        return None
    except Exception as e:
        logger.error(f"Error - Unexpected error occurred during index fetching/processing: {e}")

    yaml_output = None
    try:
        with open(path_to_index, 'r') as f:
            yaml_output = f.read()
    except (IOError, OSError) as e:
        logger.error(f"Error reading cache file {path_to_index}: {e}")
    return yaml_output

@app.route('/')
def index():
    entries = dict()
    yaml_data = yaml.safe_load(return_index(downstream_index_path))
    if isinstance(yaml_data, dict) and 'entries' in yaml_data:
        if isinstance(yaml_data['entries'], dict):
            for entry_name, releases_list in yaml_data['entries'].items():
                entries[entry_name] = []
                if isinstance(releases_list, list):
                    for release in releases_list:
                        if isinstance(release, dict):
                            version = release['version']
                            urls = release['urls']
                            chart_path = f"{cache_path}/{entry_name}-{version}.tgz"
                            file_age = None
                            if os.path.exists(chart_path):
                                mtime = os.path.getmtime(chart_path)
                                file_age = f"{time.time() - mtime:.0f}" 
                            entries[entry_name].append({
                                'version': version,
                                'urls': urls,
                                'cache_age': file_age
                            })

    return render_template('index.html', upstream_repository=repository_url, repository_source=repository_source, entries=entries)

@app.route('/charts/<path:filename>')
def server_chart(filename):
    logger.info(f"Chart request received for: {filename}")

    # Validate file type
    if not filename.endswith('.tgz'):
        logger.error(f"Error: Filename {filename} does not end with .tgz")
        abort(400, description="Invalid filename format. Must end with .tgz")

    # Extract entry and version
    base_filename = filename[:-4]
    parts = base_filename.rsplit('-', 1)
    if len(parts) != 2:
        logger.error(f"Error: Cannot split {base_filename} into entry and version")
        abort(400, description="Invalid filename format. Cannot determine entry and version")

    entry_name, version = parts[0], parts[1]
    logger.info(f"Parsed - Entry: {entry_name}, Version: {version}")

    # Check upstream index file available
    if not os.path.exists(upstream_index_path):
        logger.error(f"Error: Upstream index unavailable")
        abort(404, description="Upstream index cache not found. Cannot determine external chart URL")

    # Load upstream index
    yaml_output = None
    try:
        with open(upstream_index_path, 'r') as f:
            yaml_output = f.read()
    except (IOError, OSError) as e:
        logger.error(f"Error reading cache file {upstream_index_path}: {e}")
    yaml_data = yaml.safe_load(yaml_output)

    # Lookup URLs from index
    try:
        if not isinstance(yaml_data, dict) or 'entries' not in yaml_data or entry_name not in yaml_data.get('entries', {}):
            logger.error(f"Error: Entry {entry_name} not found in upstream index")
            abort(404, description=f"Chart entry {entry_name} not found")

        releases_list = yaml_data['entries'][entry_name]
        if not isinstance(releases_list, list):
            logger.error(f"Error: Data for entry {entry_name} is not a list")
            abort(500, description="Index format error")

        found_url = None
        for release in releases_list:
            if isinstance(release, dict) and release.get('version') == version:
                original_urls = release.get('urls')
                if isinstance(original_urls, list) and len(original_urls) > 0:
                    # Assuming the first URL is the correct on for the chart package
                    found_url = original_urls[0]
                    if not isinstance(found_url, str):
                        logger.error(f"Error: Found URL is not a string: {found_url}")
                        found_url = None
                    break

        if found_url is None:
            logger.error(f"Error: Version {version} for entry {entry_name} not found or has no valid URL")
            abort(404, description=f"Version {version} for the chart {entry_name} not found or has no valid URL")
    except Exception as e:
        logger.error(f"Error: Unexpected error during lookup for {entry_name}-{version}: {e}")
        abort(500, description="Internal server error during chart lookup")


    chart_path = f"{cache_path}/{entry_name}-{version}.tgz"
    
    # Check local chart cache
    if os.path.exists(chart_path):
        mtime = os.path.getmtime(chart_path)
        file_age = time.time() - mtime 
        if file_age < chart_ttl:
            logger.info(f"Cache hit! Serving cached file (age: {file_age:.2f}s < TTL: {chart_ttl}s) {chart_path}")
            return send_file(chart_path)
        else:
            logger.info(f"Cache expired. File (age: {file_age:.2f}s >= TTL: {chart_ttl}s) {chart_path}")
    else:
        logger.info(f"Cache file not found. File {chart_path}")

    # Fetch chart
    logger.info(f"Feteching chart from {found_url}")
    try:
        response = requests.get(found_url, stream=True)
        response.raise_for_status()

        with open(chart_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
            logger.info(f"Feteched chart from {found_url} to {chart_path}")
        
        logger.info(f"Serving cached file {chart_path}")
        return send_file(chart_path)

    except requests.RequestException as e:
        print(f"Failed to fetch {found_url}: {e}")
        if os.path.exists(chart_path):
            # Serve stale file if upstream is unreachable
            logger.info(f"Serving stale cached file {chart_path}")
            return send_file(chart_path)
        else:
            logger.error(f"Error: Unable to fetch chart file from {found_url} {e}")
            abort(502, f"Failed to fetch chart from upstream: {e}")


@app.route("/healthz")
def healthcheck():
    return {"status": "ok"}, 200


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
