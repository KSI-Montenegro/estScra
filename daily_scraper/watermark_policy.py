"""Per-photo publication checks and conservative agency provenance for consumers."""
import re
import unicodedata

POLICY = 'per-image-watermarks-v1'
PORTALS = {'patuljak','estitor','realitica','oglasi'}
AGENCIES = {'Kvadrat': ('kvadrat', 'kvadrat nekretnine'),
            'Seaside Living': ('seaside living', 'seasideliving', 'seaside-living'),
            'Montenegro Prospects': ('montenegro prospects','montenegroprospects'),
            'Domus': ('domus', 'domus nekretnine'),
            'Puerta Real Estate': ('puerta real estate','puerta')}

# Property-wide quarantine: draft numbering does not identify original images.
# Individual files require a fresh output audit before this hold can be lifted.
INCIDENT_PROPERTIES = {'patuljak_'+i for i in ('86717','79250','64027','72906','72989','82189','5176','77029','86379')}
REPORTED_AGENCIES={'patuljak_64027':['Montenegro Prospects'],'patuljak_72906':['Kvadrat'],
                  'patuljak_72989':['Domus'],'patuljak_82189':['Puerta Real Estate'],'patuljak_5176':['Kvadrat']}


def normalized(value):
    value=unicodedata.normalize('NFKD',str(value).casefold().replace('đ','dj'))
    return re.sub(r'[^a-z0-9]+',' ',''.join(c for c in value if not unicodedata.combining(c))).strip()


def agency_hits(words,width,height):
    groups={}
    for word in words:
        groups.setdefault(word.get('line',''),[]).append(word)
    candidates=[[w] for w in words]+list(groups.values());hits=[];seen=set()
    for group in candidates:
        if not group or min(w.get('confidence',0) for w in group)<65:
            continue
        raw=' '.join(w.get('text','') for w in group);value=' '+normalized(raw)+' '
        for brand,aliases in AGENCIES.items():
            if not any(' '+normalized(alias)+' ' in value for alias in aliases):
                continue
            box=[min(w['box_original_pixels'][0] for w in group),min(w['box_original_pixels'][1] for w in group),
                 max(w['box_original_pixels'][2] for w in group),max(w['box_original_pixels'][3] for w in group)]
            key=(brand,*box)
            if key in seen:continue
            seen.add(key)
            hits.append({'brand':brand,'kind':'agency','recognized_text':raw[:200],
                         'ocr_confidence':min(w['confidence'] for w in group),'box_original_pixels':box,
                         'method':'known_agency_text','meaning':'agency_brand_evidence_not_owner_identity'})
    return hits


def analysis_record(analysis,digest):
    hits=analysis.get('hits',[])
    portals={h['brand'] for h in hits if h.get('brand') in PORTALS}
    agencies={h['brand'] for h in hits if h.get('kind')=='agency' or h.get('brand') in AGENCIES}
    if analysis.get('score',0)>=.85 and analysis.get('result')=='watermark_candidate':portals.add('patuljak')
    if analysis.get('reason')=='oglasi_calibrated_center':portals.add('oglasi')
    result=analysis.get('result')
    status='watermarked' if portals or agencies or result=='watermark_candidate' else 'clean' if result=='sample_passed' else 'unknown'
    return {'policy':POLICY,'source_sha256':digest,'status':status,'portal_brands':sorted(portals),
            'agency_brands':sorted(agencies),'detected_brands':sorted(portals|agencies),
            'detections':hits,'method':'per_image_template_and_ocr','meaning':'automated_detection_not_visual_certification',
            'analysis':analysis}


def checked(row):
    a=row.get('watermark_analysis') or {}
    return a.get('policy')==POLICY and a.get('source_sha256')==row.get('source_sha256')


def cleaned(row):
    return row.get('selected_version')=='cleaned' and bool(row.get('cleaned_url'))


def ready(row):
    if row.get('publication_hold') or not row.get('ready_for_listing'):
        return False
    if cleaned(row):
        out=row.get('delivered_watermark_analysis') or {}
        return (checked(row) and out.get('policy')==POLICY and out.get('status')=='clean'
                and out.get('source_sha256')==row.get('repair_output_sha256'))
    return checked(row) and row['watermark_analysis'].get('status')=='clean'


def decorate(row):
    a=row.get('watermark_analysis') or {}
    out=row.get('delivered_watermark_analysis') or {}
    if out.get('source_sha256')!=row.get('repair_output_sha256'):out={}
    row['watermark_portals']=sorted(set(a.get('portal_brands',[]))|set(out.get('portal_brands',[])))
    row['watermark_agencies']=sorted(set(a.get('agency_brands',[]))|set(out.get('agency_brands',[])))
    row['watermark_brands']=sorted(set(row['watermark_portals'])|set(row['watermark_agencies']))
    row['watermark_check_status']='checked' if checked(row) else 'pending'
    row['watermark_status']=('watermarked' if (row.get('delivered_watermark_analysis') or {}).get('status')=='watermarked'
                             else 'unknown') if row.get('publication_hold') else 'cleaned' if cleaned(row) and ready(row) else a.get('status','unknown')
    row['ready_for_listing']=ready(row)
    return row


def listing_summary(images,expected_count,property_key=None):
    audited=[r for r in images if checked(r)]
    for row in audited:decorate(row)
    agency_rows=[r for r in audited if r.get('watermark_agencies')]
    complete=expected_count>0 and len(audited)==expected_count
    all_agency=complete and len(agency_rows)==expected_count
    names=sorted({b for r in agency_rows for b in r['watermark_agencies']})
    reported=REPORTED_AGENCIES.get(property_key,[])
    return {'photo_audit_status':'complete' if complete else 'partial' if audited else 'pending',
            'photo_audited_count':len(audited),'photo_expected_count':expected_count,
            'photo_pending_count':max(0,expected_count-len(audited)),
            'watermark_agencies':names,'agency_branded_photo_count':len(agency_rows),
            'reported_agency_brands':reported,'agency_signal_status':'operator_reported' if reported else 'detected_per_image' if names else 'unknown',
            'all_photos_agency_branded':all_agency,
            'suggested_contact_queue':'agents' if all_agency or reported else 'unclassified',
            'agency_routing_reason':'agency_brand_on_every_original_photo' if all_agency else 'operator_reported_agency_watermark' if reported else None,
            'agency_identity_verified':False,
            'ready_photo_count':sum(ready(r) for r in images),
            'photos_ready_for_publication':sum(ready(r) for r in images)>=3}


def attach_photos(record,prop):
    from copy import deepcopy
    row=deepcopy(record)
    photos=[decorate(deepcopy(r)) for r in sorted(prop.get('images',[]),key=lambda r:int(r['image_number']))]
    originals=row.get('source_image_urls',row.get('image_urls',[]))
    expected=max(len(originals),len(photos)+len(prop.get('unavailable',[])))
    state=listing_summary(photos,expected,row.get('property_key'))
    urls=[r['active_url'] for r in photos if ready(r) and r.get('active_url')]
    row.update(source_image_urls=originals,image_urls=urls,image_count=len(urls),photos=photos,
               ready_image_count=len(urls),held_image_count=max(0,expected-len(urls)),
               photo_queue_status=prop.get('photo_pipeline_status','pending'),
               photo_delivery_status='pending_per_image_audit' if state['photo_audit_status']!='complete' else
                                     'complete' if len(urls)==expected else 'needs_review',
               watermark_sample_result=prop.get('watermark_sample',{}).get('result','not_checked'),
               watermark_status='per_image_complete' if state['photo_audit_status']=='complete' else 'per_image_pending',
               **state)
    return row
