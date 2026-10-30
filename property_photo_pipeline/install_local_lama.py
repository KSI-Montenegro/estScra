#!/usr/bin/env python3
"""Fetch the pinned LaMa release model with TLS, size and SHA-256 verification."""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    root = Path(__file__).resolve().parent
    config = json.loads((root/'lama_model.json').read_text())
    target = root/'models'/'big-lama.pt'
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        if target.stat().st_size == config['expected_bytes'] and hashlib.sha256(target.read_bytes()).hexdigest() == config['sha256']:
            print('Pinned LaMa model already verified.'); return
        raise ValueError('Existing model differs from the pinned release; do not load it')
    partial = target.with_suffix('.pt.download')
    digest = hashlib.sha256(); size = 0
    with urlopen(Request(config['source_url'], headers={'User-Agent':'GRADEN-Local-Inpainting/1.0'}),timeout=120) as response, partial.open('wb') as output:
        while True:
            chunk = response.read(1048576)
            if not chunk: break
            output.write(chunk); digest.update(chunk); size += len(chunk)
    if size != config['expected_bytes'] or digest.hexdigest() != config['sha256']:
        raise ValueError('Downloaded model verification failed; partial file was not promoted')
    partial.replace(target)
    print('Pinned LaMa model downloaded and verified.')


if __name__ == '__main__': main()
