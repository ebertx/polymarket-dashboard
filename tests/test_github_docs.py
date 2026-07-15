from app.services.github_docs import _cache_get, _cache_put


def test_cache_hit_within_ttl():
    cache = {}
    _cache_put(cache, "a.md", now=100.0, value="hello")
    assert _cache_get(cache, "a.md", now=150.0, ttl=60.0) == "hello"


def test_cache_miss_after_ttl():
    cache = {}
    _cache_put(cache, "a.md", now=100.0, value="hello")
    assert _cache_get(cache, "a.md", now=161.0, ttl=60.0) is None


def test_cache_miss_unknown_key():
    assert _cache_get({}, "nope.md", now=0.0, ttl=60.0) is None
