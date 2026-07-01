#!/usr/bin/env python3

__author__ = "Van Graham"
__version__ = "2.0"

from argparse import ArgumentParser
import requests
from os import path, makedirs
import logging
from concurrent.futures import ThreadPoolExecutor
import hashlib

NORAD_URL = "https://celestrak.org/NORAD/elements/gp.php"
CACHE_DIR = "/var/lib/sattracker/cache"
OUT_FILE = "/var/lib/sattracker/custom.tle"
TR_FILE = "satellites.txt"


def read_satellites_file(file_path: str) -> list:
    satellites = []
    if not path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "r") as f:
        for line in f:
            if line.startswith("#"):
                continue
            line = line.strip()
            if not line:
                continue
            if line.isdigit():
                satellites.append(line)
    return satellites


def download_tle(catalog_number: str) -> list:
    url = f"{NORAD_URL}?CATNR={catalog_number}&FORMAT=TLE"

    headers = {
        "User-Agent": "SatTracker/2.0"
    }

    try:
        r = requests.get(url, headers=headers, timeout=10)
        print(f"\n===== {catalog_number} =====")
        print("URL:", r.url)
        print("HTTP:", r.status_code)
        print(r.text)

        r.raise_for_status()

        data = [x for x in r.content.splitlines() if x.strip()]
        return data

    except Exception as e:
        logging.error(f"{catalog_number}: {e}")
        return []


def hash_tle(data: list) -> str:
    return hashlib.sha256(b"".join(data)).hexdigest()


def load_cache(cat: str):
    cpath = path.join(CACHE_DIR, cat + ".hash")
    if not path.exists(cpath):
        return None
    with open(cpath, "r") as f:
        return f.read().strip()


def save_cache(cat: str, h: str):
    makedirs(CACHE_DIR, exist_ok=True)
    with open(path.join(CACHE_DIR, cat + ".hash"), "w") as f:
        f.write(h)


def process_sat(cat: str):
    data = download_tle(cat)
    if len(data) != 3:
        logging.warning(f"Missing TLE: {cat}")
        return None

    h = hash_tle(data)

    if load_cache(cat) != h:
        save_cache(cat, h)
        logging.info(f"Updated {cat}")

    return data


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)

    parser = ArgumentParser(description="NORAD TLE generator (refactored)", prog="tle_gen")
    parser.add_argument("-i", "--input", help="Comma-separated NORAD IDs")
    parser.add_argument("-o", "--output", help="Output TLE file")
    args = parser.parse_args()

    out_file = args.output if args.output else OUT_FILE

    if args.input:
        input_list = args.input.replace(" ", "").split(",")
    else:
        input_list = read_satellites_file(TR_FILE)

    if not input_list:
        logging.error("No satellites provided")
        exit(1)

    makedirs(path.dirname(out_file), exist_ok=True)

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(process_sat, input_list):
            if res:
                results.append(res)

    if not results:
        logging.info("No updates")
        exit(0)

    with open(out_file, "wb") as f:
        for data in results:
            for line in data:
                f.write(line + b"\r\n")
            f.write(b"\r\n")

    logging.info(f"Updated TLE written: {out_file}")

with open("/var/lib/sattracker/last_update.txt", "w") as f:
    from datetime import datetime
    f.write(datetime.utcnow().isoformat() + "Z\n")