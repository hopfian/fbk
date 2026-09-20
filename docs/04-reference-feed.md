# 04 — Feed & activity reference

This is the complete command reference for the nine feed-and-activity command
families: **feed, profile, comments, stories, notifications, memories, saved,
presence, overview** — every subcommand, every flag, every output shape. Reads
replay the same persisted queries the browser surfaces register (SSR preloader
replay); writes replay live-verified, browser-captured mutation templates from
`data/captured_*.json` with only the target ids and payload text substituted.
Wire-format grounding lives in the repo-root research suite — cited here by
plain filename: docs/02-endpoint-surface-map.md (endpoint families),
docs/04-graphql-protocol-deep-dive.md (pagination, UFI, identifiers),
docs/10-rate-limiting-and-behavioral-detection.md (budgets, soft-success),
docs/15-live-calibration-findings.md (the live-calibration ground truth for
every shape below).

Every (sub)command also accepts the **global flag set** — `--root`,
`--cookies`, `--no-journal`, `--json`, `--retry N`, `--dry-run` — documented
once in [03-session-and-auth.md](03-session-and-auth.md) §8 and not repeated in
the tables below. Two things worth restating here because this guide is
mutation-heavy:

* `--dry-run` is the safe way to inspect any mutating call: it builds and
  prints the complete GraphQL request plan (variables redacted) and sends
  nothing; the first planned call ends the run with exit 0.
* Default output is a human summary followed by one final compact-JSON line of
  the same payload; `--json` prints the indented payload alone. Scripts can
  always pipe the last line.

**Mutation budget.** Every write in these families draws on the governor's
separate, much lower daily mutation budget — **40 mutations per calendar day**
(`mutation_daily_cap`, src/governor.py) — not on the read budget. Reaching the
cap raises `GovernorBlockedError` and the call is refused before anything is
sent. The governor policy, pacing shape, and containment rules are covered in
[09-safety-and-opsec.md](09-safety-and-opsec.md).

**Id forms (used throughout this guide, all synthetic).** User id
`12345678901234`, post id `12345678901234567`, story card id `12345678901234`,
comment id pair `12345678901234567_123456789012345`.

## Contents

| Command | Purpose |
|---|---|
| `fbk feed read` | Fetch one page (or N chained pages) of the home feed |
| `fbk feed paginate` | Fetch the next feed page by explicit cursor |
| `fbk feed react` | Apply one reaction to a post |
| `fbk feed unreact` | Remove your reaction from a post |
| `fbk feed comment` | Create a comment on a post |
| `fbk feed delete-comment` | Delete a comment |
| `fbk feed publish` | Publish a text (or media) post to the feed |
| `fbk feed life-categories` | List the composer's life-event categories |
| `fbk feed set-privacy` | Change an existing post's audience |
| `fbk feed edit` | Edit an existing post (full composer parity) |
| `fbk feed publish-batch` | Publish several posts sequentially in one run |
| `fbk feed share` | Share a post to your own timeline |
| `fbk feed save` | Save a post (delegates to the saved surface) |
| `fbk feed notify` | Turn post notifications from an actor on/off |
| `fbk profile me` | Show the logged-in user's own profile |
| `fbk profile view` | Read any profile's timeline feed |
| `fbk comments read` | Read a post's comments from its permalink |
| `fbk comments react` | React to a comment (the comment's own feedback id) |
| `fbk comments unreact` | Remove your reaction on a comment |
| `fbk comments edit` | Edit a comment's text |
| `fbk comments delete` | Delete a comment |
| `fbk stories tray` | The stories tray tiles |
| `fbk stories create` | Publish a text / photo / video story |
| `fbk stories viewers` | A story's seen-by viewer list |
| `fbk stories reply` | Reply to a story with text |
| `fbk stories presets` | SATP text-story background style preset catalog |
| `fbk stories fonts` | SATP text-story custom font catalog |
| `fbk stories audience` | Story audience: default mode + mode catalog |
| `fbk notifications badge` | Unseen notifications count |
| `fbk notifications list` | Recent notifications (dropdown / paginated) |
| `fbk notifications mark-seen` | Mark listed notifications seen |
| `fbk memories feed` | The memories ("On This Day") throwback feed |
| `fbk saved list` | List saved items (dashboard) |
| `fbk saved save` | Save an item by savable node id |
| `fbk saved unsave` | Unsave an item by savable node id |
| `fbk presence status` | Read the viewer's chat-visibility setting |
| `fbk overview` | Aggregate dashboard: identity, badges, presence, feed head, threads |

---

## Feed family (`fbk feed …`)

The home-feed stream plus the post-centric action verbs. Reads replay the
homepage's own SSR-registered `CometModernHomeFeedQuery` with the server's
verbatim variables (docs/15 §P2-2) — the most faithful, bypass-resistant read
available. The UFI and composer writes replay live-verified captures
(`data/captured_mutations.json`, `data/captured_comment_mutations.json`,
`data/captured_composer.json`), substituting only the target ids and payload
text; the encrypted `tracking` blobs ride untouched as part of the accepted
wire shape.

**Everything in this family except `read`, `paginate`, `life-categories`, and
`save`'s id plumbing is a mutation** and consumes the 40/day mutation budget
([09-safety-and-opsec.md](09-safety-and-opsec.md)). Soft-success caveat
(docs/10 §3.4): a success echo is not proof the mutation landed server-side —
mutations on this surface are the most soft-filtered class (docs/10 §2);
verify out-of-band before counting a write.

### Post id vs feedback id — read this first

The UFI plane reacts and comments on a **feedback context**, not on the post
directly. A post's feedback handle is `b64("feedback:<post_id>")` — the
`ZmVlZGJhY2s6…` wire token (the b64 of the literal string `feedback:`). Every
react/unreact/comment verb accepts the target in either spelling, exactly one:

* `--post-id ID` — the post's **numeric id** (the 3-dot UX's post-centric
  form). fbk derives the feedback handle through the domain codec
  (`surfaces.feed.feedback_for_post` → `FeedbackID.from_numeric`).
* the **positional feedback id** — any id spelling the service normalizes:
  b64 `ZmVlZGJhY2s6…`, decoded `feedback:<numeric>`, or a bare numeric fbid.

Passing both forms, or neither, is a usage error. Comment ids are a different
family (`comment:<post>_<cid>`, see `fbk feed delete-comment`), and a
**comment's own reaction target** is yet another handle —
`feedback:<post>_<cid>` — used by the `comments` family, never by `feed`.

---

## fbk feed read

Fetch page 1 of the home feed, optionally walking N pages by automatic cursor
chaining. Multi-page walks pace themselves between pages with the requested
gap jittered ±50% — never a fixed interval (metronomic cursor fetches are a
crawler signature; docs/10 §4, docs/11 §3).

```
fbk feed read [--pages N] [--page-gap SECONDS]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--pages N` | int | 1 | Walk N pages automatically (read + paginate chaining). The walk stops early when the stream runs dry (no cursor or `has_next_page` false); stories aggregate across pages |
| `--page-gap SECONDS` | float | 2.0 | Seconds between pages in a multi-page walk, jittered ±50% |

**Examples**

```
fbk feed read
fbk feed read --pages 3 --page-gap 4.0
fbk feed read --pages 2 --json
```

