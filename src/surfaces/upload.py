"""UPLOAD surface service: photo ingest + photo posts (docs/02 upload family,
docs/15 §P5-1).

ARCHITECTURE:

  Two-phase media publication, mirroring the real composer's split:
  Phase 1 ingests bytes on the legacy AsyncRequest multipart plane
  (NOT rupload — the P4-era rupload assumption for composer photos was
  wrong, docs/15 §P5-1), Phase 2 attaches the resulting fbid to the
  live-verified ComposerStoryCreateMutation capture. The ingest rides
  the transport's public raw seam (FBTransport.raw_post) because the
  hand-built multipart body and browser-coherence headers cannot be
  expressed through the form-only post() API; raw calls are journaled
  centrally with the ``upload`` surface tag (docs/11 §7). The photo
  fbid never reaches the wire until the Phase-2 mutation carries it —
  ingest alone is invisible data plumbing.

  Video upload lives in surfaces/video_upload.py: the P5-era honest
  omission of the video plane was superseded in Phase 6 (docs/15 §P6-1)
  — the full four-stage rupload wire (START -> chunk plane -> RECEIVE
  -> composer publish) is decoded and shipped there.

CALIBRATION NOTES — the photo-upload wire protocol decoded from live
bundle archaeology (2026-09, composer + media-upload bundles; every
shape below is ground truth from the deployed JS, not inference):

* Phase 1 - binary ingest. The feed composer does NOT use the rupload
  chunked plane for photos: module ``XComposerPhotoUploader`` drives
  ``FileInputUploader``/``AsyncUploadBase`` in a single multipart POST to the
  endpoint configured at runtime in ``ReactComposerMediaConfig.photo``:

      POST https://upload.facebook.com/ajax/react_composer/attachments/photo/upload
           ?av=<uid>&__user=<uid>&__a=1&fb_dtsg=<token>&jazoest=<xs-sum>&lsd=<lsd>
      Content-Type: multipart/form-data
      (session cookies; the AsyncRequest layer decorates cross-origin
      FormData URIs with the async params - ActorURI's av plus the
      getAsyncParams CSRF set. Live-probed: __a/__user/fb_dtsg are the
      required subset; without them the edge answers 200 with an empty
      text/html body)

  Form parts, in ``AsyncUploadBase._processUpload`` construction order:
      source=8  profile_id=<user_id>  waterfallxapp=comet   (uploadData)
      upload_id=<PhotosUploadID: small numeric string>      (file.uploadID)
      farr=<file bytes>  (the ``setFiles({farr: ...})`` input name;
                          filename + image MIME from the File object)

  Response is an FB async envelope: ``for (;;);{"__ar":1,"payload":{…}}``
  whose payload carries ``photoID`` (the photo fbid), ``imageSrc``,
  ``height`` and ``width`` - read by ``ComposerMediaFileUploader``'s
  onUploadSuccess. Live-verified end-to-end 2026-09 (browser capture +
  curl-impersonated replay both returned a real photoID).

* Phase 2 - publish. ``mediaAttachmentAreaCreationDataTransform`` +
  ``mediaAttachmentAreaTransformUtil`` build the composer input's
  ``attachments`` array; for photos the element is exactly
  ``{"photo": {"id": <photoID>, ...optional editor state...}}`` (tags and
  further fields come from view-state data an unedited upload does not
  have, so the minimal live element is just the id). That array rides in
  ``input.attachments`` of ``ComposerStoryCreateMutation`` - the same
  live-verified mutation FeedService.publish replays (docs/15 P3). An
  album post is the list extension of that calibrated shape (docs/15
  §P9): N photo elements in one attachments array, one per ingested
  photo (post_album).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import math
import mimetypes
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from pydantic import SecretStr

from domain.common import Privacy
from governor import GovernorConfig
from graphql.errors import FBGraphError

from .base import Surface, load_template

COMPOSER_MUTATION = "ComposerStoryCreateMutation"

# react_composer photo ingest endpoint: the live runtime value of
# ReactComposerMediaConfig.photo.uploadEndpoint (homepage config frame,
# fetched 2026-09 - verbatim from the bootstrap HTML).
PHOTO_UPLOAD_ENDPOINT = (
    "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload")

# The composer's own form fields (ReactComposerMediaConfig.photo.uploadData).
_UPLOAD_FIELDS = {"waterfallxapp": "comet", "source": "8"}

# File part name: XComposerPhotoUploader does setFiles({farr: [file]}).
_FILE_FIELD = "farr"

# PhotosUploadID: a per-page counter from 1025 (var e=1024; (e++).toString()).
_UPLOAD_ID_COUNTER = {"n": 1024}


class UploadError(FBGraphError):
    """The ingest endpoint rejected the upload (transport or payload error).

    Raised for non-200 statuses, non-JSON bodies, and envelopes whose
    payload carries no ``photoID`` — the three live-observable failure
    classes of the react_composer ingest plane (docs/15 §P5-1).
    """


def _next_upload_id() -> str:
    """A fresh PhotosUploadID-shaped id: a small numeric string."""
    _UPLOAD_ID_COUNTER["n"] += 1
    return str(_UPLOAD_ID_COUNTER["n"])


def _jazoest(xs: str) -> str:
    """The xs-cookie checksum the browser pairs with fb_dtsg ("2"+sum)."""
    return "2" + str(sum(ord(c) for c in xs))


def _guess_mime(path: Path) -> str:
    """The File-object content type the browser would attach."""
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


# --------------------------------------------------------------- album pacing
def _inter_upload_gap(rng: random.Random) -> float:
    """One governor-shaped lognormal inter-arrival gap, for album pacing.

    The raw photo-ingest seam is deliberately NOT governor-gated
    (docs/15 §P9-1: byte transfer is not a user-visible mutation), but
    an album of N back-to-back ingest posts IS a burst — the exact
    volume-plus-metronomic-timing shape that triggered the Phase-8
    live session kill (docs/15 §P8-1). Between uploads the album flow
    therefore spaces the raw posts exactly the way the governor spaces
    GraphQL calls (docs/11 §8): a lognormal inter-arrival sample with
    the governor's own parameters — floor 4s, mean 12s, cv 0.7 — reusing
    GovernorConfig.from_env() so FBK_GOVERNOR_* overrides shape album
    pacing too. The mean-preserving mu correction and the min-gap floor
    are the governor's sampling math verbatim (governor.py) — the gaps
    stay heavy-tailed, never metronomic (the Phase-8 trigger itself).

    Args:
        rng: The caller's fresh Random instance (per-album sampling).

    Returns:
        The sampled gap in seconds, never below the governor floor.
    """
    cfg = GovernorConfig.from_env()
    sigma = math.sqrt(math.log(1 + cfg.gap_cv ** 2))
    mu = math.log(cfg.mean_gap_s) - 0.5 * sigma ** 2
    return max(cfg.min_gap_s, rng.lognormvariate(mu, sigma))


def _build_multipart(fields: dict[str, str], file_field: str, filename: str,
                     content_type: str, content: bytes) -> tuple[bytes, str]:
    """Assemble the multipart body exactly as FormData + File would.

    Returns (body, content_type header value). Field order follows
    AsyncUploadBase._processUpload: the uploadData fields first, then
    upload_id, then the file part.
    """
    boundary = "----fbk" + uuid4().hex
    body = b""
    for name, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode()
    body += (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\n'
             f"Content-Type: {content_type}\r\n\r\n").encode()
    body += content
    body += f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _parse_async_payload(text: str) -> dict[str, Any]:
    """FB async envelope -> the JSON document (``for (;;);`` stripped)."""
    stripped = text.lstrip()
    for prefix in ("for (;;);", "while(1);", "while(true);"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].lstrip()
            break
    try:
        doc = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise UploadError(f"upload endpoint returned non-JSON body: {exc}",
                          raw=text[:256]) from exc
    if not isinstance(doc, dict):
        raise UploadError("upload endpoint returned a non-object JSON body",
                           raw=text[:256])
    return doc


# --------------------------------------------------------------- composer side
# The composer mutation template lives in assets/captured_composer.json (the
# same live capture FeedService replays, docs/15 P3). FeedService.publish
# cannot carry attachments, so this service composes the input itself against
# the same template - the loader itself is canonical in surfaces/base.py
# (load_template); this module-level seam keeps the historical name
# video_upload.py imports.
def _composer_template() -> dict[str, Any]:
    """Deep-copied ComposerStoryCreateMutation variables (captured live).

    Thin delegate to :func:`surfaces.base.load_template` (docs/15 §P3):
    same shared cache, same scan, same deep-copy semantics — and, via
    base's defaults, the exact missing-asset / missing-entry RuntimeErrors
    this module has always raised.
    """
    return load_template("captured_composer.json", COMPOSER_MUTATION)


# ------------------------------------------------------------------------ service
class UploadService(Surface):
    """The media-upload surface API (docs/02 upload family).

    All raw upload HTTP goes through the internal ``_raw_post`` seam (a thin
    delegate to FBTransport.raw_post) so offline tests can monkeypatch the
    service; raw requests are journaled centrally by the transport with
    the ``upload`` surface tag (docs/11 §7).
    """

    # ------------------------------------------------------------------ raw seam
    def _raw_post(self, url: str, data: bytes | dict[str, Any],
                  headers: dict[str, str]) -> Any:
        """One raw POST through the transport's public raw seam
        (curl-impersonated, journaled centrally with the upload surface tag).

        Governor discipline on the raw plane (docs/15 §P9-1, by design):
        raw_post/raw_get are NOT governor-gated — chunk/multipart byte
        transfer is not a user-visible mutation, and the concluding
        GraphQL publish (post_photo's ComposerStoryCreateMutation, which
        rides the gated GraphQL client) carries the mutation budget for
        the whole two-phase flow. Soft-block observation (empty-200 /
        403 / 429 feedback into the governor) and central journaling
        (docs/11 §7) still apply to every raw call.
        """
        return self.session.transport.raw_post(
            url, data=data, headers=headers, surface="upload")

    # ------------------------------------------------------------------ ingest
    def _upload_url(self) -> str:
        """Endpoint + the live-verified async query set (ActorURI av,
        getAsyncParams actor/CSRF decoration - see the module docstring)."""
        boot = self.session.bootstrap()
        uid = boot.user_id or self.session.user_id()
        fb_dtsg = boot.fb_dtsg or SecretStr("")
        lsd = boot.lsd or SecretStr("")
        params = {
            "av": uid,
            "__user": uid,
            "__a": "1",
            "fb_dtsg": fb_dtsg.get_secret_value(),
            "jazoest": _jazoest(self.session.cookies.get("xs", "")),
            "lsd": lsd.get_secret_value(),
        }
        return PHOTO_UPLOAD_ENDPOINT + "?" + urlencode(params)

    def upload_photo(self, image_path: Path | str) -> dict[str, Any]:
        """Upload one image to the composer ingest endpoint -> photo fbid.

        Replays the XComposerPhotoUploader wire shape: a single multipart
        POST carrying the uploadData fields (source/profile_id/
        waterfallxapp), a PhotosUploadID-shaped ``upload_id`` and the file
        under the ``farr`` part.

        Args:
            image_path: Path to a local image file; the part's MIME is
                guessed from the extension the way the browser's File
                object would.

        Returns:
            The decoded result dict: ``photo_id`` (the photo fbid the
            composer attaches), ``upload_id``, ``image_url`` /
            ``width`` / ``height`` (the ingest payload's preview
            fields) and the raw ``payload`` for logging.

        Raises:
            UploadError: If the file is missing, its guessed MIME is not
                an ``image/*`` type (the uploader's own file-input
                accept filter, XComposerPhotoUploader — GIFs map to
                ``image/gif`` and ride the multipart headers verbatim),
                the endpoint answers a non-200 status, a non-JSON body,
                or a payload without ``photoID``.
        """
        path = Path(image_path)
        if not path.is_file():
            raise UploadError(f"image file not found: {path}")
        content = path.read_bytes()
        mime = _guess_mime(path)
        if not mime.startswith("image/"):
            raise UploadError(
                f"not an image file ({mime}): {path} — the composer photo "
                f"ingest takes image/* only (XComposerPhotoUploader's "
                f"file-input accept filter)")
        fields = {
            **_UPLOAD_FIELDS,
            "profile_id": self.session.user_id(),
            "upload_id": _next_upload_id(),
        }
        body, content_type = _build_multipart(
            fields, _FILE_FIELD, path.name, mime, content)
        headers = {
            "content-type": content_type,
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/",
        }
        resp = self._raw_post(self._upload_url(), body, headers)
        status = getattr(resp, "status_code", None)
        if status != 200:
            raise UploadError(
                f"upload endpoint returned HTTP {status}",
                code=status, raw=str(getattr(resp, "url", PHOTO_UPLOAD_ENDPOINT)))
        doc = _parse_async_payload(resp.text)
        payload = doc.get("payload")
        if not isinstance(payload, dict) or "photoID" not in payload:
            raise UploadError(
                "upload response carries no payload.photoID",
                raw=json.dumps(doc, default=str)[:256])
        photo_id = str(payload["photoID"])
        return {
            "photo_id": photo_id,
            "upload_id": fields["upload_id"],
            "image_url": payload.get("imageSrc"),
            "width": payload.get("width"),
            "height": payload.get("height"),
            "payload": payload,
        }

    # ------------------------------------------------------------------ publish
    def post_photo(self, image_path: Path | str, caption: str,
                   privacy: Privacy, *, group_id: str | None = None) -> dict[str, Any]:
        """Upload one image and publish it as a photo post.

        Two phases on the verified wire (module docstring): upload_photo()
        ingests the bytes, then ComposerStoryCreateMutation replays the
        captured template with ``input.attachments = [{"photo": {"id":
        <photoID>}}]`` — the exact element mediaAttachmentAreaTransformUtil
        emits for an unedited photo. FeedService.publish cannot carry
        attachments, so the composer input is composed here against the
        same captured template.

        Args:
            image_path: Path to the local image to publish.
            caption: The post body; rides input.message.text.
            privacy: The audience selector value; privacy.value rides
                input.audience.privacy.base_state.
            group_id: Optional group scope overriding the top-level
                groupID on the composer input.

        Returns:
            {"photo_id", "upload" (the upload_photo result), "publish"
            (the merged ComposerStoryCreateMutation response)}.

        Raises:
            UploadError: Propagated from the ingest phase when the
                endpoint rejects the upload.
        """
        upload = self.upload_photo(image_path)
        variables = _composer_template()
        variables["input"]["message"]["text"] = caption
        variables["input"]["audience"]["privacy"]["base_state"] = privacy.value
        variables["input"]["idempotence_token"] = f"{uuid4()}_FEED"
        variables["input"]["logging"]["composer_session_id"] = str(uuid4())
        variables["input"]["actor_id"] = self.session.user_id()
        variables["input"]["attachments"] = [{"photo": {"id": upload["photo_id"]}}]
        variables["feedLocation"] = "NEWSFEED"
        variables["renderLocation"] = "homepage_stream"
        if group_id is not None:
            variables["groupID"] = group_id
        publish = self.client.call(
            COMPOSER_MUTATION, self._mutation_doc_id(COMPOSER_MUTATION), variables)
        return {"photo_id": upload["photo_id"], "upload": upload,
                "publish": publish}

    # ------------------------------------------------------------------ albums
    def post_album(self, image_paths: list[Path | str], caption: str,
                   privacy: Privacy, *, group_id: str | None = None,
                   sleep: Callable[[float], None] = time.sleep,
                   ) -> dict[str, Any]:
        """Upload N images sequentially and publish them as one album post.

        Each image rides the EXISTING upload_photo ingest unchanged; the
        concluding publish is ONE ComposerStoryCreateMutation with
        ``input.attachments = [{"photo": {"id": id1}}, ..., {"photo":
        {"id": idN}}]`` — the list extension of the calibrated
        single-photo shape (docs/15 §P9): mediaAttachmentAreaTransformUtil
        emits one ``{"photo": {"id": ...}}`` element per attached photo,
        so the N-element array repeats the exact per-element shape the
        live-verified single-photo post carries.

        PACING (anti-detection, docs/11 §8 + docs/15 §P8-1): the raw
        ingest seam is not governor-gated (docs/15 §P9-1) and N
        back-to-back multipart posts would be a burst — the only signal
        that has ever triggered live enforcement. Between uploads the
        flow sleeps one governor-shaped lognormal gap
        (:func:`_inter_upload_gap`: floor 4s, mean 12s, cv 0.7 — the
        governor's own sampling math); the concluding GraphQL publish
        rides the gated client and carries the mutation budget. The
        sleeper is injectable so tests never sleep real time (the house
        sleeper-injection discipline of the governor tests).

        Args:
            image_paths: Local image files, uploaded in list order; each
                passes upload_photo's image/* MIME guard.
            caption: The post body; rides input.message.text.
            privacy: The audience selector value; privacy.value rides
                input.audience.privacy.base_state.
            group_id: Optional group scope overriding the top-level
                groupID on the composer input.
            sleep: The between-upload gap sleeper; tests inject a
                recorder instead of sleeping.

        Returns:
            {"photo_ids" (in list order), "uploads" (each upload_photo
            result, same order), "publish" (the merged
            ComposerStoryCreateMutation response)}.

        Raises:
            UploadError: On an empty list, or propagated from any ingest
                rejection (missing file, non-image MIME, endpoint
                refusal).
        """
        paths = [Path(p) for p in image_paths]
        if not paths:
            raise UploadError("album needs at least one image")
        rng = random.Random()
        uploads: list[dict[str, Any]] = []
        for index, path in enumerate(paths):
            if index:
                # anti-burst gap between the ungated raw ingest posts
                # (docs/11 §8 + docs/15 §P8-1; see _inter_upload_gap)
                sleep(_inter_upload_gap(rng))
            uploads.append(self.upload_photo(path))
        variables = _composer_template()
        variables["input"]["message"]["text"] = caption
        variables["input"]["audience"]["privacy"]["base_state"] = privacy.value
        variables["input"]["idempotence_token"] = f"{uuid4()}_FEED"
        variables["input"]["logging"]["composer_session_id"] = str(uuid4())
        variables["input"]["actor_id"] = self.session.user_id()
        variables["input"]["attachments"] = [
            {"photo": {"id": upload["photo_id"]}} for upload in uploads]
        variables["feedLocation"] = "NEWSFEED"
        variables["renderLocation"] = "homepage_stream"
        if group_id is not None:
            variables["groupID"] = group_id
        publish = self.client.call(
            COMPOSER_MUTATION, self._mutation_doc_id(COMPOSER_MUTATION), variables)
        return {"photo_ids": [u["photo_id"] for u in uploads],
                "uploads": uploads, "publish": publish}
