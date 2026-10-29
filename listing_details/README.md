# GRADEN listing text and structured details

All 1,797 supplied listing URLs have an exported record. Original seller descriptions were recovered for 1,585 listings. There are 138 reachable pages with no published seller description, and 74 unavailable/redirected listings. Missing text has not been fabricated or replaced by a title or shortened meta description.

## Files

- `GRADEN_listings_complete.xlsx`: one row per listing, with original description, normalized fields, image URLs, contacts, missing-field lists and quality flags. The second sheet counts missing core fields.
- `listings.csv`: the same tabular data, UTF-8 with BOM for Excel.
- `listings.json`: full structured records, including all source attributes, field provenance, workbook references, contact candidates, workbook annotations and conflicting values.
- `needs_review.csv`: records missing core fields or carrying quality flags.
- `run_summary.json`: coverage counts and limitations.
- `validation.json`: completed checks.

Every property folder under `../property_images/SOURCE/LISTING_ID/` now also contains:

- `description.txt`: original seller text, with paragraph/whitespace normalization only; empty if no description was published or the source was unavailable.
- `details.json`: that listing's complete structured record.
- `listing.md`: readable draft combining the known title, facts and original description. It clearly marks missing description or missing core data; it does not invent marketing copy.

Existing `images.txt`, `images.csv` and image collection metadata remain unchanged. All 14,522 image URLs are preserved.

## Data quality and import rules

1,360 records pass the working core-data check: title, original description, sale/rent purpose, property type, city/area label, published price or explicit price-on-request, property/land area, one unambiguous contact phone, and at least one image. This is a transparent data-coverage check, not validation against GRADEN's production schema or approval to publish. All categories remain available, including the workbook's Likely agents, Not homes and Rejected tabs; use lead_categories and graden_outreach_candidate to select the intended group.

Unknown values are null/blank. Exact coordinates were not published in the captured pages and were not fabricated or replaced by city centers. Room counts and bedroom counts are separate. Bedroom counts derived from explicit local words such as Jednosoban are labeled as normalizations. Original attribute names/values remain in source_attributes. Generic area on a land listing is stored as lot_area_m2.

Published price-on-request remains explicit. Price is never used to guess sale versus rent, and rental periods are not assumed to be monthly. Source attributes, explicit breadcrumb categories, titles and original descriptions may supply the purpose/type, with field-level provenance. Ambiguous or missing values are left for review.

Source page fields take priority. Existing workbook fields are used as fallbacks only for the exact primary URL, so an owner's second ad does not inherit the first ad's price, city or area. Owner contact data can be associated with both supplied URLs, but its workbook row is retained in contact_candidates. Previous verified Estitor phone results are explicitly labeled with their earlier source. Multiple conflicting phone candidates are retained for review without choosing one. The platform number +38269257004 is excluded.

The source descriptions are kept in their original language and wording. Site navigation, related ads, contact/report forms, JavaScript and footer text are excluded. Source meta descriptions, when present, are stored separately as metadata_summary and are not treated as the full seller text. Source taxonomy location names are preserved; they are not independent address verification.

## Validation and reproduction

Checks passed for all 1,797 property mappings/folders, all 14,522 unchanged image URLs, exact description agreement across TXT/JSON/CSV/Excel, exclusion of site UI text, numeric/locale cases, three site-specific field fixtures, preservation of price-on-request/land area, and the primary-versus-secondary URL fallback rule. No description exceeds Excel's text cell limit. The original uploaded workbook was not edited; its SHA-256 is recorded in run_summary.json.

Extraction reused the HTML captured during the image collection on 2026-10-29, rather than charging Bright Data again for the same pages. Source capture and extraction timestamps are separate. Raw HTML caches remain in the existing cloud workspace and are not committed or packaged. The downloadable records are ready to read independently of those caches.

With Python 3.10+, lxml and openpyxl, re-extract from those retained page caches using the original workbook:

```bash
python -m pip install lxml openpyxl
python extract_listing_details.py --workbook /path/to/GRADEN_leads.xlsx
python verify_listing_details.py
```

The complete ZIP includes the enriched property folders, master exports, URL input mapping and extraction/verification scripts. It excludes private credentials, raw HTML, and unrelated files. No calls, contact forms, callbacks, feedback or outreach were initiated.
