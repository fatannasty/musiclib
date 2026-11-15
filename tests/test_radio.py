from musiclib.models import Track
from musiclib.radio import radio_tracks


def test_radio_uses_artist_and_genre_without_repeat_or_unrelated_tracks():
    seed = Track(id=1, title="Seed", artist="Band", genre="Jazz; Soul")
    tracks = [
        seed,
        Track(id=2, title="Same artist", artist="BAND", genre="Rock"),
        Track(id=3, title="Same genre", artist="Another", genre="soul"),
        Track(id=4, title="Both", artist="Band", genre="Jazz"),
        Track(id=5, title="Same genre (Copy)", artist="Another", genre="Soul"),
        Track(id=6, title="Unrelated", artist="Unknown", genre="Metal"),
        Track(id=7, title="Bad", artist="Band", status="bad"),
        Track(id=8, title="Seed", artist="Band", genre="Jazz"),
    ]
    assert [track.id for track in radio_tracks(seed, tracks)] == [4, 3, 2]
    assert [track.id for track in radio_tracks(seed, tracks, limit=1)] == [4]


def test_radio_does_not_infer_similarity_from_unknown_artist():
    seed = Track(id=1, title="Seed", artist="Unknown Artist")
    assert radio_tracks(seed, [Track(id=2, title="Other", artist="Unknown Artist")]) == []


def test_radio_uses_available_copy_when_earlier_copy_is_missing():
    seed = Track(id=1, title="Seed", artist="Band")
    tracks = [Track(id=2, title="Song", artist="Band"),
              Track(id=3, title="Song", artist="Band")]
    assert [track.id for track in radio_tracks(seed, tracks, available=lambda t: t.id != 2)] == [3]
