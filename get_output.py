import json
import pandas as pd
import os
from time import sleep

OUTPUT_JSONL_FILE = 'output.jsonl'
OUTPUT_JSON_FILE = 'output.json'

result = {}

def get_number_of_different_technologies():
    different_technologies = set()
    
    with open(OUTPUT_JSON_FILE, 'r', encoding='utf-8') as f:
        output_dict = json.load(f)

        for _, info in output_dict.items():
            for technology in info['technologies']:
                different_technologies.add(technology)

    print(f'We have {len(different_technologies)} different technologies!')

def get_output():
    doc = pd.read_parquet('db.parquet', engine='pyarrow')
    domains = [row.root_domain for row in doc.itertuples()]

    if os.path.exists(OUTPUT_JSON_FILE):
        with open(OUTPUT_JSON_FILE, 'r+') as f:
            f.seek(0)
            f.truncate()
    
    with open(OUTPUT_JSONL_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                entry = json.loads(line)
                for url_name, url_info in entry.items():
                    root_domain = None
                    for domain in domains:
                        if domain in url_name:
                            root_domain = domain
                            break

                    if root_domain:
                        if root_domain not in result:
                            result[root_domain] = {
                                'count': 0,
                                'technologies': {},
                                'proofs': set()
                            }

                        result[root_domain]['technologies'].update(url_info.get('technologies', {}))
                        result[root_domain]['proofs'].update(url_info.get('proofs', []))

            except Exception as e:
                print(e)
                continue

    for domain in result:
        result[domain]['proofs'] = list(result[domain]['proofs'])
        result[domain]['count'] = len(result[domain]['technologies'])
    
    with open(OUTPUT_JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=4)

    get_number_of_different_technologies()

# get_output()