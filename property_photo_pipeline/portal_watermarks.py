"""Calibrated central portal stamps. OCR never supplies an erasure mask.

Published header assets identify the word shapes. The photo stamps have different
mascot details and taglines, so those small footprints are masked in full rather
than treating a header's coloured silhouette as the photo stamp's alpha channel.
Unrecognised placements stay in the review queue.
"""
import io
import json
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from pipeline import optimize, sha
from remove_local import focused_brand_ocr, repair_rgb

VERSION = 'portal-centers-v2-byte-optimized'
ASSETS = Path(__file__).resolve().parent / 'reference_assets'
WORD_BOXES = {'autodiler': (138, 40, 420, 86), 'oglasi': (103, 35, 424, 103)}


@lru_cache(maxsize=1)
def references():
    config = json.loads((ASSETS / 'portal_centers.json').read_text())
    result = {}
    for brand, ref in config['references'].items():
        data = (ASSETS / ref['file']).read_bytes()
        if sha(data) != ref['sha256']:
            raise ValueError('Portal reference checksum mismatch')
        rgb = np.asarray(Image.open(io.BytesIO(data)).convert('RGBA'))
        result[brand] = (rgb[:, :, 3] / 255).astype(np.float32) if brand == 'autodiler' else (
            rgb[:, :, :3].min(axis=2) < 240).astype(np.float32)
    return result


def highpass(gray):
    return gray - cv2.GaussianBlur(gray, (0, 0), 2)


def whiten(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    detail = highpass(gray)
    return np.tanh(detail / (np.sqrt(cv2.GaussianBlur(detail * detail, (0, 0), 6)) + .01))


def geometry(brand, width, height, scale=None):
    reference = references()[brand]
    if scale is None:
        scale = width / 960 if brand == 'autodiler' else 1.325
    if brand == 'autodiler':
        x = round((width - reference.shape[1] * scale) / 2 + 20 * scale)
        y = round(height / 2 - .05 * width)
    else:
        x = round((width - reference.shape[1] * scale) / 2 + 2)
        y = round((height - reference.shape[0] * scale) / 2 + 5)
    return x, y, scale


def word_template(brand, scale):
    reference = references()[brand]
    resized = cv2.resize(reference, (round(reference.shape[1] * scale), round(reference.shape[0] * scale)))
    a, b, c, d = (round(v * scale) for v in WORD_BOXES[brand])
    return highpass(resized)[b:d, a:c].astype(np.float32), (a, b, c, d)


def locate(gray, brand, broad=False):
    height, width = gray.shape
    base = width / 960 if brand == 'autodiler' else 1.325
    # Some source photos were resized before the stamp was applied. Only a
    # bounded central region is searched; arbitrary text cannot set a mask.
    factors = (.7, .75, .8, .825, .85, .875, .9, .95, .975, 1., 1.025, 1.05, 1.1) if brand == 'autodiler' else (1.,)
    best = None
    for factor in factors:
        x, y, scale = geometry(brand, width, height, base * factor)
        template, (a, b, c, d) = word_template(brand, scale)
        mx = round(width * .04) if brand == 'autodiler' else 8
        my = round(height * .15) if brand == 'autodiler' else 8
        left, top = max(0, x + a - mx), max(0, y + b - my)
        right, bottom = min(width, x + c + mx + 1), min(height, y + d + my + 1)
        region = gray[top:bottom, left:right]
        if min(template.shape) < 5 or region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
            continue
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, point = cv2.minMaxLoc(scores)
        # Noise controls are away from the true peak. A small Oglasi search
        # otherwise has most of its samples inside the correlation peak.
        null_region = gray[max(0, y + b - 30):min(height, y + d + 31),
                           max(0, x + a - 50):min(width, x + c + 51)]
        null = cv2.matchTemplate(null_region, template, cv2.TM_CCOEFF_NORMED)
        null_q99 = float(np.quantile(null, .99))
        fit = {'brand': brand, 'score': float(score), 'null_q99': null_q99,
               'x': point[0] + left - a, 'y': point[1] + top - b, 'scale': scale}
        if best is None or score > best['score']:
            best = fit
    if best is None:
        return None
    threshold = .17 if broad else .12
    best['eligible'] = best['score'] >= threshold and best['score'] - best['null_q99'] >= .055
    return best


def mask_for_fit(shape, fit):
    """Word glyphs plus the calibrated mascot/tagline footprints, with edge padding."""
    height, width = shape[:2]
    brand, scale = fit['brand'], fit['scale']
    reference = references()[brand]
    local = np.zeros(reference.shape, np.uint8)
    a, b, c, d = WORD_BOXES[brand]
    local[b:d, a:c] = (reference[b:d, a:c] > .03).astype(np.uint8) * 255
    if brand == 'autodiler':
        local[:, :140] = 255  # The photo mascot differs from the header artwork.
        local[88:, 115:] = 255  # Baseline and www.autodiler.me, not the header tagline.
        local[17:41, 410:] = 255  # Registered-mark footprint.
    else:
        local[:, :108] = 255
        local[99:, 180:] = 255
    local = cv2.resize(local, (round(reference.shape[1] * scale), round(reference.shape[0] * scale)),
                       interpolation=cv2.INTER_NEAREST)
    x, y = fit['x'], fit['y']
    h, w = local.shape
    if not (0 <= x < x + w <= width and 0 <= y < y + h <= height):
        raise ValueError('Portal stamp outside calibrated bounds')
    mask = np.zeros((height, width), np.uint8)
    mask[y:y+h, x:x+w] = local
    pad = max(2, round(scale * 3))
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * pad + 1, 2 * pad + 1)))
    return mask


