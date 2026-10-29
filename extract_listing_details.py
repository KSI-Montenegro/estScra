#!/usr/bin/env python3
"""Export source listing text/details from the captured property pages, without inventing facts."""
import argparse
import csv
import hashlib
import html as html_std
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from lxml import html
from openpyxl import load_workbook,Workbook
from openpyxl.styles import Font,PatternFill,Alignment
import collect_images as galleries

CORE=['title','description_original','deal_type','property_type','city','price','area','contact_phone','images']
HOME_TYPES={'apartment','studio','house','villa','duplex','penthouse','room'}

def now():return datetime.now(timezone.utc).isoformat()
def norm(s):
    s=str(s or '').casefold().replace('đ','dj')
    return ''.join(c for c in unicodedata.normalize('NFKD',s) if not unicodedata.combining(c)).strip()
def line(s):return re.sub(r'\s+',' ',str(s or '')).strip()
def text(node):
    if node is None:return ''
    def walk(e):
        if not isinstance(e.tag,str) or e.tag.lower() in ('script','style','noscript','svg','form','button','input'):return ''
        if e.tag.lower()=='br':return '\n'
        value=(e.text or '')+''.join(walk(c)+(c.tail or '') for c in e)
        return value+('\n' if e.tag.lower() in ('p','div','li','ul','ol','h2','h3','blockquote') else '')
    raw=walk(node).replace('\xa0',' ')
    raw='\n'.join(re.sub(r'[\t\r ]+',' ',v).strip() for v in raw.split('\n'))
    return re.sub(r'\n{3,}','\n\n',raw).strip()
def numeric(value):
    if isinstance(value,bool) or value is None:return None
    if isinstance(value,(int,float)):return value
    s=str(value).replace('\xa0',' ')
    m=re.search(r'-?\d[\d .,]*',s)
    if not m:return None
    n=m[0].strip().replace(' ','')
    if '.' in n and ',' in n:
        decimal=',' if n.rfind(',')>n.rfind('.') else '.'
        n=n.replace('.' if decimal==',' else ',','').replace(decimal,'.')
    elif n.count('.')>1 or re.fullmatch(r'\d{1,3}(?:\.\d{3})+',n):n=n.replace('.','')
    elif n.count(',')>1 or re.fullmatch(r'\d{1,3}(?:,\d{3})+',n):n=n.replace(',','')
    else:n=n.replace(',','.')
    try:
        f=float(n);return int(f) if f.is_integer() else f
    except ValueError:return None

def property_type(value):
    s=norm(value)
    rules=[('commercial',r'poslov|lokal|commercial|office|kancelar|ured|magacin|skladis'),('land',r'\bplac\b|zemlj|land|parcela'),
           ('villa',r'\bvila\b|\bvilla\b'),('studio',r'garson|\bstudio\b'),('duplex',r'dupleks|duplex'),('penthouse',r'penthaus|penthouse'),
           ('house',r'\bkuc[aeu]?\b|house|vikendic|cottage'),('apartment',r'\bstan(?:ovi|ova|u|a)?\b|apartment|apartman'),('room',r'\bsoba\b|\broom\b'),('garage',r'garaz|garage')]
    for key,pat in rules:
        if re.search(pat,s):return key
    return None

def deal(value):
    s=norm(value)
    if re.search(r'\btrazim\b|\bpotraznja\b|\bwanted\b',s):return None
    if re.search(r'prodaj|prodajem|prodaje|na prodaju|\bsale\b|\bsell',s):return 'sale'
    if re.search(r'izdav|izdaj|iznajm|najam|\brent\b|\bzakup\b',s):return 'rent'
    return None

def bool_value(value):
    s=norm(value)
    if s in ('true','da','ima','yes'):return True
    if s in ('false','ne','nema','no'):return False
    return None

def phone(value):
    if not isinstance(value,str):return None
    value=re.sub(r'[\s().-]','',value)
    return value if re.fullmatch(r'\+[1-9][0-9]{6,14}',value) and value!='+38269257004' else None

def put(r,key,value,origin):
    if value is not None and value!='':r[key]=value;r['field_sources'][key]=origin

def first(root,path):
    a=root.xpath(path);return a[0] if a else None

