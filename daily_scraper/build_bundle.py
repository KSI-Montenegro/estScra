#!/usr/bin/env python3
"""Build a portable ZIP; excludes credentials, runtime state and raw caches."""
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUT = HERE / 'dist/graden_daily_scraper.zip'
FILES = {
    HERE / 'daily_scraper.py': 'daily_scraper.py',
    HERE / 'brightdata_browser.py': 'brightdata_browser.py',
    HERE / 'process_photos.py': 'process_photos.py',
    HERE / 'photo_catalog.py': 'photo_catalog.py',
    HERE / 'photo_queue.py': 'photo_queue.py',
    HERE / 'photo_backfill.json': 'photo_backfill.json',
    HERE / 'photo-requirements.txt': 'photo-requirements.txt',
    HERE / 'test_photos.py': 'test_photos.py',
    HERE / 'baseline.json': 'baseline.json',
    HERE / 'requirements.txt': 'requirements.txt',
    HERE / 'README.md': 'README.md',
    HERE / 'DAILY_PHOTOS.md': 'DAILY_PHOTOS.md',
    HERE / 'photo_pipeline_validation.json': 'photo_pipeline_validation.json',
    HERE / 'test_daily_scraper.py': 'test_daily_scraper.py',
    ROOT / 'collect_images.py': 'collect_images.py',
    ROOT / 'extract_listing_details.py': 'extract_listing_details.py',
    ROOT / '.github/workflows/daily-property-scrape.yml': 'daily-property-scrape.yml',
    ROOT / 'property_images/all_image_urls.csv': 'property_images/all_image_urls.csv',
}
for name in ('pipeline.py', 'remove_local.py', 'watermark_mask.py', 'match_known_logo.py',
             'install_local_lama.py', 'lama_model.json', 'r2_config.json'):
    FILES[ROOT / 'property_photo_pipeline' / name] = 'property_photo_pipeline/' + name
for name in ('ocr_scan.py', 'detect_watermarks.py'):
    FILES[ROOT / 'watermark_detection' / name] = 'watermark_detection/' + name
for path in (ROOT / 'property_photo_pipeline/reference_assets').iterdir():
    if path.is_file():
        FILES[path] = 'property_photo_pipeline/reference_assets/' + path.name

if __name__ == '__main__':
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED) as z:
        for path, name in FILES.items():
            z.write(path, name)
    print(OUTPUT)
