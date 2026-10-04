# Wattson

**Gary watches your Rivian. Wattson knows it.**

## Why this exists

I drive a Rivian R2. Love the car. But Rivian doesn't keep drive history, so
every unrecorded drive is just gone. I wanted to know my real efficiency per
trip, what home charging actually costs me, and how much charge disappears
overnight. The app shows none of that.

So I built this. It was supposed to be a small weekend thing. It was not.

Along the way I figured out how Rivian's unofficial API works, how to poll a
car without keeping it awake, and how to handle credentials without being
careless about it (only encrypted session tokens, never your password). There
were bugs, rewrites, and one very confusing stretch where my overnight drain
numbers made no sense at all.

This is a personal side project. It works, I run it every day, and I'm
sharing it in case it's useful or fun for you too. No roadmap, no SLA, no
promises. Take it, run it, break it, make it yours.

## What it does

- **Per-trip efficiency**, every drive with mi/kWh, energy used, and a route map
- **Charging log**, sessions with kWh added, your real home electricity cost,
  and charge curves
- **Overnight drain tracking**, multi-night vampire-drain trends
- **Drive history**, the record Rivian never kept

## Quick start

**You need:** a machine that's always on (Raspberry Pi, old laptop, NAS, or a
small VPS) and [Docker](https://docs.docker.com/get-docker/).

```bash
git clone https://github.com/firerock31/wattson.git
cd wattson
cp .env.example .env
# Edit .env: add your home coordinates and electricity rate
```

Link your Rivian account (one time, interactive):

```bash
docker compose run --rm tracker python auth.py
```

Enter your Rivian email and password when prompted, then the OTP code Rivian
texts you. Only encrypted session tokens are stored; your password is never
written to disk.

Then start it:

```bash
docker compose up -d
```

Open **http://localhost:8080**. The first poll takes a few minutes; the
dashboard builds itself after that.

Want it on your phone? Put [Tailscale](https://tailscale.com/) on the machine
and your phone. Free, five minutes, no port forwarding.

## Try the demo (no Rivian account needed)

```bash
python demo.py
```

This builds a 10-day fictional driving history for a Demo R2 in Los
Angeles (Venice home base, Disneyland day trip, Newport Beach fast
charge, the works), runs the real analysis pipeline on it, and writes
a dashboard stamped with a demo banner. All data is synthetic; the
places are real, the driver is not.

Open `demo-data/www/rivian-dashboard.html` in a browser to click around.
The whole `demo-data/www/` folder is a static site, so you can also
upload it anywhere to share the demo.

## How it works

A poller hits Rivian's API on an adaptive schedule, frequent while driving,
backed off when the car is asleep, and stores snapshots in a local SQLite
database. An analyzer turns snapshots into trips, charging sessions, and
drain events. A report generator builds the dashboard HTML. Everything runs
locally: your data never leaves your machine.

> **Note:** this uses Rivian's unofficial API, which isn't publicly
> documented and can change without notice. If it breaks, the fix is usually
> quick, but there are no guarantees. Not affiliated with Rivian.

## Configuration

| Variable | Required | What it does |
|---|---|---|
| `RIVIAN_HOME_LAT` / `RIVIAN_HOME_LON` | Yes | Home coordinates (decimal degrees), for home-charge detection |
| `RIVIAN_HOME_KWH_RATE` | No | Your all-in $/kWh home rate, for charging cost (default `0.30`) |
| `WATTSON_TIMEZONE` | No | IANA timezone for dashboard dates/times (default `America/Los_Angeles`) |
| `RIVIAN_VEHICLE_NAME` | No | Dashboard display name (defaults to your Rivian account name) |

Drop a `hero.webp` image of your car in `assets/` to customize the dashboard
hero. (Optional; the layout holds without it.)

## Project layout

- `poller.py`, periodic vehicle polling and snapshot capture
- `analyze.py`, trips, charging sessions, and overnight drain detection
- `report.py`, dashboard HTML generation
- `auth.py`, one-time Rivian account linking
- `geocode.py`, `weather.py`, location names and ambient temperatures

## Contributing

Hobby project, no SLA. Issues and pull requests welcome.

## License

MIT. See [LICENSE](LICENSE).