**Output.** Human mode prints one line per story — `actor | text head | feedback id | permalink` — then a summary line
`-- <N> stories | has_next_page=<bool> | end_cursor=<head>`. `--json` returns
`{"stories": [...], "end_cursor": …, "has_next_page": …, "pages": <int>}` where
each story carries `{id, key, actor, text, feedback_id, permalink, creation_time}`
— `key` is the opaque `Uzpf…` Relay story key, `feedback_id` the react/comment
target handle. Exit 0 even for an empty or thin feed: on a throttled session a
degraded payload is itself a diagnostic (docs/10 §3.5), not a failure.

**Live status:** live-verified read (homepage preloader replay, docs/15 §P2-2).
Feed reads are the surface's ground-truth calibration leg.

---

## fbk feed paginate

Fetch the next feed page by explicit cursor — the manual form of what
`read --pages` does automatically. The cursor is opaque server state; never
edit it.

```
fbk feed paginate --cursor CURSOR [--limit N]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--cursor CURSOR` | string | required | `end_cursor` from a previous read/paginate |
| `--limit N` | int | none | Emit at most N stories from the page; slices locally — the payload's own chaining cursor is unaffected |

**Examples**

```
fbk feed paginate --cursor "AQHR6..." --limit 5
```

**Output.** Same human story lines as `read`. `--json` returns
`{"page": {"stories": […], "end_cursor": …, "has_next_page": …}, "next_cursor": …}`.
Exit 0 even on an exhausted cursor (empty page).

**Live status:** live-verified read (`CometNewsFeedPaginationQuery`, docs/04 §5
cursor convention).

---

## fbk feed react

Apply one reaction to a post via its feedback id. One mutation covers every
reaction kind and the remove (docs/15 §P2-3): the kind rides
`input.feedback_reaction_id`, and the viewer's own id is always substituted for
the captured (scrubbed) actor.

```
fbk feed react [--post-id ID | feedback_id] --reaction KIND [--referrer REFERRER]
```

| Argument/flag | Type | Default | Meaning |
|---|---|---|---|
| `feedback_id` (positional) | string | — | Feedback id: b64 `ZmVlZGJhY2s6…`, decoded `feedback:<numeric>`, or bare numeric — alternative to `--post-id` |
| `--post-id ID` | string | — | The target post's numeric id; the feedback id is derived via the b64 codec. Exactly one of this or the positional form |
| `--reaction KIND` | enum | required | One of the reaction kinds below |
| `--referrer REFERRER` | string | captured value | `feedback_referrer` override — use when replaying a template captured in a different surface context |

Reaction kinds (src/constants.py `UFI_REACTION_IDS`, harvested live from the
CometUFIFunnelLogger switch, docs/15 §P3-2):

| Kind | Wire id | Note |
|---|---|---|
| `LIKE` | `1635855486666999` | |
| `LOVE` | `1678524932434102` | |
| `WOW` | `478547315650144` | |
| `HAHA` | `115940658764963` | |
| `SORRY` | `908563459236466` | displayed as "Care" in the 2024+ UI |
| `ANGER` | `444813342392137` | |
| `SUPPORT` | `613557422527858` | |

(`REMOVE` = `"0"` exists in the enum but is not a CLI choice — removing is what
`fbk feed unreact` does.)

**Examples**

```
fbk feed react --post-id 12345678901234567 --reaction LIKE
fbk feed react ZmVlZGJhY2s6... --reaction WOW
fbk feed react --post-id 12345678901234567 --reaction SUPPORT --referrer /story.php
```

**Output.** The merged mutation response is the payload; human mode prints it
as the compact JSON line, `--json` indented. Read the updated `reaction_count`
from the echo. Soft-success caveat applies (docs/10 §3.4).

**Live status:** live-verified on own and others' posts (docs/15 §P2-3 — the
like→remove cycle moved the live reaction count up and back).

---

## fbk feed unreact

Remove the viewer's reaction from a post. There is no separate "unlike"
mutation: remove is the same `CometUFIFeedbackReactMutation` with
`feedback_reaction_id "0"` (docs/15 §P2-3).

```
fbk feed unreact [--post-id ID | feedback_id]
```

| Argument/flag | Type | Default | Meaning |
|---|---|---|---|
| `feedback_id` (positional) | string | — | Feedback id (b64 / decoded / numeric) — alternative to `--post-id` |
| `--post-id ID` | string | — | The target post's numeric id; feedback id derived via the b64 codec |

**Examples**

```
fbk feed unreact --post-id 12345678901234567
```

**Output.** The merged mutation response (updated feedback in the echo); same
emission shape as `react`.

**Live status:** live-verified (the remove half of the docs/15 §P2-3
like→remove calibration pair).

---

## fbk feed comment

Create a comment on a post via its feedback id
(`useCometUFICreateCommentMutation`). The comment body rides
`input.message.text` with an empty ranges list and a fresh
`client_mutation_id`.

```
fbk feed comment [--post-id ID | feedback_id] --text TEXT [--group-id GROUP_ID]
```

| Argument/flag | Type | Default | Meaning |
|---|---|---|---|
| `feedback_id` (positional) | string | — | Feedback id (b64 / decoded / numeric) — alternative to `--post-id` |
| `--post-id ID` | string | — | The target post's numeric id |
| `--text TEXT` | string | required | Comment text |
| `--group-id GROUP_ID` | string | — | Group id override for group-post comment contexts. The captured capture is a GROUP-context comment, so `groupID` is always substituted (your value, or `None`): a group id that does not match the feedback's owning context is a wire error |

**Examples**

```
fbk feed comment --post-id 12345678901234567 --text "noted"
fbk feed comment ZmVlZGJhY2s6... --text "nice find" --group-id 12345678901234
```

**Output.** The merged mutation response; the echo carries the refreshed
comment connection. Comments are the most soft-filtered mutation class
(docs/10 §2) — verify out-of-band before counting the comment.

**Live status:** PARKED. Originally live-verified in-browser (docs/15 §P3-3),
but the registration rotated with deploy revision 1047963790 — the old
doc_id registration returned `field_exception 1357010` live, and the doc-id
was re-pinned from the fresh harvest (src/constants.py). A deeper schema-drift
finding persists on the same mutation: the captured group-context `groupID`
replayed against a personal-post feedback is a `field_exception 1357010`
(live finding 2026-09-20 — groupID must match the feedback's owning context or
be `None`). Needs fresh calibration before counting on it.

---

## fbk feed delete-comment

Delete a comment (`CometUFIDeleteCommentMutation`). The comment id accepts the
b64 global id, the decoded `comment:<post>_<cid>` form, or the bare
`<post_fbid>_<comment_fbid>` pair — the story_fbid + id legacy key family.

```
fbk feed delete-comment comment_id [--group-id GROUP_ID]
```

| Argument/flag | Type | Default | Meaning |
|---|---|---|---|
| `comment_id` (positional) | string | required | Comment id: b64, decoded `comment:<post>_<cid>`, or `<post>_<cid>` |
| `--group-id GROUP_ID` | string | — | Group id override for the owning group's render context |

**Examples**

```
fbk feed delete-comment 12345678901234567_123456789012345
```

**Output.** The merged mutation response; the echo carries
`data.comment_delete.deleted_comment_id`. The variable set is strict —
extra undeclared variables provoke `1675012 noncoercible_variable_value`
(docs/15 §P3-3).

**Live status:** PARKED pending re-verification on the current deploy. The
mutation shape itself is live-proven (the captured comment was actually
deleted, docs/15 §P3-3), but the comment-mutation family's registration
rotated with the 1047963790 deploy (the stale create-sibling id returned
`field_exception 1357010` live) — re-verify before counting a delete.

