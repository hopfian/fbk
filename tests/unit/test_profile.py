"""Client-profile coherence tests (docs/08 §5, docs/11 §2).

Unit (offline): pure ClientProfile construction/validation plus
save/load round-trips through tmp_path files — no network, no session.
"""
import pytest

from transport.profile import ClientProfile, load_or_default

# A coherent non-default identity: every axis agrees on Chrome/131 (a
# real resolve_impersonate ladder target, transport/session.py).
CHROME_131_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEC_CH_UA_131 = ('"Chromium";v="131", "Not_A Brand";v="99", '
                 '"Google Chrome";v="131"')


class TestCoherence:
    """Pins validate_coherence() cross-field checks and load-time rejection
    of an incoherent saved profile."""

    def test_default_profile_is_coherent(self):
        assert ClientProfile().validate_coherence() == []

    def test_windows_ua_needs_windows_platform(self):
        p = ClientProfile(sec_ch_ua_platform='"Android"')
        problems = p.validate_coherence()
        assert any("platform" in msg for msg in problems)

    def test_android_ua_needs_mobile_flag(self):
        p = ClientProfile(user_agent="Mozilla/5.0 (Linux; Android 14) …Chrome/136…")
        problems = p.validate_coherence()
        assert any("sec_ch_ua_mobile" in msg for msg in problems)

    def test_chrome_version_mismatch_detected(self):
        p = ClientProfile(user_agent="…Chrome/131.0.0.0 Safari/537.36",
                          sec_ch_ua='"Google Chrome";v="136"')
        assert any("mismatch" in m for m in p.validate_coherence())

    def test_bad_wd_and_dpr_flagged(self):
        p = ClientProfile(wd="not-.dimensions", dpr="high")
        problems = p.validate_coherence()
        assert len([m for m in problems if "wd" in m or "dpr" in m]) == 2

    def test_load_or_default_rejects_incoherent(self, tmp_path):
        bad = ClientProfile(sec_ch_ua_platform='"Android"')  # contradicts UA
        bad.save(tmp_path / "profile.json")
        with pytest.raises(ValueError):
            load_or_default(str(tmp_path / "profile.json"))

    def test_roundtrip_save_load(self, tmp_path):
        p = ClientProfile(accept_language="bn-BD,bn;q=0.9")
        p.save(tmp_path / "p.json")
        assert load_or_default(str(tmp_path / "p.json")) == p


class TestImpersonateAxis:
    """The TLS↔UA axis (docs/08 §5 matrix, docs/09 §1.2): a versioned
    chrome<N> pin must agree with the UA's Chrome major — now guarded at
    the TRANSPORT level so every profile load (Session, doctor,
    fingerprint) refuses it, not just the freeze command."""

    def test_impersonate_major_mismatch_refused_exact_message(self):
        p = ClientProfile(impersonate="chrome124")
        assert p.validate_coherence() == [
            "impersonate target chrome124 disagrees with UA Chrome/136 major"]

    def test_impersonate_with_ua_lacking_chrome_major_refused(self):
        p = ClientProfile(user_agent="Mozilla/5.0 (X11; Linux x86_64)")
        assert p.validate_coherence() == [
            "impersonate target chrome136 with a UA carrying "
            "no Chrome major is incoherent"]

    def test_unversioned_target_unguarded_no_false_positive(self):
        assert ClientProfile(impersonate="chrome").validate_coherence() == []

    def test_empty_target_unguarded_no_false_positive(self):
        assert ClientProfile(impersonate="").validate_coherence() == []

    def test_coherent_non_default_major_identity_passes(self):
        p = ClientProfile(impersonate="chrome131", user_agent=CHROME_131_UA,
                          sec_ch_ua=SEC_CH_UA_131)
        assert p.validate_coherence() == []

    def test_load_or_default_rejects_impersonate_mismatch(self, tmp_path):
        bad = ClientProfile(impersonate="chrome120")
        bad.save(tmp_path / "profile.json")
        with pytest.raises(ValueError):
            load_or_default(str(tmp_path / "profile.json"))
