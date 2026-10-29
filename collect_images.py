#!/usr/bin/env python3
"""Collect observed property gallery URLs; no photo downloads or phone reveals."""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error, parse
from lxml import html

HOSTS = {'estitor.com':'estitor', 'patuljak.me':'patuljak', 'www.realitica.com':'realitica', 'realitica.com':'realitica'}
PATTERNS = {'estitor': r'/id-(\d+)(?:/|$)', 'patuljak':r'--(\d+)(?:/|$)', 'realitica':r'/listing/(\d+)(?:/|$)'}
USER_AGENT = 'Mozilla/5.0 (compatible; PropertyGalleryCollector/1.0)'

def now(): return datetime.now(timezone.utc).isoformat()

def atomic(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        f.write(text);f.flush();os.fsync(f.fileno())
    tmp.replace(path)

def identify(url):
    u=parse.urlsplit(url);host=(u.hostname or '').lower()
    if u.scheme not in ('http','https') or host not in HOSTS or u.username or u.password:
        raise ValueError('Unsupported listing URL')
    source=HOSTS[host];m=re.search(PATTERNS[source],u.path)
    if not m:raise ValueError('Missing listing ID')
    return source,str(int(m[1]))

def make_properties(input_path):
    data=json.loads(Path(input_path).read_text());items={}
    for url,refs in data.items():
        source,ident=identify(url);key=source+'_'+ident
        item=items.setdefault(key,{'key':key,'source':source,'listing_id':ident,'listing_url':url,'listing_urls':[], 'workbook_references':[]})
        if url not in item['listing_urls']:item['listing_urls'].append(url)
        item['workbook_references'].extend(refs)
    return list(items.values())

def image_url(value, base):
    if not isinstance(value,str) or not value.strip():return None
    value=parse.urljoin(base,value.strip());u=parse.urlsplit(value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:return None
    name=u.path.rsplit('/',1)[-1].lower()
    if name.endswith('.svg') or any(x in name for x in ('placeholder','no-photo','no_photo','noimage','no-image','logo.')):return None
    return parse.urlunsplit((u.scheme,u.netloc,u.path,u.query,''))

def extract(body, url, source, ident):
    root=html.fromstring(body)
    title=' '.join(root.xpath('//title/text()')).strip()
    canonical=root.xpath('//link[@rel="canonical"]/@href')
    if canonical:
        try:
            if identify(parse.urljoin(url,canonical[0])) != (source,ident):
                return [],title,'canonical_listing_mismatch',0
        except ValueError:
            # A listing redirecting to a category is no longer a listing page.
            return [],title,'canonical_not_a_listing',0
    values=[];method=''
    if source=='estitor':
        galleries=root.xpath('//*[@gallery-data]/@gallery-data')
        for text in galleries:
            try:
                entries=json.loads(text)
                if isinstance(entries,list):
                    for item in entries:
                        if isinstance(item,dict): values.append(item.get('srcModal') or item.get('src'))
            except (ValueError,TypeError):pass
        if values:method='estitor_gallery_data'
        else:
            values=root.xpath('//img[contains(concat(" ",normalize-space(@class)," ")," mobile-gallery-image ")]/@src')
            method='estitor_mobile_gallery'
        if not values:
            for element in root.xpath('//script[@type="application/ld+json"]'):
                try: data=json.loads(element.text or '')
                except ValueError: continue
                nodes=data if isinstance(data,list) else data.get('@graph',[data]) if isinstance(data,dict) else []
                for node in nodes:
                    if not isinstance(node,dict):continue
                    typ=node.get('@type',[]);typ=[typ] if isinstance(typ,str) else typ
                    if not set(typ)&{'RealEstateListing','Apartment','House','Residence','SingleFamilyResidence','Product'}:continue
                    images=node.get('image',[])
                    if isinstance(images,(str,dict)):images=[images]
                    for image in images:
                        values.append(image.get('url') or image.get('contentUrl') if isinstance(image,dict) else image)
            method='listing_json_ld'
    elif source=='patuljak':
        values=root.xpath('//*[contains(@itemtype,"schema.org/ImageGallery")]//a/@href')
        method='patuljak_property_image_gallery'
        if not values:
            values=root.xpath('//*[contains(@class,"product_full--galerija")]//a/@href')
    elif source=='realitica':
        values=root.xpath('//a[contains(@href,"/listing_photos/'+ident+'_")]/@href')
        method='realitica_full_size_gallery_links'
    result=[];seen=set()
    for value in values:
        image=image_url(value,url)
        if not image:continue
        if not parse.urlsplit(image).path.lower().endswith(('.jpg','.jpeg','.png','.webp','.gif','.avif','.bmp')):continue
        if source=='estitor' and parse.urlsplit(image).hostname != 'content.estitor.com':continue
        if image not in seen:result.append(image);seen.add(image)
    return result,title,method,len(values)

class Fetcher:
    def __init__(self,cache,key_file,zone):
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.key_file=key_file;self.zone=zone;self.refresh_cache=False
        self.host_locks={source:threading.Lock() for source in set(HOSTS.values())}
        self.host_next={};self.paused={};self.api_blocked=None;self.state_lock=threading.Lock()
    def gate(self,source):
        with self.host_locks[source]:
            if source in self.paused:raise RuntimeError('source_rate_limited')
            time.sleep(max(0,self.host_next.get(source,0)-time.monotonic()))
            self.host_next[source]=time.monotonic()+0.5
    def get(self,item,via_api=False):
        self.gate(item['source'])
        url=item['listing_url']
        if via_api:
            if not self.key_file or self.api_blocked:raise RuntimeError('bright_data_unavailable')
            # Credentials are read only from the existing private file, never logged.
            key=Path(self.key_file).read_text().strip()
            payload={'zone':self.zone,'url':url,'format':'json','method':'GET','headers':{'User-Agent':USER_AGENT}}
            req=request.Request('https://api.brightdata.com/request',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            try:
                with request.urlopen(req,timeout=40) as r:envelope=json.load(r)
            except error.HTTPError as exc:
                if exc.code in (401,402,403,429):self.api_blocked='provider_http_'+str(exc.code)
                raise RuntimeError('bright_data_http_'+str(exc.code)) from None
            status=int(envelope.get('status_code',0))
            if status!=200:
                if status==429:self.paused[item['source']]='target_http_429'
                raise RuntimeError('target_http_'+str(status))
            data=envelope.get('body','')
            if not isinstance(data,str):raise RuntimeError('unexpected_bright_data_body')
            return data.encode(),url,'bright_data_https_api'
        req=request.Request(url,headers={'User-Agent':USER_AGENT})
        try:
            with request.urlopen(req,timeout=20) as r:
                body=r.read(8_000_001)
                if len(body)>8_000_000:raise RuntimeError('html_response_too_large')
                final=r.geturl()
                try:
                    if identify(final)!=(item['source'],item['listing_id']):raise RuntimeError('redirect_not_same_listing')
                except ValueError:raise RuntimeError('redirect_not_a_listing') from None
                return body,final,'direct_https'
        except error.HTTPError as exc:
            if exc.code==429:self.paused[item['source']]='http_429_retry_after_'+str(exc.headers.get('Retry-After','unknown'))
            raise
    def run(self,item):
        result={**item,'checked_at':now(),'images':[],'status':'failed'}
        cache=self.cache/(item['key']+'.html')
        attempts=[]
        body=None
        if cache.exists() and not self.refresh_cache:body=cache.read_bytes();transport='saved_html_cache'
        else:
            for attempt in range(2):
                try:body,final,transport=self.get(item);break
                except error.HTTPError as exc:
                    attempts.append('direct_http_'+str(exc.code))
                    if exc.code in (404,410):
                        result.update(status='unavailable',http_status=exc.code,errors=attempts);return result
                    if exc.code in (403,429):break
                except Exception as exc:
                    attempts.append(str(exc) if isinstance(exc,RuntimeError) else 'direct_'+type(exc).__name__)
                    if isinstance(exc,RuntimeError) and 'redirect' in str(exc):
                        result.update(status='unavailable_redirect',errors=attempts);return result
                    if isinstance(exc,RuntimeError) and 'rate_limited' in str(exc):break
                if attempt==0:time.sleep(1)
            if body is None and item['source'] not in self.paused:
                for attempt in range(2):
                    try:body,final,transport=self.get(item,via_api=True);break
                    except Exception as exc:
                        message=str(exc) if isinstance(exc,RuntimeError) else 'api_'+type(exc).__name__
                        attempts.append(message)
                        if any(x in message for x in ('401','402','403','404','410','429','unavailable')):break
                        if attempt==0:time.sleep(2)
        if body is None:
            result.update(errors=attempts,status='rate_limited' if item['source'] in self.paused else 'failed');return result
        images,title,method,raw_count=extract(body,item['listing_url'],item['source'],item['listing_id'])
        # A 200 challenge page or incomplete direct response can need the API route.
        if not images and transport=='direct_https' and method not in ('canonical_listing_mismatch','canonical_not_a_listing'):
            try:
                alternative,_,api_transport=self.get(item,via_api=True)
                extracted=extract(alternative,item['listing_url'],item['source'],item['listing_id'])
                if extracted[0]:body=alternative;images,title,method,raw_count=extracted;transport=api_transport
            except Exception as exc:attempts.append(str(exc) if isinstance(exc,RuntimeError) else 'api_'+type(exc).__name__)
        if not cache.exists() or self.refresh_cache:cache.write_bytes(body)
        result.update(images=images,title=title,extraction_method=method,gallery_entries_observed=raw_count,transport=transport,errors=attempts,
                      status='ok' if images else 'no_gallery_images')
        return result

def save_property(root,result):
    folder=root/result['source']/result['listing_id'];folder.mkdir(parents=True,exist_ok=True)
    result['folder']=str(folder.relative_to(root))
    atomic(folder/'images.txt',''.join(u+'\n' for u in result['images']))
    atomic(folder/'listing.json',json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    buf=io.StringIO(newline='');writer=csv.writer(buf);writer.writerow(['image_number','image_url'])
    writer.writerows(enumerate(result['images'],1));atomic(folder/'images.csv',buf.getvalue())

def manifests(root,properties,results):
    buf=io.StringIO(newline='');writer=csv.writer(buf)
    writer.writerow(['property_key','source','listing_id','title','listing_url','image_count','status','folder','workbook_sheets'])
    imgs=io.StringIO(newline='');iw=csv.writer(imgs);iw.writerow(['property_key','listing_url','image_number','image_url'])
    refs=io.StringIO(newline='');rw=csv.writer(refs);rw.writerow(['property_key','workbook_sheet','workbook_row','url_column'])
    for prop in properties:
        r=results.get(prop['key'],{**prop,'images':[],'status':'pending'})
        writer.writerow([r['key'],r['source'],r['listing_id'],r.get('title',''),r['listing_url'],len(r['images']),r['status'],r.get('folder',''),'; '.join(dict.fromkeys(x['sheet'] for x in r['workbook_references']))])
        for n,u in enumerate(r['images'],1):iw.writerow([r['key'],r['listing_url'],n,u])
        for x in r['workbook_references']:rw.writerow([r['key'],x['sheet'],x['row'],x['column']])
    atomic(root/'properties_index.csv',buf.getvalue());atomic(root/'all_image_urls.csv',imgs.getvalue());atomic(root/'workbook_references.csv',refs.getvalue())
    summary={'updated_at':now(),'total_properties':len(properties),'completed':len(results),'statuses':dict(Counter(r['status'] for r in results.values())),
             'total_image_urls':sum(len(r['images']) for r in results.values()),'by_source':{source:{'total':sum(p['source']==source for p in properties),
             'completed':sum(r['source']==source for r in results.values()),'with_images':sum(r['source']==source and bool(r['images']) for r in results.values()),
             'image_urls':sum(len(r['images']) for r in results.values() if r['source']==source)} for source in ('estitor','patuljak','realitica')}}
    atomic(root/'run_summary.json',json.dumps(summary,indent=2)+'\n');return summary

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',default='image_collection/input_urls.json');p.add_argument('--output',default='property_images')
    p.add_argument('--cache',default='image_collection/cache/pages');p.add_argument('--api-key-file',default='private/brightdata_api_key')
    p.add_argument('--zone',default='esstitor_site_phone_scraper');p.add_argument('--workers',type=int,default=6)
    p.add_argument('--retry-failed',action='store_true');p.add_argument('--limit',type=int,default=0)
    args=p.parse_args();properties=make_properties(args.input);root=Path(args.output);root.mkdir(parents=True,exist_ok=True)
    results={}
    for prop in properties:
        path=root/prop['source']/prop['listing_id']/'listing.json'
        if path.exists():
            r=json.loads(path.read_text())
            if r['listing_urls']!=prop['listing_urls']:raise SystemExit('Input URL mapping changed; refusing ambiguous resume')
            if r['status']=='ok' or not args.retry_failed:results[prop['key']]=r
    pending=[p for p in properties if p['key'] not in results]
    if args.limit:pending=pending[:args.limit]
    fetcher=Fetcher(args.cache,args.api_key_file,args.zone);fetcher.refresh_cache=args.retry_failed
    print('Properties:',len(properties),'already checkpointed:',len(results),'this run:',len(pending),flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(fetcher.run,p):p for p in pending}
        for future in as_completed(futures):
            prop=futures[future]
            try:r=future.result()
            except Exception as exc:r={**prop,'images':[],'status':'parser_error','error_type':type(exc).__name__,'checked_at':now()}
            save_property(root,r);results[r['key']]=r
            with (root/'collection_checkpoint.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps({'property_key':r['key'],'status':r['status'],'images':len(r['images']),'checked_at':r['checked_at']})+'\n');f.flush()
            if len(results)%25==0 or len(results)==len(properties):
                summary=manifests(root,properties,results)
                print(json.dumps(summary),flush=True)
    summary=manifests(root,properties,results);print(json.dumps(summary),flush=True)
    return 0 if len(results)==len(properties) else 2

if __name__=='__main__':raise SystemExit(main())
