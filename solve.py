import json
import builtwith
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from random import shuffle
import requests
from tqdm.asyncio import tqdm
from bs4 import BeautifulSoup
from functools import partial
import threading
from urllib.parse import urljoin, urlparse, urldefrag
from threading import Lock
from Wappalyzer import Wappalyzer, WebPage
import os
import webtech
import glob
import subprocess
import signal
from get_output import get_output

NUMBER_OF_WORKERS = 100
TIMEOUT_SECONDS = 300
OUTPUT_FILENAME = 'output.json'
OUTPUT_FILENAME_JSONL = 'output.jsonl'

subprocess_executor = ThreadPoolExecutor(max_workers=20)
file_lock = threading.Lock()

def cleanup_whatweb_files_signal(signum=None, frame=None):
    files = glob.glob('/tmp/ww_result_*.json')
    for f in files:
        try:
            os.remove(f)
        except Exception:
            pass
        print(f'Cleaned up {f}')

    if signum is not None:
        exit(0)


signal.signal(signal.SIGINT, cleanup_whatweb_files_signal)

def detect_with_wappalyzer_local(url):
    try:
        wapp = Wappalyzer.latest()
        webpage = WebPage.new_from_url(url, verify=False)
        technologies = wapp.analyze_with_categories(webpage)

        result = {}
        for tech, info in technologies.items():
            categories = info.get('categories', set())
            category = next(iter(categories), 'unknown')
            result[tech] = category

        return result
    except Exception as e:
        # print(f'Wappalizer error: {e}')
        return {}

def detect_with_whatweb(url):
    try:
        url_modified = url.replace('https://', '').replace('http://', '').replace('/', '_').replace(':', '_')
        file_path = f'/tmp/ww_result_{url_modified}.json'
        result = subprocess.run(
            ['whatweb', f'--log-json={file_path}', '-a', '3', url, '--quiet'],
            capture_output=True,
            timeout=1_000_000
        )

        with open(file_path, 'r') as f:
            data = json.load(f)

        if os.path.isfile(file_path):
            os.remove(file_path)

        technologies = {}

        skip_names_list = ['country', 'ip', 'cookies', 'email', 'frame', 'object',
                           'script', 'title', 'httpserver', 'open-graph-protocol',
                           'strict-transport-security', 'x-ua-compatible',
                           'x-frame-options', 'meta-refresh', 'html5', 'headers',
                           'redirect']

        for plugin_name, plugin_data in data[0].get('plugins', {}).items():
            if plugin_name.lower() not in skip_names_list:
                technologies[plugin_name] = 'Found with whatweb'

        return technologies
    except Exception as e:
        # print(f'Error: {e}')
        return {}

def detect_with_webtech(url):
    try:
        wt = webtech.WebTech(options={
        'urls': [url],
        'json': True
        })
        wt.start()

        result_webtech = {}

        for wt_name, wt_values in wt.output.items():
            for tech in wt_values['tech']:
                if not result_webtech.get(tech['name']):
                    result_webtech[tech['name']] = 'Found category from webtech'

        return result_webtech
    except Exception as e:
        return {}

def detect_with_builtwith(result, url, html_content):
    try:
        tech_found = builtwith.parse(url, html=html_content)
        for categroy, technology in tech_found.items():
            for t in technology:
                if t not in result['technologies']:
                    result['technologies'][t] = categroy
                    result['proofs'].append(f'[{t}] Found category \'{categroy}\' in Builtwith library')
    except Exception as e:
        pass


def write_file(filename, domain, result):
    with file_lock:
        entry = { domain: result }
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + "\n")

async def scan_domain(session, domain, semaphore):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    result = {
        "technologies": {},
        "proofs": [],
        "count": 0
    }

    async with semaphore:
        url = domain
        try:
            async with session.get(
                url,
                headers=headers, 
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                allow_redirects=True,
                ssl=False) as response:

                html_content = await response.text(errors='replace')
                loop = asyncio.get_event_loop()

                await loop.run_in_executor(
                    None,
                    partial(detect_with_builtwith, result, str(response.url), html_content)
                )
                
                whatweb_techs = await loop.run_in_executor(
                    subprocess_executor,
                    partial(detect_with_whatweb, url)
                )
                
                if whatweb_techs:
                    for tech, category in whatweb_techs.items():
                        if tech not in result['technologies']:
                            result['technologies'][tech] = category
                            result['proofs'].append(f'[{tech}] Found with WhatWeb Linux subprocess')
                
                wapp_techs = await loop.run_in_executor(
                    None,
                    partial(detect_with_wappalyzer_local, url)
                )

                if wapp_techs:
                    for tech, category in wapp_techs.items():
                        if tech not in result['technologies']:
                            result['technologies'][tech] = category
                            result['proofs'].append(f'[{tech}] Found in Wappalyzer')
                            

                webtech_techs = await loop.run_in_executor(
                    None,
                    partial(detect_with_webtech, url)
                )
                
                if webtech_techs:
                    for tech, category in webtech_techs.items():
                        if tech not in result['technologies']:
                            result['technologies'][tech] = category
                            result['proofs'].append(f'[{tech}] Found in Webtech')

                result['count'] = len(result['technologies'])
        except requests.exceptions.SSLError:
            try:
                response = requests.get(url, timeout=TIMEOUT_SECONDS, headers=headers, verify=False)
            except Exception as e:
                # print(e)
                pass
        except (aiohttp.ClientConnectorError, 
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientOSError,
                aiohttp.ClientResponseError
            ):
            pass
        except asyncio.TimeoutError:
            result['error'] = f"Timeout on {url}"
            
        except Exception as e:
            result['error'] = f"{type(e).__name__}: {str(e)}"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        partial(write_file, OUTPUT_FILENAME_JSONL, domain, result)
    )

    return domain, result

