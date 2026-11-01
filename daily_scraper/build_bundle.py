#!/usr/bin/env python3
"""Build a portable ZIP; excludes credentials, runtime state and raw caches."""
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUT = HERE / 'dist/graden_daily_scraper.zip'
FILES = {
    HERE / 'daily_scraper.py': 'daily_scraper.py',
    HERE / 'baseline.json': 'baseline.json',
    HERE / 'requirements.txt': 'requirements.txt',
    HERE / 'README.md': 'README.md',
    HERE / 'test_daily_scraper.py': 'test_daily_scraper.py',
    ROOT / 'collect_images.py': 'collect_images.py',
    ROOT / 'extract_listing_details.py': 'extract_listing_details.py',
    ROOT / '.github/workflows/daily-property-scrape.yml': 'daily-property-scrape.yml',
}

if __name__ == '__main__':
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED) as z:
        for path, name in FILES.items():
            z.write(path, name)
    print(OUTPUT)