def description_section(root,label):
    for h in root.xpath('//h2'):
        if norm(text(h)).rstrip(':')==label:
            parent=deepcopy(h.getparent())
            for heading in parent.xpath('./h2'):heading.drop_tree()
            return text(parent)
    return ''

def listing_ld(root,ident):
    for s in root.xpath('//script[@type="application/ld+json"]'):
        try:data=json.loads(s.text or '')
        except ValueError:continue
        nodes=data if isinstance(data,list) else data.get('@graph',[data]) if isinstance(data,dict) else []
        for node in nodes:
            if not isinstance(node,dict):continue
            typ=node.get('@type',[]);typ=[typ] if isinstance(typ,str) else typ
            if 'RealEstateListing' in typ:
                if node.get('identifier') is not None and str(node['identifier'])!=ident:continue
                if node.get('url'):
                    try:
                        if galleries.identify(node['url'])[1]!=ident:continue
                    except ValueError:continue
                return node
    return {}

def estitor(root,r):
    ld=listing_ld(root,r['listing_id']);offer=ld.get('offers') or {};offer=offer[0] if isinstance(offer,list) and offer else offer
    entity=offer.get('itemOffered',{}) if isinstance(offer,dict) else {}
    address=entity.get('address') or {};seller=offer.get('seller') or {}
    put(r,'title',text(first(root,'//h1')) or ld.get('name'),'page:h1')
    desc=description_section(root,'opis nekretnine')
    put(r,'description_original',desc or ld.get('description'),'page:description_section' if desc else 'page:RealEstateListing.description')
    put(r,'description_language',ld.get('inLanguage') or root.get('lang'),'page:language')
    for key,k in [('published_date','datePosted'),('updated_date','dateModified')]:put(r,key,ld.get(k),'page:structured_data')
    put(r,'price_amount',numeric(offer.get('price')),'page:Offer.price');put(r,'price_currency',offer.get('priceCurrency'),'page:Offer.priceCurrency')
    if offer.get('price') is not None:put(r,'price_raw',str(offer['price'])+' '+str(offer.get('priceCurrency') or ''),'page:Offer')
    size=entity.get('floorSize') or {};put(r,'area_m2',numeric(size.get('value')) if isinstance(size,dict) else numeric(size),'page:floorSize')
    put(r,'rooms',numeric(entity.get('numberOfRooms')),'page:numberOfRooms')
    put(r,'bedrooms',numeric(entity.get('numberOfBedrooms')),'page:numberOfBedrooms')
    put(r,'bathrooms',numeric(entity.get('numberOfBathroomsTotal')),'page:numberOfBathroomsTotal')
    if isinstance(address,dict):
        for key,k in [('city','addressLocality'),('region','addressRegion'),('street_address','streetAddress'),('country','addressCountry'),('postal_code','postalCode')]:put(r,key,address.get(k),'page:PostalAddress')
    put(r,'advertiser_name',seller.get('name') if isinstance(seller,dict) else None,'page:Offer.seller')
    geo=entity.get('geo') or ld.get('geo') or {}
    if isinstance(geo,dict):
        put(r,'latitude',numeric(geo.get('latitude')),'page:GeoCoordinates');put(r,'longitude',numeric(geo.get('longitude')),'page:GeoCoordinates')
    attrs=r['source_attributes']
    for h in root.xpath('//h2'):
        if norm(text(h))=='detalji o nekretnini':
            for p in h.getparent().xpath('.//p[@title]'):
                label=p.getparent().xpath('./div/span/text()')
                if label:attrs[line(label[0])]=text(p)
    for amenity in entity.get('amenityFeature',[]) or []:
        if isinstance(amenity,dict) and amenity.get('name'):attrs.setdefault(amenity['name'],amenity.get('value'))
    # Breadcrumbs identify deal/type/city and the location path identifies the district only if published elsewhere.
    for s in root.xpath('//script[@type="application/ld+json"]'):
        try:data=json.loads(s.text or '')
        except ValueError:continue
        for node in data.get('@graph',[]) if isinstance(data,dict) else []:
            if isinstance(node,dict) and node.get('@type')=='BreadcrumbList':
                names=[x.get('name','') for x in node.get('itemListElement',[]) if isinstance(x,dict)]
                if names:
                    put(r,'deal_type',deal(names[0]),'page:breadcrumbs')
                    if len(names)>1:put(r,'property_type',property_type(names[1]),'page:breadcrumbs')
    apply_attributes(r)