mutex = Lock()

url_json_mutex = Lock()

def get_multiple_urls(url, max_pages=100):
    visited = set()
    to_visit = [url]
    found = []
    seen = set()
    seen.add(url)

    skip_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.pdf', '.zip', '.mp4', '.webp'}
    skip_tuple = tuple(skip_extensions)
    while len(to_visit) != 0 and len(visited) < max_pages:
        new_url = to_visit.pop()

        new_url, _ = urldefrag(new_url)

        if new_url in visited:
            continue

        visited.add(new_url)

        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(new_url, timeout=10, headers=headers)

            if response.status_code in (404, 403, 401, 410):
                continue

            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.text, 'html.parser')

            for link in soup.find_all('a'):
                href = link.get('href')
                if not href:
                    continue

                full_url = urljoin(new_url, href)

                full_url, _ = urldefrag(full_url)

                if urlparse(full_url).netloc == urlparse(url).netloc:
                    if full_url not in seen and not full_url.lower().endswith(skip_tuple):
                        seen.add(full_url)
                        to_visit.append(full_url)
                        found.append(full_url)
                        with mutex:
                            if full_url not in existing_domains_set:
                                existing_domains_set.add(full_url)
                                with open('domains.txt', 'a', encoding='utf-8') as f:
                                    f.write(f'{full_url}\n')

        except Exception as e:
            # print(e)
            continue

    with url_json_mutex:
        entry = { url : list(visited) }
        with open('domains.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')

    return found

existing_domains_set = set()

async def main():
    try:
        global existing_domains_set

        if not os.path.exists(OUTPUT_FILENAME_JSONL):
            print(f'{OUTPUT_FILENAME_JSONL} not found — creating empty file')
            with open(OUTPUT_FILENAME_JSONL, 'w', encoding='utf-8') as f:
                pass

        if os.path.exists('domains.txt'):
            with open('domains.txt', 'r', encoding='utf-8') as f:
                for line in f:
                    existing_domains_set.add(line.strip())

        doc = pd.read_parquet('db.parquet', engine='pyarrow')
        domains = [row.root_domain for row in doc.itertuples()]
        domains = ["https://" + domain for domain in domains]
        shuffle(domains)

        all_urls = []

        if input('Do you want to search new domains? (Y/N): ').lower() == 'y':
            print('Getting the domains from crawling urls')
            with ThreadPoolExecutor(max_workers=30) as executor:
                futures = {executor.submit(get_multiple_urls, d): d for d in domains}
                for future in tqdm(as_completed(futures), total=len(futures), desc="Crawling domains"):
                    try:
                        result = future.result()
                        all_urls.extend(result)
                    except Exception as e:
                        print(f"Error: {e}")

            domains.extend(all_urls)
        else:
            print('oki doki')
            print('Getting the domains from the domains.txt file')
            with open('domains.txt', 'r', encoding='utf-8') as f:
                for line in tqdm(f, desc="Loading domains"):
                    line = line.strip()
                    if line not in domains:
                        domains.append(line)

        print('Got the domains')
        domains_set = set(domains)

        already_used_urls = set()
        print('Getting the output sites so we do not look here again')

        with open(OUTPUT_FILENAME_JSONL, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    already_used_urls.update(entry.keys())
                except Exception as e:
                    continue

        if len(already_used_urls) == 0:
            print('Did not get any result from output.json...starting from 0')
        else:
            print('Got some data from output.json')

        domains_set = domains_set - already_used_urls
        semaphore = asyncio.Semaphore(NUMBER_OF_WORKERS)

        print('Starting app...brace yourselves!')
        async with aiohttp.ClientSession() as session:
            tasks = [scan_domain(session, domain, semaphore) for domain in domains_set]
            result_list = await tqdm.gather(*tasks, desc="Scanning domains")

        print("Task Complete!")
        get_output()
    finally:
        cleanup_whatweb_files_signal()

if __name__ == "__main__":
     asyncio.run(main())