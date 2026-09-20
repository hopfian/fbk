"""Coherent browser client profiles (docs/08 §5, docs/11 §1-2).

The Prime Directive of coherence: one profile = one stable identity tuple.
UA, sec-ch-ua, platform, locale and tz must never contradict each other —
the validator below rejects incoherent profiles at construction time.

ARCHITECTURE:

  Identity Tuple (docs/08 §5):
    ``ClientProfile`` freezes the full header-level identity the transport
    presents: impersonation target, UA, client-hint trio, locale, tz, and
    the screen-metric echoes (``wd``/``dpr``) the edge cross-checks against
    cookies and IP geo. The cross-axis coherence matrix of docs/08 §5 is
    what ``validate_coherence`` encodes — every pair of axes that the edge
    can compare must not contradict, including the TLS↔UA major axis
    (docs/09 §1.2) scored first by the edge.

  Validation as the Value:
    The validator is the point of the module (docs/08 §7): it *rejects*
    incoherent tuples before they ever reach the wire, both at profile
    load time (``load_or_default``) and at transport construction
    (``FBTransport.__init__``). A rejected profile is never silently
    downgraded to defaults — incoherence is an operator error.

CALIBRATION NOTES (docs/15 §P9-2, capture evidence docs/15 §P3-1):
  ``sanitize_user_agent`` exists because the live-captured MQTT CONNECT
  rode a UA containing ``HeadlessChrome/152`` (the capture browser ran
  headless) — a loud automation fingerprint that must never ship on any
  fbk transport. The identity blob builders in ``realtime.mqtt.connect``
  and the WS handshake headers both pass through it.

USER-DOC ANCHOR: cli/docs/02-configuration.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import os
import re

from pydantic import BaseModel, Field


class ClientProfile(BaseModel):
    """The identity the transport presents to the edge (docs/08 §5).

    One frozen tuple covering every header-level axis the edge can
    cross-check: TLS impersonation target, UA, client-hint trio,
    locale/timezone, and the ``wd``/``dpr`` screen metrics mirrored in the
    cookie jar. Persistence is JSON (``save``/``load``) so a profile is
    generated once, versioned, and replayed forever — rotating any axis
    between sessions is a known scraper signature (docs/16 §3).
    """

    # TLS/h2 target; must stay within the same browser family+major as
    # the UA (docs/09 §2.3 ratchet, docs/16 §6 pinning policy).
    impersonate: str = Field(
        default="chrome136",
        description=(
            "curl_cffi impersonation target. Pinned to a major version "
            "deliberately; the unversioned 'chrome' target would drift on "
            "library upgrade and break day-to-day coherence (docs/16 §6)."
        ),
    )
    # Chrome 136 on Windows: the shape confirmed live 2026-09 (docs/15 §1).
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        description=(
            "Full UA string; must match the impersonation target's family "
            "and major version (docs/08 §5 coherence matrix)."
        ),
    )
    sec_ch_ua: str = Field(
        default='"Chromium";v="136", "Not_A Brand";v="99", "Google Chrome";v="136"',
        description=(
            "The sec-ch-ua client hint; the Chrome major inside it must "
            "agree with user_agent (the validator enforces it)."
        ),
    )
    sec_ch_ua_platform: str = Field(
        default='"Windows"',
        description='sec-ch-ua-platform; OS class must match the UA exactly.',
    )
    sec_ch_ua_mobile: str = Field(
        default="?0",
        description="?0 desktop / ?1 mobile; cross-checked against platform and wd.",
    )
    accept_language: str = Field(
        default="en-US,en;q=0.9",
        description="Accept-Language; must agree with the locale cookie and IP geo.",
    )
    timezone: str = Field(
        default="UTC",
        description="IANA tz; must agree with IP geo or carry a plausible travel story.",
    )
    # Screen-metric echoes of the wd/dpr cookies (docs/08 §4): plausible,
    # stable per profile, coherent with the platform class.
    dpr: str = Field(
        default="1",
        description="Device pixel ratio echo of the dpr cookie; numeric.",
    )
    wd: str = Field(
        default="1280:720",
        description="Screen size echo of the wd cookie; 'width:height' with both > 0.",
    )
    notes: dict[str, str] = Field(default_factory=dict)

    def validate_coherence(self) -> list[str]:
        """Check every cross-axis coherence rule the edge can score.

        Encodes the docs/08 §5 coherence matrix: UA ↔ platform class,
        UA ↔ sec-ch-ua major version, the TLS impersonation pin ↔ UA
        Chrome major (docs/09 §1.2 — the cross-check the edge scores
        first), mobile flag ↔ platform ↔ screen class, and the
        plausibility of the ``wd``/``dpr`` cookie echoes. Each check
        names the contradicting axis pair in its message so an
        operator can repair the profile rather than guess.

        Boundary: only VERSIONED ``chrome<N>`` pins are major-checked.
        Unversioned targets (a bare ``"chrome"``), non-Chrome targets,
        and an empty ``impersonate`` carry no major this rule can see
        and stay UNGUARDED here — no false positives on targets the
        version rule cannot score; their wire validity is owned by the
        local refused-port probe (doctor, docs/12 §3).

        Returns:
            A list of human-readable coherence problems; an empty list
            means the profile is safe to put on the wire.
        """
        problems: list[str] = []
        if "Windows NT 10.0" in self.user_agent and self.sec_ch_ua_platform != '"Windows"':
            problems.append("UA says Windows but sec_ch_ua_platform disagrees")
        if "Android" in self.user_agent:
            if self.sec_ch_ua_mobile != "?1":
                problems.append("UA says Android but sec_ch_ua_mobile is not ?1")
            if self.sec_ch_ua_platform != '"Android"':
                problems.append("UA/platform contradiction (Android)")
        if self.sec_ch_ua_mobile == "?1" and self.sec_ch_ua_platform == '"Windows"':
            problems.append("mobile=?1 with Windows platform is incoherent")
        if "Chrome/" in self.user_agent:
            ua_major = self.user_agent.split("Chrome/")[1].split(".")[0]
            for part in self.sec_ch_ua.replace('"', "").split(","):
                if "v=" in part and "Chrome" in part:
                    ch_major = part.split("v=")[1]
                    if ua_major != ch_major:
                        problems.append(
                            f"UA Chrome/{ua_major} vs sec-ch-ua Chrome/{ch_major} mismatch")
                    break
        # TLS↔UA axis (docs/08 §5 matrix, docs/09 §1.2 — the edge scores
        # this pair first): a versioned chrome<N> pin must agree with
        # the UA's Chrome major. Unversioned / non-Chrome / empty pins
        # stay unguarded here — see the method docstring boundary.
        pin = re.fullmatch(r"chrome(\d+)", self.impersonate)
        if pin is not None:
            if "Chrome/" not in self.user_agent:
                problems.append(
                    f"impersonate target {self.impersonate} with a UA "
                    "carrying no Chrome major is incoherent")
            else:
                ua_major = self.user_agent.split("Chrome/")[1].split(".")[0]
                if pin.group(1) != ua_major:
                    problems.append(
                        f"impersonate target {self.impersonate} disagrees "
                        f"with UA Chrome/{ua_major} major")
        try:
            w, h = self.wd.split(":")
            if not (int(w) > 0 and int(h) > 0):
                raise ValueError
        except Exception:
            problems.append(f"wd cookie value {self.wd!r} is not a plausible 'w:h'")
        try:
            float(self.dpr)
        except ValueError:
            problems.append(f"dpr {self.dpr!r} not numeric")
        return problems

    # ------------------------------------------------------------- (de)serialisation
    def save(self, path: str | os.PathLike[str]) -> None:
        """Persist the profile as JSON, creating parent directories.

        Args:
            path: Destination file path (``profiles/<name>.json`` per the
                docs/08 §7 freeze-once-version-forever lifecycle).
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.model_dump(), fh, indent=2)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ClientProfile:
        """Rehydrate a profile from its persisted JSON form.

        Args:
            path: File previously written by ``save``.

        Returns:
            The reconstructed ``ClientProfile``; call
            ``validate_coherence`` before trusting it (as
            ``load_or_default`` does).
        """
        with open(path, encoding="utf-8") as fh:
            return cls(**json.load(fh))