def patuljak(root,r):
    put(r,'title',text(first(root,'//h1')),'page:h1')
    put(r,'description_original',description_section(root,'detaljan opis'),'page:Detaljan opis')
    put(r,'description_language',root.get('lang'),'page:html.lang')
    for li in root.xpath('//*[contains(@class,"product_full__ispod_slike--details")]//li'):
        spans=li.xpath('./span')
        if len(spans)>=2:r['source_attributes'][text(spans[0])]=text(spans[1])
    price=text(first(root,'//*[@itemprop="price"]'))
    put(r,'price_raw',price,'page:itemprop.price')
    if price:
        put(r,'price_amount',numeric(price),'page:itemprop.price')
        if '€' in price or 'eur' in norm(price):put(r,'price_currency','EUR','page:itemprop.price')
        r['price_on_request']=any(x in norm(price) for x in ('dogovor','upit','na poziv'))
    seller=root.xpath('//*[@itemprop="seller"]')
    if seller:
        name=first(seller[0],'.//*[@itemprop="name"]')
        put(r,'advertiser_name',text(name),'page:seller.name')
    for crumb in root.xpath('//*[contains(@itemtype,"schema.org/BreadcrumbList")]//*[@itemprop="name"]'):
        typ=property_type(text(crumb))
        if typ:put(r,'property_type',typ,'page:property_category_breadcrumb')
    for h in root.xpath('//h2'):
        if norm(text(h)).rstrip(':')=='dodatno':
            for el in h.getparent().xpath('./li'):
                label=text(el)
                if label and norm(label) not in ('undefined','null','http://','-'):
                    r['source_attributes'][label]=True
    apply_attributes(r)

def following_value(marker):
    value=marker.tail or '';n=marker.getnext()
    while n is not None and isinstance(n.tag,str) and n.tag not in ('strong','br','div','script'):
        value+=text(n)+(n.tail or '');n=n.getnext()
    return value.lstrip(': ').strip()

def realitica(root,r):
    body=first(root,'//*[@id="listing_body"]')
    if body is None:return
    put(r,'title',text(first(body,'./h2')),'page:listing_body.h2')
    marker=None
    for el in body:
        if isinstance(el.tag,str) and el.tag in ('strong','b'):
            label=text(el).rstrip(':')
            if norm(label)=='opis':marker=el;break
            r['source_attributes'][label]=following_value(el) or True
    if marker is not None:
        fragments=[html_std.escape((marker.tail or '').lstrip(': '))]
        n=marker.getnext()
        while n is not None:
            if isinstance(n.tag,str) and (n.get('id') in ('aboutAuthor','shareThisDiv','listMap') or n.tag=='script'):break
            if isinstance(n.tag,str):fragments.append(html.tostring(n,encoding='unicode',with_tail=True))
            n=n.getnext()
        container=html.fromstring('<div>'+''.join(fragments)+'</div>')
        put(r,'description_original',text(container),'page:listing_body.Opis')
    for el in body.xpath('.//strong'):
        if norm(text(el)) in ('postavljeno','zadnja promjena','oglas broj'):
            r['source_attributes'][text(el)]=following_value(el)
    put(r,'description_language',root.get('lang') or 'hr','page:html.lang_or_url')
    apply_attributes(r)

