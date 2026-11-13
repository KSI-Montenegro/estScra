"""Read listing-scoped public Oglasi.me property SSR data; never profile/recommendation ads."""
import json
import re
from urllib.parse import urljoin, urlsplit


class OglasiFailure(ValueError):
    pass


def next_state(root):
    try:
        nodes = root.xpath('//script[@id="__NEXT_DATA__"]/text()')
        state = json.loads(nodes[0])['props']['initialState']
        if not isinstance(state, dict):
            raise TypeError()
        return state
    except (IndexError, KeyError, TypeError, ValueError):
        raise OglasiFailure('oglasi_ssr_schema_missing_or_changed') from None


def current_ad(root, ident):
    ad = next_state(root).get('ads', {}).get('detail', {}).get('currentAd')
    if not isinstance(ad, dict) or str(ad.get('code', '')).upper() != 'OG' + ident + 'ME':
        raise OglasiFailure('oglasi_detail_identity_mismatch')
    if (ad.get('category') or {}).get('seo') not in ('stanovi', 'kuce'):
        raise OglasiFailure('oglasi_non_home_category')
    if str((ad.get('country') or {}).get('abr', '')).upper() not in ('ME', 'MNE'):
        raise OglasiFailure('oglasi_outside_montenegro')
    if ad.get('status') != 'Published' or ad.get('listingStatus') != 'Active':
        raise OglasiFailure('oglasi_listing_unavailable')
    return ad


def gallery(root, ident):
    ad = current_ad(root, ident)
    result = []
    for item in sorted(ad.get('media', []), key=lambda v: (not bool(v.get('main')), v.get('priority') or 0)):
        value = item.get('fd')
        if item.get('visible') is False or item.get('ads') not in (None, ad.get('id')) or not isinstance(value, str):
            continue
        u = urlsplit(value)
        if (u.scheme == 'https' and u.hostname == 'slike.oglasi.me' and not u.username and not u.password
                and u.port in (None, 443) and '/OG' + ident + 'ME/' in u.path
                and u.path.lower().endswith(('.jpg', '.jpeg', '.webp', '.png')) and value not in result):
            result.append(value)
    return result


def listing_links(root, page_url):
    import collect_images as galleries
    ads = next_state(root).get('ads', {})
    items = ads.get('list', {}).get('elements', [])
    codes = {str(item.get('code', '')).upper() for item in items if isinstance(item, dict)}
    found = {}
    for value in root.xpath('//a/@href'):
        url = urljoin(page_url, value).split('?')[0].split('#')[0]
        try:
            source, ident = galleries.identify(url)
        except ValueError:
            continue
        if source == 'oglasi' and 'OG' + ident + 'ME' in codes:
            found['oglasi_' + ident] = url
    return found