def load_or_default(profile_path: str | None) -> ClientProfile:
    """Load a profile JSON or return the safe default; a loaded profile that
    fails coherence validation is REJECTED (not silently used).

    Args:
        profile_path: Path to the profile JSON, or None for the default.

    Returns:
        A coherence-validated ``ClientProfile`` — either the loaded one or
        the safe default.

    Raises:
        ValueError: If the loaded profile fails ``validate_coherence``;
            an incoherent profile must never reach the wire silently.
    """
    if profile_path and os.path.isfile(profile_path):
        profile = ClientProfile.load(profile_path)
        problems = profile.validate_coherence()
        if problems:
            raise ValueError("incoherent client profile: " + "; ".join(problems))
        return profile
    return ClientProfile()


def sanitize_user_agent(ua: str) -> str:
    """Strip automation signatures from a UA before it hits the wire.

    Live evidence (docs/15 §P9-2, capture from docs/15 §P3-1): the captured
    MQTT CONNECT carried 'HeadlessChrome/152' because the capture browser
    ran headless — a loud automation fingerprint the transport identities
    must NEVER ship. 'HeadlessChrome/<v>' is rewritten to the matching
    'Chrome/<v>' and other headless markers are dropped entirely.

    Args:
        ua: The candidate UA string (captured, configured, or default).

    Returns:
        The UA with headless markers removed, safe for any transport path
        (HTTP headers, MQTT identity blob, DGW handshake headers).
    """
    # HeadlessChrome/<v> -> Chrome/<v>: version-preserving rewrite so the
    # sanitized UA still matches the profile's sec-ch-ua major (docs/15 §P9-2).
    ua = re.sub(r"HeadlessChrome/(\d+)", r"Chrome/\1", ua)
    # ' Headless' suffix markers (desktop headless builds) have no
    # browser-equivalent rendering — drop them outright.
    ua = ua.replace(" Headless", "")
    return ua
