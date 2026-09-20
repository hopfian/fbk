"""VIDEO UPLOAD surface service: rupload ingest + video posts (docs/02 upload
family, docs/15 §P6-1).

ARCHITECTURE:

  Four-stage media publication on the rupload resumable plane: START
  (session create on the vupload-edge async plane) -> rupload chunk
  transfer (resume probe + raw byte POST on
  ``rupload-ccu2-1.up.facebook.com``) -> RECEIVE (handle re-attach) ->
  composer publish. The start/receive POSTs and the rupload GET+POST ride
  the transport's public raw seams (FBTransport.raw_post/raw_get) because
  their hand-built form bodies and X-Entity-* header families cannot be
  expressed through the form-only post() API. Governor discipline on the
  raw plane (docs/15 §P9-1, by design): raw calls are NOT governor-gated
  — chunk transfer is not a user-visible mutation, and the concluding
  GraphQL publish (post_video's ComposerStoryCreateMutation, which rides
  the gated GraphQL client) carries the mutation budget for the whole
  flow. Soft-block observation (empty-200 / 403 / 429 feedback into the
  governor) and central journaling with the ``video_upload`` surface tag
  (docs/11 §7) still apply to every raw call.

CALIBRATION NOTES — the web video-upload wire protocol, decoded from live
bundle archaeology (2026-09, attempt 2). Every shape below is ground truth
from the deployed JS (3608-bundle harvest, module names cited) plus two
live-probed read-only config queries; unlike the photo plane
(surfaces/upload.py) the ingest itself was NOT live-replayed during
decode - posting a video is heavier state than an only-me photo post, so
end-to-end live verification is left to the operator.

* Config queries (both live-probed read-only 2026-09):

  - ``useComposerVideoUploaderConfigQuery`` (doc_id 9734072893355148; vars
    ``{actorID, entryPoint:"feed_composer", targetID:""}``) returns
    ``viewer.comet_composer_video_uploader_config``: a JSON *string* (the
    ``useJSON`` hook parses it) with the per-surface composer config -
    ``start_uri``/``chunk_start_uri``, ``receive_uri``/``chunk_receive_uri``,
    ``source`` ("composer"), ``composer_entry_point_ref`` ("feed"),
    ``resumable_service_name``/``resumable_service_domain``, validation
    limits (``useComposerVideoUploaderConfig`` +
    ``CometFeedVideoUploaderV2.$30`` applies the chunk_* URI overrides).
  - ``MediaUploadFBDefaultServerConfigurationRetrieverQuery`` (doc_id
    26396735533340887; vars ``{source_type:"composer"}``) returns
    ``data.media_upload_config``: ``network_start.uri``,
    ``network_receive.uri``, ``network_upload_service.default``/``targeted``
    (``{service_name, service_domain}``), ``media_metadata_validation`` and
    ``network_monitor``. Live: targeted = ``rupload-ccu2-1.up.facebook.com``;
    ``MediaUploadFBUploadServiceRequest.$4`` picks the TARGETED service
    whenever retryAttempts < 10, i.e. on the first attempt.

* Stage 1 - START (``MediaUploadFBStartRequest.$8`` via
  ``MediaUploadFBEndpointRequest``): POST ``<chunk_start_uri>`` (live
  ``https://vupload-edge.facebook.com/ajax/video/upload/requests/start/``)
  with the standard AsyncRequest decoration (av, __user, __a, fb_dtsg,
  jazoest, lsd - the same set the photo endpoint live-proved sufficient),
  header ``X_FB_VIDEO_WATERFALL_ID: <session uuid>`` and a form-encoded body:
  ``waterfall_id, target_id, source, composer_entry_point_ref,
  supports_chunking, supports_file_api, file_size, file_extension,
  partition_start_offset=0, partition_end_offset=<size>,
  has_file_been_replaced`` (plus ``composer_dialog_version``,
  ``video_publisher_action_source``, ``composer_work_shared_draft_mode`` as
  null bare keys per ``flattenPHPQueryData``/``PHPQuerySerializer``:
  booleans serialize as "true"/"false"). The response is the FB async
  envelope whose payload carries ``video_id, start_offset, end_offset,
  upload_session_id, region_hint, skip_upload, xpv_asset_id,
  is_xpv_single_prod`` (field names from
  ``VideoUploadStartRequestManager.__getSuccessInformData``).

* Stage 2 - RUPLOAD chunk plane (``MediaUploadFBUploadServiceRequest.$4`` +
  ``ResumableUploadServiceComet``): the rupload session id is
  ``md5([mtime, name, mime, size, hashseed].join("-")) + "-" + start_offset
  + "-" + end_offset`` (client-opaque; hashseed is the MediaUploadFBFileHasher
  pseudo-hash or the ``"<sessionID>-<assetID>"`` fallback - this service ships
  the fallback shape). Wire: ``GET https://<rupload host>/fb_video/<session_key>``
  -> ``{offset: N}`` resume probe, then one ``POST`` of the remaining bytes
  (``XHRRequest.setRawData``) with headers ``X-Entity-Name`` (encodeURIComponent
  of the filename), ``X-Entity-Type`` (MIME), ``X-Entity-Length`` (size),
  ``Offset``, ``start_offset``, ``end_offset``, ``composer_session_id`` (the
  waterfall id), ``id`` (upload_session_id), ``product_media_id`` (video_id),
  ``X-FB-Region`` (region_hint), ``X-Total-Asset-Size``; the async params ride
  as query string (?__a=1&__user&fb_dtsg&lsd&jazoest - getAsyncParams("POST")).
  The response JSON carries ``h`` - the everstore chunk handle.
  ``skip_upload=true`` in the START payload skips stages 2 and 3 (server-side
  de-dupe). Deviation from the JS (documented): fetchOffset failures default
  to offset 0 instead of failing the upload.

* Stage 3 - RECEIVE (``MediaUploadFBReceiveRequest.$8``): POST
  ``<chunk_receive_uri>`` (live
  ``https://vupload-edge.facebook.com/ajax/video/upload/requests/receive/``)
  with the same decoration + waterfall header and form body: ``waterfall_id,
  target_id, video_id, source, composer_entry_point_ref, supports_chunking,
  supports_upload_service, partition_start_offset=0,
  partition_end_offset=<size>, start_offset=0, end_offset=<size>,
  upload_speed, fbuploader_video_file_chunk=<h>, has_file_been_replaced`` -
  ``fbuploader_video_file_chunk`` re-attaches the stage-2 handle. The async
  envelope response completes ingest; the video fbid is the stage-1 video_id.

* Stage 4 - publish: the feed V2 publish hook is a NO-OP
  (``CometFeedVideoUploaderV2.registerClientConfiguration`` resolves
  ``{isSuccessful:true}`` without a call); the post itself rides
  ``ComposerStoryCreateMutation`` - the same live-verified mutation the photo
  surface replays - with ``input.attachments = [{"video": {...}}]``:
  ``mediaAttachmentAreaTransformUtil``'s element for an unedited upload is
  ``{id: video_id, audio_descriptions: null, transcriptions: null,
  notify_when_processed: true, was_created_via_unified_video_flow: null,
  additional_video_metadata: {}}``.

  REELS (parked, deliberately): the captured composer input carries a
  ``reels_remix`` block, but those are relay-provider/GK gates that ride
  VERBATIM in every feed composer publish - they are not a
  reels-publish switch. Making the post a reel needs a reels-composer
  capture (a different composer_entry_point / feedLocation /
  renderLocation than the feed capture this template is), which the
  live bundle does not contain; inventing those values would violate
  the no-invention rule (docs/15 §P1), so no ``reels`` flag is exposed
  on post_video until that capture exists.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from pydantic import SecretStr

from domain.common import Privacy
from graphql.errors import FBGraphError

from .base import Surface
from .upload import (
    _composer_template,
    _guess_mime,
    _jazoest,
    _parse_async_payload,
)

if TYPE_CHECKING:  # pragma: no cover
    from session import Session

COMPOSER_MUTATION = "ComposerStoryCreateMutation"

COMPOSER_CONFIG_QUERY = "useComposerVideoUploaderConfigQuery"
COMPOSER_CONFIG_DOC_ID = "9734072893355148"
SERVER_CONFIG_QUERY = "MediaUploadFBDefaultServerConfigurationRetrieverQuery"
SERVER_CONFIG_DOC_ID = "26396735533340887"
CONFIG_SOURCE_TYPE = "composer"
CONFIG_ENTRY_POINT = "feed_composer"

START_URI_DEFAULT = (
    "https://vupload-edge.facebook.com/ajax/video/upload/requests/start/")
RECEIVE_URI_DEFAULT = (
    "https://vupload-edge.facebook.com/ajax/video/upload/requests/receive/")
RUPLOAD_CONSUMER = "fb_video"
RUPLOAD_SERVICE_NAME_DEFAULT = "rupload-ccu2-1.up"
RUPLOAD_SERVICE_DOMAIN_DEFAULT = "facebook.com"
DEFAULT_ENTRY_POINT_REF = "feed"


class VideoUploadError(FBGraphError):
    """The video ingest plane rejected the upload (transport or payload).

    Raised for non-200 statuses, non-JSON bodies, async ``error``
    envelopes, and missing load-bearing payload fields (video_id, the
    rupload ``h`` handle) across all three ingest stages.
    """


def _form_encode(payload: dict[str, Any]) -> bytes:
    """PHPQuerySerializer semantics: None -> bare key, bool -> true/false."""
    parts: list[str] = []
    for key, value in payload.items():
        enc_key = quote(str(key), safe="")
        if value is None:
            parts.append(enc_key)
            continue
        if value is True:
            text = "true"
        elif value is False:
            text = "false"
        else:
            text = str(value)
        parts.append(f"{enc_key}={quote(text, safe='')}")
    return "&".join(parts).encode()


def _video_attachment(video_id: str) -> dict[str, Any]:
    """The minimal unedited VIDEO attachments element (the module docstring)."""
    return {
        "id": video_id,
        "audio_descriptions": None,
        "transcriptions": None,
        "notify_when_processed": True,
        "was_created_via_unified_video_flow": None,
        "additional_video_metadata": {},
    }


def _rupload_session_key(path: Path, mime: str, size: int, session_id: str,
                         start_offset: int, end_offset: int) -> str:
    """MediaUploadFBUploadServiceRequest.$4's sessionKey, fallback hashseed."""
    seed = f"{session_id}-fbk"
    joined = "-".join([
        str(int(path.stat().st_mtime)), path.name, mime, str(size), seed])
    digest = hashlib.md5(joined.encode("utf-8")).hexdigest()
    return f"{digest}-{start_offset}-{end_offset}"


