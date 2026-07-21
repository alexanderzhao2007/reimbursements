"""Minimal Slack Bolt Socket Mode app (Implementation Order step 4).

Connectivity check only: receives DM (`message.im`) events and logs whether a
file is attached, so we can confirm the app connects and receives DMs before
building the rest of the pipeline (image download, Vision, Supabase, modal).

Run from the repo root:  python -m app.main
"""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from utils import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reimbursements")

app = App(token=config.SLACK_BOT_TOKEN)


@app.event("message")
def handle_message(event, logger):
    """Log incoming DMs. Only DMs to the bot (channel_type == 'im') are the
    intake path; anything else is ignored for now."""
    if event.get("channel_type") != "im":
        return

    # A file upload arrives as a `message` event with subtype "file_share", so we
    # must let that through. Ignore other subtypes (edits, deletions, joins) and
    # the bot's own messages, which are noise for the intake pipeline.
    subtype = event.get("subtype")
    if subtype not in (None, "file_share"):
        return
    if event.get("bot_id"):
        return

    files = event.get("files") or []
    logger.info(
        "DM received: user=%s channel_type=im file_attached=%s files=%d subtype=%s",
        event.get("user"),
        bool(files),
        len(files),
        subtype,
    )


def main():
    logger.info("Starting reimbursement bot in Socket Mode...")
    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
