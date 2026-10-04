#!/usr/bin/env python3
"""Generate fictional demo data for Wattson.

Usage:
    python demo.py [--out demo-data]

Builds a 10-day fictional driving history for a Demo R2 based in Venice,
Los Angeles (Rivian Space Venice as home base), runs the real derive +
report pipeline on it, and writes a dashboard stamped with a demo banner.

All data is synthetic. No Rivian account needed. Place names are real;
the driver and the history are fictional.
"""

import argparse
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Fictional setting
# ---------------------------------------------------------------------------
TZ = ZoneInfo("America/Los_Angeles")
HOME_KEY = "home"
VEHICLE = "Demo R2"

# (label, lat, lon) — geocoded Oct 2026 via Nominatim. Places are real;
# the driver and history are fictional.
PLACES = {
    "home": ("Home", 33.99202, -118.47159),
    "antico_vinaio": ("All'Antico Vinaio", 34.06365, -118.30879),
    "cafe_gratitude": ("Cafe Gratitude", 33.99801, -118.47378),
    "whole_foods_venice": ("Whole Foods Venice", 34.00124, -118.46974),
    "sm_pier": ("Santa Monica Pier", 34.00890, -118.49740),
    "lobster_sm": ("The Lobster", 34.00608, -118.49146),
    "urth_sm": ("Urth Caffe Santa Monica", 34.00473, -118.48631),
    "disneyland": ("Disneyland", 33.81365, -117.91974),
    "newport_beach": ("Balboa Pier", 33.60000, -117.90041),
    "fashion_island": ("Fashion Island", 33.61604, -117.87548),
    "ran_oc": ("Rivian Adventure Network", 33.64000, -117.86000),
    "griffith": ("Griffith Observatory", 34.11822, -118.30029),
    "kanomwaan": ("Kanomwaan", 34.10218, -118.30550),
    "getty": ("Getty Center", 34.07702, -118.47401),
    "shin_sushi": ("Shin Sushi", 34.15876, -118.49426),
    "ranch_99": ("99 Ranch Market", 34.05605, -118.44186),
    "lax": ("LAX", 33.94217, -118.42136),
    "century_city": ("Westfield Century City", 34.05891, -118.41935),
    "erewhon_bh": ("Erewhon Beverly Hills", 34.06911, -118.40136),
    "grove": ("The Grove", 34.07260, -118.35835),
    "antico_nuovo": ("Antico Nuovo", 34.07632, -118.31070),
    "lacma": ("LACMA", 34.06374, -118.35894),
    "pink_wall": ("Paul Smith Pink Wall", 34.08076, -118.38959),
    "lucky_tiki": ("The Lucky Tiki", 34.08984, -118.37545),
    "urth_melrose": ("Urth Caffe Melrose", 34.08207, -118.37885),
    "camarillo_outlets": ("Camarillo Premium Outlets", 34.21562, -119.06461),
    "institution_ale": ("Institution Ale Co.", 34.21645, -119.01936),
    "conejo_garden": ("Conejo Valley Botanic Garden", 34.19172, -118.88545),
}

# Itinerary: each day is a list of (place_key, dwell_minutes).
# Drives are inferred between consecutive stops. Day 4's ran_oc is a
# charging stop (handled specially).
ITINERARY = [
    # Day 1: Venice errands
    [("home", 0), ("antico_vinaio", 70), ("cafe_gratitude", 35),
     ("whole_foods_venice", 50), ("home", 0)],
    # Day 2: Santa Monica
    [("home", 0), ("sm_pier", 90), ("lobster_sm", 70), ("urth_sm", 30),
     ("home", 0)],
    # Day 3: Disneyland
    [("home", 0), ("disneyland", 480), ("home", 0)],
    # Day 4: Newport Beach + RAN fast charge
    [("home", 0), ("newport_beach", 120), ("fashion_island", 90),
     ("ran_oc", -1), ("home", 0)],
    # Day 5: Griffith hike
    [("home", 0), ("griffith", 150), ("kanomwaan", 30), ("home", 0)],
    # Day 6: Getty + Valley
    [("home", 0), ("getty", 150), ("shin_sushi", 75), ("ranch_99", 40),
     ("home", 0)],
    # Day 7: LAX dropoff + Century City
    [("home", 0), ("lax", 20), ("century_city", 120), ("erewhon_bh", 35),
     ("home", 0)],
    # Day 8: Grove + dinner (afternoon start)
    [("home", 0), ("grove", 120), ("antico_nuovo", 100), ("home", 0)],
    # Day 9: Museums + WeHo
    [("home", 0), ("lacma", 120), ("pink_wall", 20), ("lucky_tiki", 45),
     ("urth_melrose", 30), ("home", 0)],
    # Day 10: Camarillo outlets
    [("home", 0), ("camarillo_outlets", 150), ("institution_ale", 60),
     ("conejo_garden", 60), ("home", 0)],
]