def apply_attributes(r):
    amap={norm(k):v for k,v in r['source_attributes'].items()}
    def get(*names):
        for name in names:
            if norm(name) in amap:return amap[norm(name)]
        return None
    fields={
        'city':get('Grad','Grad / Opština','Grad/Opština','Opština','Područje'),
        'district':get('Naselje','Lokacija','Mikrolokacija','Dio grada'),
        'street_address':get('Adresa'), 'floor_raw':get('Sprat','Kat'), 'building_floors_raw':get('Spratnost','Broj spratova'),
        'room_layout_raw':get('Broj soba','Sobnost'), 'furnishing_raw':get('Namješten','Opremljen','Namještenost','Namještaj'),
        'heating_raw':get('Vrsta grijanja','Grijanje'), 'condition_raw':get('Stanje','Stanje nekretnine'),
        'parking_raw':get('Parking','Parking mjesto','Parking Mjesta'), 'orientation_raw':get('Orijentacija'),
        'construction_type_raw':get('Tip gradnje'), 'published_date_raw':get('Postavljeno','Objavljeno'), 'updated_date_raw':get('Zadnja Promjena','Ažuriran')}
    for k,v in fields.items():put(r,k,v,'page:source_attributes')
    numeric_fields={
        'area_m2':get('Stambena kvadratura','Stambena Površina','Površina','Kvadratura','Površina objekta'),
        'lot_area_m2':get('Kvadratura zemljišta','Površina zemljišta','Površina placa','Površina Zemljišta','Plac'),
        'bathrooms':get('Broj kupatila','Kupatila'), 'bedrooms':get('Spavaćih Soba','Broj spavaćih soba'),
        'balconies':get('Broj terasa','Terase'), 'year_built':get('Godina izgradnje'), 'parking_spaces':get('Parking Mjesta','Parking mjesto'),
        'distance_to_sea_m':get('Od Mora (m)')}
    for k,v in numeric_fields.items():put(r,k,numeric(v),'page:source_attributes')
    if get('Vrsta','Tip nekretnine','Tip','Kategorija'):
        raw=get('Tip nekretnine','Vrsta','Tip','Kategorija');put(r,'property_type_raw',raw,'page:source_attributes')
        put(r,'property_type',property_type(raw),'page:source_attributes');put(r,'deal_type',deal(raw),'page:source_attributes')
    put(r,'deal_type',deal(get('Namjena','Tip oglasa')),'page:source_attributes')
    price=get('Cijena','Cena')
    if price is not None:
        put(r,'price_raw',price,'page:source_attributes');put(r,'price_amount',numeric(price),'page:source_attributes')
        if '€' in str(price) or 'eur' in norm(price):put(r,'price_currency','EUR','page:source_attributes')
        r['price_on_request']=any(x in norm(price) for x in ('dogovor','upit','na poziv'))
    r['amenities']=[k for k,v in r['source_attributes'].items() if bool_value(v) is True or v is True]


def workbook_data(path):
    wb=load_workbook(path,read_only=True,data_only=True);data={}
    for sh in wb:
        rows=sh.iter_rows(values_only=True);header=next(rows)
        for n,values in enumerate(rows,2):data[(sh.title,n)]=dict(zip(header,values))
    return data

