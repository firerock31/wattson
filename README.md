# Wattson

**Gary watches your Rivian. Wattson knows it.**
*A friendly dashboard for your Rivian. Easy to read, useful, and free.*

## Why this exists

I drive a Rivian R2. Love the car. But Rivian doesn't keep drive history, so
every unrecorded drive is just gone. I wanted to know my real efficiency per
trip, what home charging actually costs me, and how much charge disappears
overnight. The app shows none of that.

So I built this. It was supposed to be a small weekend thing. It was not.

Along the way I figured out how Rivian's unofficial API works, how to poll a
car without keeping it awake, and how to not be careless with credentials (encrypted tokens only, your
password never touches disk). There
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
- **Fuel savings vs gas**, per-session and lifetime savings against a gas
  SUV, priced at each charge day's AAA gas price
- **Drive history**, the record Rivian never kept

## Recent updates

See [CHANGELOG.md](CHANGELOG.md) for the full release history. Latest:
**drive-mode trip cards** and **per-session paid display** (0.4.0).

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
and your phone. Free, five minutes, no port forwarding. Or put it behind a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
at something like `wattson.example.com`, but set a dashboard password
first (see below).

## Exposing the dashboard

Out of the box the dashboard has no login, because it's meant for
localhost or a private network. Before you expose it to the internet,
add a password to `.env` (compose reads the file literally, so generate
the value in your shell):

```bash
echo "WATTSON_DASHBOARD_TOKEN=$(openssl rand -hex 24)" >> .env
```

Then `docker compose up -d` again. Now:

- Browsers get a login page once, then stay logged in for 30 days.
- Scripts skip the login with `Authorization: Bearer <token>`.
- Changing the token logs everyone out.
- Five wrong passwords from one IP within 10 minutes locks that IP out
  for the rest of the 10 minutes.

`/api/health` stays open so uptime checks keep working.

## Try the demo (no Rivian account needed)

**[Click through the live demo](https://www.givemeasec.com/wattson-demo/)**, or build it yourself:

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
backing off when the car sleeps, and stores snapshots in a local SQLite
database. An analyzer turns snapshots into trips, charging sessions, and
drain events. A report generator builds the dashboard HTML. Everything runs
locally: your data never leaves your machine.

**Polling without waking the car**

The tricky part: asking a car for its status can keep it awake, and an
awake car drains battery. So the poller runs on an adaptive schedule,
frequent while driving, backing off when the car sleeps. Sleep detection
uses the cloud connection state, since Rivian's power and gear fields
report nothing useful. A lightweight daemon handles the polling on its
own; nothing needs to wake up every few minutes just to check.

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
| `WATTSON_GAS_COMPARISON_MPG` | No | MPG of the gas SUV you compare against (default `25`) |
| `WATTSON_GAS_PRICE` | No | Pin a $/gal gas price (skips the AAA scrape; demo sets this) |
| `WATTSON_GAS_PRICE_URL` | No | AAA gas-price page (default `https://gasprices.aaa.com?state=CA`) |
| `WATTSON_GAS_PRICE_METRO` | No | Metro label on the AAA page (default `Los Angeles-Long Beach`) |
| `WATTSON_DASHBOARD_TOKEN` | No | Dashboard password; unset leaves the dashboard open (see [Exposing the dashboard](#exposing-the-dashboard)) |

Drop a `hero.webp` image of your car in `assets/` to customize the dashboard
hero. (Optional; the layout holds without it.)

## Project layout

- `poller.py`, periodic vehicle polling and snapshot capture
- `analyze.py`, trips, charging sessions, and overnight drain detection
- `report.py`, dashboard HTML generation
- `auth.py`, one-time Rivian account linking
- `geocode.py`, `weather.py`, location names and ambient temperatures
- `gasprice.py`, AAA gas-price scraping with daily caching
- `api.py`, records what you paid per kWh at DC fast chargers
- `dashboard_auth.py`, the optional dashboard login

## Fuel savings vs gas

The hero card shows lifetime savings against driving a gas SUV
(`WATTSON_GAS_COMPARISON_MPG`, default 25 MPG). Each charging session is
priced individually. EV cost is kWh times your home rate (or the DC
price you entered). Gas cost is the session's miles divided by the
comparison MPG, times that day's AAA gas price. Home sessions count automatically; DC fast
charges are excluded until you record what you paid.

To record a DC price, click **? add cost** on the charging-log row. A small
form saves the $/kWh rate and regenerates the dashboard. Editing a
priced session reopens the form with the current value. Entering `$0.00`
marks a free session.

Use **Export CSV** in the charging log to download all stored charging sessions,
not just the latest rows shown on the dashboard. Times are UTC; energy, distance,
duration, and battery levels use kWh, miles, minutes, and percent. Unentered
prices stay blank, including home rates derived from configuration rather than
stored on the session. The download follows the optional dashboard login.

## Contributing

Hobby project, no SLA. Issues and pull requests welcome.

Built something cool with Wattson? I'd love to hear about it. And pay
it forward: keep it open, keep it fun.

## License

MIT. See [LICENSE](LICENSE).

