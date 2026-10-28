"""Data type catalog for the Google Health API v4.

Everything here is derived from the official discovery document
(``https://health.googleapis.com/$discovery/rest?version=v4``).
"""

from __future__ import annotations

import enum
from typing import Dict, FrozenSet


class DataType(str, enum.Enum):
    """Data type identifiers (kebab-case, as used in resource names)."""

    ACTIVE_ENERGY_BURNED = "active-energy-burned"
    ACTIVE_MINUTES = "active-minutes"
    ACTIVE_ZONE_MINUTES = "active-zone-minutes"
    ACTIVITY_LEVEL = "activity-level"
    ALTITUDE = "altitude"
    BASAL_ENERGY_BURNED = "basal-energy-burned"
    BLOOD_GLUCOSE = "blood-glucose"
    BODY_FAT = "body-fat"
    CALORIES_IN_HEART_RATE_ZONE = "calories-in-heart-rate-zone"
    CORE_BODY_TEMPERATURE = "core-body-temperature"
    DAILY_HEART_RATE_VARIABILITY = "daily-heart-rate-variability"
    DAILY_HEART_RATE_ZONES = "daily-heart-rate-zones"
    DAILY_OXYGEN_SATURATION = "daily-oxygen-saturation"
    DAILY_RESPIRATORY_RATE = "daily-respiratory-rate"
    DAILY_RESTING_HEART_RATE = "daily-resting-heart-rate"
    DAILY_SLEEP_TEMPERATURE_DERIVATIONS = "daily-sleep-temperature-derivations"
    DAILY_VO2_MAX = "daily-vo2-max"
    DISTANCE = "distance"
    ELECTROCARDIOGRAM = "electrocardiogram"
    EXERCISE = "exercise"
    FLOORS = "floors"
    HEART_RATE = "heart-rate"
    HEART_RATE_VARIABILITY = "heart-rate-variability"
    HEIGHT = "height"
    HYDRATION_LOG = "hydration-log"
    IRREGULAR_RHYTHM_NOTIFICATION = "irregular-rhythm-notification"
    MENSTRUAL_PERIOD = "menstrual-period"
    MOODS = "moods"
    NUTRITION_LOG = "nutrition-log"
    OVULATION_TEST = "ovulation-test"
    OXYGEN_SATURATION = "oxygen-saturation"
    RESPIRATORY_RATE_SLEEP_SUMMARY = "respiratory-rate-sleep-summary"
    RUN_VO2_MAX = "run-vo2-max"
    SEDENTARY_PERIOD = "sedentary-period"
    SLEEP = "sleep"
    STEPS = "steps"
    SWIM_LENGTHS_DATA = "swim-lengths-data"
    SYMPTOMS = "symptoms"
    TIME_IN_HEART_RATE_ZONE = "time-in-heart-rate-zone"
    TOTAL_CALORIES = "total-calories"
    VO2_MAX = "vo2-max"
    WEIGHT = "weight"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def snake(self) -> str:
        """snake_case name, as used in filter expressions."""
        return self.value.replace("-", "_")

    @property
    def camel(self) -> str:
        """camelCase name, as used for the payload key inside a DataPoint."""
        head, *rest = self.value.split("-")
        return head + "".join(p.capitalize() for p in rest)

    @property
    def kind(self) -> DataKind:
        return DATA_KINDS[self]


class DataKind(str, enum.Enum):
    """How a data type's points are anchored in time (drives filter syntax)."""

    INTERVAL = "interval"
    SAMPLE = "sample"
    DAILY = "daily"
    SESSION = "session"
    #: Only available through roll-ups (no raw data points).
    ROLLUP_ONLY = "rollup_only"


class DataSourceFamily(str, enum.Enum):
    ALL_SOURCES = "all-sources"
    GOOGLE_WEARABLES = "google-wearables"
    GOOGLE_SOURCES = "google-sources"
    SELF_SOURCES = "self-sources"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


_I, _S, _D, _X = DataKind.INTERVAL, DataKind.SAMPLE, DataKind.DAILY, DataKind.SESSION

