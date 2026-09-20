# 05 — Content & media reference

The complete command reference for fbk's content and media surfaces: **search**,
**marketplace**, **video**, **photos**, **events**, **upload**, and **draft**. These are
the discovery surfaces (find people, pages, groups, listings, videos, events), the
media planes (photos, Watch, uploads), and the CLI-local draft store that decouples
composing a post from publishing it.

Every subcommand below was verified against `fbk <family> <sub> --help` and the
command/surface sources listed at the end of this guide. Research internals (wire
shapes, preload-replay discipline, mutation decoding) are cited by plain filename from
the repo-root research docs (`docs/01…16`); the fbk-side guides are
[03-session-and-auth.md](03-session-and-auth.md) (global flags — not repeated here),
[04-reference-feed.md](04-reference-feed.md) (the feed/composer surface this guide
builds on), and [09-safety-and-opsec.md](09-safety-and-opsec.md) (the governor).

Conventions used throughout:

* Every subcommand accepts the common flags (`--root`, `--cookies`, `--no-journal`,
  `--json`, `--retry`, `--dry-run`) — see
  [03-session-and-auth.md](03-session-and-auth.md). Only family-specific flags appear
  in the tables below.
* Mutations — `events create`/`delete`, `upload photo`/`video` with `--caption`,
  `draft publish` — consume the separate **40/day mutation budget**, not the 500/day
  read cap. See [09-safety-and-opsec.md](09-safety-and-opsec.md) before your first
  mutation.
* "Preload replay" means: GET the surface's web page, harvest the SSR preload
  registration (query name, doc_id, and the **verbatim variables the server itself
  used**), and replay that pair unchanged — never a re-assembled variables dict
  (15-live-calibration-findings.md §P2-2, §P3-5). It is the most faithful and
  bypass-resistant read available; where a surface's variables embed opaque blobs,
  it is the only shape that works at all.
* Each mutation carries a one-line **Live status** note stating exactly what has been
  verified live, per the calibration notes in the surface sources.

## Contents

