# Inbound Media Storage

Durable Cloudinary copies of the images, videos, voice notes and
documents customers send over WhatsApp, linked from the `messages` row.

## Problem

Gupshup delivers inbound media as a short-lived file-manager URL
(`payload.payload.url`, with a `urlExpiry` timestamp). The webhook never
read that URL: an image message was reduced to the transcript line
`[Image message]: Customer sent image`, the customer got the
media-unsupported reply, and the asset became unreachable the moment
Gupshup's URL expired.

That cost human agents the one piece of context that matters most in
return/damage conversations — the photo itself — and it was
unrecoverable after the fact.

## Solution

`fashion_bot/utils/media_storage.py` downloads the asset once at receive
time and re-uploads it to Cloudinary, returning a permanent link. Callers
persist that link twice:

- appended to `messages.message`, so existing dashboards render it with
  no frontend change;
- as a structured descriptor under `messages.message_metadata` →
  `{"media": {...}}` (a JSONB column that already existed and was always
  written as `NULL`).

The bot still cannot *read* the media — the short-circuit in
`gupshup_webhook.py` still answers with the per-client media-unsupported
reply. This only stops the asset from being thrown away.

### Storage paths

Three inbound paths reach the same helper, and **none of them makes a
customer wait on the upload**:

| Path | Where storage happens | Link lands on the row via |
|------|----------------------|---------------------------|
| Bot mode | `_astore_inbound_media_and_backfill`, detached at the media short-circuit | `aattach_media_to_message` backfill, concurrent with the reply |
| Agent mode | the same helper, scheduled on `BackgroundTasks` | the same backfill, after the ACK |
| Queued mid-turn | `GupshupRuntimeSupport._amerge_text_with_stored_media` at enqueue | The merged payload's text |

The transcript row is always inserted first with the placeholder text;
storage runs behind it and fills in the durable link when the upload
lands. `_extract_runtime_message_content` — which sits directly in front
of the reply — does no storage work at all, so reply latency is exactly
what it was before this feature. A queued message is not replied to at
all (it is merged into the turn in flight), so its upload is off the reply
path by construction.

The backfill matches on conversation + message side + the exact
placeholder text, so a message that arrived in between is never
rewritten. If the text no longer matches, the backfill is a no-op and the
row keeps its placeholder. It also skips rows that already carry a media
descriptor: two media messages in one conversation share a placeholder
("[Video message]: Customer sent video"), so without that guard both
backfills would land on the newest row and the older one would never get
its link. The guard tests for the `media` key rather than for NULL
metadata, because the async tag generator writes to the same column.

One consequence worth knowing: for a second or two after the message
appears, the dashboard shows the placeholder without the link. The live
Redis publish carries the placeholder only — the link arrives with the
row, not with the push — so an open conversation view picks it up on its
next fetch.

### Object layout

```
{CLOUDINARY_UPLOAD_FOLDER}/{client_id}/{last 4 phone digits}/{sha256(client_id:message_id)[:32]}
```

Only the last four digits of the phone appear in the object path — the
full number already lives on the `messages` row, and object paths end up
inside CDN URLs.