---

## fbk feed publish

Publish a post to the feed via `ComposerStoryCreateMutation`, replaying the
browser-captured composer template from `data/captured_composer.json`
(docs/15 §P3 — live-verified: the calibration test post was created and
deleted). With `--media`, the attach dispatches to the upload surfaces' own
composer-template publishes (photo post / album / video post,
docs/02 §2.6) — `FeedService.publish` cannot carry attachments.

```
fbk feed publish --text TEXT --privacy {public|friends|private}
                 [--tag USER_ID:NAME] [--feeling ID] [--activity ID] [--place ID]
                 [--ai-label {on|off}] [--background PRESET_ID]
                 [--media PATH[,PATH...]]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--text TEXT` | string | required | Post text (with `--media`: the caption) |
| `--privacy {public,friends,private}` | enum | required | Audience. Maps to the live-captured `base_state` string enum: `public`→`EVERYONE`, `friends`→`FRIENDS`, `private`→`SELF` (src/constants.py `PRIVACY_BASE_STATES`); the wire never sees the CLI spelling |
| `--tag USER_ID:NAME` | pair, repeatable | — | Tag a friend: appends an `@NAME` mention marker to the text and a mention range to `input.message.ranges`. Splits on the first colon (names may contain colons); both halves required. **Candidate shape — unverified against live** |
| `--feeling ID` | string | — | Feeling target id; rides `input.target_type="FEELING"` + `input.target_id`. **Candidate shape — unverified against live** |
| `--activity ID` | string | — | Activity target id; rides `input.target_type="ACTIVITY"` + `input.target_id`. Mutually exclusive with `--feeling`. **Candidate shape — unverified against live** |
| `--place ID` | string | — | Check-in place id; rides `input.place_id`. **Candidate shape — unverified against live** |
| `--ai-label {on,off}` | enum | — | Self-disclose the post as AI-generated; maps to the captured `input.ai_generated_self_disclosure_metadata.was_self_disclosed_as_ai_generated` bool |
| `--background PRESET_ID` | string | — | Background-color preset id (small-text posts); rides `input.text_format_preset_id`, `"0"` clears the background. The valid-id census is a parked recon item; the stories composer root now exposes the SATP background preset catalog live (`fbk stories presets`) |
| `--media PATH[,PATH...]` | path list, repeatable | — | Attach media instead of a plain text post: one image (photo post), a comma-separated or repeated image list (album), or a single `.mp4`/`.mov` (video post). Mixed image+video lists, multiple videos, and media+enrichment combinations are rejected up front (nothing sent, exit 1) |

Fresh `idempotence_token` (`<uuid>_FEED`) and `composer_session_id` are minted
per call; the actor id is always the live viewer. Wrong candidate shapes fail
safe through the composer's `1675012` coercion gate — a typed error, no post
created.

**Examples**

```
fbk feed publish --text "calibration note" --privacy friends
fbk feed publish --text "public service note" --privacy public --ai-label on
fbk feed publish --text "album caption" --privacy friends --media a.png,b.png --media c.png
fbk feed publish --text "clip" --privacy friends --media clip.mp4
```

**Output.** Plain-text path: the merged mutation echo (compact JSON line in
human mode, indented under `--json`). Media paths print a human summary —
`photo id: …` (or `photo ids: …` / `video id: …`), `post id: …` (or `-` when
the echo carries no extractable id), `privacy: …` — and return
`--json` payloads keyed by media kind (`media`, `photo_id`/`photo_ids`/`video_id`,
`post_id`, `upload`/`uploads`).

**Live status:** the text-post publish and the media dispatch are live-verified
(docs/15 §P3, §P5-1). Every enrichment flag (`--tag`, `--feeling`,
`--activity`, `--place`) rides a CANDIDATE input shape not yet verified against
live — a bad value fails safe via the coercion gate (typed error, no post).
`--background` valid-id census is a parked recon item.

---

## fbk feed life-categories

List the composer's life-event categories with their event types
(`CometComposerLifeEventCategoryListQuery` — the live-calibrated 2026-09-20
query: doc_id 32116841221248541, variables `{"scale": 1}`, categories at
`data.viewer.life_event_categories.nodes`). The publish-side input for life
events is UNKNOWN and deliberately not invented — this command lists only.

```
fbk feed life-categories
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk feed life-categories
fbk feed life-categories --json
```

**Output.** Human mode: one category line `name (id)` with one bullet per
event type `  - type (id)`. `--json` returns
`{"categories": [{"id", "name", "icon_id", "types": [{"id", "identifier",
"name"}…]}…]}`. An empty category list is a valid observation; exit 0.

**Live status:** live-calibrated query (2026-09-20, docs/15 §P3 composer
ground truth).

---

## fbk feed set-privacy

Change one existing post's audience
(`CometPrivacySelectorSavePrivacyMutation` re-targeted at the story's own
privacy scope — live-verified 2026-09-20).

```
fbk feed set-privacy --post-id ID --privacy {public|friends|private}
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--post-id ID` | string | required | The target post's numeric id — the `story_create.feed_story_edge.node.post_id` the publish echo returns |
| `--privacy {public,friends,private}` | enum | required | The new audience (same `base_state` mapping as publish: EVERYONE/FRIENDS/SELF) |

The per-post privacy write id is the story's own scope
(`b64('privacy_scope_renderer:{"id":<post_id>}')`); the captured save template
is replayed with `base_state`, the write id, the actor id, and a fresh
`client_mutation_id` substituted.

**Examples**

```
fbk feed set-privacy --post-id 12345678901234567 --privacy friends
```

**Output.** The merged mutation echo. Verify the change took effect by
re-reading the post's scope through the picker query (the selected option
flips to the new base_state).

**Live status:** live-verified 2026-09-20 (the picker resolved the candidate
scope id live and the save was accepted).

---

## fbk feed edit

Edit one existing post — full composer parity with publish (`ComposerStoryEditMutation`).
An edit is a **delta**: only the flags you pass change the post; omitted flags
keep the captured template values. With no edit flag at all, the command
defaults to the safe preview (print the current editable state, exit 0).

```
fbk feed edit --post-id ID [--text TEXT] [--privacy {…}] [--ai-label {on|off}]
              [--background PRESET_ID] [--tag USER_ID:NAME] [--feeling ID]
              [--activity ID] [--place ID] [--photo PATH] [--show]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--post-id ID` | string | required | The target post's numeric id (`story_create.feed_story_edge.node.post_id`) |
| `--text TEXT` | string | — | Replacement post text (omit to keep current). `--tag` markers append to THIS text, never to the unknown live text |
| `--privacy {public,friends,private}` | enum | — | New audience (omit to keep current) |
| `--ai-label {on,off}` | enum | — | Self-disclosure toggle |
| `--background PRESET_ID` | string | — | Background-color preset id (`"0"` clears); census is a parked recon item |
| `--tag USER_ID:NAME` | pair, repeatable | — | Mention tags, same range construction as publish. **Candidate shape — unverified; requires `--text`** |
| `--feeling ID` | string | — | Feeling target id. **Candidate — unverified** |
| `--activity ID` | string | — | Activity target id; mutually exclusive with `--feeling`. **Candidate — unverified** |
| `--place ID` | string | — | Check-in place id. **Candidate — unverified** |
| `--photo PATH` | path, repeatable | — | Upload one image via the existing photo ingest and attach it to the post; uploads are paced between like an album (docs/15 §P9-1) |
| `--show` | flag | — | Fetch and print the editable state WITHOUT mutating (the safe preview). Contradictory with any edit flag — rejected up front, exit 1, nothing sent |

