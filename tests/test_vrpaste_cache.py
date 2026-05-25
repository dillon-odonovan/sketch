from sketch.vrpaste.cache import InMemoryVRPasteCacheStore


class TestInMemoryVRPasteCacheStore:
    def test_get_returns_none_for_unknown_id(self):
        store = InMemoryVRPasteCacheStore()
        assert store.get("gxmfscC1") is None

    def test_set_then_get_returns_entry(self):
        store = InMemoryVRPasteCacheStore()
        entry = store.set(
            "gxmfscC1",
            "https://pokepast.es/abc123",
            user_id=111,
            guild_id=222,
        )
        assert entry.pokepaste_url == "https://pokepast.es/abc123"
        assert entry.created_by_user_id == 111
        assert entry.created_by_guild_id == 222

        fetched = store.get("gxmfscC1")
        assert fetched is not None
        assert fetched.pokepaste_url == "https://pokepast.es/abc123"

    def test_set_overwrites_existing_entry(self):
        # Last-write-wins is the documented contract — a re-mint
        # naturally produces a new (still-valid) Pokepaste URL on race.
        store = InMemoryVRPasteCacheStore()
        store.set("gxmfscC1", "https://pokepast.es/old", user_id=111, guild_id=222)
        store.set("gxmfscC1", "https://pokepast.es/new", user_id=333, guild_id=444)
        fetched = store.get("gxmfscC1")
        assert fetched is not None
        assert fetched.pokepaste_url == "https://pokepast.es/new"
        assert fetched.created_by_user_id == 333

    def test_case_sensitive_keys(self):
        # VRPaste ids are case-sensitive at the source, so cache keys
        # must be too. abc and ABC must not collide.
        store = InMemoryVRPasteCacheStore()
        store.set("abc", "https://pokepast.es/lower", user_id=1, guild_id=1)
        store.set("ABC", "https://pokepast.es/upper", user_id=1, guild_id=1)
        assert store.get("abc").pokepaste_url == "https://pokepast.es/lower"
        assert store.get("ABC").pokepaste_url == "https://pokepast.es/upper"