def build_mask(rgb, source, include_other=False):
    if source not in WORD_BOXES or min(rgb.shape[:2]) < 600:
        return None, {'eligible': False, 'reason': 'unsupported_image_geometry', 'version': VERSION}
    gray = whiten(rgb)
    fits = {brand: locate(gray, brand, broad=brand != source) for brand in (WORD_BOXES if include_other else (source,))}
    own = fits[source]
    meta = {'eligible': False, 'version': VERSION, 'detections': fits,
            'reference_config_sha256': sha((ASSETS / 'portal_centers.json').read_bytes())}
    if not own or not own['eligible']:
        return None, {**meta, 'reason': 'uncertain_primary_portal_stamp'}
    other = next(brand for brand in WORD_BOXES if brand != source)
    second = fits.get(other)
    if second and not second['eligible'] and second['score'] >= .12:
        return None, {**meta, 'reason': 'uncertain_second_portal_stamp'}
    selected = [fit for fit in fits.values() if fit and fit['eligible']]
    mask = np.zeros(rgb.shape[:2], np.uint8)
    try:
        for fit in selected:
            mask |= mask_for_fit(rgb.shape, fit)
    except ValueError:
        return None, {**meta, 'reason': 'stamp_outside_image'}
    fraction = float(np.count_nonzero(mask) / mask.size)
    if not 0 < fraction <= .10:
        return None, {**meta, 'reason': 'mask_area_limit', 'mask_fraction': fraction}
    ys, xs = np.nonzero(mask)
    meta.update(eligible=True, reason='calibrated_word_shape_and_placement', mask_fraction=fraction,
                stamps=selected, overlay_box_original_pixels=[int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
                mask_sha256=sha(mask.tobytes()), mask_kind='word_glyphs_and_mascot_tagline_footprints')
    return mask, meta


def residual_scores(rgb, stamps, source_dimensions=None):
    gray = whiten(rgb)
    height, width = rgb.shape[:2]
    source_width, source_height = source_dimensions or (width, height)
    sx, sy = width / source_width, height / source_height
    result = []
    for fit in stamps:
        template, (a, b, c, d) = word_template(fit['brand'], fit['scale'])
        x, y = fit['x'], fit['y']
        left, top, right, bottom = round((x+a)*sx), round((y+b)*sy), round((x+c)*sx), round((y+d)*sy)
        if sx != 1 or sy != 1:
            template = cv2.resize(template, (right-left, bottom-top))
        region = gray[max(0, top-2):min(height, bottom+3), max(0, left-2):min(width, right+3)]
        if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
            raise ValueError('Residual check outside image')
        score = float(cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))[1])
        result.append({'brand': fit['brand'], 'before': fit['score'], 'after': score,
                       'passed': score < .10 and score < fit['score'] * .5})
    return result


def repair(rgb, mask, metadata, infer, model_sha, source_sha, ocr=focused_brand_ocr):
    if not metadata.get('eligible') or not metadata.get('stamps'):
        raise ValueError('An eligible calibrated mask is required')
    if mask.shape != rgb.shape[:2] or sha(mask.tobytes()) != metadata['mask_sha256']:
        raise ValueError('Portal mask checksum or dimensions changed')
    repaired, region = repair_rgb(rgb, mask, infer, context=128)
    pre_encoding = residual_scores(repaired, metadata['stamps'])
    focused = ocr(repaired, metadata['overlay_box_original_pixels'])
    proof = {'policy': VERSION, 'model_sha256': model_sha, 'source_sha256': source_sha,
             'mask_sha256': sha(mask.tobytes()), 'mask_metadata': metadata,
             'outside_mask_unchanged_before_encoding': True, 'dimensions_preserved_before_encoding': repaired.shape == rgb.shape,
             'region': region, 'residual_pre_encoding': pre_encoding, 'focused_ocr': focused,
             'qc': 'failed', 'certification': 'automatic_checks_only_not_certified'}
    if not all(row['passed'] for row in pre_encoding) or focused.get('brand_detected') is not False:
        return None, proof
    output = io.BytesIO()
    Image.fromarray(repaired).save(output, format='PNG')
    encoded, size, original_size, quality = optimize(output.getvalue(), 300000, 400000, 75)
    # Recheck the delivered lossy image, not just the intermediate pixels.
    delivered = np.asarray(Image.open(io.BytesIO(encoded)).convert('RGB'))
    if len(encoded) > 400000 or (delivered.shape[1], delivered.shape[0]) != size:
        proof['qc'] = 'encoded_size_or_dimensions_failed'
        return None, proof
    post_encoding = residual_scores(delivered, metadata['stamps'], source_dimensions=original_size)
    proof.update(residual_post_encoding=post_encoding, output_bytes=len(encoded), quality=quality,
                 original_dimensions=list(original_size), output_dimensions=list(size),
                 dimensions_preserved=size == original_size, resized_for_byte_limit=size != original_size)
    if not all(row['passed'] for row in post_encoding):
        return None, proof
    proof['qc'] = 'portal_word_shapes_and_focused_ocr_passed'
    return encoded, proof
