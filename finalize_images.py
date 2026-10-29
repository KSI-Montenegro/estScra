#!/usr/bin/env python3
"""Validate and package completed listing gallery URL collection (no secrets)."""
import csv
import io
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
import collect_images as c

def main():
    root=Path('property_images');props=c.make_properties('image_collection/input_urls.json');results={}
    for prop in props:
        path=root/prop['source']/prop['listing_id']/'listing.json'
        if not path.exists():raise SystemExit('Collection unfinished: '+prop['key'])
        r=json.loads(path.read_text())
        # Direct redirects to a category/homepage confirmed by API 302 are an unavailable listing.
        if r['status']=='failed' and any(x in r.get('errors',[]) for x in ('redirect_not_a_listing','redirect_not_same_listing')):
            r['status']='unavailable_redirect'
            c.save_property(root,r)
        if r['status']=='no_gallery_images':
            from lxml import html
            cache=Path('image_collection/cache/pages')/(r['key']+'.html')
            page=html.fromstring(cache.read_bytes())
            if r['source']=='realitica':
                assert not page.xpath('//a[contains(@href,"/listing_photos/")]/@href')
                assert not any(page.xpath('//meta[@property="og:image"]/@content'))
                r['no_images_reason']='Returned property page has an empty photo gallery and empty og:image; no published image URLs were observed.'
            elif r['source']=='patuljak':
                gallery=page.xpath('//*[contains(@itemtype,"schema.org/ImageGallery")]//a/@href')
                assert not c.extract(cache.read_bytes(),r['listing_url'],r['source'],r['listing_id'])[0]
                r['no_images_reason']='Property gallery supplied no valid image-file URLs; media-directory links without a filename were excluded.'
                r['rejected_gallery_links']=gallery
            else:raise AssertionError('Unexplained missing gallery: '+r['key'])
            c.save_property(root,r)
        assert r['key']==prop['key'] and r['listing_urls']==prop['listing_urls']
        assert r['workbook_references']==prop['workbook_references']
        assert len(r['images'])==len(set(r['images']))
        assert (r['status']=='ok')==bool(r['images'])
        for u in r['images']:
            assert c.image_url(u,r['listing_url'])==u
            assert c.parse.urlsplit(u).path.lower().endswith(('.jpg','.jpeg','.png','.webp','.gif','.avif','.bmp')),(prop['key'],u)
        assert (path.parent/'images.txt').read_text().splitlines()==r['images']
        with (path.parent/'images.csv').open(newline='') as f:
            assert [x['image_url'] for x in csv.DictReader(f)]==r['images']
        results[r['key']]=r
    assert len(list(root.glob('*/*/listing.json')))==len(props), 'Unexpected property folders'
    summary=c.manifests(root,props,results)
    summary['distinct_image_urls']=len({u for r in results.values() for u in r['images']})
    with (root/'all_image_urls.csv').open(newline='') as f:
        assert sum(1 for _ in csv.DictReader(f))==summary['total_image_urls']
    sheets=sorted({x['sheet'] for p in props for x in p['workbook_references']})
    sheet_counts={}
    for sheet in sheets:
        filename=re.sub(r'[^A-Za-z0-9]+','_',sheet).strip('_')+'.csv'
        buf=io.StringIO(newline='');w=csv.writer(buf)
        w.writerow(['property_key','listing_url','title','image_count','status','folder'])
        chosen=[r for r in results.values() if any(x['sheet']==sheet for x in r['workbook_references'])]
        for r in chosen:w.writerow([r['key'],r['listing_url'],r.get('title',''),len(r['images']),r['status'],r['folder']])
        c.atomic(root/'by_workbook_sheet'/filename,buf.getvalue());sheet_counts[sheet]=len(chosen)
    failed=io.StringIO(newline='');w=csv.writer(failed);w.writerow(['property_key','listing_url','status','observed_errors','extraction_method'])
    for r in results.values():
        if r['status']!='ok':w.writerow([r['key'],r['listing_url'],r['status'],'; '.join(r.get('errors',[]))+(' '+r.get('no_images_reason','')).rstrip(),r.get('extraction_method','')])
    c.atomic(root/'unresolved_properties.csv',failed.getvalue())
    checks=json.loads((root/'image_url_sample_checks.json').read_text())
    summary.update(validation={'properties_mapped_to_input':len(results),'image_url_rows_verified':summary['total_image_urls'],
                    'folder_files_and_url_order':'passed','property_scoped_gallery_extraction':'passed',
                    'sample_image_urls_checked':len(checks),'sample_image_urls_verified':sum(x['is_image'] for x in checks),
                    'all_image_urls_individually_requested':False},workbook_sheet_property_counts=sheet_counts,
                    transports=dict(Counter(r.get('transport','no_html') for r in results.values())))
    c.atomic(root/'run_summary.json',json.dumps(summary,indent=2)+'\n')
    counts='\n'.join('- '+key+': '+str(value) for key,value in summary['statuses'].items())
    text=f'''# GRADEN property image URLs

Collected {summary['total_image_urls']:,} observed gallery image URLs for {summary['statuses'].get('ok',0):,} listings, out of {len(props):,} unique listing URLs supplied in GRADEN_leads.xlsx. Collection completed on {c.now()[:10]}.

## Folder layout

Each listing has its own folder: `estitor/LISTING_ID/`, `patuljak/LISTING_ID/`, or `realitica/LISTING_ID/`.

- `images.txt`: one full gallery image URL per line, in gallery order.
- `images.csv`: the same URLs with image numbers.
- `listing.json`: source URL, title, all supplied URL aliases, workbook tab/row/column references, extraction method, status, and observed image URLs.

These folders contain URLs, not downloaded image files. Separate ads belonging to the same owner are kept separate. Listings across different sites are not assumed to represent the same physical property.

## Indexes and exceptions

- `properties_index.csv`: every requested listing, status, count and folder.
- `all_image_urls.csv`: all gallery URLs, grouped by property key and listing URL.
- `by_workbook_sheet/`: separate indexes for Outreach list, Likely agents, Not homes and Rejected.
- `workbook_references.csv`: exact links back to spreadsheet rows, including secondary ad URLs.
- `unresolved_properties.csv`: all listings with no verified gallery URLs, with observed reasons.
- `run_summary.json`: final counts and validation results.
- `collection_checkpoint.jsonl` plus each `listing.json`: resumable audit state. Per-property metadata is authoritative; journal entries record original attempts and may precede later status classification.

All supplied primary, secondary and rejected-list URLs were included. The workbook is grouped by owner and provides at most two listing URLs per row; ads_count does not provide the addresses of any additional ads, so unprovided URLs were not invented.

Final statuses:
{counts}

## Image selection and verification

Five Patuljak galleries contained a media-directory URL without an image filename. Direct and Bright Data rechecks exposed no valid photo URLs for those five listings, so those directory links were excluded. Three Realitica pages exposed empty photo galleries and empty og:image metadata. These eight cases are recorded separately from the unavailable listings.

Estitor: observed gallery-data full-size/modal URLs, with gallery/structured-data fallbacks. Patuljak: links from the ad's ImageGallery. Realitica: observed full-size gallery links matching the actual listing ID. Icons, logos, thumbnails when a full-size URL is provided, and recommendation images are excluded. No image URLs were guessed or generated by changing filenames or crop paths. Duplicate gallery URLs within a property are removed. Redirects to other pages are not treated as galleries for the requested listing.

All output folders, URL counts, input references and gallery ordering were checked. Nine sampled image URLs (three per source) returned image content; Patuljak required GET signature checks because its HEAD responses omitted Content-Type. Every URL has not been individually fetched. Galleries can change or disappear after collection.

The original workbook and summary were not edited. No phone-reveal, call, callback, feedback or outreach actions were performed.

## Reproduce or resume

From the repository root, use Python 3.10+ and lxml:

```bash
python -m pip install lxml
python collect_images.py
```

The existing private Bright Data API key is used only if direct access fails; the key is not included in the results or repository. Supply a private key file with `--api-key-file /path/to/key` in another environment. Missing credentials do not prevent direct requests. TLS verification stays enabled. Requests are bounded and paced to at most two starts per second per source; six workers maximum by default. Rate-limited sources are paused and their outcomes recorded.

Previously completed records are retained. To retry unresolved listings after a meaningful availability/network change:

```bash
python collect_images.py --retry-failed
python finalize_images.py
```

The full supplied URL-to-workbook-row mapping is in `image_collection/input_urls.json`. Raw HTML caches stay outside the deliverable. The ZIP contains only property folders, indexes, collector scripts and input URL references; it excludes credentials and raw HTML.
'''
    c.atomic(root/'README.md',text)
    paths=[p for p in root.rglob('*') if p.is_file()]
    paths += [Path('collect_images.py'),Path('finalize_images.py'),Path('image_collection/input_urls.json')]
    keyfile=Path('private/brightdata_api_key');secret=keyfile.read_bytes().strip() if keyfile.exists() else None
    for path in paths:
        if secret:assert secret not in path.read_bytes(),'Credential detected in deliverable'
    with zipfile.ZipFile('GRADEN_property_image_urls.zip','w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(paths):z.write(path,path.as_posix())
    with zipfile.ZipFile('GRADEN_property_image_urls.zip') as z:
        assert z.testzip() is None
        assert not any('private/' in name or '/cache/' in name for name in z.namelist())
    print(json.dumps(summary,indent=2));print('ZIP bytes:',Path('GRADEN_property_image_urls.zip').stat().st_size)

if __name__=='__main__':main()
