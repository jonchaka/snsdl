import json

from app.state import AppState


def test_defaults_when_file_missing(settings):
    st = AppState(settings.state_file)
    st.load()
    assert st.queue_paused is False
    assert not settings.state_file.exists()


def test_set_persists_and_reloads(settings):
    st = AppState(settings.state_file)
    st.load()
    st.set_queue_paused(True)
    assert st.queue_paused is True
    assert json.loads(settings.state_file.read_text()) == {"queue_paused": True}
    assert not settings.state_file.with_suffix(".json.tmp").exists()
    again = AppState(settings.state_file)
    again.load()
    assert again.queue_paused is True
    again.set_queue_paused(False)
    assert json.loads(settings.state_file.read_text()) == {"queue_paused": False}


def test_corrupt_or_foreign_file_falls_back_to_defaults(settings):
    settings.state_file.write_text("{not json")
    st = AppState(settings.state_file)
    st.load()
    assert st.queue_paused is False
    settings.state_file.write_text(json.dumps({"queue_paused": "yes please", "other": 1}))
    st.load()
    assert st.queue_paused is True   # truthy coerced with bool()
    settings.state_file.write_text(json.dumps([1, 2]))
    st.load()
    assert st.queue_paused is False