def enrich(r,meta,workbook,old_phones):
    primary=[];contacts=[];leadgroups=[]
    for ref in meta['workbook_references']:
        row=workbook.get((ref['sheet'],ref['row']),{});origin='workbook:'+ref['sheet']+'!'+str(ref['row'])
        if ref['sheet'] not in leadgroups:leadgroups.append(ref['sheet'])
        annotation={k:row.get(k) for k in ('notes','agent_reasons','priority','foreign_number','already_in_graden') if row.get(k) not in (None,'')}
        if annotation:r.setdefault('workbook_annotations',[]).append({'source':origin,**annotation})
        if ref['column'] in ('ad_url','url'):primary.append((row,origin))
        number=phone(row.get('phone_e164')) or phone(row.get('phone'))
        if number:contacts.append({'phone_e164':number,'name':row.get('name'),'email':row.get('email'),'source':origin})
    if r['source']=='estitor' and r['listing_id'] in old_phones:
        row=old_phones[r['listing_id']]
        number=phone(row.get('phone_numbers'))
        if number:contacts.append({'phone_e164':number,'name':row.get('private_advertiser_name'),'source':'previous_verified_phone_csv_2026-10-25'})
    r['contact_candidates']=list({json.dumps(c,sort_keys=True,ensure_ascii=False):c for c in contacts}.values())
    numbers=list(dict.fromkeys(c['phone_e164'] for c in contacts));r['contact_phones']=numbers
    if len(numbers)==1:put(r,'phone_e164',numbers[0],'provided_contacts:see_contact_candidates')
    if len(numbers)>1:r['quality_flags'].append('multiple_contact_phones_review')
    r['lead_categories']=leadgroups
    for row,origin in primary:
        mapping={'title':row.get('title'),'deal_type':row.get('deal') if row.get('deal') in ('sale','rent') else None,
                 'property_type':row.get('type') if row.get('type') in HOME_TYPES|{'land','commercial','garage'} else None,
                 'city':row.get('city'),'district':row.get('area'),'area_m2':numeric(row.get('m2')),
                 'price_amount':numeric(row.get('price_eur')),'price_currency':'EUR' if row.get('price_eur') is not None else None}
        if r.get('price_on_request') and r['source_page_available']:
            mapping.pop('price_amount',None);mapping.pop('price_currency',None)
        for k,v in mapping.items():
            if v is not None and v!='':
                if r.get(k) is None or r.get(k)=='':put(r,k,v,origin+':primary_url_fields')
                elif k in ('price_amount','area_m2') and numeric(r[k])!=numeric(v):
                    r['workbook_conflicts'].append({'field':k,'page_value':r[k],'workbook_value':v,'source':origin})
    # Explicit title words are a fallback, with provenance, never guesses based on the price.
    if not r.get('deal_type'):put(r,'deal_type',deal(r.get('title')),'normalization:explicit_title_deal_words')
    if not r.get('property_type'):put(r,'property_type',property_type(r.get('title')),'normalization:explicit_title_type_words')
    if not r.get('deal_type') and r.get('description_original'):
        d=norm(r['description_original'])
        sale=bool(re.search(r'prodaj|prodaje|prodajem|na prodaju|for sale',d))
        rent=bool(re.search(r'izdaj|izdav|iznajm|for rent|najam',d))
        if sale!=rent:put(r,'deal_type','sale' if sale else 'rent','normalization:explicit_original_description_deal_words')
        elif sale and rent:r['quality_flags'].append('description_mentions_both_sale_and_rent')
    if r['source']=='estitor':
        path=urlsplit(r['listing_url']).path
        if not r.get('deal_type'):
            if '/namjena-izdavanje/' in path:put(r,'deal_type','rent','listing_url:namjena-izdavanje')
            elif '/namjena-prodaja/' in path:put(r,'deal_type','sale','listing_url:namjena-prodaja')
        if not r.get('property_type'):
            m=re.search(r'/tip-([^/]+)/',path)
            if m:put(r,'property_type',property_type(m[1].replace('-',' ')),'listing_url:tip')
    # Preserve numeric room counts separately from bedroom counts.
    if r.get('bedrooms') is None:
        layouts={r'\bjednosoban\b':1,r'\bdvosoban\b':2,r'\btrosoban\b':3,r'\bcetvorosoban\b':4,r'\bpetosoban\b':5}
        for pat,count in layouts.items():
            if re.search(pat,norm(r.get('room_layout_raw') or r.get('title'))):
                put(r,'bedrooms',count,'normalization:explicit_local_room_layout');break
    if r.get('furnishing_raw') is not None:put(r,'furnished',bool_value(r['furnishing_raw']),'normalization:published_furnishing_value')
    fullprice=norm(r.get('price_raw'))
    for period,pattern in [('month',r'mjesec|mesec|mjesecno|mesecno|monthly|/mj'),('day',r'dnevno|po danu|/dan|daily'),('year',r'godisnje|yearly')]:
        if re.search(pattern,fullprice):put(r,'rental_period',period,'normalization:explicit_price_period');break
    if r.get('property_type')=='land' and r.get('lot_area_m2') is None and r.get('area_m2') is not None:
        original_source=r['field_sources'].get('area_m2')
        put(r,'lot_area_m2',r['area_m2'],'normalization:published_land_area; '+str(original_source))
        r['area_m2']=None;r['field_sources'].pop('area_m2',None)
    if r.get('area_m2') is not None and r['area_m2']<=0:r['quality_flags'].append('nonpositive_area_requires_confirmation')
    if r.get('price_amount')==0:r['quality_flags'].append('zero_price_requires_confirmation')
    if r.get('property_type') not in HOME_TYPES and r.get('property_type') is not None:r['quality_flags'].append('not_a_home')
    if r.get('workbook_conflicts'):r['quality_flags'].append('page_and_workbook_values_differ')
    r['missing_core_fields']=[]
    for key in CORE:
        value={'price':r.get('price_amount') is not None or r.get('price_on_request'),
               'area':r.get('area_m2') is not None or r.get('lot_area_m2') is not None,
               'contact_phone':bool(r.get('phone_e164')),'images':bool(r['image_urls'])}.get(key,r.get(key))
        if not value:r['missing_core_fields'].append(key)
    r['optional_fields_not_published']=[k for k in ('street_address','latitude','longitude','bedrooms','bathrooms','year_built','floor_raw','furnishing_raw','heating_raw','rental_period') if r.get(k) is None]
    r['core_fields_complete']=not r['missing_core_fields'] and r['source_page_available']
    r['graden_outreach_candidate']='Outreach list' in leadgroups


