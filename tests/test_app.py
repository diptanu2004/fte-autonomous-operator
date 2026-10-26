"""Streamlit UI smoke test: renders, and a run against an unreachable endpoint fails gracefully."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_app_renders_and_handles_bad_endpoint():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.selectbox[0].select("custom").run()
    at.text_input(key="url-custom").set_value("http://127.0.0.1:9/v1").run()
    at.text_input(key="model-custom").set_value("nope").run()
    at.text_input(key="key-custom").set_value("x").run()
    at.text_area(key="request").set_value("Process the invoices").run()
    next(b for b in at.button if b.label == "▶ Run").click().run()
    h = at.session_state["handle"]
    h.thread.join(timeout=240)
    assert not h.running
    assert any(e["event"] == "done" for e in h.events)
    assert h.agent.state.status == "failed"
    at.run()
    assert not at.exception
