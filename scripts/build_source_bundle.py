#!/usr/bin/env python3
"""Package an allowlisted, repository-shaped source tree without runtime data."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'dist/estScra-source.zip'
DIRECTORIES = {'daily_scraper', 'property_photo_pipeline', 'watermark_detection', 'scripts'}
SOURCE_SUFFIXES = {'.py', '.md', '.txt', '.sh', '.mjs'}
EXCLUDED_PARTS = {'private', 'runtime', 'exports', 'photo_exports', 'contact_exports',
                  'models', 'reports', 'health_reports', 'dist', '__pycache__', '.venv',
                  'cache', 'runs', 'output', 'ready', 'staging'}
CONFIGS = {'daily_scraper/baseline.json', 'property_photo_pipeline/lama_model.json',
           'property_photo_pipeline/r2_config.json'}


def include(path):
    parts = path.parts
    name = path.as_posix()
    if name in {'README.md', 'LOCAL_SETUP.md', '.gitignore', '.env.example'}:
        return True
    if parts[:2] == ('.github', 'workflows'):
        return path.suffix in {'.yml', '.yaml'}
    if len(parts) == 1:
        return path.suffix == '.py'
    if parts[0] not in DIRECTORIES or any(part in EXCLUDED_PARTS for part in parts):
        return False
    if 'fixtures' in parts or 'reference_assets' in parts:
        return path.suffix in SOURCE_SUFFIXES | {'.json', '.png'}
    return path.suffix in SOURCE_SUFFIXES or name in CONFIGS or path.name in {'.gitignore', '.env.example'}


def main():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    files = sorted(Path(name) for name in tracked if name and include(Path(name)))
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    manifest = {'repository': 'https://github.com/KSI-Montenegro/estScra', 'revision': revision,
                'layout': 'repository root',
                'omitted': ['credentials', 'runtime state', 'captured listings and contact history',
                            'model weights', 'image downloads and generated reports'],
                'files': {path.as_posix(): hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in files}}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(ROOT/path, 'estScra-source/' + path.as_posix())
        archive.writestr('estScra-source/SOURCE_BUNDLE.json', json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({'path': str(OUTPUT), 'files': len(files), 'bytes': OUTPUT.stat().st_size,
                      'revision': revision, 'sha256': hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
