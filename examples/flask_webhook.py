"""Minimal Flask webhook receiver for Google Health API notifications.

    pip install flask "google-health[webhooks]"
    HEALTH_WEBHOOK_SECRET="Bearer $(openssl rand -hex 32)" flask --app flask_webhook run

Respond fast (Google expects 204 quickly) and do the actual data fetching in a
background worker – notifications only say *what changed*, not the values.
"""

import os
import queue
import threading

from flask import Flask, request

from google_health.webhooks import WebhookReceiver

app = Flask(__name__)
# During secret rotation pass both: WebhookReceiver([old, new])
receiver = WebhookReceiver(os.environ["HEALTH_WEBHOOK_SECRET"])
jobs: "queue.Queue" = queue.Queue()


@app.post("/health-webhook")
def health_webhook():
    result = receiver.handle(request.headers, request.get_data())
    for n in result.notifications:
        jobs.put(n)
    return "", result.status_code


def worker() -> None:
    while True:
        n = jobs.get()
        # Idempotent processing: Google retries for up to 7 days, duplicates happen.
        print(f"{n.operation} {n.data_type} for {n.health_user_id}: "
              f"{[(i.civil_start, i.civil_end) for i in n.intervals]}")


threading.Thread(target=worker, daemon=True).start()