The public id is derived from the Gupshup message id, hashed with the
client id, and uploaded with `overwrite=true`. That makes the upload
**idempotent**: a webhook redelivery overwrites the same asset instead of
leaving a duplicate behind. Dedup normally prevents reprocessing, but it
fails open when Redis is down, so the upload cannot rely on it. Hashing
keeps the id underivable from the message id alone, so the delivery URL
stays unguessable under the default public delivery type. Payloads with
no message id fall back to a random id.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `INBOUND_MEDIA_STORAGE_ENABLED` | `true` | Global on/off. Per-client override below wins. |
| `CLOUDINARY_CLOUD_NAME` | — | Required to upload. |
| `CLOUDINARY_API_KEY` | — | Required to upload. |
| `CLOUDINARY_API_SECRET` | — | Required to upload (used to sign, never sent). |
| `CLOUDINARY_UPLOAD_FOLDER` | `whatsapp-inbound` | Base folder. |
| `CLOUDINARY_DELIVERY_TYPE` | `upload` | Set to `authenticated` for signed delivery instead of unguessable URLs. |
| `INBOUND_MEDIA_MAX_BYTES` | `26214400` (25 MB) | Larger assets are skipped, not uploaded. |
| `INBOUND_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | `30` | Gupshup fetch. |
| `INBOUND_MEDIA_UPLOAD_TIMEOUT_SECONDS` | `45` | Cloudinary upload. |
| `INBOUND_MEDIA_TOTAL_TIMEOUT_SECONDS` | `60` | Hard ceiling on download + upload. |

No customer-visible reply waits on these timeouts — every path stores
behind the reply. They bound how long the detached task lives, and
therefore how stale the link can be before the backfill gives up. Sized
for the largest thing WhatsApp delivers rather than for a photo:
production round-trips measured ~3s for an image and ~6.5s for a short
video, so the ceiling only bites on a genuinely sick network path.

Storage is **on by default**. Two independent kill switches sit in front
of it: `INBOUND_MEDIA_STORAGE_ENABLED=false` turns it off globally, and
`client_configs.inbound_media_storage_enabled` (`true`/`false`, read
through the tiered config cache) overrides the global default either way,
so a single tenant can be opted out — or back in — without a deploy.

Enabling alone is inert: with no Cloudinary credentials configured the
storage step no-ops, warns once per process, and every path keeps its
pre-feature behaviour. The effective switch-on is provisioning
Cloudinary, not the flag.

The official `cloudinary` SDK is synchronous, so this module signs and
calls the REST upload endpoint over the shared `httpx.AsyncClient`
instead (AGENTS.md §1, §4). No new dependency.

### Required Cloudinary account setting for documents

**PDF and ZIP delivery is disabled by default on Cloudinary accounts.**
With it off, a document uploads and stores perfectly well, and then every
attempt to open the link fails — Cloudinary answers `HTTP 401` with
`x-cld-error: deny or ACL failure`, which Chrome surfaces as "File wasn't
available on site". Nothing in the logs reports a problem, because the
upload genuinely succeeded.

Fix it in the console: **Settings → Security → Allow delivery of PDF and
ZIP files**. Images and video are unaffected by this setting.

Verified in production on 2026-08-18: an image (`image/upload`, 200) and a
video (`video/upload`, 200) delivered from the same folder, while a PDF
stored in the same run (`raw/upload`, 430 KB, upload logged as successful)
returned 401 `deny or ACL failure`. If that setting is already enabled and
documents still 401, check whether the account restricts `raw` delivery.

## Failure behaviour

All three paths go through one entry point, `aprepare_inbound_media`,
which returns `(transcript_text, metadata)`. The invariant callers rely
on:

> **The message row changes if and only if an asset was actually stored.**

Feature disabled, no media on the payload, credentials missing, asset
oversized, download or upload error, timeout — every one of them returns
the placeholder unchanged with `None` metadata, which is byte-for-byte
the behaviour that predates this module (`message_metadata` stays
`NULL`). Failures are surfaced through logs and Rollbar, not by writing a
breadcrumb onto the customer's transcript. Media storage must never cost
a customer their response.

The feature-flag lookup reads config (memory → Redis → Postgres), so it
runs *inside* the timeout budget rather than in front of it: with the
flag off and both Redis and Postgres unreachable, the image path still
returns the unchanged placeholder within the ceiling instead of stacking
pool-acquire retries onto the reply.

## Media kinds

All four inbound kinds Gupshup delivers are stored, routed to the
matching Cloudinary resource type:

| Gupshup type | Resource type | Transcript line |
|--------------|---------------|-----------------|
| `image` | `image` | `[Image message]: <caption>` |
| `video` | `video` | `[Video message]: <caption>` |
| `audio` | `video` (Cloudinary serves audio through the video pipeline) | `[Audio message]: Customer sent audio` |
| `file` | `raw` | `[File message]: <filename>` |

Documents short-circuit like the other three, with their own
`media_unsupported_reply_file` per-client key. Before this change a
document fell through to the agent graph as the bare text
`[file message]` and the LLM improvised a reply; now it gets the same
canned response as an image, and the attachment itself is retained.

Voice notes are **not transcribed**. They used to run through Whisper
(`whisper-1`, language auto-detected) in front of the reply, and Hindi
speech came back written in Urdu script — a transcript that was
misleading rather than useful, on the reply path, at a per-message API
cost. The stored recording is the better artefact: an agent can simply
play it. `utils/audio_processor.py` is left in the tree but nothing calls
it any more.

## Known gaps

- **Retention is Cloudinary-side.** Customer photos can include IDs and
  payment screenshots. Set an auto-expiry / retention rule on the folder,
  and prefer `CLOUDINARY_DELIVERY_TYPE=authenticated` for tenants that
  need signed delivery rather than unguessable URLs.
- **The bot still cannot see the media.** The stored link makes vision
  (or document parsing) a follow-up change rather than a redesign — the
  URL is now on the message where the graph could read it.
