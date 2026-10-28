"""FastAPI webhook receiver.

    pip install fastapi uvicorn "google-health[webhooks]"
    uvicorn fastapi_webhook:app
"""

import os

from fastapi import BackgroundTasks, FastAPI, Request, Response

from google_health.webhooks import Notification, WebhookReceiver

app = FastAPI()
receiver = WebhookReceiver(os.environ["HEALTH_WEBHOOK_SECRET"])


async def process(n: Notification) -> None:
    ...  # fetch the changed range with AsyncHealthClient for n.health_user_id


@app.post("/health-webhook")
async def health_webhook(request: Request, tasks: BackgroundTasks) -> Response:
    result = receiver.handle(request.headers, await request.body())
    for n in result.notifications:
        tasks.add_task(process, n)
    return Response(status_code=result.status_code)