**Examples**

```
fbk feed edit --post-id 12345678901234567                # preview current state
fbk feed edit --post-id 12345678901234567 --show        # explicit preview
fbk feed edit --post-id 12345678901234567 --text "revised text" --privacy friends
```

**Output.** Preview: human lines `post:`, `text:`, `privacy:`,
`attachments: <n> (…)`, `feeling:`, `activity:`, `place:` (`-` where the
dialog carried nothing — absence is a valid observation); `--json` returns
`{"post_id": …, "editable": {…}}` (the typed `EditablePost`: story_id, text,
privacy_base_state, attachments, feeling, activity, place_id). The write leg
emits the merged edit-mutation echo.

**Live status:** PARKED. No captured edit variables exist — every variable
shape is a CANDIDATE pending the live probe (the dialog query's base variables,
the per-post key, and the story-id path are probe-correctable constants in
src/surfaces/feed.py), and the server rejected the probe's candidate inputs
with `1675012 noncoercible_variable_value` (the registration accepts the
mutation; one live probe showed the story identifier must be the b64 story
token form `S:_I<actor>:<post>:<post>`, not the numeric post id). Needs a fresh
calibration (one browser capture of the edit dialog + mutation) before use.

---

## fbk feed publish-batch

Publish several posts in ONE invocation, strictly sequentially under the
governor — never concurrent network calls (true parallelism is a metronomic
burst, the exact Phase-8 kill signature, docs/15 §P8-1). Each `--text` is one
`FeedService.publish` call in order, inside one session; the governor inserts
its own lognormal inter-arrival gaps naturally between the mutations.

```
fbk feed publish-batch --text TEXT [--text TEXT …] [--privacy {…}] [--batch-gap SECONDS]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--text TEXT` | string, repeatable | required (min 2) | One post's text; repeat the flag per post — `--text A --text B --text C` |
| `--privacy {public,friends,private}` | enum | `friends` | Audience for every post in the batch |
| `--batch-gap SECONDS` | float | 0.0 | EXTRA jittered (±50%) gap between posts on top of the governor's own pacing; 0 relies on the governor alone |

Fail-soft per post: a typed `FBGraphError` on post k is recorded and the batch
**continues**. `GovernorBlockedError` is fatal — the cap is the cap
(docs/11 §5 containment) — the batch aborts immediately and the un-attempted
remainder is marked skipped.

**Examples**

```
fbk feed publish-batch --text "first post" --text "second post"
fbk feed publish-batch --text "a" --text "b" --text "c" --privacy friends --batch-gap 5
```

**Output.** Human mode prints one status line per post —
`[i/N] status: text head (post_id …)` with status `published` / `failed` /
`governor_blocked` / `skipped` — then a summary line
`summary: X published, Y failed, Z skipped`. `--json` returns
`{"total", "published", "failed", "skipped", "aborted", "governor_error",
"results": [{index, text_head, status, post_id, error}…]}`. Exit 0 only when
every text published and the governor did not abort; 1 otherwise (also 1 when
fewer than 2 `--text` posts were given).

**Live status:** built on the live-verified publish mutation; the batch
plumbing (governor serialization, fail-soft, abort) is behavior-local. Every
post in the batch draws on the shared mutation budget.

---

## fbk feed share

Share a post to your own timeline through the composer
(`ComposerStoryCreateMutation` carrying a share attachment element).

```
fbk feed share --post-id ID [--text TEXT] [--privacy {public|friends|private}]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--post-id ID` | string | required | The shared post's numeric id |
| `--text TEXT` | string | "" (none) | Optional share comment; rides the composer message |
| `--privacy {public,friends,private}` | enum | `friends` | Audience for the shared post |

**Examples**

```
fbk feed share --post-id 12345678901234567
fbk feed share --post-id 12345678901234567 --text "worth reading" --privacy public
```

**Output.** The merged mutation echo of the created share story.

**Live status:** PROBE-PENDING. The share element inside
`input.attachments` is a CANDIDATE shape (`{"share": {"shareable_id": …}}` —
the field name is family-naming inference, not a capture); the dialog probes
(`ShareToFeedComposerCometDialogQuery` / `CometUnifiedShareSheetDialogQuery`)
must confirm it. A wrong candidate fails safe through the composer's `1675012`
coercion gate — typed error, no post created.

---

## fbk feed save

Save a post — the 3-dot convenience verb delegating to the saved surface's
live-verified `CometSaveMutation` (src/surfaces/saved.py).

```
fbk feed save --post-id ID
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--post-id ID` | string | required | The **savable node id**: for a plain post this IS the post id; for photo/video posts use the media fbid — the story's `save_info.savable.id`. Never the story key |

**Examples**

```
fbk feed save --post-id 12345678901234567
```

**Output.** Human: `saved <id> -> <state>`. `--json`:
`{"post_id": …, "viewer_saved_state": …}` — the echoed
`viewer_saved_state` (e.g. `SAVED`) is the single authoritative field
confirming the server-side bookmark state; `"?"` when the node shape is absent.

**Live status:** live-verified end-to-end via the saved surface (save →
dashboard count 1 → unsave → count 0, state-neutral, docs/15 §P4-1).

---

## fbk feed notify

Turn post notifications from one actor on or off — the 3-dot menu's
"turn on/off notifications" action
(`CommitActorSubscribeStatusSubscribe`/`Unsubscribe` mutations,
registry-grounded v3 "carried" doc_ids).

```
fbk feed notify --actor-id ID {on|off}
```

| Argument/flag | Type | Default | Meaning |
|---|---|---|---|
| `--actor-id ID` | string | required | The actor (user/page) id |
| `state` (positional) | `on` \| `off` | required | `on` subscribes to the actor's posts, `off` unsubscribes |

**Examples**

```
fbk feed notify --actor-id 12345678901234 on
```

**Output.** Human: `notifications <state> for actor <id>`. `--json`:
`{"actor_id": …, "state": …, "response": <merged mutation echo>}` — an ack
envelope; verify out-of-band per docs/10 §3.4.

**Live status:** PROBE-PENDING. No browser capture of either mutation exists;
the variables envelope is the standard comet convention (actor id + fresh
`client_mutation_id`) and nothing more — the live probe must confirm the field
set (a subscribe-status flag is the likeliest addition).

---

## Profile family (`fbk profile …`)

Own-profile identity reads and arbitrary-profile timeline reads, both through
the preload-replay strategy (fetch the page, harvest its SSR-registered
query, replay it verbatim — docs/15 §P2-2). Read-only; no mutation budget.

## fbk profile me

Show the logged-in user's own profile. Enriches the bootstrap identity with a
live replay of the `/me` page's own profile-query preload; on any fetch or
replay failure it degrades cleanly to the bootstrap identity (always
available) with `raw={}`.

```
fbk profile me
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk profile me
fbk profile me --json
```

**Output.** Human mode prints `id`, `name`, `business`, and `data keys` (the
replayed raw payload's top-level field names — a template-harvest debugging
aid). `--json` returns `{"profile": {user: {id, name, is_self}, is_business,
raw}}` where `raw` is a depth-trimmed, journal/emit-safe view (top level kept;
deeper dicts collapse to key lists, lists to length fingerprints). Exit 0 —
identity shape is never a failure condition.

