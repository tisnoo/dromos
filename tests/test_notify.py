import hashlib
import hmac
import json
from datetime import date

import httpx
import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from conftest import raw_run, raw_sleep
from test_plan import make_plan

from dromos import db, notify, plan
from dromos.garmin import normalise, normalise_sleep
from dromos.scheduler import add_jobs

TODAY = date(2026, 11, 9)  # Monday of plan week 1: an easy 5 km + gym


@pytest.fixture
def sent(monkeypatch):
    """Captures POSTs to Hermes instead of sending them."""
    monkeypatch.setenv("DROMOS_HERMES_WEBHOOK_URL", "http://hermes.test/webhooks/")
    monkeypatch.setenv("DROMOS_HERMES_WEBHOOK_SECRET", "s3cret")
    requests = []

    def fake_post(url, content, headers, timeout):
        requests.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    return requests


def add_run(conn, garmin_id, start):
    raw = raw_run(garmin_id, start, aerobicTrainingEffect=2.8, trainingEffectLabel="AEROBIC_BASE")
    run = normalise(raw)
    db.upsert_activity(conn, run, raw)
    return run


def load(conn):
    plan.import_plan(conn, make_plan(weeks=2), reason="test")
    night = raw_sleep("2026-11-09", score=72)
    db.upsert_sleep(conn, normalise_sleep(night), night)


def test_new_run_is_sent_with_plan_and_sleep(conn, sent):
    load(conn)
    run = add_run(conn, 1, "2026-11-09 07:00:00")
    notify.notify_new_runs(conn, [run], today=TODAY)

    assert len(sent) == 1
    request = sent[0]
    assert request["url"] == "http://hermes.test/webhooks/dromos-run"
    assert request["headers"]["X-Request-ID"] == "dromos-run-1"
    payload = json.loads(request["body"])
    assert payload["event_type"] == "run_ingested"
    assert payload["run"]["distance_km"] == 10.0
    assert payload["run"]["training_effect_label"] == "AEROBIC_BASE"
    assert "vo2max" not in payload["run"]  # None values are dropped
    assert [s["title"] for s in payload["planned"]] == ["Easy run 5 km", "Gym"]
    assert payload["this_week"]["actual_km"] == 10.0
    assert payload["last_night"]["score"] == 72


def test_request_is_signed_like_hermes_expects(conn, sent):
    load(conn)
    notify.send_daily_brief(conn, TODAY)
    headers, body = sent[0]["headers"], sent[0]["body"]
    expected = hmac.new(
        b"s3cret", headers["X-Webhook-Timestamp"].encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Webhook-Signature-V2"] == expected


def test_backfilled_runs_are_not_sent(conn, sent):
    load(conn)
    old = add_run(conn, 1, "2026-11-01 07:00:00")
    yesterday = add_run(conn, 2, "2026-11-08 07:00:00")
    notify.notify_new_runs(conn, [old, yesterday], today=TODAY)
    assert [r["headers"]["X-Request-ID"] for r in sent] == ["dromos-run-2"]


def test_nothing_is_sent_without_settings(conn, sent, monkeypatch):
    monkeypatch.delenv("DROMOS_HERMES_WEBHOOK_SECRET")
    load(conn)
    notify.notify_new_runs(conn, [add_run(conn, 1, "2026-11-09 07:00:00")], today=TODAY)
    assert not notify.send_daily_brief(conn, TODAY)
    assert sent == []


def test_daily_brief_has_today_the_week_ahead_and_yesterday(conn, sent):
    load(conn)
    add_run(conn, 1, "2026-11-08 07:00:00")
    assert notify.send_daily_brief(conn, TODAY)

    assert sent[0]["url"].endswith("/dromos-daily")
    assert sent[0]["headers"]["X-Request-ID"] == "dromos-daily-2026-11-09"
    payload = json.loads(sent[0]["body"])
    assert payload["event_type"] == "daily_brief"
    assert payload["date"] == "2026-11-09"
    assert payload["today"][0]["title"] == "Easy run 5 km"
    assert payload["next_days"]["Tue 2026-11-10"] == "rest"
    assert payload["next_days"]["Mon 2026-11-16"] == "easy: Easy run 5 km; gym: Gym"
    assert len(payload["next_days"]) == 7
    assert payload["yesterday_planned"] == "rest"  # before the plan starts
    assert payload["yesterday_runs"][0]["distance_km"] == 10.0
    assert payload["this_week"]["week"] == 1
    assert len(sent[0]["body"]) < 4000  # Hermes cuts {__raw__} off at 4000 chars


def test_failed_send_is_logged_not_raised(conn, sent, monkeypatch, caplog):
    def refused(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(notify.httpx, "post", refused)
    load(conn)
    assert not notify.send_daily_brief(conn, TODAY)
    assert "Sending dromos-daily-2026-11-09 to Hermes failed" in caplog.text


def test_daily_brief_runs_at_six_in_local_time(monkeypatch):
    monkeypatch.setenv("DROMOS_TIMEZONE", "Europe/Amsterdam")
    job = add_jobs(BackgroundScheduler()).get_job("daily_brief")
    assert str(job.trigger) == "cron[hour='6']"
    assert str(job.trigger.timezone) == "Europe/Amsterdam"
