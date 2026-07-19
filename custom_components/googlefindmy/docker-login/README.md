# Docker login helper for GoogleFindMy-HA

A one-command Docker wrapper that performs the **one-time Google login** for the
GoogleFindMy-HA integration and produces a ready-to-use `secrets.json`.

> **Shipped with the integration (no `git clone`).**
> These files are delivered by HACS together with the integration. After you
> install GoogleFindMy-HA they already exist on the machine running Home
> Assistant, under:
> ```
> config/custom_components/googlefindmy/docker-login/
> ```
> You do **not** need to clone any repository — just open that folder on your
> Docker host and run the launcher below.
>
> **Home Assistant OS users:** HA-OS has no user shell and no Docker daemon you
> can reach, so these compose files cannot be run there directly. See
> [Which setup can run this?](#which-setup-can-run-this) for the Add-on path.

Unlike the upstream *GoogleFindMyTools* Docker image, this wrapper runs the
**integration's own** standalone CLI
(`custom_components/googlefindmy/main.py`) — the same, up-to-date fork code that
Home Assistant runs. The `secrets.json` it writes is therefore exactly what the
integration consumes; no format drift between a separate CLI and the
integration.

The login itself needs a real Chrome window (it is *not* a copy-paste token), so
the container bundles Chrome + a virtual display + a **noVNC** browser viewer.
You log in through a browser tab that shows Chrome running inside the container;
no local Python or Chrome is needed, and it works on ARM Linux too.

## Run it on the Docker host, not inside Home Assistant

`docker compose` must run **where the Docker daemon runs** (your NAS, your
Docker Desktop, the Linux box hosting HA in a container). It does **not** run
inside the Home Assistant container itself: HA has no Docker socket and cannot
start a sibling container. So open the delivered `docker-login/` folder on the
Docker host and launch it there.

## noVNC access & security

The noVNC viewer uses the base image's **fixed password `secret`**, and during
the Google sign-in it exposes a fully authenticated Chrome session. To avoid
handing that to anyone on the network, the port is bound to **loopback
(`127.0.0.1:7900`) by default** — it is only reachable from the Docker host
itself.

- **Reach it from another machine (recommended): SSH tunnel.**
  ```bash
  ssh -L 7900:127.0.0.1:7900 <docker-host>
  ```
  Then open `http://localhost:7900` on your own machine.
- **Trusted LAN opt-in.** Only on a network you trust, bind noVNC to all
  interfaces by setting `GFMY_NOVNC_BIND=0.0.0.0` before starting:
  ```bash
  GFMY_NOVNC_BIND=0.0.0.0 ./login.sh
  ```
  Then browse to `http://<docker-host-ip>:7900`. Prefer the tunnel over this.

## Which setup can run this?

| Home Assistant install | Has a host shell + Docker daemon? | Login path |
|---|---|---|
| **HA Container** (e.g. HA in Docker on a NAS/QNAP) | yes | This wrapper (`login.sh` / `login.cmd`) |
| **HA Supervised** | yes | This wrapper, or a Supervisor Add-on |
| **HA Core** (venv) | yes | This wrapper |
| **HA OS** | no (no shell, no reachable Docker) | A Home Assistant **Add-on** (planned; the shipped compose files are inert here) |

## Quick start (recommended: the launcher)

The launcher takes care of the three common pitfalls (missing token file,
duplicate containers, stale code after an update) for you.

- **Linux / macOS / QNAP:**
  ```bash
  cd config/custom_components/googlefindmy/docker-login
  ./login.sh
  ```
- **Windows (Docker Desktop):** double-click `login.cmd`, or from a terminal:
  ```bat
  cd config\custom_components\googlefindmy\docker-login
  login.cmd
  ```

Then follow [First run (Google login)](#first-run-google-login-required) from
step 3.

The launcher runs a single command:
`docker compose up --build --force-recreate --remove-orphans`. It builds (if
needed) **and** starts the container in the foreground, and `--force-recreate
--remove-orphans` guarantees exactly one container even if you run it twice.

### Manual alternative (`docker compose`)

If you prefer to run it by hand instead of the launcher:

```bash
cd config/custom_components/googlefindmy/docker-login
mkdir -p data
chmod 0777 data            # let the container user (seluser) write secrets.json
docker compose up --build
```

The `chmod` matters on Linux/QNAP: the image runs as `seluser`, whose UID may
differ from yours, so the `./data` volume must be writable by others or the
atomic write of `secrets.json` fails. Docker Desktop (Windows/macOS) handles this
for you.

## First run (Google login required)

1. (Launcher does this for you.) Create the writable token volume:
   ```bash
   mkdir -p data && chmod 0777 data
   ```
   `secrets.json` is written here (to `data/secrets.json`) via the
   `GOOGLEFINDMY_SECRETS_PATH=/data/secrets.json` setting in
   `docker-compose.yml`, so the integration code mount stays read-only.

2. Start the container **in the foreground** (no `-d`; it prompts for input) —
   `./login.sh` / `login.cmd`, or `docker compose up --build`.

3. Wait for this line in the terminal, then press **Enter**:
   ```
   [AuthFlow] Press Enter to continue...
   ```

4. Open **http://localhost:7900** in your normal browser. Password: `secret`.
   You now see a live view of Chrome running inside the container.

5. In that noVNC window, log into your Google account as usual (2FA works the
   same as in any browser).

6. When the terminal prints `[AuthFlow] Retrieved Account Token successfully.`
   the CLI continues and lists your Find My Device / Find Hub trackers. Your
   tokens are now cached to `data/secrets.json` on the host.

## Running on QNAP / Container Station

On a QNAP NAS running Home Assistant as a container, this wrapper is the
intended login path (there is no Supervisor Add-on store for HA Container):

- **SSH:** enable SSH on the NAS, `cd` into
  `.../config/custom_components/googlefindmy/docker-login`, then run `./login.sh`
  (or `docker compose up --build`). noVNC is bound to loopback, so open it via an
  SSH tunnel from your workstation:
  ```bash
  ssh -L 7900:127.0.0.1:7900 <nas-ip>
  ```
  then browse to `http://localhost:7900`. Only on a trusted LAN, start with
  `GFMY_NOVNC_BIND=0.0.0.0 ./login.sh` to reach `http://<nas-ip>:7900` directly
  (see [noVNC access & security](#novnc-access--security)).
- **Container Station:** you can also import `docker-compose.yml` as an
  application in Container Station. Keep it in the foreground for the first
  (login) run so you can press Enter and watch the log.

Because HA and the login container run on the same box here, the produced
`data/secrets.json` is on the same host you import it from.

## Normal (already authenticated) runs

```bash
./login.sh          # or: docker compose up
```
If `data/secrets.json` already holds valid tokens, the browser step is skipped
and the CLI lists devices directly. You can ignore the noVNC link.

## Forcing a fresh login

```bash
# Option A: wipe and start over
echo '{}' > data/secrets.json

# Option B: keep the file but force re-authentication
GFMY_ARGS="--reauth" docker compose up
```

`GFMY_ARGS` is forwarded to `main.py`. Useful values: `--reauth` (force login),
`--debug` (verbose bootstrap/FCM logging), `--entry <id>` (select one config
entry when the cache holds several).

## Using `secrets.json` in Home Assistant

Copy `data/secrets.json` to the machine running Home Assistant (on a single-host
setup it is already there) and import it via the integration's configuration
flow (auth method *"GoogleFindMyTools secrets.json"*). Because it was produced by
the integration's own code, no conversion is needed.

## Stopping

```bash
docker compose down
```
`data/secrets.json` survives (it is a host-mounted file, not inside the
container).

## Keeping in sync with the integration

Because the integration code is bind-mounted read-only at run time (it is **not**
baked into the image), the container always runs the integration version that is
currently installed. After a HACS update just start the login container again;
there is no separate image to rebuild for code changes.

## Troubleshooting

- **Stuck with no prompt / can't type:** you probably ran `docker compose up -d`
  (detached). Run it in the foreground (the launcher does).
- **noVNC page won't load:** give the container a few seconds; check
  `docker compose logs` for `[entrypoint] Display ready.`
- **noVNC password:** it is `secret` — a fixed default of the base image.
- **`SessionNotCreatedException` / chromedriver version mismatch:** set
  `GOOGLEFINDMY_CHROME_VERSION` to the container's Chrome major version
  (uncomment the line in `docker-compose.yml`). The integration's
  `chrome_driver.py` normally auto-detects this.
- **Permission error writing `secrets.json`:** make sure `data/secrets.json`
  exists as a *file* before the first run (the launcher handles this).

## Known limitation

The Google login must be done by a human through the noVNC window — there is no
way (nor reason) to script your real Google credentials.

## Credits

- **[sincze/GoogleFindMyTools @ `docker-novnc-auth`](https://github.com/sincze/GoogleFindMyTools/tree/docker-novnc-auth)**
  — the containerised noVNC login design (Chrome + virtual display + noVNC +
  `entrypoint.sh`) this wrapper is adapted from. Our wrapper turns that design
  onto the integration's **own** `main.py`, so the produced `secrets.json`
  matches the installed integration byte-for-byte instead of a separate CLI.
- **[leonboe1/GoogleFindMyTools](https://github.com/leonboe1/GoogleFindMyTools)**
  — the original GoogleFindMyTools CLI the integration is based on.
