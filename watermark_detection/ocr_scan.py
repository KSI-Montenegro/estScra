#!/usr/bin/env python3
"""Batch local Tesseract OCR from image URLs. Detection only; never edits photos."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor,as_completed
from collections import Counter,defaultdict
from difflib import SequenceMatcher
import cv2
import numpy as np
import detect_watermarks as d

BRANDS=('patuljak','estitor','realitica')

def token(s):return re.sub(r'[^a-z0-9]','',s.lower())

def parse_tsv(tsv,scale,width,height):
    words=[]
    for r in csv.DictReader(io.StringIO(tsv),delimiter='\t'):
        value=(r.get('text') or '').strip()
        try:confidence=float(r['conf'])
        except (ValueError,KeyError):continue
        if not value or confidence<0:continue
        x,y,w,h=(int(r[k]) for k in ('left','top','width','height'))
        box=[max(0,round(x/scale)),max(0,round(y/scale)),min(width,math.ceil((x+w)/scale)),min(height,math.ceil((y+h)/scale))]
        words.append({'text':value,'confidence':round(confidence,2),'box_original_pixels':box,
                      'line':':'.join(str(r[k]) for k in ('page_num','block_num','par_num','line_num'))})
    return words

def brand_hits(words,width,height,brands=BRANDS,min_confidence=45,similarity=0.82):
    hits=[];seen=set()
    # Single tokens first; line joins also handle "patuljak .me" spacing.
    groups=defaultdict(list)
    for word in words:groups[word['line']].append(word)
    candidates=[[w] for w in words]+[g for g in groups.values() if len(g)>1]
    for group in candidates:
        value=' '.join(w['text'] for w in group);clean=token(value)
        confidence=sum(w['confidence'] for w in group)/len(group)
        if confidence<min_confidence:continue
        for brand in brands:
            b=token(brand)
            if not b or len(clean)<max(4,len(b)*0.6) or len(clean)>len(b)*2:continue
            score=SequenceMatcher(None,clean,b).ratio()
            if b not in clean and score<similarity:continue
            x1=min(w['box_original_pixels'][0] for w in group);y1=min(w['box_original_pixels'][1] for w in group)
            x2=max(w['box_original_pixels'][2] for w in group);y2=max(w['box_original_pixels'][3] for w in group)
            key=(brand,x1,y1,x2,y2)
            if key in seen:continue
            seen.add(key);cx=(x1+x2)/(2*width);cy=(y1+y2)/(2*height)
            hits.append({'brand':brand,'recognized_text':value,'ocr_confidence':round(confidence,2),
                         'string_similarity':round(score,4),'box_original_pixels':[x1,y1,x2,y2],
                         'position':'bottom_right' if cx>=0.5 and cy>=0.7 else 'bottom_left' if cy>=0.7 else 'top' if cy<=0.3 else 'middle',
                         'is_verified_watermark':False})
    return hits

def ocr(image,binary,timeout):
    height,width=image.shape[:2]
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
    footer_start=round(height*0.70)
    footer=cv2.threshold(gray[footer_start:,:],0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
    passes=[('full_gray',gray,min(2.0,1600/max(width,height)),0),
            ('footer_otsu',footer,min(3.0,2000/width),footer_start)]
    words=[];env=os.environ.copy();env['OMP_THREAD_LIMIT']='1'
    for name,array,scale,yoffset in passes:
        if scale!=1:array=cv2.resize(array,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        data=cv2.imencode('.png',array)[1].tobytes()
        proc=subprocess.run([binary,'stdin','stdout','--oem','1','--psm','11','-l','eng','tsv'],
                            input=data,capture_output=True,timeout=timeout,env=env)
        if proc.returncode:raise RuntimeError('tesseract_exit_'+str(proc.returncode))
        found=parse_tsv(proc.stdout.decode('utf-8',errors='replace'),scale,width,height-yoffset)
        for word in found:
            word['box_original_pixels'][1]+=yoffset;word['box_original_pixels'][3]+=yoffset
            word['ocr_pass']=name;word['line']=name+':'+word['line']
        words.extend(found)
    return words

def scan_cached_image(row,downloader,cache,binary,timeout,config,brands,scan,min_free_bytes=0):
    """Bind detections to immutable captured bytes, not just a remote URL."""
    result=dict(row);result['scan_id']=scan
    original=downloader.cache/(row['image_id']+'.image')
    if not original.exists() and shutil.disk_usage(downloader.cache).free<min_free_bytes:
        return result|{'status':'download_error','error':'disk_reserve_reached','words':[],'brand_hits':[],'edit_approved':False}
    image,status=downloader.get(row)
    if image is None:return result|{'status':'download_error','error':status,'words':[],'brand_hits':[],'edit_approved':False}
    raw=original.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    result.update(source_sha256=digest,original_bytes=len(raw))
    path=cache/(row['image_id']+'_'+config+'.json');data=None
    if path.exists():
        try:data=json.loads(path.read_text())
        except (ValueError,OSError):pass
    if data and data.get('source_sha256')==digest:
        words=data['words'];width=data['width'];height=data['height'];result['ocr_cache_reused']=True
    else:
        height,width=image.shape[:2]
        try:words=ocr(image,binary,timeout)
        except Exception as exc:return result|{'status':'ocr_error','error':type(exc).__name__,'words':[],'brand_hits':[],'edit_approved':False}
        d.atomic(path,json.dumps({'words':words,'width':width,'height':height,'source_sha256':digest},ensure_ascii=False))
        result['ocr_cache_reused']=False
    hits=brand_hits(words,width,height,brands)
    return result|dict(width=width,height=height,words=words,recognized_text=' '.join(w['text'] for w in words),brand_hits=hits,
        status='known_brand_text_candidate' if hits else 'other_text_detected' if any(w['confidence']>=45 and len(token(w['text']))>=3 for w in words) else 'no_text_detected',
        edit_approved=False,interpretation='OCR finds text, not proof of a watermark; require a verified logo/mask policy before editing.')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',default='property_images/all_image_urls.csv');p.add_argument('--sample',type=int,default=0)
    p.add_argument('--output',default='watermark_detection/runs/ocr_full');p.add_argument('--cache',default='watermark_detection/cache')
    p.add_argument('--workers',type=int,default=4);p.add_argument('--timeout',type=int,default=35)
    p.add_argument('--min-free-gb',type=float,default=5,help='Pause new downloads below this local disk reserve (decimal GB)')
    p.add_argument('--brand',action='append',help='Additional known brand word; this does not prove text is a watermark')
    args=p.parse_args()
    if args.sample<0 or not 1<=args.workers<=8 or args.min_free_gb<0:p.error('Invalid sample/workers/disk reserve')
    binary=shutil.which('tesseract')
    if not binary:p.error('Install Tesseract with English language data before running')
    version=subprocess.check_output([binary,'--version'],stderr=subprocess.STDOUT,text=True).splitlines()[0]
    cv2.setNumThreads(1);rows=d.load_manifest(args.manifest);selected=d.sample_records(rows,args.sample)
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);cache=Path(args.cache)/'ocr';cache.mkdir(parents=True,exist_ok=True)
    downloader=d.Downloader(args.cache);brands=list(dict.fromkeys(BRANDS+tuple(args.brand or [])))
    scan=d.now();config=token(version)+'_eng_oem1_psm11_gray1600_footer2000_autoDPI_v3'
    ocr_locks=defaultdict(threading.Lock)
    def process(row):
        with ocr_locks[row['image_id']]:
            return scan_cached_image(row,downloader,cache,binary,args.timeout,config,brands,scan,int(args.min_free_gb*1e9))
    done={}
    print('OCR selected',len(selected),'of',len(rows),'images;',version,'; no hosted OCR or editing API calls.',flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(process,r):r for r in selected}
        for future in as_completed(futures):
            row=futures[future]
            try:r=future.result()
            except Exception as exc:r=row|{'status':'ocr_error','error':type(exc).__name__,'words':[],'brand_hits':[]}
            done[(row['property_key'],row['image_number'])]=r
            with (out/'checkpoint.jsonl').open('a') as f:f.write(json.dumps(r)+'\n');f.flush()
            if len(done)%25==0 or len(done)==len(selected):
                progress={'updated_at':d.now(),'processed':len(done),'total':len(selected),
                          'statuses':dict(Counter(item['status'] for item in done.values())),
                          'original_bytes':sum(item.get('original_bytes',0) for item in done.values()),
                          'image_editing_calls':0,'uploaded_images':0}
                d.atomic(out/'progress.json',json.dumps(progress,indent=2)+'\n')
                print('OCR processed',len(done),'/',len(selected),progress['statuses'],flush=True)
    results=[done[(r['property_key'],r['image_number'])] for r in selected]
    fields=['property_key','image_number','image_url','source_sha256','original_bytes','status','recognized_text','brand_hits','edit_approved']
    buf=io.StringIO(newline='');w=csv.DictWriter(buf,fields);w.writeheader();groups=defaultdict(list)
    for r in results:
        flat={k:json.dumps(r.get(k,[]),ensure_ascii=False) if k=='brand_hits' else r.get(k,'') for k in fields}
        w.writerow(flat);groups[r['property_key']].append(flat)
    d.atomic(out/'ocr_results.csv',buf.getvalue());d.atomic(out/'results.json',json.dumps(results,indent=2,ensure_ascii=False)+'\n')
    for key,items in groups.items():
        b=io.StringIO(newline='');writer=csv.DictWriter(b,fields);writer.writeheader();writer.writerows(items)
        d.atomic(out/'by_property'/key/'images.csv',b.getvalue())
    summary={'created_at':d.now(),'tesseract_version':version,'method':'local_Tesseract_LSTM_OCR; no paid vision API',
             'ocr_uses_recognition_models':True,'manifest_images':len(rows),'selected_images':len(selected),'full_manifest_scan':len(selected)==len(rows),
             'statuses':dict(Counter(r['status'] for r in results)),'known_brands':brands,'hosted_ocr_calls':0,'image_editing_calls':0,
             'images_automatically_approved_for_edit':0,'word_box_coordinates':'EXIF-oriented original pixels',
             'interpretation':'known_brand_text_candidate is a text finding, not a verified watermark; no_text_detected does not mean clean',
             'real_image_accuracy_measured':False}
    d.atomic(out/'summary.json',json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
