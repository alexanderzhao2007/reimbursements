"""Slack helpers: file download and profile lookup (HTTP + WebClient mocked)."""

from unittest.mock import MagicMock

import pytest
import requests
from slack_sdk.errors import SlackApiError

from services import slack_helpers


def _http_resp(content=b"bytes", content_type="image/jpeg", status=200):
    resp = MagicMock()
    resp.content = content
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status.side_effect = (
        requests.HTTPError(f"{status}") if status >= 400 else None
    )
    return resp


@pytest.fixture
def fake_get(monkeypatch):
    get = MagicMock(return_value=_http_resp())
    monkeypatch.setattr(slack_helpers.requests, "get", get)
    return get


def test_download_prefers_url_private_download_and_sends_bearer(fake_get):
    data = slack_helpers.download_file(
        {"url_private": "https://a", "url_private_download": "https://b"}, "xoxb-1"
    )
    assert data == b"bytes"
    args, kwargs = fake_get.call_args
    assert args[0] == "https://b"
    assert kwargs["headers"] == {"Authorization": "Bearer xoxb-1"}
    assert kwargs["timeout"]


def test_download_falls_back_to_url_private(fake_get):
    slack_helpers.download_file({"url_private": "https://a"}, "t")
    assert fake_get.call_args.args[0] == "https://a"


def test_download_without_url_raises(fake_get):
    with pytest.raises(ValueError):
        slack_helpers.download_file({}, "t")
    fake_get.assert_not_called()


def test_download_html_login_page_raises(fake_get):
    fake_get.return_value = _http_resp(b"<html>", "text/html; charset=utf-8")
    with pytest.raises(ValueError, match="HTML"):
        slack_helpers.download_file({"url_private": "https://a"}, "t")


def test_download_http_error_raises(fake_get):
    fake_get.return_value = _http_resp(status=403)
    with pytest.raises(requests.HTTPError):
        slack_helpers.download_file({"url_private": "https://a"}, "t")


def test_profile_happy_path(slack_client):
    assert slack_helpers.get_user_profile(slack_client, "U1") == {
        "employee_name": "Ada Lovelace", "employee_email": "ada@example.com",
    }


def test_profile_falls_back_to_display_name(slack_client):
    slack_client.users_info.return_value = {"user": {"profile": {"display_name": "ada"}}}
    assert slack_helpers.get_user_profile(slack_client, "U1") == {
        "employee_name": "ada", "employee_email": None,
    }


def test_profile_api_failure_degrades_to_none(slack_client):
    slack_client.users_info.side_effect = SlackApiError("x", {"error": "user_not_found"})
    assert slack_helpers.get_user_profile(slack_client, "U1") == {
        "employee_name": None, "employee_email": None,
    }
