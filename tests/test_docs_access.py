from app.services.docs_access import normalize_path, sign_path, verify_signature


def test_sign_path_matches_shared_vector():
    # Same vector is asserted in polymarket-team's doc_link tests — the two
    # implementations MUST agree or Telegram links 404 on the dashboard.
    assert sign_path("test-secret", "markets/foo/bar.md") == "44414d34600f1dafa35111eb1bbb6d2c"


def test_verify_signature_roundtrip():
    sig = sign_path("s3cret", "portfolio/reviews/review-2026-07-15.md")
    assert verify_signature("s3cret", "portfolio/reviews/review-2026-07-15.md", sig)
    assert not verify_signature("s3cret", "portfolio/reviews/review-2026-07-15.md", sig[:-1] + "0")
    assert not verify_signature("s3cret", "portfolio/reviews/other.md", sig)


def test_verify_signature_rejects_empty_secret_or_sig():
    assert not verify_signature("", "markets/foo/bar.md", "abc")
    assert not verify_signature("test-secret", "markets/foo/bar.md", "")


def test_normalize_path_accepts_allowlisted_md():
    assert normalize_path("markets/foo/bar.md") == "markets/foo/bar.md"
    assert normalize_path("briefings/2026-W29-cluster-briefing.md") is not None
    assert normalize_path("retrospectives/2026-07-15-foo.md") is not None


def test_normalize_path_rejects_bad_paths():
    assert normalize_path("data/credentials/.env") is None          # not allowlisted, not .md
    assert normalize_path("markets/../data/credentials/.env") is None  # traversal
    assert normalize_path("/markets/foo.md") is None                 # absolute
    assert normalize_path("markets//foo.md") is None                 # empty segment
    assert normalize_path("markets/foo.txt") is None                 # not markdown
    assert normalize_path("") is None


def test_extensionless_url_resolves_to_md_path():
    # Route-level convention: /docs/<path-without-.md> resolves by appending
    # ".md" when the raw path fails normalization (Telegram download fix).
    raw = "markets/foo/bar"
    assert normalize_path(raw) is None
    assert normalize_path(raw + ".md") == "markets/foo/bar.md"