DATA_KINDS: Dict[DataType, DataKind] = {
    DataType.ACTIVE_ENERGY_BURNED: _I,
    DataType.ACTIVE_MINUTES: _I,
    DataType.ACTIVE_ZONE_MINUTES: _I,
    DataType.ACTIVITY_LEVEL: _D,
    DataType.ALTITUDE: _I,
    DataType.BASAL_ENERGY_BURNED: _I,
    DataType.BLOOD_GLUCOSE: _S,
    DataType.BODY_FAT: _S,
    DataType.CALORIES_IN_HEART_RATE_ZONE: DataKind.ROLLUP_ONLY,
    DataType.CORE_BODY_TEMPERATURE: _S,
    DataType.DAILY_HEART_RATE_VARIABILITY: _D,
    DataType.DAILY_HEART_RATE_ZONES: _D,
    DataType.DAILY_OXYGEN_SATURATION: _D,
    DataType.DAILY_RESPIRATORY_RATE: _D,
    DataType.DAILY_RESTING_HEART_RATE: _D,
    DataType.DAILY_SLEEP_TEMPERATURE_DERIVATIONS: _D,
    DataType.DAILY_VO2_MAX: _D,
    DataType.DISTANCE: _I,
    DataType.ELECTROCARDIOGRAM: _X,
    DataType.EXERCISE: _X,
    DataType.FLOORS: _I,
    DataType.HEART_RATE: _S,
    DataType.HEART_RATE_VARIABILITY: _S,
    DataType.HEIGHT: _S,
    DataType.HYDRATION_LOG: _X,
    DataType.IRREGULAR_RHYTHM_NOTIFICATION: _X,
    DataType.MENSTRUAL_PERIOD: _I,
    DataType.MOODS: _S,
    DataType.NUTRITION_LOG: _X,
    DataType.OVULATION_TEST: _S,
    DataType.OXYGEN_SATURATION: _S,
    DataType.RESPIRATORY_RATE_SLEEP_SUMMARY: _S,
    DataType.RUN_VO2_MAX: _S,
    DataType.SEDENTARY_PERIOD: _I,
    DataType.SLEEP: _X,
    DataType.STEPS: _I,
    DataType.SWIM_LENGTHS_DATA: _I,
    DataType.SYMPTOMS: _S,
    DataType.TIME_IN_HEART_RATE_ZONE: _I,
    DataType.TOTAL_CALORIES: DataKind.ROLLUP_ONLY,
    DataType.VO2_MAX: _S,
    DataType.WEIGHT: _S,
}

#: Data types accepted by ``dataPoints:rollUp`` (physical-time windows).
ROLLUP_TYPES: FrozenSet[DataType] = frozenset(
    DataType(v)
    for v in (
        "active-energy-burned active-minutes active-zone-minutes activity-level altitude "
        "blood-glucose body-fat calories-in-heart-rate-zone core-body-temperature distance "
        "floors heart-rate hydration-log nutrition-log run-vo2-max sedentary-period steps "
        "swim-lengths-data time-in-heart-rate-zone total-calories weight"
    ).split()
)

#: Data types accepted by ``dataPoints:dailyRollUp`` (civil-day windows).
DAILY_ROLLUP_TYPES: FrozenSet[DataType] = ROLLUP_TYPES | {
    DataType.DAILY_HEART_RATE_VARIABILITY,
    DataType.DAILY_RESTING_HEART_RATE,
}

#: Data types whose roll-up range is limited to 14 days (90 days for the rest).
SHORT_RANGE_ROLLUP_TYPES: FrozenSet[DataType] = frozenset(
    {
        DataType.CALORIES_IN_HEART_RATE_ZONE,
        DataType.HEART_RATE,
        DataType.ACTIVE_MINUTES,
        DataType.TOTAL_CALORIES,
    }
)

#: Data types that support webhook subscriptions (per the Subscription schema).
SUBSCRIBABLE_TYPES: FrozenSet[DataType] = frozenset(
    DataType(v) for v in ("altitude", "distance", "floors", "sleep", "steps", "weight")
)


def max_rollup_days(data_type: DataType | str) -> int:
    return 14 if DataType(data_type) in SHORT_RANGE_ROLLUP_TYPES else 90


class Scope:
    """OAuth 2.0 scopes for the Google Health API."""

    _P = "https://www.googleapis.com/auth/googlehealth."
    ACTIVITY_AND_FITNESS_READ = _P + "activity_and_fitness.readonly"
    ACTIVITY_AND_FITNESS_WRITE = _P + "activity_and_fitness.writeonly"
    ECG_READ = _P + "ecg.readonly"
    HEALTH_METRICS_READ = _P + "health_metrics_and_measurements.readonly"
    HEALTH_METRICS_WRITE = _P + "health_metrics_and_measurements.writeonly"
    IRN_READ = _P + "irn.readonly"
    LOCATION_READ = _P + "location.readonly"
    LOGGED_SYMPTOMS_READ = _P + "logged_symptoms.readonly"
    LOGGED_SYMPTOMS_WRITE = _P + "logged_symptoms.writeonly"
    MINDFULNESS_READ = _P + "mindfulness.readonly"
    MINDFULNESS_WRITE = _P + "mindfulness.writeonly"
    NUTRITION_WRITE = _P + "nutrition.writeonly"
    PROFILE_READ = _P + "profile.readonly"
    PROFILE_WRITE = _P + "profile.writeonly"
    REPRODUCTIVE_HEALTH_READ = _P + "reproductive_health.readonly"
    REPRODUCTIVE_HEALTH_WRITE = _P + "reproductive_health.writeonly"
    SETTINGS_READ = _P + "settings.readonly"
    SETTINGS_WRITE = _P + "settings.writeonly"
    SLEEP_READ = _P + "sleep.readonly"
    SLEEP_WRITE = _P + "sleep.writeonly"
    CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"

    #: A sensible read-only bundle covering what most Fitbit Web API apps used.
    READ_COMMON = (
        ACTIVITY_AND_FITNESS_READ,
        HEALTH_METRICS_READ,
        SLEEP_READ,
        PROFILE_READ,
        SETTINGS_READ,
    )
