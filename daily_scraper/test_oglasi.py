"""Property identity, SSR scoping, phone matching, and gallery queue regressions."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1])]
import collect_images as galleries
import daily_scraper as scraper
import phone_matches as matching
from photo_queue import input_property

URL='https://oglasi.me/nekretnine/kuce/example-og12345me'
PHOTO='https://slike.oglasi.me/2026/11/13/OG12345ME/photo.webp'


def ad():
    return {'id':999,'code':'OG12345ME','category':{'seo':'kuce'},'country':{'abr':'MNE','name':'Crna Gora'},
            'status':'Published','listingStatus':'Active','titleCompiled':'Kuća 270 m2',
            'description':'Izdaje se kuća. 067 123 456','priceMonthly':1800,'priceOnRequest':False,
            'city':{'name':'Podgorica'},'location':{'name':'Gorica C'},
            'user':{'name':'Fixture','phones':[{'phone':'00382 67 123 456'}],'type':{'name':'Korisnik'}},
            'adsSpecifications':[{'specification':{'title':k},'value':v} for k,v in [
                ('Tip oglasa','Izdavanje'),('Površina','270'),('Površina placa','350'),('Broj kupatila','3')]],
            'media':[{'fd':PHOTO,'ads':999,'main':True,'visible':True,'priority':0}],
            'createdAt':'2026-10-01T12:00:00Z','refreshed':'2026-11-13T10:00:00Z'}


def body(item=None, extra=''):
    state={'ads':{'detail':{'currentAd':item if item is not None else ad()},
                  'list':{'elements':[{'code':'OG12345ME'}]}}}
    return ('<html><link rel="canonical" href="'+URL+'"><h1>Fixture</h1>'
            '<script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'initialState':state}})+'</script>'+extra+'</html>').encode()


class OglasiTests(unittest.TestCase):
    def test_identity_only_accepts_montenegro_home_category_paths(self):
        self.assertEqual(galleries.identify(URL),('oglasi','12345'))
        for url in ['https://oglasi.me/nekretnine/kuce','https://oglasi.me/vozila-i-djelovi/automobili/example-og12345me',
                    'https://oglasi.me/nekretnine/plac/example-og12345me','https://evil.example/nekretnine/kuce/example-og12345me']:
            with self.subTest(url=url),self.assertRaises(ValueError):galleries.identify(url)

    def test_public_ssr_extracts_rental_price_description_fields_and_own_phone(self):
        row=scraper.parse_listing(body(extra='<footer><a href="tel:+38268999999">Support</a></footer>'),URL,'oglasi','12345','now')
        self.assertEqual(row['price_amount'],1800);self.assertEqual(row['rental_period'],'month')
        self.assertEqual(row['deal_type'],'rent');self.assertEqual(row['city'],'Podgorica')
        self.assertEqual(row['area_m2'],270);self.assertEqual(row['lot_area_m2'],350)
        self.assertEqual(row['image_urls'],[PHOTO]);self.assertEqual(row['contact_phones'],['+38267123456'])
        self.assertIn('Izdaje',row['description_original']);self.assertEqual(row['missing_core_fields'],[])
        self.assertEqual(input_property(row)['property_key'],'oglasi_12345')
        self.assertEqual(row['published_date'],'2026-10-01T12:00:00Z')
        self.assertNotEqual(row['published_date'],row['source_refreshed_at'])
        from contact_enrichment import page_contacts
        from lxml import html
        self.assertEqual(page_contacts(html.fromstring('<html/>'),row)[0]['phone_e164'],'+38267123456')

    def test_wrong_detail_identity_country_category_or_inactive_ad_is_rejected(self):
        for change in [{'code':'OG999ME'},{'category':{'seo':'automobili'}},{'country':{'abr':'RS'}},{'listingStatus':'Expired'}]:
            item=ad();item.update(change)
            with self.subTest(change=change),self.assertRaises(scraper.FetchFailure):
                scraper.parse_listing(body(item),URL,'oglasi','12345','now')

    def test_gallery_excludes_profile_hidden_recommendation_and_foreign_host_images(self):
        item=ad()
        item['media'] += [{'fd':PHOTO.replace('photo','hidden'),'ads':999,'visible':False},
                          {'fd':PHOTO.replace('photo','other'),'ads':1000,'visible':True},
                          {'fd':PHOTO.replace('/OG12345ME/','/profile/'),'ads':999},
                          {'fd':PHOTO.replace('slike.oglasi.me','example.org'),'ads':999}]
        self.assertEqual(galleries.extract(body(item),URL,'oglasi','12345')[0],[PHOTO])

    def test_discovery_ignores_recommendations_not_in_current_search_results(self):
        links='<a href="'+URL+'">Listing</a><a href="'+URL.replace('12345','99999')+'">Related</a>'
        self.assertEqual(scraper.listing_links(body(extra=links),'https://oglasi.me/nekretnine/kuce','oglasi'),{'oglasi_12345':URL})
        self.assertEqual(scraper.search_url('oglasi','stanovi',1),'https://oglasi.me/nekretnine/stanovi?page=2')

    def test_price_on_request_keeps_amount_blank(self):
        item=ad();item.update(priceOnRequest=True,price=0)
        row=scraper.parse_listing(body(item),URL,'oglasi','12345','now')
        self.assertTrue(row['price_on_request']);self.assertIsNone(row['price_amount'])

    def test_apartments_and_houses_each_get_capture_slots(self):
        items=[('a'+str(i),{'url':'https://oglasi.me/nekretnine/stanovi/'+str(i)}) for i in range(5)]
        items += [('h'+str(i),{'url':'https://oglasi.me/nekretnine/kuce/'+str(i)}) for i in range(2)]
        self.assertEqual([key for key,_ in scraper.balanced_oglasi_pending(items)],['a0','h0','a1','h1','a2','a3','a4'])


class PhoneMatchTests(unittest.TestCase):
    def row(self,key,phone):
        return {'property_key':key,'source':key.split('_')[0],'listing_url':'https://example.org/'+key,'contact_phones':[phone]}

    def test_normalizes_local_csv_and_international_phones_and_keeps_separate_ads(self):
        existing=self.row('estitor_1','+38267123456');new=self.row('oglasi_2','067/123-456')
        rows=matching.annotate([existing,new],[existing,new])
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['phone_match_status'],'existing_contact')
        self.assertEqual(rows[1]['matched_property_keys'],['estitor_1'])
        self.assertNotIn('phone_match_status',new)
        self.assertEqual(matching.phones({'phone_e164':"'+382 67 123 456"}),['+38267123456'])
        self.assertEqual(matching.phones({'phone':'38267123456'}),['+38267123456'])

    def test_same_listing_never_matches_itself_and_missing_numbers_stay_unknown(self):
        row=self.row('oglasi_2','+38267123456')
        self.assertEqual(matching.annotate([row],[row,row])[0]['phone_match_status'],'new_contact')
        blank=self.row('oglasi_3','+38269257004')
        self.assertEqual(matching.annotate([blank],[row])[0]['phone_match_status'],'no_phone')

    def test_multi_phone_matches_preserve_ambiguity_and_all_references(self):
        a=self.row('estitor_1','+38267123456');b=self.row('realitica_2','+38268654321')
        new={'property_key':'oglasi_3','phone_e164':None,'contact_phones':[a['contact_phones'][0],b['contact_phones'][0]]}
        row=matching.annotate([new],[a,b])[0]
        self.assertEqual(row['phone_match_count'],2);self.assertIsNone(row['phone_e164'])
        self.assertEqual(len(row['phone_matches']),2)

    def test_loader_supports_contact_state_and_exported_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';row=self.row('estitor_1','+38267123456')
            path.write_text(json.dumps({'records':{'estitor_1':row}}))
            self.assertEqual(matching.load_records(path),[row])
            csv_path=Path(folder)/'feed.csv';csv_path.write_text(scraper.csv_text(['property_key','contact_phones'],[row]))
            self.assertEqual(matching.phones(matching.load_records(csv_path)[0]),['+38267123456'])


if __name__=='__main__':unittest.main()
