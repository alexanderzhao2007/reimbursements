"""Slack Bolt Socket Mode app — entry point and listener registration.

Wires the DM receipt-intake pipeline and the confirmation-modal handlers onto a
Socket Mode app (no public Request URL needed). Run from the repo root:

    python -m app.main
"""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.listeners import file_upload, modal_submit
from utils import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reimbursements")

app = App(token=config.SLACK_BOT_TOKEN)

file_upload.register(app)    # DM file upload → download, parse, post "Review & Submit"
modal_submit.register(app)   # button → modal; modal submit → confirm + notify finance


def main():
    logger.info("Starting reimbursement bot in Socket Mode...")
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
