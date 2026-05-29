import pytest

from sketch.pokepaste.validator import ValidationError
from sketch.vrpaste.cache import InMemoryVRPasteCacheStore, lookup_pokepaste_url


class TestInMemoryVRPasteCacheStore:
    def test_get_returns_none_for_unknown_id(self):
        store = InMemoryVRPasteCacheStore()
        assert store.get("gxmfscC1") is None

    def test_create_then_get_returns_entry(self):
        store = InMemoryVRPasteCacheStore()
        entry = store.create(
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

    def test_create_is_fail_if_exists_returns_winner(self):
        # Race convergence: the second create returns the FIRST entry,
        # not the new one. That's what gives the resolver a single URL
        # to write to the sheet so dedup catches the second submission.
        store = InMemoryVRPasteCacheStore()
        winner = store.create(
            "gxmfscC1",
            "https://pokepast.es/winner",
            user_id=111,
            guild_id=222,
        )
        loser = store.create(
            "gxmfscC1",
            "https://pokepast.es/loser",
            user_id=333,
            guild_id=444,
        )
        # Loser receives the winner's entry verbatim — same URL, same audit fields.
        assert loser.pokepaste_url == "https://pokepast.es/winner"
        assert loser.created_by_user_id == 111
        assert loser.created_by_guild_id == 222
        assert loser == winner
        # And the stored entry stays the winner's.
        stored = store.get("gxmfscC1")
        assert stored is not None
        assert stored.pokepaste_url == "https://pokepast.es/winner"

    def test_case_sensitive_keys(self):
        # VRPaste ids are case-sensitive at the source, so cache keys
        # must be too. abc and ABC must not collide.
        store = InMemoryVRPasteCacheStore()
        store.create("abc", "https://pokepast.es/lower", user_id=1, guild_id=1)
        store.create("ABC", "https://pokepast.es/upper", user_id=1, guild_id=1)
        assert store.get("abc").pokepaste_url == "https://pokepast.es/lower"
        assert store.get("ABC").pokepaste_url == "https://pokepast.es/upper"


class TestLookupPokepasteUrl:
    def test_cache_hit_returns_pokepaste_url(self):
        store = InMemoryVRPasteCacheStore()
        store.create(
            "gxmfscC1",
            "https://pokepast.es/minted",
            user_id=1,
            guild_id=2,
        )
        assert (
            lookup_pokepaste_url("https://www.vrpastes.com/gxmfscC1", store)
            == "https://pokepast.es/minted"
        )

    def test_cache_miss_returns_none(self):
        store = InMemoryVRPasteCacheStore()
        assert lookup_pokepaste_url("https://www.vrpastes.com/unknown", store) is None

    def test_non_vrpaste_url_raises(self):
        store = InMemoryVRPasteCacheStore()
        with pytest.raises(ValidationError):
            lookup_pokepaste_url("https://pokepast.es/abc", store)
