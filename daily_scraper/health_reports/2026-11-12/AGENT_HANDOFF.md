# Real-estate producer update — 12 November 2026

Continue the platform's existing qualification process and preserve its outreach history. This update describes producer data; it does not authorize messages or change CRM queues.

Read the cumulative index and follow assets.listings.json:
https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/graden-imports/listings/latest.json

Upsert by property_key. This index joins all cumulative contact records to the complete R2 photo catalog. The separate daily photo index describes its last processing batch. Per-record image_urls contains selected ready images; photos provides ready_for_listing, selected_version and evidence. Original source_image_urls and held source_copy_url values are provenance/cleanup inputs.

The authenticated phone backfill added contacts to 195 Patuljak and seven Estitor listings: 202 listing relationships, representing 34 previously unseen distinct numbers. Existing contacts were preserved. The remaining 97 multiple-number records retain their candidates without an arbitrary primary. Advertiser contacts do not independently establish private-owner identity.

The requested 18 records now contain 88 ready R2 images, 76 more than before recovery; 23 were actually inpainted. Other ready images are sources accepted under gallery sampling or individual OCR/template checks. Known branding, posters and uncertain cases remain held. Automatic detection is not a certificate for every possible watermark. Check ready_for_listing and the scoped CSV; two galleries currently have only two approved images.

The four Realitica ads previously reported removed — realitica_3955786, realitica_3955834, realitica_3955967 and realitica_3956026 — returned valid, live listing pages during recheck. Requalify their CRM disposition; a blocked/challenge response does not prove removal. patuljak_86867 redirects away from its listing. No CRM status was changed by this recovery.

The workflow now prioritizes fresh daily galleries, supports photos_only and explicit priority_properties recovery, retries dependency downloads, and waits for actual source DOM after Realitica challenges. The provided Patuljak session is stored as PATULJAK_COOKIE in GitHub Actions; consumers need no cookie or API credentials for the public read feed. Four live verification runs succeeded. Source discovery and photos remain bounded by per-run budgets; reports expose the queue instead of implying full-market completion.

report.json contains verification evidence and ready URLs for the 18 requested records. property_photo_status.csv contains their current counts and disposition notes without phone numbers.