**Live status:** live-verified read leg (the /me preload replay; bootstrap
identity per docs/15 §2).

---

## fbk profile view

Read any profile's timeline feed by numeric user/page id or vanity slug. The
service fetches `https://www.facebook.com/<user>`, harvests the page's
`ProfileCometTimelineListViewRootQuery` preload (the clean-replaying content
query, docs/15 §P4-4 — the sibling `ProfileCometTimelineFeedQuery` errors
partially even verbatim), substitutes the numeric id over the preloaded
`userID`, and parses stories with the shared page-feed walker.

```
fbk profile view --user-id USER_ID [--limit LIMIT]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--user-id USER_ID` | string | required | Target user/page id (numeric) **or** vanity slug (the `www.facebook.com/<vanity>` namespace). A slug needs the profile page's own preload to resolve; if the page carries none, the command refuses with a `ValueError` (exit 2) telling you to pass the numeric id |
| `--limit LIMIT` | int | 10 | Max stories to return; negative means unlimited (no truncation) |

**Examples**

```
fbk profile view --user-id 12345678901234
fbk profile view --user-id examplevanity --limit 25
```

**Output.** Human: a `user : name (id)` head line, then the shared story
lines (`actor | text head | feedback id | permalink`) and the page summary.
`--json` returns `{"user": {id, name}, "stories": […], "end_cursor": …,
"has_next_page": …, "count": …}` — carry `end_cursor` into feed-style cursor
chaining if the surface supports it. The timeline list-view selection carries
no bare `user.name` field, so the identity head is harvested from the
payload's own story-actor nodes — the name may be absent (`null`) on an empty
timeline. Exit 0; an empty timeline is valid account state.

**Live status:** live-probed 2026-09 (docs/15 §P4-4, the public-page replay
calibration).

---

## Comments family (`fbk comments …`)

Comment-level operations, distinct from `feed`'s post-level comment create.
Reads GET the post's permalink page and replay its own
`CometSinglePostDialogContentQuery` preload (docs/15 §P3-5); reactions replay
the live-verified captured templates against the **comment's own** feedback
id — the b64 `feedback:<post>_<cid>` global id found next to each comment
node, a different handle from the post's feedback id (docs/04 §6). Comment
CREATE stays in `fbk feed comment`; this family owns read, react, unreact,
edit, delete.

**The react/unreact/edit/delete verbs are mutations** and consume the
mutation budget.

## fbk comments read

Read a post's comments (including replies) from its full permalink URL. The
page GET is the doc_id/variable source — there is no offline template for
this surface.

```
fbk comments read --permalink PERMALINK [--limit LIMIT]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--permalink PERMALINK` | URL | required | The post's full permalink URL (http(s)) |
| `--limit LIMIT` | int | 30 | Maximum comment rows to emit; the walk is sliced locally, the wire shape is never edited |

**Examples**

```
fbk comments read --permalink "https://www.facebook.com/examplevanity/posts/12345678901234567"
fbk comments read --permalink "https://www.facebook.com/p/example.page/12345678901234567" --limit 100
```

**Output.** Human: `comments: N`, then one line per comment —
`author | text head | comment id`. `--json` returns
`{"comments": [{"id", "decoded", "author", "author_id", "text"}…], "count": N}`
— `decoded` is the `<post>_<cid>` pair (the delete/edit key); each comment's
reaction target is its OWN feedback id, readable from the permalink payload
but carried by the wire next to each comment node. Reply-expander stub nodes
are skipped. Exit 0; zero comments (or a collapsed thread) is valid state. A
permalink page with no `CometSinglePostDialogContentQuery` preload raises
`RuntimeError` (exit 2) — the surface moved; re-probe per docs/13 §2.

**Live status:** live ground truth — verified 2026-09 on a public permalink
(docs/15 §P3-5).

---

## fbk comments react

React to a comment via the **comment's own** feedback id (b64
`ZmVlZGJhY2s6<post>_<cid>`) — not the post's. The comment context sends
`feedback_source "OBJECT"` (decoded from the live comment-create capture and
live-verified; the captured `NEWS_FEED` value is the feed-story context).

```
fbk comments react --feedback-id FEEDBACK_ID --reaction KIND
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--feedback-id FEEDBACK_ID` | string | required | The COMMENT's own feedback id (b64 `ZmVlZGJhY2s6<post>_<cid>`; the decoded `feedback:<post>_<cid>` form and bare numeric are also normalized) |
| `--reaction KIND` | enum | required | Same kinds as `fbk feed react` (LIKE/LOVE/WOW/HAHA/SORRY/ANGER/SUPPORT) |

**Examples**

```
fbk comments react --feedback-id "ZmVlZGJhY2s6MTIzNDU2Nzg5MDEyMzQ1NjdfMTIzNDU2Nzg5MDEyMzQ1" --reaction LIKE
```

(b64 of `feedback:12345678901234567_123456789012345` — the comment's own
`<post>_<cid>` feedback context.)

**Output.** The merged mutation response with the updated comment feedback
(viewer_feedback_reaction_info, reaction counts). Soft-success caveat applies.

**Live status:** live-verified — the like→remove pair was calibrated live on
the target post's top comment (docs/15 §P4-3).

---

## fbk comments unreact

Remove the viewer's reaction on a comment — the same mutation family with
reaction id `"0"`, same feedback-id semantics and verbatim tracking replay
as `comments react`.

```
fbk comments unreact --feedback-id FEEDBACK_ID
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--feedback-id FEEDBACK_ID` | string | required | The COMMENT's own feedback id (b64) |

**Examples**

```
fbk comments unreact --feedback-id "ZmVlZGJhY2s6MTIzNDU2Nzg5MDEyMzQ1NjdfMTIzNDU2Nzg5MDEyMzQ1"
```

**Output.** The merged mutation response with the updated comment feedback.

**Live status:** live-verified (the remove half of the docs/15 §P4-3
like→remove pair).

---

## fbk comments edit

Edit a comment's text (`useCometUFIEditCommentMutation`). The replacement body
rides `input.message.text` with an empty ranges list and `PLAIN_TEXT`
formatting, plus the decoded commit handler's envelope (attribution,
comment_id, feedLocation, scale, translationType, relay-provider gates).

```
fbk comments edit --comment-id COMMENT_ID --text TEXT
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--comment-id COMMENT_ID` | string | required | Comment id: b64, decoded `comment:<post>_<cid>`, or bare `<post>_<cid>` |
| `--text TEXT` | string | required | The new comment text |

**Examples**

```
fbk comments edit --comment-id 12345678901234567_123456789012345 --text "revised wording"
```

**Output.** The merged mutation response carrying the edited comment.

**Live status:** schema-decoded from the owning bundle (registry doc_id
28765227863112159) — the input envelope follows the decoded
CometUFICommentEditor commit handler; not live-verified end-to-end.

---

## fbk comments delete

Delete a comment via the live-proven `CometUFIDeleteCommentMutation` shape. The
variable set is exactly the decoded LocalArguments — extra undeclared
variables provoke `1675012 noncoercible_variable_value` (docs/15 §P3-3).

```
fbk comments delete --comment-id COMMENT_ID [--render-location RENDER_LOCATION]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--comment-id COMMENT_ID` | string | required | Comment id: b64, decoded `comment:<post>_<cid>`, or bare `<post>_<cid>` |
| `--render-location RENDER_LOCATION` | string | `group` | The renderLocation enum value; the live-verified capture rode a group-render context — override for feed-context comments |

