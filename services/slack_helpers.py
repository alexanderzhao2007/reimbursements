"""Slack helpers: image download, user profile lookup, message posting.

These wrap the Slack Web API (via a slack_sdk WebClient) plus a direct HTTPS GET
for downloading private files — downloading a Slack-hosted file is not a Web API
method; it requires the bot token as a bearer header against the file's
`url_private`.
"""

import logging

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 30  # seconds


def download_file(file_obj: dict, bot_token: str) -> bytes:
    """Download a Slack-hosted file's raw bytes.

    Prefers `url_private_download` (forces an attachment response) and falls back
    to `url_private`. Raises ValueError if the file object has no URL, or if Slack
    returns an HTML page instead of the file — which is what happens when the
    token or `files:read` scope is wrong (Slack answers 200 with a login page
    rather than a proper error).
    """
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        raise ValueError("Slack file object has no url_private/url_private_download")

    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=_DOWNLOAD_TIMEOUT,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if content_type.startswith("text/html"):
        raise ValueError(
            "Slack returned HTML instead of the file — check the bot token and "
            "files:read scope"
        )
    return resp.content


def get_user_profile(client: WebClient, user_id: str) -> dict:
    """Look up a member's name and email via users.info.

    Requires the `users:read` and `users:read.email` scopes. Degrades gracefully:
    on API failure it returns None for each field so the pipeline can still record
    the submission (the caller decides how to display a missing value). Returns
    keys matching the Submission model: employee_name, employee_email.
    """
    try:
        resp = client.users_info(user=user_id)
        user = resp["user"]
        profile = user.get("profile", {})
        real_name = user.get("real_name") or profile.get("real_name")
        return {
            "employee_name": real_name or profile.get("display_name"),
            "employee_email": profile.get("email"),
        }
    except SlackApiError as e:
        logger.warning(
            "users.info failed for %s: %s", user_id, e.response.get("error")
        )
        return {"employee_name": None, "employee_email": None}


def post_message(client: WebClient, channel: str, text: str, blocks=None):
    """Post a message to a channel or DM. `text` is the fallback/notification text
    (also shown in notifications); `blocks` is optional Block Kit for richer
    formatting. Returns the Slack API response."""
    return client.chat_postMessage(channel=channel, text=text, blocks=blocks)
