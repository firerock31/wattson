"""Shared configuration for Wattson, the open-source Rivian tracker."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RIVIAN_DB_PATH", BASE / "rivian.db"))
TOKENS_PATH = Path(os.environ.get("RIVIAN_TOKENS_PATH", BASE / "tokens.enc"))
KEY_PATH = Path(os.environ.get("RIVIAN_TOKEN_KEY_PATH", BASE / ".token_key"))
STATE_PATH = BASE / "poller_state.json"
FLAG_REAUTH = BASE / "NEEDS_REAUTH"
# When a drive is expected, a watch file with an "until" epoch can be
# written here; poller.py captures at 1-min resolution until then,
# regardless of the cloud tier (which can lag and still say "asleep").
WATCH_PATH = BASE / "drive_watch.json"
# Burst-mode heartbeat: poller.py --burst touches this file on every poll.
# The overlap guard checks it (not the snapshots table), so a lone single
# poll can never false-positive as "previous burst still running".
BURST_HEARTBEAT_PATH = BASE / "burst_heartbeat"
BURST_PID_PATH = BASE / "burst.pid"
# Where the generated dashboard HTML goes. Override with RIVIAN_REPORT_PATH
# (the Docker setup points it at the served www/ directory).
REPORT_PATH = Path(os.environ.get(
    "RIVIAN_REPORT_PATH", BASE / "www" / "rivian-dashboard.html"))

# Home coordinates live in the environment (see .env.example), never in
# source. Needed to detect home charging sessions and compute home cost.
HOME_LAT = float(os.environ.get("RIVIAN_HOME_LAT", "nan"))
HOME_LON = float(os.environ.get("RIVIAN_HOME_LON", "nan"))
# Home charging cost in $/kWh, all-in for your utility rate. Used for the
# cost shown on home charging sessions.
HOME_KWH_RATE = float(os.environ.get("RIVIAN_HOME_KWH_RATE", "0.30"))
# Display name for the dashboard hero. Falls back to the vehicle name on
# your Rivian account, then to a generic label.
VEHICLE_NAME = os.environ.get("RIVIAN_VEHICLE_NAME", "")
VEHICLE_MODEL = os.environ.get("RIVIAN_VEHICLE_MODEL", "")
# Local timezone for dashboard dates/times (IANA name).
LOCAL_TZ = ZoneInfo(os.environ.get("WATTSON_TIMEZONE",
                                  "America/Los_Angeles"))

# Subset of GraphQL vehicle-state properties we poll (keeps payloads small).
# NOTE: "powerKW" is NOT a valid vehicleState field (Rivian returns
# GRAPHQL_VALIDATION_FAILED for it), so charge power is derived from the
# SoC curve instead of read live. Kept as a tuple for deterministic query
# order (a set randomizes field order per process via hash randomization,
# which made API errors harder to diagnose).
PROPS = (
    "batteryLevel",
    "batteryCapacity",
    "distanceToEmpty",
    "chargerState",
    "chargerStatus",
    "chargePortState",
    "powerState",
    "gearStatus",
    "gnssLocation",
    "geoLocation",
    "gnssSpeed",
    "vehicleMileage",
    "cloudConnection",
    "cabinPreconditioningStatus",
)

# Adaptive polling tiers (seconds). Reads hit Rivian's cloud cache, not the
# car, so fast polling while driving costs no extra battery: the car is
# already awake. We back off hard once it looks asleep.
POLL_INTERVAL_DRIVING = 30    # moving (odo/GPS): 30s trip resolution, car awake
POLL_INTERVAL_PARKED = 300    # parked but awake: catch drive starts quickly
POLL_INTERVAL_ASLEEP = 1800  # asleep: heartbeat only, don't delay deep sleep

# Trips shorter than this are noise (SoC resolution makes their Wh/mi junk);
# they roll into the daily "errands" aggregate instead of individual rows.
TRIP_MIN_SECONDS = 3 * 60

# gnssSpeed above this means the car is moving. Unit unconfirmed (m/s? mph?);
# kept small.
SPEED_DRIVE_EPS = 0.5

# Peak charge power at or above this means DC fast charging; below is AC
# (home wall chargers top out well under this on the R2).
DC_FAST_MIN_KW = 20.0

# Usable battery capacity (kWh) for energy math. Rivian rates the R2 at
# 87.9 kWh usable (their support page); the API's batteryCapacity=91.4 sits
# between usable and the ~94 kWh gross, so all SoC-derived kWh figures use
# 87.9. Cross-checked against the car's own trip meter (a 1.3% SoC
# drop read ~1.1 kWh, implying ~88 kWh usable, not 91.4).
USABLE_KWH = 87.9

# vehicleMileage unit: the API reports a raw number; a reading in the
# hundreds of thousands on a new vehicle is consistent with meters.
# Confirm against the in-app odometer.
ODOMETER_METERS_PER_UNIT = 1.0
