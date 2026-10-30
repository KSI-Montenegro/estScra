"""Fixed-pose edge correlation for Patuljak text; diagnostic only, not a QC decision."""
import json
import math

import cv2
import numpy as np

# Reference boxes in the source image whose mascot box is [530,400,605,493].
BOXES = {'word': (376, 441, 535, 492), 'dotme': (598, 447, 665, 490)}


def shape_scores(image_rgb, logo_box, templates):
    if isinstance(logo_box, str):
        logo_box = json.loads(logo_box)
    if len(logo_box) != 4:
        raise ValueError('logo bounding box required')
    bx1, by1, bx2, by2 = map(float, logo_box)
    sx, sy = (bx2 - bx1) / 75, (by2 - by1) / 93
    if not (math.isfinite(sx) and math.isfinite(sy) and sx > 0 and sy > 0 and abs(sx-sy) < .08):
        raise ValueError('invalid logo scale')
    height, width = image_rgb.shape[:2]
    result = {}
    for name, (x1, y1, x2, y2) in BOXES.items():
        x = round(bx1 + (x1-530)*sx)
        y = round(by1 + (y1-400)*sy)
        w = max(2, round((x2-x1)*sx))
        h = max(2, round((y2-y1)*sy))
        xx1, yy1, xx2, yy2 = max(0,x), max(0,y), min(width,x+w), min(height,y+h)
        if xx1 >= xx2 or yy1 >= yy2 or (xx2-xx1)*(yy2-yy1) < .7*w*h:
            result[name] = {'score': None, 'coverage': 0.0, 'box': [xx1, yy1, xx2, yy2]}
            continue
        reference = cv2.resize(templates[name], (w, h), interpolation=cv2.INTER_AREA if sx < 1 else cv2.INTER_LINEAR)
        reference = reference[yy1-y:yy2-y, xx1-x:xx2-x]
        image = image_rgb[yy1:yy2, xx1:xx2]
        def grad(rgb):
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
            return np.stack((cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
                             cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)), axis=2)
        a, b = grad(reference), grad(image)
        denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
        score = float(np.sum(a*b) / denominator) if denominator > 1e-12 else None
        result[name] = {'score': score, 'coverage': (xx2-xx1)*(yy2-yy1)/(w*h),
                        'box': [xx1, yy1, xx2, yy2]}
    return result