class VideoUploadService(Surface):
    """The video-upload surface API: rupload ingest + video posts.

    Raw HTTP (start / rupload GET+POST / receive) goes through the
    ``_raw_post``/``_raw_get`` seams so offline tests can monkeypatch the
    transport; the two config queries and the composer mutation ride the
    GraphQL client.
    """

    def __init__(self, session: Session):
        super().__init__(session)
        self._config_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------- raw seams
    def _raw_post(self, url: str, data: bytes, headers: dict[str, str]) -> Any:
        """One raw POST through the transport's public raw seam
        (curl-impersonated, journaled centrally with the video_upload
        surface tag — same seam and discipline the photo upload service
        uses: NOT governor-gated by design — chunk transfer is not a
        user-visible mutation and the concluding GraphQL publish carries
        the mutation budget; soft-block observation and central
        journaling still apply)."""
        return self.session.transport.raw_post(
            url, data=data, headers=headers, surface="video_upload")

    def _raw_get(self, url: str, headers: dict[str, str]) -> Any:
        """One raw GET through the transport's public raw seam (the rupload
        resume probe), journaled centrally with the video_upload surface
        tag — same no-governor-gate-by-design discipline as _raw_post
        (docs/15 §P9-1)."""
        return self.session.transport.raw_get(
            url, headers=headers, surface="video_upload")

    # ------------------------------------------------------------- plumbing
    def _config(self) -> dict[str, Any]:
        """Merged upload config: live queries first, bundle defaults behind."""
        if self._config_cache is not None:
            return self._config_cache
        uid = self.session.user_id()
        cfg: dict[str, Any] = {
            "start_uri": START_URI_DEFAULT,
            "receive_uri": RECEIVE_URI_DEFAULT,
            "entry_point_ref": DEFAULT_ENTRY_POINT_REF,
            "source": CONFIG_SOURCE_TYPE,
            "rupload_host": (f"{RUPLOAD_SERVICE_NAME_DEFAULT}."
                             f"{RUPLOAD_SERVICE_DOMAIN_DEFAULT}"),
            "config_source": "defaults",
        }
        try:
            res = self.client.call(COMPOSER_CONFIG_QUERY, COMPOSER_CONFIG_DOC_ID,
                                    {"actorID": uid,
                                     "entryPoint": CONFIG_ENTRY_POINT,
                                     "targetID": ""})
            raw = (res.get("data", {}).get("viewer", {})
                   .get("comet_composer_video_uploader_config"))
            blob = (json.loads(raw) if isinstance(raw, str)
                    else raw if isinstance(raw, dict) else None)
        except FBGraphError:
            blob = None
        if blob:
            cfg["config_source"] = "composer_config"
            cfg["start_uri"] = (blob.get("chunk_start_uri")
                                or blob.get("start_uri") or cfg["start_uri"])
            cfg["receive_uri"] = (blob.get("chunk_receive_uri")
                                  or blob.get("receive_uri")
                                  or cfg["receive_uri"])
            cfg["entry_point_ref"] = (blob.get("composer_entry_point_ref")
                                      or cfg["entry_point_ref"])
            cfg["source"] = blob.get("source") or cfg["source"]
            name = blob.get("resumable_service_name")
            domain = blob.get("resumable_service_domain")
            if name and domain:
                cfg["rupload_host"] = f"{name}.{domain}"
        try:
            res = self.client.call(SERVER_CONFIG_QUERY, SERVER_CONFIG_DOC_ID,
                                    {"source_type": CONFIG_SOURCE_TYPE})
            service = (res.get("data", {}).get("media_upload_config", {})
                       .get("network_upload_service", {}))
            targeted = service.get("targeted") or {}
            if targeted.get("service_name") and targeted.get("service_domain"):
                cfg["rupload_host"] = (f"{targeted['service_name']}."
                                       f"{targeted['service_domain']}")
                cfg["config_source"] = "server_config"
        except FBGraphError:
            pass
        self._config_cache = cfg
        return cfg

    def _async_url(self, uri: str) -> str:
        """One ajax endpoint URL with the live-verified async query set."""
        boot = self.session.bootstrap()
        uid = boot.user_id or self.session.user_id()
        params = {
            "av": uid,
            "__user": uid,
            "__a": "1",
            "fb_dtsg": (boot.fb_dtsg or SecretStr("")).get_secret_value(),
            "jazoest": _jazoest(self.session.cookies.get("xs", "")),
            "lsd": (boot.lsd or SecretStr("")).get_secret_value(),
        }
        return uri + ("&" if "?" in uri else "?") + urlencode(params)

    def _rupload_params(self, *, get: bool) -> dict[str, str]:
        """getAsyncParams' decoration for the rupload plane (module docstring)."""
        boot = self.session.bootstrap()
        uid = boot.user_id or self.session.user_id()
        params = {
            "__a": "1",
            "__user": uid,
            "jazoest": _jazoest(self.session.cookies.get("xs", "")),
        }
        if not get:
            params["fb_dtsg"] = (boot.fb_dtsg
                                 or SecretStr("")).get_secret_value()
            params["lsd"] = (boot.lsd or SecretStr("")).get_secret_value()
        return params

    def _response_doc(self, resp: Any, what: str) -> dict[str, Any]:
        """FB async envelope -> the full JSON doc, with the error guards."""
        status = getattr(resp, "status_code", None)
        if status != 200:
            raise VideoUploadError(f"{what} returned HTTP {status}",
                                   code=status, raw=str(getattr(resp, "url", "")))
        try:
            doc = _parse_async_payload(resp.text)
        except FBGraphError as exc:
            raise VideoUploadError(f"{what} returned a non-JSON body: {exc}",
                                   raw=str(getattr(exc, "raw", ""))) from exc
        if doc.get("error") is not None:
            raise VideoUploadError(
                f"{what} error {doc.get('error')}: "
                f"{doc.get('errorSummary') or ''} "
                f"{doc.get('errorDescription') or ''}".strip(),
                code=doc.get("error"), raw=resp.text[:256])
        return doc

    def _ajax_payload(self, resp: Any, what: str) -> dict[str, Any]:
        """FB async envelope -> payload (start/receive responses)."""
        doc = self._response_doc(resp, what)
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            raise VideoUploadError(f"{what} response carries no payload",
                                   raw=resp.text[:256])
        return payload

    # ------------------------------------------------------------- the stages
    def _start_request(self, path: Path, size: int, session_id: str,
                       cfg: dict[str, Any]) -> dict[str, Any]:
        """Stage 1 (MediaUploadFBStartRequest.$8): the form-encoded START
        POST to <chunk_start_uri> — decodes the async payload and returns
        it with video_id stringified (the video fbid for stage 4)."""
        payload = {
            "waterfall_id": session_id,
            "target_id": self.session.user_id(),
            "source": cfg["source"],
            "composer_entry_point_ref": cfg["entry_point_ref"],
            "supports_chunking": True,
            "supports_file_api": True,
            "file_size": size,
            "file_extension": path.suffix.lstrip(".") or "mp4",
            "partition_start_offset": 0,
            "partition_end_offset": size,
            "has_file_been_replaced": False,
            "composer_dialog_version": None,
            "video_publisher_action_source": None,
            "composer_work_shared_draft_mode": None,
        }
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/",
            "X_FB_VIDEO_WATERFALL_ID": session_id,
        }
        resp = self._raw_post(self._async_url(cfg["start_uri"]),
                              _form_encode(payload), headers)
        start = self._ajax_payload(resp, "video start request")
        if start.get("video_id") is None:
            raise VideoUploadError("start response carries no payload.video_id",
                                   raw=resp.text[:256])
        start["video_id"] = str(start["video_id"])
        return start

    def _rupload(self, path: Path, content: bytes, mime: str, session_id: str,
                 start: dict[str, Any], cfg: dict[str, Any]) -> str:
        """Stage 2 (the rupload chunk plane): the resume probe (GET ->
        {offset}), then one POST of the remaining bytes — returns the
        everstore chunk handle ``h`` the RECEIVE stage re-attaches."""
        start_offset = int(start.get("start_offset") or 0)
        end_offset = int(start["end_offset"]
                         if start.get("end_offset") is not None
                         else len(content))
        session_key = _rupload_session_key(
            path, mime, len(content), session_id, start_offset, end_offset)
        base = (f"https://{cfg['rupload_host']}/{RUPLOAD_CONSUMER}"
                f"/{session_key}")
        get_headers: dict[str, str] = {
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/",
        }
        for key in ("xpv_asset_id", "is_xpv_single_prod"):
            if start.get(key) is not None:
                get_headers[key] = str(start[key])
        offset = 0
        try:
            resp = self._raw_get(
                base + "?" + urlencode(self._rupload_params(get=True)),
                get_headers)
            if getattr(resp, "status_code", None) == 200:
                doc = _parse_async_payload(resp.text)
                if isinstance(doc.get("offset"), (int, float)):
                    offset = int(doc["offset"])
        except FBGraphError:
            offset = 0
        headers = {
            "X-Entity-Name": quote(path.name, safe="!'()*-._~"),
            "X-Entity-Type": mime,
            "X-Entity-Length": str(len(content)),
            "Offset": str(offset),
            "start_offset": str(start_offset),
            "end_offset": str(end_offset),
            "composer_session_id": session_id,
            "X-Total-Asset-Size": str(len(content)),
            "content-type": mime,
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/",
        }
        if start.get("upload_session_id"):
            headers["id"] = str(start["upload_session_id"])
        if start.get("video_id"):
            headers["product_media_id"] = str(start["video_id"])
        if start.get("region_hint"):
            headers["X-FB-Region"] = str(start["region_hint"])
        for key in ("xpv_asset_id", "is_xpv_single_prod"):
            if start.get(key) is not None:
                headers[key] = str(start[key])
        resp = self._raw_post(
            base + "?" + urlencode(self._rupload_params(get=False)),
            content[offset:], headers)
        doc = self._response_doc(resp, "rupload chunk request")
        handle = doc.get("h")
        if not isinstance(handle, str) or not handle:
            raise VideoUploadError("rupload response carries no h handle",
                                   raw=resp.text[:256])
        return handle

    def _receive_request(self, size: int, session_id: str, start: dict[str, Any],
                         cfg: dict[str, Any], handle: str,
                         upload_speed_bps: int) -> dict[str, Any]:
        """Stage 3 (MediaUploadFBReceiveRequest.$8): the RECEIVE POST that
        re-attaches the stage-2 chunk handle — completes the ingest."""
        payload = {
            "waterfall_id": session_id,
            "target_id": self.session.user_id(),
            "video_id": start["video_id"],
            "source": cfg["source"],
            "composer_entry_point_ref": cfg["entry_point_ref"],
            "supports_chunking": True,
            "supports_upload_service": True,
            "partition_start_offset": 0,
            "partition_end_offset": size,
            "start_offset": 0,
            "end_offset": size,
            "upload_speed": upload_speed_bps,
            "fbuploader_video_file_chunk": handle,
            "has_file_been_replaced": False,
            "composer_dialog_version": None,
            "composer_work_shared_draft_mode": None,
        }
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/",
            "X_FB_VIDEO_WATERFALL_ID": session_id,
        }
        resp = self._raw_post(self._async_url(cfg["receive_uri"]),
                              _form_encode(payload), headers)
        return self._ajax_payload(resp, "video receive request")

    # ------------------------------------------------------------- public API
    def upload_video(self, video_path: Path | str) -> dict[str, Any]:
        """Ingest one video: START -> rupload bytes -> RECEIVE -> video fbid.

        Replays the decoded three-stage wire (module docstring). When the
        START payload answers ``skip_upload=true`` the server-side
        de-dupe path short-circuits stages 2-3 (an identical asset is
        already ingested) and the stage-1 video_id is returned directly.

        Args:
            video_path: Path to a local video file; the chunk MIME is
                guessed from the extension the way the browser's File
                object would.

        Returns:
            The stage record: ``video_id`` (the video fbid the composer
            attaches), ``session_id`` (the waterfall id), ``chunk_handle``
            (the everstore handle, None on the skip_upload path), and
            the raw ``start`` / ``receive`` payloads.

        Raises:
            VideoUploadError: If the file is missing or empty, any
                stage answers a non-200 status or error envelope, or
                the rupload response carries no ``h`` handle.
        """
        path = Path(video_path)
        if not path.is_file():
            raise VideoUploadError(f"video file not found: {path}")
        content = path.read_bytes()
        if not content:
            raise VideoUploadError(f"video file is empty: {path}")
        mime = _guess_mime(path)
        cfg = self._config()
        session_id = str(uuid4())
        started = time.monotonic()
        start = self._start_request(path, len(content), session_id, cfg)
        if start.get("skip_upload"):
            return {
                "video_id": start["video_id"],
                "session_id": session_id,
                "chunk_handle": None,
                "start": start,
                "receive": {"skip_upload": True},
            }
        handle = self._rupload(path, content, mime, session_id, start, cfg)
        elapsed = time.monotonic() - started
        speed = int(len(content) / max(elapsed, 0.001))
        receive = self._receive_request(len(content), session_id, start, cfg,
                                        handle, speed)
        return {
            "video_id": start["video_id"],
            "session_id": session_id,
            "chunk_handle": handle,
            "start": start,
            "receive": receive,
        }

    def post_video(self, video_path: Path | str, caption: str,
                   privacy: Privacy, *, group_id: str | None = None
                   ) -> dict[str, Any]:
        """Upload one video and publish it as a video post.

        Ingest via upload_video(), then ComposerStoryCreateMutation replays
        the captured template with the decoded minimal VIDEO attachments
        element (the module docstring) - the same mutation the photo surface
        live-verified, carrying ``{"video": {...}}`` instead. This
        concluding GraphQL publish is the mutation-budgeted leg of the
        whole flow (module docstring's governor discipline).

        Args:
            video_path: Path to the local video to publish.
            caption: The post body; rides input.message.text.
            privacy: The audience selector value; privacy.value rides
                input.audience.privacy.base_state.
            group_id: Optional group scope overriding the top-level
                groupID on the composer input.

        Returns:
            {"video_id", "upload" (the upload_video result), "publish"
            (the merged ComposerStoryCreateMutation response)}.

        Raises:
            VideoUploadError: Propagated from the ingest phase when any
                stage fails.

        Note:
            The publish is the captured FEED composer context
            (feedLocation NEWSFEED, renderLocation homepage_stream);
            a reels variant is deliberately NOT exposed - see the
            module docstring's REELS note (no reels-composer capture
            exists to replay; inventing one is out of calibration).
        """
        upload = self.upload_video(video_path)
        variables = _composer_template()
        variables["input"]["message"]["text"] = caption
        variables["input"]["audience"]["privacy"]["base_state"] = privacy.value
        variables["input"]["idempotence_token"] = f"{uuid4()}_FEED"
        variables["input"]["logging"]["composer_session_id"] = upload["session_id"]
        variables["input"]["actor_id"] = self.session.user_id()
        variables["input"]["attachments"] = [
            {"video": _video_attachment(upload["video_id"])}]
        variables["feedLocation"] = "NEWSFEED"
        variables["renderLocation"] = "homepage_stream"
        if group_id is not None:
            variables["groupID"] = group_id
        publish = self.client.call(
            COMPOSER_MUTATION, self._mutation_doc_id(COMPOSER_MUTATION),
            variables)
        return {"video_id": upload["video_id"], "upload": upload,
                "publish": publish}
