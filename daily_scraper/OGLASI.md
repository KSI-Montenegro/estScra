# Oglasi.me property collection

Oglasi apartments (`/nekretnine/stanovi`) and houses (`/nekretnine/kuce`) are
included in the existing daily property workflow's `all` source selection. The
manual workflow also offers `oglasi`. No separate account or browser extension
is required. Direct HTTPS worked in live checks on 2026-11-13; the existing
bounded Bright Data fallback remains available if direct access fails.

The observed page parameter is `?page=1`, `?page=2`, etc. Discovery reads the
current search result IDs from public `__NEXT_DATA__` and matches them to observed
listing links. Recommendation ads do not enter discovery. Capture slots alternate
apartments and houses so a larger apartment backlog cannot starve houses. The
normal URL/ID checkpoint prevents capturing the same Oglasi ad twice.

Listing identity uses the **public OG code**, e.g. `oglasi_75078779`, rather than
the website's separate internal database ID. Canonical URL, current-ad code,
home category, Montenegro country, and active published status are checked.
Details, original description, prices, rental period, attributes, equipment, and
the current advertiser's published phone list come from `ads.detail.currentAd`.
The publication date is preserved separately from refresh timestamps. Unknown
facts remain blank. Multiple phones remain separate candidates; profile type is
reported without asserting that the advertiser owns the property.

Gallery URLs use visible `currentAd.media[].fd` originals on `slike.oglasi.me`,
scoped to the exact OG code and internal ad ID. Hidden images, profile pictures,
recommendations, and category thumbnails are excluded. The examples expose 13
visible apartment images and 15 visible house images; the house's 22 total media
entries include hidden entries.

## Watermarks and R2

New galleries enter the existing durable photo queue. One representative image
flags the property; the Oglasi centre stamp is located independently on every
image that needs cleaning. The calibrated Oglasi word shape and placement select
the mask; OCR does not choose an arbitrary erasure region. Unrecognized placement,
additional unsupported text/brands, and failed checks remain held for review.
Because verified Oglasi CDN originals have faint centre stamps, negative OCR
alone never releases an Oglasi image whose calibrated stamp could not be located.
Only the Oglasi template is selected automatically for these properties; scenery
that resembles another portal's logo does not authorize a second edit region.

The pinned local LaMa model performs removal without an image-editing API fee.
Pixels outside the mask are unchanged before encoding. Template residual and
focused OCR checks run before encoding, and template residual checks run again on
the delivered WebP. These are automated checks, not a guarantee of invisible
inpainting artifacts. Optimization targets 300 KB with a 400,000-byte ceiling.

Files use the existing `graden-property-photos` bucket and catalog:

```text
properties/oglasi/<public-numeric-code>/source/<number>-<hash>.webp
properties/oglasi/<public-numeric-code>/cleaned/<number>-<hash>.webp
graden-imports/property-photos/latest.json
graden-imports/listings/latest.json
```

The cumulative listing feed's `photos` retains held/source evidence, while
`image_urls` contains only ready selections. Existing cleaned selections are
preserved. The run caps and cooperative checkpoints also apply to Oglasi.

## Checking phones against existing scraped listings

The cumulative publication now includes `phone_matches.json` and
`phone_matches.csv` in `latest.json`'s `assets`, alongside the normal listing feed.
Every listing also has `phone_match_status`, `phone_match_count`,
`matched_property_keys`, and `phone_matches` with the matching source URLs.

The index combines the historical `listing_details/listings.json` import, the
earlier `qualified_listings_with_phones.csv`, and all cumulative daily records
across portals. Local `067…`, `00382…`, bare `382…`, and
international `+382…` formatting normalize to the same number. Platform support
numbers are excluded. A listing never matches itself. No phone means `no_phone`,
not a confirmed new contact.

`existing_contact` means the number appears on another known listing. It does
**not** prove that the two ads describe the same property, that the contact is a
private owner, or that anyone has already contacted them. No ads are deleted or
merged by this check; the CRM can group by phone while retaining every listing.

Run the standalone script against local exports:

```sh
python daily_scraper/phone_matches.py \
  --candidates path/to/new-listings.json \
  --known listing_details/listings.json qualified_listings_with_phones.csv daily_scraper/runtime/contact-state.json \
  --output /tmp/oglasi-phone-matches
```

It writes `/tmp/oglasi-phone-matches.json` and `.csv`. Inputs accept listing JSON
arrays, cumulative contact-state JSON, or CSV. Use the current API/feed or CRM
export as an additional `--known` input if it contains contacts outside this
scraper's historical and daily records.

Initial scans are bounded by the workflow's page and capture limits. Adding the
source does not imply that every historical Oglasi ad has already been imported.

Live GitHub verification on 2026-11-13 passed in
[run 43425482745](https://github.com/KSI-Montenegro/estScra/actions/runs/43425482745).
The cumulative feed includes four Oglasi ads. The two supplied example galleries
have 26 cleaned ready photos (11 apartment, 15 house), with apartment images 2 and
5 held because their stamp masks could not be located confidently. Ready image
URLs were checked over public HTTPS. The house phone matches four earlier ads;
the apartment phone also matches another Oglasi ad found in the new capture batch.
See `oglasi_validation.json` for the evidence and report URL.
