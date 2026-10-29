#!/usr/bin/env python3
"""Measure local WebP encoding without creating publishable images or clearances."""
import argparse
import json
import statistics
from pathlib import Path
from pipeline import optimize, sha


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-dir', required=True)
    p.add_argument('--limit', type=int, default=60)
    p.add_argument('--output', required=True, help='Path to JSON measurements')
    p.add_argument('--target-bytes', type=int, default=300000)
    p.add_argument('--max-bytes', type=int, default=400000)
    p.add_argument('--quality-floor', type=int, default=75)
    a = p.parse_args()
    if a.limit < 1:
        p.error('limit must be positive')
    records, errors = [], []
    for path in sorted(Path(a.input_dir).glob('*.image')):
        if len(records) == a.limit:
            break
        try:
            original = path.read_bytes()
            encoded, size, original_size, quality = optimize(original, a.target_bytes, a.max_bytes, a.quality_floor)
            records.append({'source_sha256': sha(original), 'source_bytes': len(original),
                            'original_oriented_dimensions': list(original_size), 'webp_bytes': len(encoded),
                            'dimensions': list(size), 'quality': quality})
        except (OSError, ValueError) as exc:
            errors.append({'file': path.name, 'error': str(exc)})
    sizes = sorted(x['webp_bytes'] for x in records)
    result = {'purpose': 'encoding measurement only; no clearance or publishable files',
              'settings': {'target_bytes': a.target_bytes, 'max_bytes': a.max_bytes, 'quality_floor': a.quality_floor},
              'count': len(records), 'errors': errors, 'records': records,
              'size_bytes': {'min': min(sizes), 'median': statistics.median(sizes), 'max': max(sizes)} if sizes else {}}
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'count': len(records), 'errors': len(errors), 'size_bytes': result['size_bytes']}))
    return 0 if len(records) == a.limit else 1


if __name__ == '__main__':
    raise SystemExit(main())
