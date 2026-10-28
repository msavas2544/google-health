"""Print a week of activity, sleep and resting heart rate.

    google-health login
    python sync_last_week.py
"""

import datetime as dt
from pathlib import Path

from google_health import DataType, HealthClient, OAuthCredentials

TOKEN = Path.home() / ".config" / "google-health" / "token.json"

with HealthClient(OAuthCredentials.load(TOKEN)) as client:
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=7)

    steps = {d.date: d.value for d in client.daily(DataType.STEPS, start, end)}
    dist = {d.date: d.value for d in client.daily(DataType.DISTANCE, start, end)}
    rhr = {}
    for p in client.data_points.list(DataType.DAILY_RESTING_HEART_RATE, start, end):
        v = p["dailyRestingHeartRate"]
        rhr[dt.date(**v["date"])] = int(v["beatsPerMinute"])
    sleep = {}
    for p in client.data_points.list(DataType.SLEEP, start, end, civil=True):
        s = p["sleep"]
        if s.get("metadata", {}).get("mainSleep"):
            e = s["interval"]["civilEndTime"]["date"]
            sleep[dt.date(**e)] = int(s["summary"]["minutesAsleep"]) / 60

    print(f"{'date':<12}{'steps':>8}{'km':>8}{'RHR':>6}{'sleep h':>9}")
    d = start
    while d < end:
        print(f"{d.isoformat():<12}{int(steps.get(d) or 0):>8}"
              f"{(dist.get(d) or 0) / 1000:>8.2f}{rhr.get(d, '-'):>6}"
              f"{sleep.get(d, 0):>9.1f}")
        d += dt.timedelta(days=1)