**Examples**

```
fbk comments delete --comment-id 12345678901234567_123456789012345
```

**Output.** The merged mutation response carrying
`data.comment_delete.deleted_comment_id`.

**Live status:** the mutation shape is live-proven (the captured comment was
actually deleted, docs/15 §P3-3); the comment-mutation registration family
rotated with the 1047963790 deploy (see `fbk feed comment` above) — re-verify
on the current deploy before counting on it.

---

## Stories family (`fbk stories …`)

The stories tray read plus the story lifecycle (create/viewers/reply). The
tray read replays the homepage's own SSR-registered
`StoriesTrayRectangularRootQuery` with the server's verbatim variables
(live-verified 2026-09-18). The lifecycle operations were bundle-decoded
2026-09-20 from the `/stories/create/` composer chunk graph
(docs/15 §P10-1). **`create` and `reply` are mutations** and consume the
mutation budget; tray/presets/fonts/audience are reads.

## fbk stories tray

The stories tray tiles — the entries a viewer taps to open a story stack.

```
fbk stories tray [--limit LIMIT]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--limit LIMIT` | int | 20 | Maximum tiles to emit; also raises the wire-side `bucketsToFetch` when larger than the baked default of 6 |

**Examples**

```
fbk stories tray
fbk stories tray --limit 50
```

**Output.** Human: `tray tiles: N`, then one line per tile —
`[live|seen|new] owner (N cards)` (the exact live/seen/new states the UI
surfaces). `--json` returns `{"tiles": […], "count": N}` where each tile
carries `{id, owner_id, owner_name, card_count, thumbnail, is_seen, is_live,
bucket_type, typename}`. Exit 0; an empty tray is valid account state.

**Live status:** live-verified read (2026-09-18; tiles decoded from
`data.me.unified_stories_buckets.edges`, docs/15 §P2-2).

---

## fbk stories create

Publish a story: a text (SATP) story by default, a photo story with `--photo`,
or a video story with `--video`. The commit rides `StoriesCreateMutation`
with the input assembled exactly the way the decoded transformer pipeline
builds it; the photo/video ingestion reuses the live-verified composer upload
surfaces.

```
fbk stories create [--text TEXT | --photo PATH | --video PATH] [--story-text TEXT]
                   [--audience {public|friends|private}] [--ai-label {on|off}]
                   [--font-id FONT_ID] [--preset-id PRESET_ID]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--text TEXT` | string | — | Publish a TEXT story with this text (the default mode). Mutually exclusive with `--photo`/`--video` |
