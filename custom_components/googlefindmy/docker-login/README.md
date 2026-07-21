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
`docker compose run --build --service-ports --rm googlefindmy-login`. `run`
builds (if needed) **and** starts a fresh one-shot container in the foreground
with your terminal attached (so the "Press Enter" prompt reaches Python);
`--rm` removes it on exit, so repeated logins never stack containers; and
`--service-ports` publishes the noVNC port declared in `docker-compose.yml`.
On Linux/QNAP the launcher also exports your `GFMY_HOST_UID`/`GFMY_HOST_GID` so
the finished `secrets.json` is handed back to you (see
[Using `secrets.json`](#using-secretsjson-in-home-assistant)).

### Manual alternative (`docker compose`)

If you prefer to run it by hand instead of the launcher:

```bash
cd config/custom_components/googlefindmy/docker-login
mkdir -p data
docker compose run --build --service-ports --rm googlefindmy-login
```

No host `chmod` is needed: the container takes ownership of `./data` for the run
(it has passwordless `sudo` in the selenium base image) and writes
`secrets.json` there. If you run it by hand like this — without exporting
`GFMY_HOST_UID`/`GFMY_HOST_GID` (the launcher does this) — the file stays owned
by the container user; export those IDs, or `sudo chown` the file afterwards, to
read it as your host user. Docker Desktop (Windows/macOS) maps ownership for you.

## First run (Google login required)

1. (Launcher does this for you.) Create the token volume directory:
   ```bash
   mkdir -p data
   ```
   `secrets.json` is written here (to `data/secrets.json`) via the
   `GOOGLEFINDMY_SECRETS_PATH=/data/secrets.json` setting in
   `docker-compose.yml`, so the integration code mount stays read-only. The
   container takes ownership of `./data` for the run, so no host `chmod` is needed.

2. Start the container **in the foreground** (it prompts for input) —
   `./login.sh` / `login.cmd`, or
   `docker compose run --build --service-ports --rm googlefindmy-login`.

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
  (or `docker compose run --build --service-ports --rm googlefindmy-login`).
  noVNC is bound to loopback, so open it via an
  SSH tunnel from your workstation:
  ```bash
  ssh -L 7900:127.0.0.1:7900 <nas-ip>
  ```
  then browse to `http://localhost:7900`. Only on a trusted LAN, start with
  `GFMY_NOVNC_BIND=0.0.0.0 ./login.sh` to reach `http://<nas-ip>:7900` directly
  (see [noVNC access & security](#novnc-access--security)).
- **Container Station:** the interactive first login needs a terminal attached to
  its STDIN, which only the `docker compose run` path (the **SSH** option above)
  provides. Importing `docker-compose.yml` as a Container Station *application*
  starts it with `docker compose up` semantics, which does **not** forward a
  terminal — so the `[AuthFlow] Press Enter to continue...` prompt can never
  proceed and the login stalls (see the compose file's own note and
  [Troubleshooting](#troubleshooting)). Run the **login** via SSH; you may use
  Container Station afterwards for the normal, already-authenticated runs, which
  skip the Enter prompt.

Because HA and the login container run on the same box here, the produced
`data/secrets.json` is on the same host you import it from.

## Normal (already authenticated) runs

```bash
./login.sh          # or: docker compose run --service-ports --rm googlefindmy-login
```
If `data/secrets.json` already holds valid tokens, the browser step is skipped
and the CLI lists devices directly. You can ignore the noVNC link.

## Forcing a fresh login

```bash
# Option A: wipe and start over
echo '{}' > data/secrets.json

# Option B: keep the file but force re-authentication
GFMY_ARGS="--reauth" ./login.sh
```

`GFMY_ARGS` is forwarded to `main.py`. Useful values: `--reauth` (force login),
`--debug` (verbose bootstrap/FCM logging), `--entry <id>` (select one config
entry when the cache holds several).

## One-click handoff (optional, no manual copy)

By default the login writes `data/secrets.json` and you import that file into
Home Assistant. Two optional switches automate the handoff so you never touch
the file yourself — pick the one that matches your setup. Both are off unless you
opt in, and neither changes the classic file behaviour.

### Container login over a loopback endpoint (`GFMY_ONECLICK=1`)

After a successful login the container serves the freshly minted bundle on a
one-shot, nonce-authenticated endpoint that is reachable **only on the Docker
host's loopback** (`127.0.0.1:7901`), then deletes the file once Home Assistant
confirms it (or after a short timeout).

The endpoint is strictly single-use: once the bundle has been handed out, every
further request is refused, and repeated wrong pairing codes lock it out. A
lockout closes the endpoint but **keeps** `secrets.json`, so you never have to
repeat the Google login: fall back to the file handoff (Track A) or the
clear-text output (Track C), or simply run the container again for a fresh code.

> **How the loopback guarantee is enforced.** Under Docker's default *bridge*
> network the published port is DNAT'd onto the container's `eth0`, not onto the
> container's loopback, so the server inside the container binds `0.0.0.0` (all
> container interfaces) on purpose — otherwise the published port would be
> unreachable. The "no LAN exposure" boundary is the **host-side publish**
> `127.0.0.1:7901:7901` in `docker-compose.yml`, which is pinned to loopback.
>
> **`network_mode: host` is NOT supported for this service.** In host networking
> there is no bridge and no publish indirection: the `0.0.0.0` bind would then be
> LAN-visible and expose the clear-text token endpoint to the whole network. Keep
> the default bridge network with the loopback publish; use the SSH tunnel below
> for the split-machine case.

```bash
GFMY_ONECLICK=1 ./login.sh
```

The container prints a **pairing code** (generated at runtime — there is no
default). In Home Assistant, choose the *Container login* auth method and enter
host `127.0.0.1`, port `7901`, and that pairing code. The endpoint is
loopback-only by design (it carries the tokens in the clear); to drive it from
another machine, tunnel it:

```bash
ssh -L 7901:127.0.0.1:7901 <docker-host>
```

There is deliberately **no LAN opt-in** for this port — the split-machine path
is the SSH tunnel above.

> **If Home Assistant itself runs in a bridged Docker container** (the *HA
> Container* install method), `127.0.0.1` inside Home Assistant points at the
> **HA container**, not at the Docker host — so the host-published
> `127.0.0.1:7901` is unreachable and the SSH tunnel only helps if its local end
> is opened inside HA's own network namespace. Two supported routes:
>
> 1. **Same Docker network (no LAN exposure).** This needs two deliberate steps,
>    because the shipped launchers (`login.sh` / `login.cmd`) start a throwaway
>    one-off container that gets a *generated* name and, by default, **no network
>    alias** you could dial:
>
>    a. Put both containers on one user-defined network. Create a
>       `docker-compose.override.yml` next to `docker-compose.yml`:
>
>    ```yaml
>    services:
>      googlefindmy-login:
>        networks: [gfmy]
>    networks:
>      gfmy:
>        external: true   # the network your Home Assistant container is on
>    ```
>
>    b. Start it **with** the service alias instead of using `login.sh`:
>
>    ```bash
>    docker compose run --use-aliases --build --service-ports --rm googlefindmy-login
>    ```
>
>    Then enter `googlefindmy-login` (the **service** name, which `--use-aliases`
>    turns into a resolvable DNS alias, not the generated container name) as the
>    host, port `7901`. The server binds `0.0.0.0` *inside* the container, so a
>    peer container on the shared network reaches it directly, without publishing
>    anything to the LAN. Without `--use-aliases` the name does not resolve and
>    Home Assistant reports `container_unreachable`.
>
>    If that is more plumbing than you want, use route 2 instead: it is simpler
>    and needs no network at all.
> 2. **File handoff instead (Track A, no network at all).** Point the integration
>    at `docker-login/data/secrets.json` via the options and let the secrets
>    watcher pick it up — this needs no reachable port.
>
> For **HA OS, HA Core, or host-networked HA on the same machine**, the plain
> `127.0.0.1:7901` above is correct.

### noVNC clear-text copy fallback (`GFMY_CLEARTEXT=1`)

If you cannot share a filesystem *and* cannot open a port (or you simply prefer
copy/paste), this switch prints the full `secrets.json` in the terminal at the
end of the login — inside the noVNC window you can **select, copy, and paste** it
straight into Home Assistant's *secrets.json* field. No port is opened.

```bash
GFMY_CLEARTEXT=1 ./login.sh
```

The block is framed by `BEGIN secrets.json` / `END secrets.json` markers so it is
easy to select. The file is **ephemeral**: it is deleted immediately after it is
displayed, so nothing lingers on disk. This switch is independent of
`GFMY_ONECLICK` and of the plain file handoff.

## Using `secrets.json` in Home Assistant

Copy `data/secrets.json` to the machine running Home Assistant (on a single-host
setup it is already there) and import it via the integration's configuration
flow (auth method *"GoogleFindMyTools secrets.json"*). Because it was produced by
the integration's own code, no conversion is needed.

The container writes the file as its own user and, on exit, hands ownership back
to your host user (the `GFMY_HOST_UID`/`GFMY_HOST_GID` the launcher exports)
while **keeping owner-only `0600`**. So you can read and copy it, but no other
local account can — the file is never made world-readable. Treat
`data/secrets.json` as a credential and delete it once imported.

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

- **Stuck with no prompt / can't type:** you probably ran `docker compose up`
  (which does not attach your terminal's stdin). Use `./login.sh` /
  `docker compose run --service-ports --rm googlefindmy-login`, which runs in the
  foreground with stdin attached.
- **noVNC page won't load:** give the container a few seconds; check
  `docker compose logs` for `[entrypoint] Display ready.`
- **noVNC password:** it is `secret` — a fixed default of the base image.
- **`SessionNotCreatedException` / chromedriver version mismatch:** set
  `GOOGLEFINDMY_CHROME_VERSION` to the container's Chrome major version
  (uncomment the line in `docker-compose.yml`). The integration's
  `chrome_driver.py` normally auto-detects this.
- **Permission error writing `secrets.json`:** make sure `./data` exists (the
  launcher runs `mkdir -p data`). The container chowns it to itself for the run,
  so a host `chmod` is not required.
- **Can't read `secrets.json` as your user after a manual run:** you ran it by
  hand without exporting `GFMY_HOST_UID`/`GFMY_HOST_GID`, so the file stayed
  container-owned. Re-run via `./login.sh`, or `sudo chown "$(id -u):$(id -g)"
  data/secrets.json`.

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