* [fbk search](#fbk-search)
* [fbk marketplace browse](#fbk-marketplace-browse) · [search](#fbk-marketplace-search) · [item](#fbk-marketplace-item)
* [fbk video feed](#fbk-video-feed) · [badge](#fbk-video-badge)
* [fbk photos albums](#fbk-photos-albums) · [photos](#fbk-photos-photos)
* [fbk events list](#fbk-events-list) · [feed](#fbk-events-feed) · [create](#fbk-events-create) · [delete](#fbk-events-delete)
* [fbk upload photo](#fbk-upload-photo) · [video](#fbk-upload-video)
* [fbk draft](#fbk-draft-save) — [save](#fbk-draft-save) · [list](#fbk-draft-list) · [show](#fbk-draft-show) · [publish](#fbk-draft-publish) · [delete](#fbk-draft-delete)
* [Files that feed this guide](#files-that-feed-this-guide)

---

## fbk search

Search Facebook's top results for a free-text query, via the preload replay of the
browser's own SSR search query. This is the read counterpart of typing into the
facebook.com search box: `GET /search/top?q=<terms>` embeds
`SearchCometResultsInitialResultsQuery` (doc_id 29118914987711900) plus its verbatim
variables in the page preload registry; the replay of that pair yields a ~1.2MB typed
results payload (15-live-calibration-findings.md §P3-5). The page-parallel
`SearchCometResultsInitialResultsParallelFetchQuery` is an ads companion that returns
an empty envelope and is deliberately ignored. Search is the most integrity-gated
read surface (02-endpoint-surface-map.md §2.4, 10-rate-limiting-and-behavioral-detection.md
§2) — expect checkpoint-class enforcement here earlier than on profile reads.

Syntax:

```
fbk search [flags] QUERY [QUERY ...]
```

The query is one or more positional terms, joined with spaces — quote naturally
(`fbk search "python programming"`). The terms ride urlencoded in the page URL; the
page bakes the term into the preload's `args.text`, and the replay is verbatim, never
re-assembled.

| flag | meaning |
|---|---|
| `--limit N` | maximum typed results to emit (default **20**) |

Examples:

```
fbk search "python programming"
fbk search open source robotics --limit 50 --json
fbk search "python programming" --dry-run
```

Output notes:

* Human mode prints the query, the result count plus the raw payload byte weight
  (`raw_size` — a degraded-but-large response stays observable, per
  10-rate-limiting-and-behavioral-detection.md §3.5), then one typed line per result:
  `[__typename] name url`.
* Typed results are `User` / `Page` / `Group` / `Story` / `Video` nodes, deduped on
  (typename, id); each carries `typename`, `name`, `id`, `url`, and a `snippet` (any
  nearby human-text field: text, subtitle, category_type, description, city).
* `--json` emits the full `SearchResponse` model: `{query, results[], raw_size}`.
* Exit code is **0 even with zero results** — an empty result set on a
  soft-throttled session is itself diagnostic data, not a failure.

Live status: read-only; the preload-replay pattern is live-verified
(15-live-calibration-findings.md §P3-5). When the fetched page carries no preload
entry, the baked live-captured variables (with your query swapped into `args.text`)
are the fallback.

---

## fbk marketplace browse

Browse the Marketplace home feed (read-only). `GET /marketplace/` embeds
`MarketplaceCometBrowseFeedLightContainerQuery` (doc_id 28053535904279798) with
verbatim variables; the replay's units carry `GroupCommerceProductItem` listing
nodes — the concrete Marketplace listing typename (title, price, id; item URL
assembled from the numeric id). Marketplace pagination is among the tightest
surfaces (10-rate-limiting-and-behavioral-detection.md §2): deep walks degrade to
empty or challenge payloads before reads elsewhere show pressure.

Syntax:

```
fbk marketplace browse [flags]
```

| flag | meaning |
|---|---|
| `--limit N` | maximum listings to emit (default **20**); also rides the `count` variable |

Examples:

```
fbk marketplace browse
fbk marketplace browse --limit 50 --json
```

Output notes:

* Human mode prints `listings: N`, then one row per listing:
  `[typename] name price-snippet url` (the snippet is the formatted price).
* `--json` emits `{listings: [SearchResult...], count}` — each row carries
  `typename`, `name` (listing title), `id`, `url`, `snippet` (price).
* Exit 0 even when the feed is empty — an empty feed is valid state and, per
  10-rate-limiting-and-behavioral-detection.md §2, a possible soft-throttle
  signature on a hot session.

Live status: read-only; the browse preload pattern is live-verified
(15-live-calibration-findings.md §P2-2).

---

## fbk marketplace search

Search Marketplace listings by a free-text term (read-only). `GET
/marketplace/search/?query=<term>` preloads
`CometMarketplaceSearchContentContainerQuery` (the search page's shell
`CometMarketplaceSearchRootQuery` preloads too but carries no listings); its replay
carries the actual listing nodes — `ProductItem` in the search feed (same listing
shape as the browse feed's `GroupCommerceProductItem`). The query term rides inside
opaque params blobs (`params.bqf.query` / `savedSearchQuery`), so the preload is
replayed **exactly as harvested** — re-assembling variables is structurally
impossible, and there is no offline template.

Syntax:

```
fbk marketplace search [flags] QUERY
```

| flag | meaning |
|---|---|
| `--limit N` | maximum listings to emit (default **10**); the slice happens locally |

Examples:

```
fbk marketplace search laptop
fbk marketplace search "office chair" --limit 25 --json
```

Output notes:

* Human mode prints `listings for 'QUERY': N`, then one row per listing
  (`[typename] name price url`) — same typed row shape as `browse`.
* `--json` emits `{query, listings, count}`.
* Zero hits is valid (exit 0) — repeated thin results are an enforcement signal on
  this tightly-quota'd surface, not a bug. If the fetched search page preloads no
  search query at all, the command raises a typed `ValueError` (there is no
  fallback shape for this surface).

Live status: read-only; live-probed 2026-09 (surfaces/marketplace.py calibration
notes).

---

## fbk marketplace item

Decode one listing in full by numeric item id (read-only). `GET
/marketplace/item/<id>/` preloads `MarketplacePDPContainerQuery` — its replay
carries the full listing via `viewer.marketplace_product_details_page.target` —
plus the registry-backed `MarketplacePDPC2CMediaViewerWithImagesQuery`, whose
replay carries the listing photo URLs (`target.listing_photos[].image.uri`). The
item id rides as the top-level `targetId` variable.

Syntax:

```
fbk marketplace item [flags] --item-id ITEM_ID
```

| flag | meaning |
|---|---|
| `--item-id ID` | numeric Marketplace item id (**required**) |

Examples:

```
fbk marketplace item --item-id 12345678901234567
fbk marketplace item --item-id 12345678901234567 --json
```

Output notes:

* Human mode prints the decoded fields:

  ```
  item 12345678901234567: Vintage typewriter
  price: $45  location: Springfield, IL
  seller: Marketplace Seller 12345678901234567
  description: <first 280 characters, or ?>
  images: 3
    https://scontent.example/...jpg
    ...
  ```

* The full decoded field set: `id`, `typename`, `title`
  (`marketplace_listing_title`), `description` (the redacted description head, 280
  chars in human mode), `price` (formatted, zeros-stripped when available),
  `seller` (`{id, name}` off `marketplace_listing_seller`), `location`
  (`location_text`), `images` (up to 6 photo URLs from the best-effort media-viewer
  companion replay — a failed companion call degrades to no images, never to a
  failed detail), `url`, `creation_time`.
* `--json` emits `{item: <full detail dict>}` — nothing is truncated in machine mode.
* An unparsable or missing listing surfaces as a typed error (via the command
  runner), not as a silent zero-field success.

Live status: read-only; item-detail pattern live-probed 2026-09. A dead item-page
GET deliberately degrades to the baked live-captured variables template — the
GraphQL replay that follows is itself a live, typed-error call.

---

## fbk video feed

Browse the `/watch/` video feed (read-only). `GET /watch/` preloads the unified
video feed entry query (live `FBUnifiedVideoRootWithEntrypointQuery`, registry
doc_id 38063999043248030) with verbatim variables; the replay is walked for `Video`
nodes, threaded through their enclosing feed-story wrapper so the post caption and
UFI feedback id survive even when the `Video` node itself omits them. Rows are
deduped on the video id.

Watch pagination is **chained**: continuation replays the schema-decoded
`CometWatchAndScrollChainingQuery` (registry doc_id 28164369823234560) with the
opaque `chainingCursor` **and** the previous page's last video id as
`seedVideoID` — the `/watch/` surface keys continuation on the pair, unlike the
feed's cursor-only pagination.

Syntax:

```
fbk video feed [flags]
```

| flag | meaning |
|---|---|
| `--limit N` | maximum videos to emit (default **10**); rides the `count` variable |
| `--cursor CURSOR` | chaining cursor from a previous page's `end_cursor` |
| `--seed-video-id ID` | seed video id for chaining (the **last video id of the previous page**) |

Examples:

```
fbk video feed
fbk video feed --limit 20 --json
# chaining: pass BOTH the cursor and the previous page's last video id
fbk video feed --cursor <end_cursor> --seed-video-id 12345678901234567
```

Output notes:

* Human mode prints `videos: N`, then one line per video:
  `[id] title — owner url`.
* Typed rows carry: `id`, `url` (permalink/watch link), `title` (first line, 120
  chars, resolved from the node's message / creation story / track metadata /
  enclosing story), `owner_id`, `owner_name`, `length_s`, `view_count`,
  `feedback_id` (the UFI target for follow-ups).
* `--json` emits `{videos, count, end_cursor, has_next_page}` — chain the next page
  from `end_cursor` plus the last row's `id`.
* An empty feed is valid state (exit 0).

Live status: read-only; the feed entry, chaining hook, and row shapes are
live-verified (15-live-calibration-findings.md; surfaces/video.py calibration
notes, live-probed 2026-09).

---

## fbk video badge

The unseen-video count for the Watch tab (read-only) — the cheapest video-surface
read. `useCometWatchBadgeCountQuery` (registry doc_id 23979318198368825) is the rare
zero-argument persisted query — every argument is a query-text literal (the Watch
top-tab bookmark id 2392950137), so the replay sends empty variables `{}`; the
count rides `viewer.bookmarks.edges[].node.unread_count`.

Syntax:

```
fbk video badge [flags]
```

No family-specific flags.

Example:

```
fbk video badge
fbk video badge --json
```

Output notes:

* Human mode prints `unseen videos: N`.
* `--json` emits `{unseen: N}`.
* The count is valid whatever its value (exit 0); 0 means the payload carried no
  readable count.

Live status: read-only; live-verified (the response carries exactly one bookmark
edge — surfaces/video.py calibration notes).

---

## fbk photos albums

Read the **own profile's** album tiles (read-only). This family reads only your own
photos surface — there is no other-profile scope. The photos tab
(`profile.php?id=<uid>&sk=photos`) preloads `ProfileCometTopAppSectionQuery`; the
service replays it twice — once for the default grid (whose `nav_collections`
carry the tab's collection tiles), then scoped to the Albums tile's opaque
collection token, whose `TimelineAppCollectionAlbumsRenderer` grid carries the
album rows.

Syntax:

```
fbk photos albums [flags]
```

| flag | meaning |
|---|---|
| `--limit N` | maximum albums to emit (default **10**); sliced locally |

Examples:

```
fbk photos albums
fbk photos albums --json
```

Output notes:

* Human mode prints `albums: N`, then one row per album: `name id count url` (the
  count is the "<N> items" subtitle).
* `--json` emits `{albums, count}` — rows carry `id` (the numeric album id), `name`,
  `count`, `cover` (cover-image URI), `url`.
* Zero albums is valid account state (exit 0). Album ids feed
  `fbk photos photos --album-id`.

Live status: read-only; own-profile pattern live-verified 2026-09
(surfaces/photos.py calibration notes). The photos-tab section id inside the
sectionToken is a live-verified global constant (`app_section:<uid>:2305272732`),
so the own-profile token is constructible from the viewer id alone.

---

## fbk photos photos

Read the **own profile's** photo grid (read-only), optionally scoped to one album.
Without `--album-id` the default photos-grid collection is replayed (`Photo` nodes
with `viewer_image` URI/dimensions). With a numeric `--album-id` (as returned by
`fbk photos albums`) the album page's own preload
(`ProfileCometLegacyAlbumViewRootQuery`, `mediaSetToken = "a.<album_id>"` —
derivable from the numeric id, no opaque token needed) is replayed; its
`data.mediaset.grid_media.edges` carry that album's photos.

Syntax:

```
fbk photos photos [flags]
```

| flag | meaning |
|---|---|
| `--album-id ID` | numeric album id (from `photos albums`); scopes the grid to that album when given |
| `--limit N` | maximum photos to emit (default **20**); sliced locally |

Examples:

```
fbk photos photos
fbk photos photos --album-id 12345678901234567 --limit 50
fbk photos photos --json
```

Output notes:

* Human mode prints `photos (album 12345678901234567): N` (or
  `photos (default grid): N`), then one row per photo: `id WIDTHxHEIGHT uri`.
* `--json` emits `{photos, count}` — rows carry `id`, `uri`, `width`, `height`,
  plus `url` (grid scope) or `caption` (the accessibility caption, album scope).
* An empty grid is valid account state (exit 0).
* The photo fbid these rows carry is the savable node id `fbk saved save` takes
  (see 04-reference-feed.md for the saved family).

Live status: read-only; both scopes live-verified 2026-09 (surfaces/photos.py
calibration notes). **Own-profile only** — no other-user flag exists on this
family.

---

## fbk events list

List the viewer's events (read-only), via the preload replay of the `/events/` page.
`GET /events/` SSR-registers `EventCometHomeRootQuery` (doc_id 38585542624394211)
with the verbatim variables the server itself used; the replay returns
`viewer.actor.upcoming_events.edges[].node` (own upcoming events) plus
`viewer.actor.content_tab.requested_tab.events.edges[].node` (discover content) —
Event nodes carrying id / name / day_time_sentence / start_timestamp / is_past /
is_viewer_host.

Syntax:

```
fbk events list [flags]
```

| flag | meaning |
|---|---|
| `--limit N` | maximum event rows to emit (default **20**); sliced locally |

Examples:

```
fbk events list
fbk events list --json
```

Output notes:

* Human mode prints `events: N`, then one row per event; rows you host are marked
  `[host]` — those are the events `events delete` may legally target:

  ```
  events: 3
  [host] Community meetup — Saturday, Sep 26 at 2:00 PM (12345678901234567)
         City tour — Sunday, Sep 27 (12345678901234568)
  ```

* `--json` emits `{events, count}` — rows carry `id`, `name`, `date_text`,
  `start_timestamp`, `is_past`, `is_viewer_host`, `going_count` (when the card
  variant carries one), `typename`, `source` (`upcoming_events` or `discover`).
* Zero events is valid account state (exit 0).

Live status: read-only; live-verified preload replay (live-probed 2026-09-18,
surfaces/events.py calibration notes).

---

## fbk events feed

Read one event's discussion feed (read-only, cursor-paginated). Replays
`EventCometPermalinkDiscussionFeedPaginationQuery` (registry doc_id
28948528681410727) — the schema-decoded LocalArgument template with the Relay node
id substituted (`base64("Event:<numeric>")`, 14-glossary-and-reference.md identifier
table) — and extracts typed stories with the shared feed walker
(04-graphql-protocol-deep-dive.md §5).

Syntax:

```
fbk events feed [flags] --event-id EVENT_ID
```

| flag | meaning |
|---|---|
| `--event-id ID` | event id (**required**); numeric fbid or Relay base64 form |
| `--limit N` | maximum feed stories to emit (default **10**); rides the `count` variable |
| `--cursor CURSOR` | pagination cursor from a previous page's `end_cursor` |

Examples:

```
fbk events feed --event-id 12345678901234567
fbk events feed --event-id 12345678901234567 --limit 30 --json
fbk events feed --event-id 12345678901234567 --cursor <end_cursor>
```

Output notes:

* Human mode prints `event feed: N stories (next: true|false)`, then one line per
  story: `[creation_time] actor: text`.
* `--json` emits `{event_id, stories, count, end_cursor, has_next_page}`.
* An empty discussion feed is valid state (exit 0).

Live status: read-only; the query schema is decoded from its owning bundle and the
template verified against it, but the surface has **not** been live-replayed
end-to-end — the probe account had zero events, so the id substitution is
schema-derived (an honest omission, surfaces/events.py calibration notes).

---

## fbk events create

Create an event (**mutation**) via `EventCometLightweightCreateMutation` (doc_id
24003471242569934). The mutation was discovered by delta-harvesting the
`/events/create/` bundle set (2026-09-18); the input is the schema-decoded
committer's ~45-key input object with the CLI-substitutable fields applied —
neutral decoded defaults fill everything else. The decoded schema carries no
`client_mutation_id`, so none is invented.

Syntax:

```
fbk events create [flags] --name NAME --start-date DATE --start-time TIME
```

| flag | meaning |
|---|---|
| `--name NAME` | event name (**required**) |
| `--start-date DATE` | start date, `YYYY-MM-DD` (**required**) |
| `--start-time TIME` | start time, `HH:MM` 24h, in the **event timezone** (**required**) |
| `--end-date DATE` | end date, `YYYY-MM-DD` |
| `--end-time TIME` | end time, `HH:MM` |
| `--timezone TZ` | IANA timezone name anchoring the local times (default **UTC**) |
| `--description TEXT` | event description |
| `--privacy {public,private}` | event privacy (default **public**); maps to the decoded committer's `PUBLIC_TYPE` / `PRIVATE_TYPE` enum literals |

There is no location flag: the decoded input template carries `location_id` /
`location_name` / `location_latlong` fields, but the CLI exposes none of them —
they ship as the decoded neutral defaults. Never invent wire the source does not
carry.

Examples:

```
fbk events create --name "Community meetup" --start-date 2026-10-03 --start-time 14:00
fbk events create --name "Team sync" --start-date 2026-10-05 --start-time 09:30 \
    --timezone Europe/Berlin --end-time 11:00 --privacy private --description "Quarterly sync"
```

Output notes:

* Human mode prints `created: <name> (<event id>)`.
* `--json` emits `{mutation, name, event_id, response}` — the full merged
  mutation response rides `response`.
* **Exit 1 when the response carries no created event id** — an unconfirmed
  mutation is a failed precondition
  (10-rate-limiting-and-behavioral-detection.md §3.4), not a silent success.
* Consumes the 40/day mutation budget
  ([09-safety-and-opsec.md](09-safety-and-opsec.md)).

Live status: schema-decoded 2026-09-18, **never live-fired** — execution is the
operator's call (13-recon-methodology.md single-variable probe discipline: never
fire a newly decoded mutation incidentally).

---

## fbk events delete

Delete an event you host (**mutation**) via `useEventCometDeleteMutation` (doc_id
28037817465841827). Variables are the decoded hook shape
(`{input: {acontext, event_id}, scale}`) with the event id substituted; the
acontext is the live-observed action-history blob from `/events/` rail links. Own
events only — the server rejects deletes of non-hosted events. The schema carries
no `client_mutation_id`; none is invented.

Syntax:

```
fbk events delete [flags] --event-id EVENT_ID
```

| flag | meaning |
|---|---|
| `--event-id ID` | event id to delete (**required**); numeric or Relay form |

Examples:

```
fbk events delete --event-id 12345678901234567
fbk events delete --event-id 12345678901234567 --json
```

Output notes:

* Human mode prints `deleted: <canceled event id>`.
* `--json` emits `{mutation, event_id, canceled_event_id, response}`.
* **Exit 0 only when the response carries `canceled_event_id`** — an unconfirmed
  delete is a failed precondition that must be verified out-of-band before being
  believed (10-rate-limiting-and-behavioral-detection.md §3.4).
* Consumes the 40/day mutation budget
  ([09-safety-and-opsec.md](09-safety-and-opsec.md)).

Live status: schema-decoded 2026-09-18, **never live-fired** — same operator-call
discipline as create (surfaces/events.py calibration notes).

---

## fbk upload photo

Upload one image through the composer photo-ingest plane, optionally publishing it
as a photo post (**mutation** only when publishing).

The two phases mirror the real composer's split
(15-live-calibration-findings.md §P5-1):

* **Phase 1 — ingest.** A single multipart POST to the react_composer ingest
  endpoint (`upload.facebook.com/ajax/react_composer/attachments/photo/upload`),
  replaying the `XComposerPhotoUploader` wire shape: the uploadData fields
  (`source=8`, `profile_id`, `waterfallxapp=comet`), a PhotosUploadID-shaped
  `upload_id`, and the file bytes under the `farr` part. The FB async envelope
  answers with `photoID` (the photo fbid), `imageSrc`, `width`, `height`.
* **Phase 2 — publish.** Only with `--caption`: the live-verified
  `ComposerStoryCreateMutation` capture (the same mutation `fbk feed publish`
  replays) with `input.attachments = [{"photo": {"id": <photoID>}}]` — the exact
  element the composer emits for an unedited photo. "Posting" means exactly this:
  a feed composer publish carrying the uploaded photo as an attachment.

The phases are decoupled on purpose: without `--caption` the command is **ingest
only** — it prints the photo id and touches no mutation budget (the raw ingest is
deliberately not governor-gated; byte transfer is not a user-visible mutation, and
the concluding GraphQL publish carries the whole flow's mutation budget,
15-live-calibration-findings.md §P9-1). Both phases remain independently
observable in the request journal.

File requirements (per src/surfaces/upload.py): the file must exist, and its
MIME — guessed from the extension the way the browser's File object would — must
be an `image/*` type (the uploader's own file-input accept filter: JPEG, PNG, GIF
included; GIFs map to `image/gif` and ride the multipart headers verbatim).
Non-image files are rejected with a typed `UploadError` before any bytes are sent.
There is no client-side size limit on this plane; nothing larger than the file you
point at is read.

Syntax:

```
fbk upload photo [flags] --path FILE
```

| flag | meaning |
|---|---|
| `--path FILE` | image file path (**required**) |
| `--caption TEXT` | when given, publish a photo post with this text (rides `input.message.text`) |
| `--privacy {public,friends,private}` | post audience when `--caption` is given (default **friends**); the CLI spelling maps to the live-captured `base_state` enum (PUBLIC→EVERYONE, PRIVATE→SELF) |
| `--group-id ID` | group id override — retargets the publish to a group |

Examples:

```
fbk upload photo --path shot.jpg                      # ingest only: prints the photo id
fbk upload photo --path shot.jpg --caption "Field notes" 
fbk upload photo --path shot.jpg --caption "Field notes" --privacy public --json
```

Output notes:

* Ingest only: human mode prints `photo id: <fbid>`; `--json` emits the full upload
  record (`photo_id`, `upload_id`, `image_url`, `width`, `height`, raw `payload`).
* Ingest + publish: human mode prints `photo id`, `post id`, `privacy`; `--json`
  emits `{photo_id, post_id, upload}` — the post id is the composer story id.
* A missing post id in the publish echo degrades to `-` (human) / `null` (JSON)
  rather than failing — the composer's data shape varies with the deploy. Exit 0
  either way; the phases are observable in the journal.
* `--dry-run` refuses on the raw ingest leg (dry-run covers GraphQL calls only —
  the planned composer publish never runs without the real ingest first).
* Publish consumes the 40/day mutation budget
  ([09-safety-and-opsec.md](09-safety-and-opsec.md)).

Live status: photo ingest **live-verified end-to-end 2026-09** (browser capture +
curl-impersonated replay both returned a real photoID); the concluding publish
rides the live-verified `ComposerStoryCreateMutation` capture
(15-live-calibration-findings.md §P3, §P5-1).

---

## fbk upload video

Upload one video through the decoded three-stage rupload plane, optionally
publishing it as a video post (**mutation** only when publishing).

The wire (15-live-calibration-findings.md §P6-1; surfaces/video_upload.py), decoded
from live bundle archaeology plus two live-probed read-only config queries:

1. **START** — a form-encoded POST to the chunk-start endpoint (live
   `vupload-edge.facebook.com/ajax/video/upload/requests/start/`) carrying the
   waterfall id, file size/extension, and partition offsets; the async envelope
   answers `video_id`, `start_offset`, `end_offset`, `upload_session_id`,
   `region_hint`, and possibly `skip_upload`.
2. **RUPLOAD chunk plane** — a resumable transfer on
   `rupload-ccu2-1.up.facebook.com` (live targeted service): a GET resume probe
   returns `{offset: N}` (how many bytes the server already holds; a probe failure
   defaults to offset 0), then one POST of the **remaining** bytes with the
   `X-Entity-*` header family; the response carries the everstore chunk handle
   `h`. When START answers `skip_upload=true` (server-side de-dupe: an identical
   asset is already ingested), stages 2–3 are short-circuited entirely.
3. **RECEIVE** — the receive POST re-attaches the chunk handle, completing the
   ingest; the video fbid is the stage-1 `video_id`.
4. **Publish (only with `--caption`)** — `ComposerStoryCreateMutation` with the
   decoded minimal VIDEO attachments element (`{id, audio_descriptions,
   transcriptions, notify_when_processed, ...}`).

Wait/poll semantics: there are none to operate — no polling or wait flags exist.
The ingest is one synchronous start → chunk → receive pass; resumability lives in
the stage-2 offset probe (a re-run of the same file resumes from the probed
offset), not in an operator-visible session you can poll. The measured upload speed
is reported to RECEIVE automatically. Upload endpoints are resolved live from the
two config queries, with bundle-decoded defaults behind them.

As with photos, the upload→post flow is decoupled: without `--caption` the command
is **ingest only** (prints the video id, no mutation budget); the raw rupload legs
are not governor-gated by design — the concluding GraphQL publish carries the
whole flow's mutation budget. The publish always rides the captured FEED composer
context (`feedLocation NEWSFEED`) — a reels variant is deliberately **not** exposed:
no reels-composer capture exists to replay, and inventing one would violate the
no-invention rule (15-live-calibration-findings.md §P1).

File requirements (per src/surfaces/video_upload.py): the file must exist and be
non-empty; the chunk MIME is guessed from the extension (extensionless files ride
the start request with the default `mp4` extension). No client-side size limit is
enforced on this plane.

Syntax:

```
fbk upload video [flags] --path FILE
```

| flag | meaning |
|---|---|
| `--path FILE` | video file path (**required**) |
| `--caption TEXT` | when given, publish a video post with this text (rides `input.message.text`) |
| `--privacy {public,friends,private}` | post audience when `--caption` is given (default **friends**; same `base_state` mapping as photo) |
| `--group-id ID` | group id override — retargets the publish to a group |

Examples:

```
fbk upload video --path clip.mp4                       # ingest only: prints the video id
fbk upload video --path clip.mp4 --caption "Demo run"
fbk upload video --path clip.mp4 --caption "Demo run" --privacy friends --json
```

Output notes:

* Ingest only: human mode prints `video id: <fbid>`; `--json` emits the stage
  record (`video_id`, `session_id` (the waterfall id), `chunk_handle` (None on the
  `skip_upload` path), raw `start` / `receive` payloads).
* Ingest + publish: human mode prints `video id`, `post id`, `privacy`; `--json`
  emits `{video_id, post_id, upload}`. A missing post id degrades to `-` / `null`,
  exit 0.
* Any stage failure (non-200, error envelope, missing `video_id` / `h` handle)
  raises a typed `VideoUploadError`.
* `--dry-run` refuses on the raw ingest legs (GraphQL-only coverage).
* Publish consumes the 40/day mutation budget
  ([09-safety-and-opsec.md](09-safety-and-opsec.md)).

Live status: the two config queries are live-probed (read-only); the three-stage
ingest wire is decoded from the deployed JS but has **not been live-replayed** —
posting a video is heavier state than an only-me photo post, so end-to-end live
verification is left to the operator (surfaces/video_upload.py calibration notes).

---

## fbk draft save

Save a composed post spec as a draft in the **CLI-local** draft store under
`state/drafts/` (one JSON file per draft, atomic writes). Nothing is sent to the
edge — no session, no network anywhere in the store.

A draft is a `DraftSpec` — the exact publish inputs, mirroring `fbk feed publish`
plus the composer extras, persisted for later (src/drafts.py):

| spec field | from flag | sent at publish time? |
|---|---|---|
| `name` | `--name` | — (the file basename) |
| `text` | `--text` | yes — `input.message.text` |
| `privacy` | `--privacy` | yes — `input.audience.privacy.base_state` |
| `ai_label` | `--ai-label {on,off}` | yes — the AI-disclosure toggle (`ai_generated`) |
| `background` | `--background ID` | yes — the text-format preset id (`text_format_preset_id`; small-text posts) |
| `media` | `--media PATH[,PATH]...` | **stored, not yet publishable** |
| `tags` | `--tag UID:NAME` (repeatable) | **stored, not yet publishable** |
| `feeling` / `activity` / `place` | `--feeling ID` / `--activity ID` / `--place ID` | **stored, not yet publishable** |
| `created_at` | (automatic, UTC) | — |

The stored-not-publishable fields round-trip faithfully in the spec until a
captured mutation can carry them — the recon gap: the harvested mutation registry
has no ComposerDraft mutations, so Facebook's own server-side draft surface is out
of reach and this store is the honest, fully-offline substitute (src/drafts.py).

Syntax:

```
fbk draft save [flags] --name NAME --text TEXT
```

| flag | meaning |
|---|---|
| `--name NAME` | draft name (**required**; bare basename — `state/drafts/<name>.json`) |
| `--text TEXT` | post text (**required**) |
| `--privacy {public,friends,private}` | audience when published (default **friends**) |
| `--ai-label {on,off}` | AI-disclosure toggle for this post |
| `--background ID` | background-color preset id (small-text posts) |
| `--media PATH[,PATH]...` | comma-separated media paths (stored, not yet publishable) |
| `--tag UID:NAME` | a tagged person (repeatable; stored, not yet publishable) |
| `--feeling ID` | feeling/activity emotion id (stored) |
| `--activity ID` | activity id (stored) |
| `--place ID` | place id (stored) |
| `--force` | allow saving over an existing draft of this name |

Examples:

```
fbk draft save --name launch-note --text "Field notes from today's run"
fbk draft save --name q4-update --text "Long-form update ..." --privacy public \
    --ai-label on --tag 12345678901234567:Test\ Person --media a.jpg,b.png
```

Output notes:

* Human mode prints `saved draft 'name' → <path>`.
* `--json` emits `{draft, path, text_head}`.
* **Exit 1** when the name is path-shaped (path separators or dot-segments are
  rejected at the boundary — no invocation can read or write outside
  `state/drafts/`) or when a draft of that name exists and `--force` was not
  passed. An existing draft is never silently overwritten.

Live status: pure offline state — no live status applies.

---

## fbk draft list

Enumerate every draft under the resolved drafts directory (offline).

Syntax:

```
fbk draft list [flags]
```

No family-specific flags.

Example:

```
fbk draft list
fbk draft list --json
```

Output notes:

* Human mode prints `N draft(s) under <state>/drafts`, then one row per draft in
  name order: `name created_at [media_count] text_head`. Zero drafts prints
  `no drafts` — a reported fact, never an error (exit 0).
* `--json` emits `{state_dir, count, drafts}` — each entry carries `name`,
  `created_at`, `text_head`, `media_count`.
* A corrupt draft file stays **loud**: it raises a validation error on load, never
  a silently-absent row (atomic writes mean a corrupt file is disk rot or
  tampering, never a torn write).

Live status: pure offline state — no live status applies.

---

## fbk draft show

Print one draft's full spec (offline).

Syntax:

```
fbk draft show [flags] --name NAME
```

| flag | meaning |
|---|---|
| `--name NAME` | draft name (**required**; bare name) |

Example:

```
fbk draft show --name launch-note
```

Output notes:

* Human mode prints the draft name and creation time, then the full spec: `text`,
  `privacy`, `ai_label`, `background`, each media path, the tag list, and any
  feeling / activity / place.
* `--json` emits `{draft, spec}` — the raw spec dict.
* **Exit 1** when the name is path-shaped or no such draft exists (failed
  precondition; see `fbk draft list`).

Live status: pure offline state — no live status applies.

---

## fbk draft publish

Publish one draft to the feed (**mutation**): the exact call `fbk feed publish`
makes — `FeedService.publish` with the spec's stored `text` and `privacy`, plus the
two composer extras the service already accepts (`ai_label` → `ai_generated`,
`background` → `text_format_preset_id`). Stored-not-publishable fields (media,
tags, feeling, activity, place) are **not** sent; the publish report carries only
what actually went out.

Syntax:

```
fbk draft publish [flags] --name NAME
```

| flag | meaning |
|---|---|
| `--name NAME` | draft name (**required**; bare name) |
| `--delete` | delete the draft after a **successful** publish (default keeps it) |

Examples:

```
fbk draft publish --name launch-note
fbk draft publish --name q4-update --delete --json
```

Output notes:

* Human mode prints `published draft 'name'` (plus ` (draft deleted)` when
  `--delete` removed it).
* `--json` emits `{draft, published}` (+ `draft_deleted` when requested) — the
  full merged mutation response rides `published`.
* **Exit 1** when the name is path-shaped or no such draft exists. A failed
  publish propagates its typed error and the draft is kept — `--delete` only
  fires after success.
* Consumes the 40/day mutation budget
  ([09-safety-and-opsec.md](09-safety-and-opsec.md)); sequential by design like
  every mutation — the governor paces inside the transport.

Live status: rides the **live-verified** `ComposerStoryCreateMutation` path — the
same captured mutation `fbk feed publish` replays
(15-live-calibration-findings.md §P3).

---

## fbk draft delete

Remove one draft from the store (offline).

Syntax:

```
fbk draft delete [flags] --name NAME
```

| flag | meaning |
|---|---|
| `--name NAME` | draft name (**required**; bare name) |

Example:

```
fbk draft delete --name launch-note
```

Output notes:

* Human mode prints `deleted draft 'name'`; `--json` emits `{draft, deleted: true}`.
* **Exit 1** when the name is path-shaped or no such draft exists (failed
  precondition).

Live status: pure offline state — no live status applies.

---

### Why drafts exist

The draft store decouples **composing** from **publishing**. The mutation budget is
small (40/day) and quiet-hours/soft-block discipline makes the *when* of publishing
deliberate (09-safety-and-opsec.md); drafting lets you compose, review, and revise
posts offline at any time — `save`/`list`/`show`/`delete` never touch a session —
and spend a mutation only when `publish` runs. Drafts complement the batch side of
the composer family (see `fbk feed publish-batch` in
[04-reference-feed.md](04-reference-feed.md)): compose and stage content as drafts
now, then publish — one at a time via `draft publish`, or in a paced batch via
`publish-batch` — when the budget and the window allow. The stored-not-publishable
fields (media, tags, feeling, activity, place) exist precisely so a future
captured mutation can publish them without changing the store's schema.

---

## Files that feed this guide

CLI sources (all under `cli/src/`):

* `commands/search.py`, `surfaces/search.py` — search family
* `commands/marketplace.py`, `surfaces/marketplace.py` — marketplace family
* `commands/video.py`, `surfaces/video.py` — video family
* `commands/photos.py`, `surfaces/photos.py` — photos family
* `commands/events.py`, `surfaces/events.py` — events family
* `commands/upload.py`, `surfaces/upload.py` — photo upload + composer publish
* `commands/video_upload.py`, `surfaces/video_upload.py` — video upload (rupload)
* `commands/drafts.py`, `drafts.py` — the CLI-local draft store

Research docs (repo-root `docs/`, cited by filename above):

* `02-endpoint-surface-map.md` — surface families (§2.4 search, §2.6 media, §2.8
  marketplace, events)
* `04-graphql-protocol-deep-dive.md` — pagination, UFI, request shape
* `10-rate-limiting-and-behavioral-detection.md` — enforcement signatures, §3.4
  unconfirmed-mutation discipline
* `13-recon-methodology.md` — single-variable probe discipline
* `14-glossary-and-reference.md` — identifier tables (Relay node ids)
* `15-live-calibration-findings.md` — the live wire ground truth for every surface
  in this guide (§P2-2 preload replay, §P3 composer, §P3-5 search, §P5-1 photo
  upload, §P6-1 video upload, §P9 album/batch pacing, §P1 no-invention rule)

fbk guides: [01-getting-started.md](01-getting-started.md),
[03-session-and-auth.md](03-session-and-auth.md) (global flags),
[04-reference-feed.md](04-reference-feed.md) (feed/composer/saved),
[09-safety-and-opsec.md](09-safety-and-opsec.md) (governor, budgets, disengagement).