| `--photo PATH` | path | — | Publish a PHOTO story from an image file (image/* MIME): composer ingest → fbid → attachment `{"photo": {"id", "overlays": []}}` |
| `--video PATH` | path | — | Publish a VIDEO story from a video file (.mp4/.mov): three-stage ingest (START → rupload bytes → RECEIVE) → fbid → attachment `{"video": {"id"}}` |
| `--story-text TEXT` | string | — | Alternative spelling of the text story's text (same effect as `--text`) |
| `--audience {public,friends,private}` | enum | account default | Story audience; maps through the same `PRIVACY_BASE_STATES` table (EVERYONE/FRIENDS/SELF) onto the self audience's `story_privacy_row` (the same `privacy_row_input` shape the composer privacy-save carries), with `disable_server_fallback_to_default_privacy` forced true. Omitted = the account's own default story audience |
| `--ai-label {on,off}` | enum | — | Mark the story as self-disclosed AI content — rides `ai_generated_self_disclosure_metadata.was_self_disclosed_as_ai_generated`. The field always rides the input (true or false), never omitted |
| `--font-id FONT_ID` | string | — | Custom font id for TEXT stories (the SATP font picker); rides `text_format_metadata.inspirations_custom_font_id`. Absent options are omitted, never null — an explicit null is a noncoercible wire value |
| `--preset-id PRESET_ID` | string | — | Background style preset id for TEXT stories (the SATP swatch picker); rides `text_format_preset_id` |

**Examples**

```
fbk stories create --text "calibration story" --audience friends
fbk stories create --text "styled" --preset-id 401372137331149 --font-id 240532164481720
fbk stories create --photo shot.png --ai-label on
fbk stories create --video clip.mp4 --audience public
```

**Output.** Human: `story created: text` (or `photo (<fbid>)` / `video
(<fbid>)`) and `story id: …` (`(not echoed)` when the response carries no
extractable id). `--json` returns `{"kind", "media_id", "story_id", "story"}`.
Exit 2 when no story text/content was given at all.

**Live status:** the create commit is PARKED behind a live blocker. The
mutation and its input are bundle-decoded, but every reconstruction of the
decoded input — 37 live probes covering explicit-null variants, tracking
shapes, audience_info flags, list-wrapped inputs — fails with `1675012
noncoercible_variable_value` on the live edge; the registration resolves and
sibling queries succeed, so a required input field exists that the deferred
component adds at runtime and no static decode reveals (docs/15 §P10-1). The
next lever is one browser capture of the create request payload. Expect the
ingest legs to succeed and the story-commit to be rejected today.

---

## fbk stories viewers

A story's seen-by viewer list — the viewer-sheet's own query
(`StoriesSuspenseViewerSheetViewerListV2Query`, cursor-paged).

```
fbk stories viewers --story-id STORY_ID [--limit LIMIT]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--story-id STORY_ID` | string | required | The story CARD id — the id the tray tiles carry in their `unified_stories.nodes` card list |
| `--limit LIMIT` | int | 20 | Maximum viewer rows; rides the query's `viewerCount` |

**Examples**

```
fbk stories viewers --story-id 12345678901234
```

**Output.** Human: `viewers: N`, then one line per row `name (user_id)`.
`--json` returns `{"viewers": [{"user_id", "name", "typename"}…], "count": N}`.
Exit 0; zero viewers is valid for a fresh or unseen story.

**Live status:** bundle-decoded operation (registry-orphaned; the baked
doc_id 38550750487903492 is the resolution source, docs/15 §P10-1) — not
live-verified end-to-end.

---

## fbk stories reply

Reply to a story with text — the viewer-sheet's own send-reply commit
(`useStoriesSendReplyMutation`, TEXT variant).

```
fbk stories reply --story-id STORY_ID --text TEXT
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--story-id STORY_ID` | string | required | The story CARD id (from the tray tiles) |
| `--text TEXT` | string | required | The reply text |

The decoded TEXT-variant input is `{message, story_id, story_reply_type:
"TEXT"}`; the optional `attribution_id_v2` the browser context carries is
omitted (the decoded commit builds it from context state a CLI run does not
have).

**Examples**

```
fbk stories reply --story-id 12345678901234 --text "nice shot"
```

**Output.** Human: `reply sent to story <id>`. `--json` returns
`{"story_id": …, "result": <merged mutation echo>}`.

**Live status:** bundle-decoded (docs/15 §P10-1) — not live-verified
end-to-end; sticker/gif reply variants are decoded but not wired.

---

## fbk stories presets

The SATP text-story background style preset catalog — a live composer-root
read (`StoriesCreateQuery`), not a parked recon item: the previously-parked
"gated swatch picker" catalog is fully readable here (docs/15 §P10-1).

```
fbk stories presets
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk stories presets
fbk stories presets --json
```

**Output.** Human: `presets: N`, then one line per preset —
`<preset_id>` plus ` (font <font_id>)` when the preset pairs a font and
`, background image` when it carries one. `--json` returns
`{"presets": [{"preset_id", "font_id", "has_background_image"}…], "count": N}`.
The live catalog holds 30 presets; exit 0 with an empty list on a degraded
payload.

**Live status:** live read-verified 2026-09-20 (docs/15 §P10-1).

---

## fbk stories fonts

The SATP text-story custom font catalog (live composer-root read).

```
fbk stories fonts
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk stories fonts
```

**Output.** Human: `fonts: N`, then one line per font `name (id)`. `--json`
returns `{"fonts": [{"id", "name", "url"}…], "count": N}`. The live catalog
holds 13 custom fonts.

**Live status:** live read-verified 2026-09-20 (docs/15 §P10-1).

---

## fbk stories audience

The story audience state: the account's default mode plus the privacy
selector's audience-mode catalog. Reads both live-verified sources — the
composer root's `unified_stories_setting.audience_mode` (the account default)
and `StoriesCometPrivacySelectorDialogQuery`'s audience-mode list.

```
fbk stories audience
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk stories audience
```

**Output.** Human: `default story audience: <mode>`, then one line per mode —
`header: description` with ` *` marking the default (live catalog: PUBLIC /
FRIENDS / CUSTOM; the observed account default was FRIENDS). `--json` returns
`{"default_mode": …, "modes": [{"mode", "header", "description"}…]}`.

**Live status:** live read-verified 2026-09-20 (docs/15 §P10-1).

---

## Notifications family (`fbk notifications …`)

The same three operations the real notifications UI performs: the unseen-count
badge, the dropdown list read, and the mark-all-seen mutation. `badge` and
`list` are plain reads; **`mark-seen` is a mutation in read's clothing** — it
draws on the shared write budget despite feeling like a passive refresh
(docs/10 §2).

## fbk notifications badge

The unseen-notifications count (the nav badge query — the cheapest verified
live read in the registry, and the measurement harness's latency canary).

```
fbk notifications badge
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk notifications badge
```

**Output.** Human: `unseen: N`. `--json` returns `{"unseen": N}`. The count is
valid whatever its value; exit 0.

**Live status:** live-verified pair (`CometNotificationsBadgeCountQuery`,
doc_id 9714526941947209, `{"environment": "MAIN_SURFACE"}` — docs/15 §3).

---

## fbk notifications list

Recent notifications. Default mode is the bell dropdown; `--page`/`--all`
switch to the `/notifications/` page's own paginated full-list walker
(`CometNotificationsListPaginationQuery`, cursor-chained the way the page
scrolls).

```
fbk notifications list [--limit LIMIT] [--page N | --all]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--limit LIMIT` | int | 20 | Rows **per page** in `--page`/`--all` modes, or the dropdown cap otherwise (rides the wire as the query's `count`) |
| `--page N` | int | — | Show page N of the full paginated list (1-based). The wire has no random access: every earlier page is fetched for its walking cursor; a history that ends earlier stops at the deepest reachable page. Mutually exclusive with `--all`. A non-positive value exits 2 |
| `--all` | flag | — | Walk the full paginated history until the wire ends, **capped at 50 pages** (the safety cap that keeps a stuck cursor loop from burning the whole request budget). Rows are deduped by notif id across page boundaries |

**Examples**

```
fbk notifications list
fbk notifications list --limit 50
fbk notifications list --page 3
fbk notifications list --all --limit 50
```

**Output.** Human: `notifications: N` (annotated ` (page N)` or
` (pages walked: N) (history ended | capped at 50 pages, more remain)` in the
paginated modes), then one line per row —
`[unread|read] title: body (id)`. `--json` returns
`{"notifications": [{id, typename, title, body, unseen}…], "count": N}` plus
mode fields: `--page` adds `{"page", "has_next", "next_cursor"}`; `--all` adds
`{"pages", "has_next"}` (the pagination query's decoded variable set rides
verbatim — count, cursor, environment, filter_tokens, notif_cache_ids,
notif_query_flags, scale). Exit 0; zero notifications is valid account state.

**Live status:** the badge/dropdown reads are live-calibrated (the dropdown's
`scale` argument was live-discovered by iterating `1675012` rejections on the
read-only query); the pagination walker's variable set is schema-decoded
2026-09-20 from the carrier bundle (docs/15 §P2-2 / surfaces/notifications.py
calibration notes).

---

## fbk notifications mark-seen

Mark the listed notifications seen — **exactly what the notifications UI fires
when its list is opened**. Replays the dropdown query to harvest the notif ids
plus the UI's own `query_id` / `last_notif_sync_time` context, then commits
the decoded `useHandleUpdateMultiNotifSeenState` hook's MARK_ALL_SEEN mutation.
The id-harvest guard mirrors the decoded hook: with no notifications at all
the mutation is never fired.

```
fbk notifications mark-seen
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk notifications mark-seen
```

**Output.** Human: `marked seen: N`, then `unseen now: M` (when the response
carries the post-mutation count). `--json` returns
`{"marked": N, "unseen": M|null, "data": <trimmed mutation response>}`; with no
notifications, `{"marked": 0, "unseen": null, "data": {}}` without firing.
The viewer id rides `input.actor_id` — wire-required (the mutation rejects a
missing actor_id with `1675012`, live-verified).

**Live status:** the mutation is schema-decoded from the UI's own bundle hook;
the wire-required actor_id and the response shape are live-verified. A
mutation — consumes the shared budget.

---

## Memories family (`fbk memories …`)

The throwback ("On This Day") feed — read-only. The `/memories/` page's own
root query is NOT replayable (verbatim replay is rejected server-side with
`field_type_no_match`), so the feed is served through the paginated
`CometMemoriesFeedQuery` with the live-gated variable template that passed
the `1675012` required-variable barrier (docs/15 §P2-2).

## fbk memories feed

Read the memories feed.

```
fbk memories feed [--limit LIMIT]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--limit LIMIT` | int | 10 | Maximum memory cards to emit; rides the wire as the `count` variable |

**Examples**

```
fbk memories feed
fbk memories feed --limit 30
```

**Output.** Human: `memories: N`, then one line per card —
`[typename] date_text|creation_time text (id)`. An empty throwback prints
`no memories yet` — the live-observed empty-account state (the server answers
an empty throwback with a `field_type_no_match` protocol error, which the
service maps to an empty feed; every other protocol error still raises).
`--json` returns `{"memories": [{id, typename, text, creation_time,
feedback_id, date_text}…], "count": N}` — `feedback_id` is the unit's UFI head
so follow-up reactions can address the card without a second read. Exit 0.

**Live status:** live-probed 2026-09-18, read-only (the live-gated variable
template passed the `1675012` required-variable gate; the probe account's
empty throwback mapped to the `field_type_no_match` artifact, docs/15 §P2-2).

---

## Saved family (`fbk saved …`)

The saved-items dashboard plus the save/unsave actions. The dashboard read
follows the preload pattern (GET `/saved/`, harvest
`CometSaveDashboardRootQuery`, replay verbatim). The save/unsave plane is the
bundle-decoded, live-verified `CometSaveMutation`/`useUnsaveMutation` pair —
not the registry's `useUpdateBookmarkBatchMutation`, which decodes to the
left-rail shortcuts editor and is deliberately not used (docs/15 §P4-1
save-plane correction). **`save` and `unsave` are mutations** and consume the
budget.

## fbk saved list

List the saved-items dashboard rows.

```
fbk saved list [--limit LIMIT]
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--limit LIMIT` | int | 20 | Maximum items to emit (applied after the deduplicating walk) |

**Examples**

```
fbk saved list
fbk saved list --limit 50
```

**Output.** Human: `saved items: N`, then one line per item —
`[type] title url (id)`. `--json` returns `{"items": [{"id", "title", "url",
"type"}…], "count": N}` — the Save id, the savable's title head (first line,
120-char cap), its permalink, and its typename (or the decoded fallback
category). Exit 0; an empty collection is valid account state.

**Live status:** live-verified dashboard read (docs/15 §P2-2 preload
pattern).

---

## fbk saved save

Save an item by **savable node id** (`CometSaveMutation`, decoded +
live-verified). The item id is the savable node the save plane consumes — for
a photo post, the photo fbid carried by the story's `save_info.savable.id` —
not the story key. The mutation input carries
`{client_mutation_id, node_id, save_action: "SAVE", save_mechanism:
"CARET_MENU", surface: "STORY"}`.

```
fbk saved save --item-id ITEM_ID
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--item-id ITEM_ID` | string | required | The savable node id (e.g. the photo fbid of a photo post) |

**Examples**

```
fbk saved save --item-id 12345678901234567
```

**Output.** Human: `saved <id> -> <state>`. `--json` returns
`{"item_id": …, "viewer_saved_state": …}` — `SAVED` on the live-verified
path. Trust the `viewer_saved_state` field, not the success envelope alone
(docs/10 §3.4 soft-suppression caveat).

**Live status:** live-verified end-to-end (save → dashboard count 1 →
unsave → count 0, state-neutral, docs/15 §P4-1).

---

## fbk saved unsave

Unsave a previously saved item by savable node id (`useUnsaveMutation`,
decoded + live-verified) — the decoded sibling of the save mutation:
`save_action: "UNSAVE"` plus the decoded `contributorRoles` (`CONTRIBUTOR`)
and `scale` envelope. Idempotent when the item is already unsaved.

```
fbk saved unsave --item-id ITEM_ID
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--item-id ITEM_ID` | string | required | The same savable node id `save` accepted |

**Examples**

```
fbk saved unsave --item-id 12345678901234567
```

**Output.** Human: `unsaved <id> -> <state>`. `--json` returns
`{"item_id": …, "viewer_saved_state": …}` — `NOT_SAVED` on the live-verified
path.

**Live status:** live-verified end-to-end (docs/15 §P4-1).

---

## Presence family (`fbk presence …`)

The viewer's chat-visibility status — a deliberately read-only surface. The
CLI reports the status; it never writes the visibility setting (that would be
a settings mutation, a budget consumer, and a socially visible act).

## fbk presence status

Read the viewer's chat-visibility setting
(`useFBChatVisibility_PresenceStatusChatVisibilityQuery` — a zero-variable
read: `viewer { chat_visibility }`).

```
fbk presence status
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk presence status
```

**Output.** Human: `chat visibility: online (visible)` / `offline` /
`unknown`. The flag is genuinely tri-state on the wire — true (online),
false (offline), or absent (unknown; logged-out or soft-blocked responses
carry no viewer node) — and is rendered distinctly rather than coerced to a
boolean. `--json` returns `{"status": {"chat_visibility": <bool|null>}}`.
Exit 0 in every branch.

**Live status:** live-verified read (2026-09; the query's empty
argumentDefinitions were bundle-decoded and the replay confirmed, docs/15
§P2-2).

---

## Overview (`fbk overview`)

The aggregate dashboard — one command, one pass over the cheapest
live-verified reads. Introduces NO new wire shapes of its own: every component
delegates to a sibling surface's already-calibrated read. Per-component
failure isolation is the core invariant: each leg is exception-guarded, so one
broken surface renders as an `ERROR` line and the rest still render — a
dashboard's value is that it degrades legibly, not atomically.

```
fbk overview
```

| Flag | Type | Default | Meaning |
|---|---|---|---|
| (none beyond the global set) | | | |

**Examples**

```
fbk overview
fbk overview --json
```

**Components** (src/surfaces/overview.py — each either its own shape or an
`{"error": "<type>: <msg>"}` record):

| Key | Source | Shape |
|---|---|---|
| `identity` | the bootstrap view (no extra network call) | `{user_id, user_name, state, revision}` |
| `notifications_badge` | `CometNotificationsBadgeCountQuery` unseen count | int |
| `watch_badge` | `useCometWatchBadgeCountQuery` unseen-video badge | int |
| `presence` | chat-visibility status | `{"chat_visibility": bool\|null}` |
| `feed_head` | first feed page: total story count plus the first 3 story heads (`{actor, text}` — text capped at 80 chars) | `{count, heads, has_next_page}` |
| `thread_count` | messenger thread-list length (8-thread sample) | int |
| `registry_size` | harvested doc_id registry size | int |

**Output.** Human mode prints the dashboard:

```
identity      : Example Name (12345678901234) [LOGGED_IN]
notifications : 3 unseen
watch badge   : 0 unseen videos
presence      : online (visible)
feed head     : 8 stories on page 1
                Example Page | calibration note…
threads       : 2 recent threads
registry      : 1024 doc ids
```

(a guarded failure renders `ERROR: <type>: <msg>` in place of the component's
line). `--json` returns the full component dict keyed exactly by the seven
stable keys above. Exit 0 always, short of a typed session-layer error.

**Live status:** every component leg is a read already live-verified in its
own surface's calibration; the aggregate is deliberately assembled from the
cheapest verified reads only (docs/10 §2: reads have the high ceilings).

---

## Files that feed this guide

Command layer (argument surfaces, human renderers, exit contracts):

- `src/commands/feed.py` — the feed family's 14 subcommands
- `src/commands/profile.py` — `profile me` / `profile view`
- `src/commands/comments.py` — the comments family's 5 subcommands
- `src/commands/stories.py` — the stories family's 7 subcommands
- `src/commands/notifications.py` — badge / list / mark-seen
- `src/commands/memories.py` — the memories feed
- `src/commands/saved.py` — list / save / unsave
- `src/commands/presence.py` — `presence status`
- `src/commands/overview.py` — the aggregate dashboard renderer
- `src/commands/common.py` — global flags, `emit` stdout discipline,
  `run_command` exit-code contract
- `src/commands/render.py` — the shared story renderers every timeline prints

Surface layer (wire shapes, calibration notes, live-status ground truth):

- `src/surfaces/feed.py` — feed reads, UFI writes, composer publish/edit,
  privacy save, share/notify candidates
- `src/surfaces/profile.py` — /me and timeline-list-view reads
- `src/surfaces/comments.py` — permalink comment reads, comment-level
  react/edit/delete
- `src/surfaces/stories.py` — tray read, the story lifecycle, SATP catalogs
- `src/surfaces/notifications.py` — badge, dropdown, pagination walker,
  mark-seen commit
- `src/surfaces/memories.py` — the throwback feed read
- `src/surfaces/saved.py` — dashboard read, save/unsave plane
- `src/surfaces/presence.py` — the chat-visibility read
- `src/surfaces/overview.py` — the guarded component aggregate

Supporting modules: `src/constants.py` (reaction ids, privacy base states,
mutation doc_ids), `src/domain/common.py` (the id codecs and typed rows),
`src/governor.py` (the 40/day mutation budget), `src/surfaces/base.py` (the
`data/captured_*.json` template loader).
