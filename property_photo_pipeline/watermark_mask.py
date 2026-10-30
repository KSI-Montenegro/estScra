"""Locate the known Patuljak overlay and map a calibrated pixel mask onto it."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image


@lru_cache(maxsize=1)
def assets():
    folder = Path(__file__).with_name('reference_assets')
    config = json.loads((folder/'patuljak_overlay_mask.json').read_text())
    data = (folder/'patuljak_overlay_mask.png').read_bytes()
    if hashlib.sha256(data).hexdigest() != config['mask_asset_sha256']:
        raise ValueError('Calibrated mask checksum mismatch')
    mask = np.asarray(Image.open(folder/'patuljak_overlay_mask.png').convert('L'))
    if list(mask.shape[::-1]) != config['canonical_overlay_size']:
        raise ValueError('Calibrated mask dimensions mismatch')
    return config, mask


def build_mask(row, original_rgb):
    height, width = original_rgb.shape[:2]
    empty = np.zeros((height, width), np.uint8)
    score = float(row.get('logo_template_score') or 0)
    box = json.loads(row.get('logo_box_original_pixels') or '[]')
    if not np.isfinite(score) or score < .65 or len(box) != 4:
        return empty, {'eligible': False, 'reason': 'Known-logo score or coordinates missing'}
    bx1, by1, bx2, by2 = map(int, box)
    if not (0 <= bx1 < bx2 <= width and 0 <= by1 < by2 <= height):
        return empty, {'eligible': False, 'reason': 'Graphic coordinates outside captured image'}
    config, canonical = assets()
    sx, sy = (bx2-bx1)/75, (by2-by1)/93
    if abs(sx-sy) > .08:
        return empty, {'eligible': False, 'reason': 'Inconsistent graphic scale'}
    support = 'high_score_graphic' if row.get('triage_group') == 'known_logo_template_candidate' and score >= .85 else ''
    if not support:
        # A weaker graphic must agree with independently recognized brand text at its expected pose.
        predicted = [bx1-154*sx, by1+41*sy, bx1+5*sx, by1+92*sy]
        for hit in json.loads(row.get('ocr_hits_json') or '[]'):
            if hit['brand'] != 'patuljak' or not hit['position'].startswith('bottom_') or hit['ocr_confidence'] < 45:
                continue
            a,b,c,d = hit['box_original_pixels']
            intersection = max(0,min(c,predicted[2])-max(a,predicted[0]))*max(0,min(d,predicted[3])-max(b,predicted[1]))
            coverage = intersection/max(1,(c-a)*(d-b))
            if coverage >= .5 and .25 <= (c-a)/(predicted[2]-predicted[0]) <= 2.3 and .25 <= (d-b)/(predicted[3]-predicted[1]) <= 2:
                support = 'graphic_plus_independent_brand_text'; break
    if not support:
        return empty, {'eligible': False, 'reason': 'Graphic and independently located brand text do not corroborate'}
    ox, oy = config['overlay_offset_from_mascot_top_left']
    x0, y0 = int(np.floor(bx1+ox*sx)), int(np.floor(by1+oy*sy))
    mask_width, mask_height = max(1, round(canonical.shape[1]*sx)), max(1, round(canonical.shape[0]*sy))
    scaled = cv2.resize(canonical, (mask_width, mask_height), interpolation=cv2.INTER_NEAREST)
    x1, y1, x2, y2 = max(0,x0), max(0,y0), min(width,x0+mask_width), min(height,y0+mask_height)
    if x1 >= x2 or y1 >= y2:
        return empty, {'eligible': False, 'reason': 'Empty clipped overlay'}
    empty[y1:y2,x1:x2] = scaled[y1-y0:y2-y0,x1-x0:x2-x0]
    count = int(np.count_nonzero(empty)); fraction = count/(height*width)
    if not count or fraction > .22:
        return np.zeros_like(empty), {'eligible': False, 'reason': 'Mask empty or covers excessive photo area'}
    return empty, {'eligible': True, 'policy_id':config['policy_id'],
        'overlay_box_original_pixels':[x1,y1,x2,y2], 'graphic_box_original_pixels':box,
        'template_score':score, 'detection_support':support, 'mask_pixels':count, 'mask_fraction':round(fraction,6),
        'canonical_mask_sha256':config['mask_asset_sha256']}