DAY_START_HOUR = {8: 14}  # day 8 (0-indexed) starts at 2pm for dinner

# Nights to skip home charging on (0-indexed) so overnight drain shows.
DRAIN_NIGHTS = {1, 4, 7}

# Physics (plausible R2 figures)
WH_PER_MI = 320.0
USABLE_KWH = 87.9
HOME_KW = 11.0
CHARGE_TARGET = 90.0
RATED_RANGE_MI = 336.0


def haversine_mi(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(h))


class Sim:
    """Generates synthetic snapshots, tracking SoC/odometer/time."""

    def __init__(self, start_ts):
        self.ts = start_ts
        self.soc = 90.0
        self.odo_m = 1314 * 1609.344
        self.lat, self.lon = PLACES["home"][1], PLACES["home"][2]
        self.snaps = []
        self.rng = random.Random(42)
        self.day_index = 0

    def cap_kwh(self):
        # Slight pack degradation over the demo window (for the trend chart)
        return 91.5 - self.day_index * 0.015

    def emit(self, gear, speed=None, charger_state=None, power_kw=None,
             geo_label=None, cloud_online=1):
        self.snaps.append({
            "ts": int(self.ts),
            "soc": round(self.soc, 2),
            "capacity_kwh": round(self.cap_kwh(), 2),
            "range_mi": round(self.soc / 100 * RATED_RANGE_MI, 1),
            "charger_state": charger_state,
            "charge_port": None,
            "power_kw": power_kw,
            "power_state": None,
            "gear": gear,
            "lat": round(self.lat, 5),
            "lon": round(self.lon, 5),
            "speed": round(speed, 1) if speed is not None else None,
            "geo_label": geo_label,
            "odometer": round(self.odo_m, 1),
            "cloud_online": cloud_online,
            "cloud_sync_ts": int(self.ts),
        })

    def drive_to(self, key):
        label, tlat, tlon = PLACES[key]
        dist = haversine_mi((self.lat, self.lon), (tlat, tlon))
        if dist < 0.05:
            return
        speed = 52 if dist > 15 else 27
        dur = dist / speed * 3600
        steps = max(2, int(dur / 120))
        flat, flon = self.lat, self.lon
        for i in range(1, steps + 1):
            f = i / steps
            # Slight route wobble so maps don't look laser-straight
            wob = math.sin(f * math.pi) * 0.004
            self.lat = flat + (tlat - flat) * f + wob * self.rng.uniform(-1, 1)
            self.lon = flon + (tlon - flon) * f + wob * self.rng.uniform(-1, 1)
            mi_step = dist / steps
            self.odo_m += mi_step * 1609.344
            self.soc -= mi_step * WH_PER_MI / 1000 / USABLE_KWH * 100
            self.ts += dur / steps
            self.emit(gear="D", speed=speed * self.rng.uniform(0.85, 1.1))
        self.lat, self.lon = tlat, tlon
        # Parked arrival snapshot
        self.emit(gear="P", geo_label=label)

    def dwell(self, minutes, label):
        if minutes <= 0:
            return
        # Mid-dwell heartbeats: car is asleep by now (arrival was awake).
        # 10-min cadence keeps gaps under the 15-min unknown threshold.
        elapsed = 0
        while elapsed + 10 < minutes:
            self.ts += 10 * 60
            elapsed += 10
            self.emit(gear="P", geo_label=label, cloud_online=0)
        self.ts += (minutes - elapsed) * 60
        # Departure snapshot: trip bracket lands at drive start.
        self.emit(gear="P", geo_label=label, cloud_online=1)

    def charge_home(self):
        need = CHARGE_TARGET - self.soc
        if need <= 0.5:
            return 0.0
        kwh = need / 100 * USABLE_KWH
        dur_s = kwh / HOME_KW * 3600
        steps = max(2, int(dur_s / 300))
        for i in range(1, steps + 1):
            self.soc = min(self.soc + need / steps, CHARGE_TARGET)
            self.ts += dur_s / steps
            done = (i == steps) or self.soc >= CHARGE_TARGET
            self.emit(gear="P",
                      charger_state="charging_complete" if done else "charging",
                      power_kw=HOME_KW if not done else 0,
                      geo_label="Home")
            if done:
                break
        return kwh

    def charge_dcfc(self, minutes=25, cap=85.0):
        # Tapered DC fast charge: 150 kW -> 60 kW, stops at cap.
        total_s = minutes * 60
        steps = max(2, int(total_s / 120))
        kwh_added = 0.0
        for i in range(1, steps + 1):
            f = i / steps
            kw = 150 - (150 - 60) * f
            kwh_step = kw * (total_s / steps) / 3600
            if self.soc + kwh_step / USABLE_KWH * 100 >= cap:
                kwh_step = max(0.0, (cap - self.soc) / 100 * USABLE_KWH)
            kwh_added += kwh_step
            self.soc = min(self.soc + kwh_step / USABLE_KWH * 100, cap)
            self.ts += total_s / steps
            done = (i == steps) or self.soc >= cap
            self.emit(gear="P",
                      charger_state="charging_complete" if done else "charging",
                      power_kw=round(kw if not done else 0, 1),
                      geo_label="Rivian Adventure Network")
            if done:
                break
        return kwh_added

    def overnight(self, charge=True):
        # Park until 11pm, optionally charge, then sleep till 7am.
        dt = datetime.fromtimestamp(self.ts, tz=TZ)
        bed = dt.replace(hour=23, minute=0, second=0)
        if bed < dt:
            bed += timedelta(days=1)
        # Parked snapshots until bedtime: first awake, then asleep.
        first = True
        while self.ts < bed.timestamp():
            self.ts = min(self.ts + 600, bed.timestamp())
            if not first:
                self.soc -= 0.008
            self.emit(gear="P", geo_label="Home",
                      cloud_online=1 if first else 0)
            first = False
        # Pre-charge dip snapshot so the charge session splits from the
        # parked tail (derive splits on >0.05% SoC drops).
        if charge:
            self.soc -= 0.08
            self.emit(gear="P", geo_label="Home", cloud_online=0)
        kwh = 0.0
        if charge:
            kwh = self.charge_home()
        # Sleep till 7am with vampire drain (~0.07%/hr, ~1.5mi/night)
        wake = (bed + timedelta(hours=8)).timestamp()
        while self.ts < wake:
            self.ts = min(self.ts + 600, wake)
            self.soc -= 0.012
            self.emit(gear="P", geo_label="Home", cloud_online=0)
        return kwh