def parse_property(meta,workbook,old_phones):
    r={k:meta[k] for k in ('key','source','listing_id','listing_url','listing_urls','workbook_references')}
    r['property_key']=r.pop('key');r.update(field_sources={},source_attributes={},quality_flags=[],workbook_conflicts=[],image_urls=meta['images'],
        image_count=len(meta['images']),image_status=meta['status'],description_original=None,price_on_request=False,
        source_page_available=False,source_page_captured_at=meta.get('checked_at'),details_extracted_at=now())
    cache=Path('image_collection/cache/pages')/(r['property_key']+'.html')
    if cache.exists() and meta['status'] not in ('unavailable','unavailable_redirect'):
        body=cache.read_bytes();root=html.fromstring(body)
        r['source_page_available']=True;r['source_html_sha256']=hashlib.sha256(body).hexdigest()
        # Validate canonical listing identity before accepting source content.
        canonical=root.xpath('//link[@rel="canonical"]/@href')
        if canonical and galleries.identify(galleries.parse.urljoin(r['listing_url'],canonical[0]))!=(r['source'],r['listing_id']):
            raise ValueError('Canonical listing mismatch')
        {'estitor':estitor,'patuljak':patuljak,'realitica':realitica}[r['source']](root,r)
        put(r,'metadata_summary',first(root,'//meta[@name="description"]/@content') or first(root,'//meta[@property="og:description"]/@content'),'page:meta_description_summary_only')
    enrich(r,meta,workbook,old_phones)
    r['description_status']='extracted' if r.get('description_original') else 'no_published_description' if r['source_page_available'] else 'source_unavailable'
    for column in COLUMNS:r.setdefault(column,None)
    return r

def listing_markdown(r):
    title=r.get('title') or r['property_key']
    out=['# '+title,'','Source: '+r['listing_url'],'']
    labels={'deal_type':'Deal','property_type':'Property type','price_raw':'Published price','price_amount':'Price amount','price_currency':'Currency',
            'city':'City','district':'District','street_address':'Address','area_m2':'Area (m²)','lot_area_m2':'Land area (m²)',
            'bedrooms':'Bedrooms','bathrooms':'Bathrooms','floor_raw':'Floor','furnishing_raw':'Furnishing','parking_raw':'Parking'}
    for key,label in labels.items():
        if r.get(key) is not None:out.append('- '+label+': '+str(r[key]))
    out+=['','## Original listing description','',r.get('description_original') or '[No source description available.]','']
    if r.get('amenities'):out+=['## Published amenities','']+['- '+str(a) for a in r['amenities']]+['']
    if r['missing_core_fields']:out+=['## Missing core data','',', '.join(r['missing_core_fields']),'']
    out+=['This is a source-based draft. Missing facts have not been invented. Field provenance and conflicts are recorded in details.json.','']
    return '\n'.join(out)

COLUMNS=['property_key','source','listing_id','listing_url','title','description_original','deal_type','property_type','price_amount','price_currency',
         'price_raw','price_on_request','rental_period','city','district','street_address','country','area_m2','lot_area_m2','rooms','bedrooms','bathrooms',
         'floor_raw','building_floors_raw','year_built','furnishing_raw','heating_raw','condition_raw','parking_raw','parking_spaces','balconies','orientation_raw',
         'latitude','longitude','published_date','updated_date','published_date_raw','updated_date_raw','advertiser_name','phone_e164','image_count',
         'description_status','source_page_available','core_fields_complete','graden_outreach_candidate','missing_core_fields','optional_fields_not_published',
         'lead_categories','quality_flags','amenities','image_urls']

