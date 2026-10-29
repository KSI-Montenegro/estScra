#!/usr/bin/env python3
"""Non-AI watermark triage: edge-template matching and recurring fixed overlays.

No OCR, learned models, embeddings, or image-analysis API is used. A negative
result is no_match, never a guarantee that an image is unwatermarked.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
from collections import defaultdict,Counter
from datetime import datetime,timezone
from urllib import request,error,parse
import cv2
import numpy as np
from PIL import Image,ImageOps

SIZE=(640,480)
REGIONS={'top_left':(0,0,220,120),'top_right':(420,0,640,120),
         'bottom_left':(0,360,220,480),'bottom_right':(420,360,640,480),
         'center':(160,140,480,340)}

def now():return datetime.now(timezone.utc).isoformat()
def atomic(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:f.write(value);f.flush();os.fsync(f.fileno())
    tmp.replace(p)

def load_manifest(path):
    records=[];seen=set()
    with open(path,encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            key=row['property_key'];url=row['image_url'];n=row['image_number']
            if (key,n) in seen:raise ValueError('Duplicate property/image number in manifest')
            seen.add((key,n));u=parse.urlsplit(url)
            if u.scheme!='https' or not u.hostname or u.username or u.password:raise ValueError('Expected credential-free HTTPS image URLs')
            records.append({'property_key':key,'image_number':n,'image_url':url,'source':key.rsplit('_',1)[0],
                            'image_id':hashlib.sha256(url.encode()).hexdigest()[:24]})
    return records

def sample_records(rows,count):
    """Balance sources, first selecting different properties, then additional photos."""
    if not count or count>=len(rows):return rows
    sources=defaultdict(list)
    for r in rows:sources[r['source']].append(r)
    ordered={}
    for source,items in sources.items():
        props=set();first=[];other=[]
        for r in items:
            if r['property_key'] not in props:first.append(r);props.add(r['property_key'])
            else:other.append(r)
        ordered[source]=iter(first+other)
    selected=[]
    while len(selected)<count and ordered:
        for source in list(ordered):
            try:selected.append(next(ordered[source]))
            except StopIteration:del ordered[source]
            if len(selected)>=count:break
    return selected

def decode_image(raw):
    with Image.open(io.BytesIO(raw)) as im:
        if im.width*im.height>50_000_000:raise ValueError('image_exceeds_pixel_limit')
        rgb=np.array(ImageOps.exif_transpose(im).convert('RGB'))
    return cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR)

def edges_of(image):
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    return cv2.Canny(gray,65,150)

def phash(image):
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY);small=cv2.resize(gray,(32,32));coeff=cv2.dct(np.float32(small))[:8,:8].reshape(-1)[1:]
    bits=coeff>np.median(coeff)
    value=0
    for b in bits:value=(value<<1)|int(b)
    return value

def hamming(a,b):return (int(a)^int(b)).bit_count()

def prepare(image):
    h,w=image.shape[:2];scaled=cv2.resize(image,SIZE,interpolation=cv2.INTER_AREA)
    ratio_bucket=round((w/h)/0.1)*0.1
    return scaled,edges_of(scaled),phash(image),f'{ratio_bucket:.1f}'

def template_matches(image,templates,threshold=0.72,scales=(0.5,0.75,1.0,1.25,1.5)):
    """Match observed template edge shapes; coordinates use the original image."""
    if not templates:return []
    h0,w0=image.shape[:2];factor=min(1.0,960/max(h0,w0))
    work=cv2.resize(image,None,fx=factor,fy=factor) if factor<1 else image
    target=edges_of(work);matches=[]
    for name,template in templates:
        best=None
        for scale in scales:
            factor_t=factor*scale
            width=max(1,round(template.shape[1]*factor_t));height=max(1,round(template.shape[0]*factor_t))
            if width<12 or height<8 or width>target.shape[1] or height>target.shape[0]:continue
            t=cv2.resize(template,(width,height));e=edges_of(t)
            if np.count_nonzero(e)<30 or np.std(e)<3:continue
            scoremap=cv2.matchTemplate(target,e,cv2.TM_CCOEFF_NORMED)
            _,score,_,loc=cv2.minMaxLoc(scoremap)
            if not math.isfinite(score):continue
            if best is None or score>best['score']:
                best={'template':name,'score':round(float(score),4),
                      'box_original_pixels':[round(loc[0]/factor),round(loc[1]/factor),round((loc[0]+width)/factor),round((loc[1]+height)/factor)]}
        if best and best['score']>=threshold:matches.append(best)
    return matches

class RepeatDetector:
    """Find stable edge shapes shared by distinct, nonduplicate property photos."""
    def __init__(self,min_properties=6,frequency=0.75,min_pixels=45):
        self.min_properties=min_properties;self.frequency=frequency;self.min_pixels=min_pixels
        self.groups={};self.models={}
    def add(self,source,ratio,property_key,image_hash,edges):
        key=(source,ratio)
        g=self.groups.setdefault(key,{'count':0,'properties':set(),'hashes':[],'sum':np.zeros((SIZE[1],SIZE[0]),np.uint32)})
        if property_key in g['properties'] or any(hamming(image_hash,h)<=7 for h in g['hashes']):return False
        g['properties'].add(property_key);g['hashes'].append(image_hash);g['count']+=1
        g['sum']+=(cv2.dilate(edges,np.ones((3,3),np.uint8))>0).astype(np.uint32)
        return True
    def build(self):
        for key,g in self.groups.items():
            if g['count']<self.min_properties:continue
            stable=(g['sum']/g['count']>=self.frequency).astype(np.uint8)*255
            models=[]
            for region,(x1,y1,x2,y2) in REGIONS.items():
                roi=stable[y1:y2,x1:x2]
                if np.count_nonzero(roi)>roi.size*0.35:continue
                joined=cv2.dilate(roi,np.ones((5,5),np.uint8))
                n,labels,stats,_=cv2.connectedComponentsWithStats(joined)
                for label in range(1,n):
                    x,y,w,h,area=map(int,stats[label]);mask=((labels==label)&(roi>0)).astype(np.uint8)
                    yy,xx=np.nonzero(mask);pixels=len(xx)
                    if pixels<self.min_pixels or w<16 or h<9 or w*h<150:continue
                    if len(set(xx))<8 or len(set(yy))<6:continue
                    eigen=np.linalg.eigvalsh(np.cov(np.stack([xx,yy])))
                    if eigen[-1]<=0 or eigen[0]/eigen[-1]<0.025:continue
                    models.append({'region':region,'mask':mask,'box':[x+x1,y+y1,x+x1+w,y+y1+h],
                                   'support_properties':g['count'],'stable_edge_pixels':pixels})
            self.models[key]=models
        return self.models
    def score(self,source,ratio,edges,threshold=0.90):
        dilated=cv2.dilate(edges,np.ones((3,3),np.uint8))>0;matches=[]
        for model in self.models.get((source,ratio),[]):
            x1,y1,x2,y2=REGIONS[model['region']];mask=model['mask']>0
            score=float(np.count_nonzero(dilated[y1:y2,x1:x2]&mask)/np.count_nonzero(mask))
            if score>=threshold:
                matches.append({k:v for k,v in model.items() if k!='mask'}|{'score':round(score,4),'box_normalized_640x480':model['box']})
        return matches

class Downloader:
    def __init__(self,cache):
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.locks=defaultdict(threading.Lock);self.image_locks=defaultdict(threading.Lock);self.next={};self.paused={}
    def get(self,row):
        with self.image_locks[row['image_id']]:return self._get(row)
    def _get(self,row):
        path=self.cache/(row['image_id']+'.image')
        if path.exists():return decode_image(path.read_bytes()),'cached'
        host=parse.urlsplit(row['image_url']).hostname
        for attempt in range(2):
            try:
                with self.locks[host]:
                    if host in self.paused:return None,'host_paused_after_rate_limit'
                    time.sleep(max(0,self.next.get(host,0)-time.monotonic()));self.next[host]=time.monotonic()+0.25
                req=request.Request(row['image_url'],headers={'User-Agent':'Mozilla/5.0 (compatible; NonAIWatermarkCheck/1.0)'})
                with request.urlopen(req,timeout=20) as response:raw=response.read(16_000_001)
                if len(raw)>16_000_000:raise ValueError('image_exceeds_download_limit')
                image=decode_image(raw);tmp=path.with_suffix('.tmp');tmp.write_bytes(raw);tmp.replace(path)
                return image,'downloaded'
            except error.HTTPError as exc:
                # Authentication/rate restrictions are not retried or rotated.
                if exc.code==429:self.paused[host]=exc.headers.get('Retry-After','unknown')
                if exc.code not in (500,502,503,504) or attempt:return None,'http_'+str(exc.code)
            except Exception as exc:
                if attempt:return None,type(exc).__name__
            time.sleep(1.5)
        return None,'download_failed'

def write_reports(output,rows,all_count,models,args):
    fields=['property_key','image_number','image_url','status','template_score','repeated_overlay_score','evidence_file','notes']
    buf=io.StringIO(newline='');w=csv.DictWriter(buf,fields);w.writeheader()
    by_property=defaultdict(list)
    for r in rows:
        flat={k:r.get(k,'') for k in fields};w.writerow(flat);by_property[r['property_key']].append(flat)
    atomic(output/'watermark_results.csv',buf.getvalue())
    for key,items in by_property.items():
        text=io.StringIO(newline='');writer=csv.DictWriter(text,fields);writer.writeheader();writer.writerows(items)
        atomic(output/'by_property'/key/'images.csv',text.getvalue())
    successful=sum(r['status'] not in ('download_error','decode_error') for r in rows)
    summary={'created_at':now(),'scan_id':args.scan_id,'method':'classical_computer_vision_no_AI_no_OCR','manifest_images':all_count,'selected_images':len(rows),
             'images_analyzed':successful,'images_with_reference_comparisons':sum(r['status'] not in ('download_error','decode_error','insufficient_reference') for r in rows),'full_manifest_scan':len(rows)==all_count,'statuses':dict(Counter(r['status'] for r in rows)),
             'known_templates':len(args.template),'template_threshold':args.template_threshold,
             'repeat_min_distinct_properties':args.min_properties,'repeat_frequency':args.repeat_frequency,
             'repeated_group_models':sum(len(v) for v in models.values()),
             'interpretation':'template_match = supplied-pattern evidence; repeated_overlay_candidate = review; no_match does not prove no watermark',
             'accuracy_measured_on_real_images':False,'automated_removal_or_deletion':False}
    atomic(output/'summary.json',json.dumps(summary,indent=2)+'\n');return summary

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',default='property_images/all_image_urls.csv');p.add_argument('--output',default='watermark_detection/runs/full')
    p.add_argument('--cache',default='watermark_detection/cache');p.add_argument('--sample',type=int,default=0,help='Balanced subset; 0 scans full manifest')
    p.add_argument('--workers',type=int,default=4);p.add_argument('--template',action='append',default=[],help='Known watermark crop; repeat for more templates')
    p.add_argument('--template-threshold',type=float,default=0.72)
    p.add_argument('--min-properties',type=int,default=6);p.add_argument('--repeat-frequency',type=float,default=0.75)
    p.add_argument('--repeat-threshold',type=float,default=0.90);p.add_argument('--skip-repeat',action='store_true')
    args=p.parse_args();args.scan_id=now()
    if args.sample<0 or not 1<=args.workers<=16 or args.min_properties<3:p.error('Invalid sample/workers/min-properties')
    if not all(0<v<=1 for v in (args.template_threshold,args.repeat_frequency,args.repeat_threshold)):p.error('Thresholds must be within (0,1]')
    cv2.setNumThreads(1)
    if args.skip_repeat and not args.template:p.error('--skip-repeat requires at least one --template')
    all_rows=load_manifest(args.manifest);selected=sample_records(all_rows,args.sample)
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True);features=output/'features';features.mkdir(exist_ok=True)
    templates=[(Path(name).name,decode_image(Path(name).read_bytes())) for name in args.template]
    detector=RepeatDetector(args.min_properties,args.repeat_frequency);downloader=Downloader(args.cache);results={}
    def process(row):
        image,status=downloader.get(row)
        if image is None:return row|{'status':'download_error','notes':status}
        _,edges,digest,ratio=prepare(image)
        np.savez_compressed(features/(row['image_id']+'.npz'),edges=np.packbits(edges>0),phash=np.uint64(digest),ratio=ratio)
        matches=template_matches(image,templates,args.template_threshold)
        return row|{'status':'template_match' if matches else 'no_match','template_matches':matches,
                    'template_score':max((m['score'] for m in matches),default=0),'ratio':ratio,'width':image.shape[1],'height':image.shape[0],
                    'notes':'no_match is not a clean-image guarantee','image_hash':digest}
    print('Selected',len(selected),'of',len(all_rows),'images; no AI/OCR used.',flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(process,r):r for r in selected}
        for future in as_completed(futures):
            row=futures[future]
            try:result=future.result()
            except Exception as exc:result=row|{'status':'decode_error','notes':type(exc).__name__}
            result.update(scan_id=args.scan_id,analyzed_at=now())
            results[(row['property_key'],row['image_number'])]=result
            with (output/'checkpoint.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(result)+'\n');f.flush()
            if len(results)%25==0:print('Downloaded/analyzed',len(results),'/',len(selected),flush=True)
    ordered=[results[(r['property_key'],r['image_number'])] for r in selected]
    if not args.skip_repeat:
        # Deterministic reference choice, independent of worker completion order.
        for r in ordered:
            if r['status'] in ('download_error','decode_error'):continue
            with np.load(features/(r['image_id']+'.npz')) as data:edges=np.unpackbits(data['edges'],count=SIZE[0]*SIZE[1]).reshape(SIZE[1],SIZE[0]).astype(np.uint8)*255
            detector.add(r['source'],r['ratio'],r['property_key'],r['image_hash'],edges)
        detector.build()
        for r in ordered:
            if r['status'] in ('download_error','decode_error'):continue
            with np.load(features/(r['image_id']+'.npz')) as data:edges=np.unpackbits(data['edges'],count=SIZE[0]*SIZE[1]).reshape(SIZE[1],SIZE[0]).astype(np.uint8)*255
            if not templates and not detector.models.get((r['source'],r['ratio'])):
                r['status']='insufficient_reference';r['notes']='No reference shapes: insufficient distinct comparable properties or no stable repeated pattern. This is inconclusive.'
            matches=detector.score(r['source'],r['ratio'],edges,args.repeat_threshold)
            r['repeated_matches']=matches;r['repeated_overlay_score']=max((m['score'] for m in matches),default=0)
            if matches and r['status']!='template_match':r['status']='repeated_overlay_candidate'
    model_info=[]
    for key,models in detector.models.items():
        for i,model in enumerate(models):
            name=f'{key[0]}_{key[1]}_{i}.png';path=output/'repeat_masks'/name;path.parent.mkdir(exist_ok=True)
            cv2.imwrite(str(path),model['mask']*255)
            model_info.append({'source':key[0],'aspect_bucket':key[1],**{k:v for k,v in model.items() if k!='mask'},'mask_file':str(path.relative_to(output))})
    atomic(output/'repeat_models.json',json.dumps(model_info,indent=2)+'\n')
    for r in ordered:
        if r['status'] not in ('template_match','repeated_overlay_candidate'):continue
        image=decode_image((Path(args.cache)/(r['image_id']+'.image')).read_bytes())
        for match in r.get('template_matches',[]):
            x1,y1,x2,y2=match['box_original_pixels'];cv2.rectangle(image,(x1,y1),(x2,y2),(0,0,255),3)
        for match in r.get('repeated_matches',[]):
            x1,y1,x2,y2=match['box_normalized_640x480'];h,w=image.shape[:2]
            cv2.rectangle(image,(round(x1*w/640),round(y1*h/480)),(round(x2*w/640),round(y2*h/480)),(0,165,255),3)
        path=output/'evidence'/(r['image_id']+'.jpg');path.parent.mkdir(exist_ok=True);cv2.imwrite(str(path),image)
        r['evidence_file']=str(path.relative_to(output));r['notes']='Review marked region; classical matching can produce false positives.'
    atomic(output/'results.json',json.dumps(ordered,indent=2)+'\n')
    print(json.dumps(write_reports(output,ordered,len(all_rows),detector.models,args),indent=2),flush=True)

if __name__=='__main__':main()