def build(out_dir: Path, days: int):
    # 10-day window ending yesterday, so the demo always looks fresh.
    today = datetime.now(tz=TZ).date()
    start_day = today - timedelta(days=days)
    sim = Sim(datetime(start_day.year, start_day.month, start_day.day, 9, 0,
                       tzinfo=TZ).timestamp())

    total_kwh_home = 0.0
    total_kwh_dcfc = 0.0
    for d in range(days):
        sim.day_index = d
        if d > 0:
            # Wake at 7am is already set; jump to day start hour
            pass
        day_plan = ITINERARY[d % len(ITINERARY)]
        start_h = DAY_START_HOUR.get(d, 9)
        dt = datetime.fromtimestamp(sim.ts, tz=TZ)
        morning = dt.replace(hour=start_h, minute=0, second=0)
        if morning.timestamp() > sim.ts:
            sim.ts = morning.timestamp()
            sim.emit(gear="P", geo_label="Home", cloud_online=0)
        for key, dwell_min in day_plan:
            if key == "ran_oc":
                # Special: DC fast charge stop
                sim.drive_to(key)
                total_kwh_dcfc += sim.charge_dcfc(25)
                continue
            if key != "home" or sim.ts > 0:  # always drive (even home->first stop)
                # Skip zero-distance "drive" from home at day start
                _, plat, plon = PLACES[key]
                if haversine_mi((sim.lat, sim.lon), (plat, plon)) > 0.05:
                    sim.drive_to(key)
            if dwell_min > 0:
                sim.dwell(dwell_min, PLACES[key][0])
        # Night
        charge = d not in DRAIN_NIGHTS
        total_kwh_home += sim.overnight(charge=charge)
        print(f"day {d + 1}: {len(sim.snaps)} snaps, soc={sim.soc:.1f}%")

    return sim, total_kwh_home, total_kwh_dcfc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="demo-data")
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    out = Path(args.out)
    (out / "www").mkdir(parents=True, exist_ok=True)

    # Environment BEFORE importing wattson modules (config reads at import).
    os.environ["RIVIAN_DB_PATH"] = str(out / "rivian.db")
    os.environ["RIVIAN_TOKENS_PATH"] = str(out / "tokens.enc")
    os.environ["RIVIAN_TOKEN_KEY_PATH"] = str(out / ".token_key")
    os.environ["RIVIAN_HOME_LAT"] = str(PLACES["home"][1])
    os.environ["RIVIAN_HOME_LON"] = str(PLACES["home"][2])
    os.environ["RIVIAN_HOME_KWH_RATE"] = "0.30"
    os.environ["RIVIAN_VEHICLE_NAME"] = VEHICLE
    os.environ["WATTSON_TIMEZONE"] = "America/Los_Angeles"
    os.environ["WATTSON_DEMO"] = "1"
    os.environ["WATTSON_DEMO_LIFETIME_MI"] = "1035"
    os.environ["RIVIAN_REPORT_PATH"] = str(out / "www" / "rivian-dashboard.html")

    # Fresh DB
    for f in [out / "rivian.db", out / "rivian.db-wal", out / "rivian.db-shm"]:
        if f.exists():
            f.unlink()

    sim, kwh_home, kwh_dcfc = build(out, args.days)

    from db import connect, init
    init()
    with connect() as con:
        con.executemany(
            """INSERT INTO snapshots (ts, soc, capacity_kwh, range_mi,
               charger_state, charge_port, power_kw, power_state, gear,
               lat, lon, speed, geo_label, odometer, cloud_online,
               cloud_sync_ts)
               VALUES (:ts, :soc, :capacity_kwh, :range_mi, :charger_state,
               :charge_port, :power_kw, :power_state, :gear, :lat, :lon,
               :speed, :geo_label, :odometer, :cloud_online, :cloud_sync_ts)""",
            sim.snaps)
    print(f"inserted {len(sim.snaps)} snapshots")

    from analyze import derive
    trips, drains, charges, errands = derive()
    print(f"derived: {len(trips)} trips, {len(drains)} drains, "
          f"{len(charges)} charges, {len(errands)} errand days")

    from report import generate
    generate()

    miles = sum(t["miles"] or 0 for t in trips)
    print(f"\ndemo complete: {miles:.0f} miles, {len(trips)} trips, "
          f"{len(charges)} charges "
          f"(home {kwh_home:.0f} kWh, DCFC {kwh_dcfc:.0f} kWh)")
    print(f"dashboard: {out / 'www' / 'rivian-dashboard.html'}")


if __name__ == "__main__":
    main()