def flat(r):return {k:'; '.join(map(str,r[k])) if isinstance(r.get(k),list) else json.dumps(r[k],ensure_ascii=False) if isinstance(r.get(k),dict) else r.get(k) for k in COLUMNS}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workbook',required=True)
    args=p.parse_args();workbook=workbook_data(args.workbook)
    old={}
    with open('qualified_listings_with_phones.csv',newline='') as f:
        for row in csv.DictReader(f):old[row['listing_id']]=row
    props=galleries.make_properties('image_collection/input_urls.json');records=[];errors=[]
    for prop in props:
        folder=Path('property_images')/prop['source']/prop['listing_id'];meta=json.loads((folder/'listing.json').read_text())
        try:r=parse_property(meta,workbook,old)
        except Exception as exc:
            errors.append({'property_key':prop['key'],'error_type':type(exc).__name__});continue
        galleries.atomic(folder/'description.txt',(r.get('description_original') or '')+'\n' if r.get('description_original') else '')
        galleries.atomic(folder/'details.json',json.dumps(r,indent=2,ensure_ascii=False)+'\n')
        galleries.atomic(folder/'listing.md',listing_markdown(r));records.append(r)
        if len(records)%250==0:print('Extracted',len(records),'properties',flush=True)
    out=Path('listing_details');out.mkdir(exist_ok=True)
    galleries.atomic(out/'extraction_errors.json',json.dumps(errors,indent=2))
    if errors:raise SystemExit('Details parser errors: '+str(len(errors))+'; investigate listing_details/extraction_errors.json')
    galleries.atomic(out/'listings.json',json.dumps(records,indent=2,ensure_ascii=False)+'\n')
    for name,selection in [('listings.csv',records),('needs_review.csv',[r for r in records if not r['core_fields_complete'] or r['quality_flags']])]:
        buf=io.StringIO(newline='');w=csv.DictWriter(buf,COLUMNS);w.writeheader();w.writerows(flat(r) for r in selection)
        galleries.atomic(out/name,'\ufeff'+buf.getvalue())
    wb=Workbook();ws=wb.active;ws.title='Listings';ws.append(COLUMNS)
    for r in records:
        ws.append([flat(r)[k] for k in COLUMNS])
        for cell in ws[ws.max_row]:
            if isinstance(cell.value,str):
                cell.value=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',cell.value)
                cell.data_type='s'
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
    for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='254B42')
    for key,width in [('A',25),('D',65),('E',65),('F',100)]:ws.column_dimensions[key].width=width
    review=wb.create_sheet('Missing field counts');review.append(['field','listings_missing'])
    for field,count in Counter(f for r in records for f in r['missing_core_fields']).most_common():review.append([field,count])
    wb.save(out/'GRADEN_listings_complete.xlsx')
    summary={'extracted_at':now(),'input_workbook_sha256':hashlib.sha256(Path(args.workbook).read_bytes()).hexdigest(),
        'total_properties':len(records),'source_pages_available':sum(r['source_page_available'] for r in records),
        'with_original_description':sum(bool(r.get('description_original')) for r in records),
        'with_title':sum(bool(r.get('title')) for r in records),'with_price_amount':sum(r.get('price_amount') is not None for r in records),
        'with_city':sum(bool(r.get('city')) for r in records),'with_area':sum(r.get('area_m2') is not None or r.get('lot_area_m2') is not None for r in records),
        'with_unique_contact_phone':sum(bool(r.get('phone_e164')) for r in records),
        'with_images':sum(bool(r['image_urls']) for r in records),
        'with_coordinates':sum(r.get('latitude') is not None and r.get('longitude') is not None for r in records),
        'with_bedrooms':sum(r.get('bedrooms') is not None for r in records),
        'with_bathrooms':sum(r.get('bathrooms') is not None for r in records),
        'maximum_original_description_length':max(len(r.get('description_original') or '') for r in records),
        'core_fields_complete':sum(r['core_fields_complete'] for r in records),
        'missing_core_field_counts':dict(Counter(f for r in records for f in r['missing_core_fields'])),
        'description_statuses':dict(Counter(r['description_status'] for r in records)),
        'by_source':{s:{'total':sum(r['source']==s for r in records),'with_description':sum(r['source']==s and bool(r.get('description_original')) for r in records)} for s in ('estitor','patuljak','realitica')},
        'extraction_errors':errors}
    galleries.atomic(out/'run_summary.json',json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
