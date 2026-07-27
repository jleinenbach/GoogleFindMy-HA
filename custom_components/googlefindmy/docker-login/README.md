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

The noVNC viewer exposes a fully authenticated Chrome session during the Google
sign-in, so both its reachability and its password matter. What the viewer uses
depends on **where you bind it**:

- **Loopback (default): fixed password `secret`, plain HTTP.** With no `--ip` and
  no `GFMY_NOVNC_BIND`/`GFMY_NOVNC_URL_HOST`, the port is bound to
  **`127.0.0.1:7900`** — reachable only from the Docker host. This is the
  historical behaviour: the viewer keeps the base image's public password
  `secret` over plain HTTP, which is safe **only** because nothing on the network
  can reach it.
- **LAN bind: per-run password + self-signed HTTPS, automatically.** The moment
  you bind to a non-loopback address, the container hardens the viewer for you:
  it mints a **fresh random password for that run** (so the public `secret` no
  longer works) and, unless you pass `--no-tls`, serves the viewer over a
  **self-signed TLS certificate** (`https://…`, SAN = the chosen address). The
  password is minted *inside the container*, so the launcher cannot print it: it
  appears in the container output below, shown as `(password: …)` in both the
  `[entrypoint] noVNC available at …` line and the `[AuthFlow]` sign-in banner.
  Because the certificate is self-signed, your browser shows a one-time warning
  you accept once. (If `openssl` is somehow unavailable in the container it logs a
  warning and falls back to plain HTTP for that run; the per-run password still
  applies.)

You reach a LAN-bound viewer in one of two ways:

- **Interactive chooser (just run `bash login.sh`).** On an interactive terminal
  with nothing pinned, the launcher lists the LAN addresses it detected on this
  host and asks you to pick one; pressing **Enter** takes the highlighted default
  (a detected `192.168.*`, else `10.*`, else the first address; loopback only
  when none was found). Picking a LAN address turns on the hardening above;
  picking loopback keeps `secret`/plain. Container and VPN interfaces (`docker0`,
  `br-*`, `veth*`, `tun*`, `wg*`) are left out of the list. A **non-interactive**
  run (no TTY) keeps the loopback default unchanged.
- **Explicit `--ip` (scriptable, also Windows).** Skip the chooser by naming the
  address directly:
  ```bash
  bash login.sh --ip 192.168.1.21
  ```
  `--no-tls` keeps the per-run password but serves plain HTTP instead of the
  self-signed viewer — for people who prefer to tunnel the transport themselves.
  `login.cmd` on Windows takes the same `--ip` flag (in both the `--ip X` and
  `--ip=X` spellings) and gets the **same** per-run password and TLS; it has no
  `--no-tls` (a LAN bind on Windows always encrypts) and does not auto-detect
  addresses, because parsing `ipconfig` output in batch is locale-dependent.
- **Direct `docker compose run` (no launcher).** If you skip the launchers and run
  `docker compose run --build --service-ports --rm googlefindmy-login` with
  `GFMY_NOVNC_BIND` set to a LAN address (in your shell or a `.env`), the
  **container itself** derives the hardening from that bind, so this path gets the
  same per-run password and self-signed TLS — it cannot expose port 7900 with the
  fixed `secret`. One nuance: with the `GFMY_NOVNC_BIND=0.0.0.0` **wildcard** and no
  `GFMY_NOVNC_URL_HOST`, the container has no single address to certify, so it keeps
  the per-run password but serves plain HTTP (set `GFMY_NOVNC_URL_HOST=<ip>` for
  TLS, or `GFMY_NOVNC_TLS=0` to opt out of TLS deliberately). Prefer a concrete
  `GFMY_NOVNC_BIND=<ip>` over the wildcard for that reason.

**Prefer an SSH tunnel over a LAN bind when you can** — it needs no LAN exposure
at all:
```bash
ssh -L 7900:127.0.0.1:7900 <docker-host>
```
Then open `http://localhost:7900` on your own machine (loopback, so password
`secret`). Prefer a concrete `--ip`/chooser address over the
`GFMY_NOVNC_BIND=0.0.0.0` wildcard, which publishes on every interface.

### The three address roles

The token endpoint and the noVNC viewer serve **different consumers**, so they
have separate settings. Never collapse them into one value.

| Setting | Port | Consumer | Default | Why |
|---|---|---|---|---|
| `GFMY_ONECLICK_BIND` | 7901 | Home Assistant, machine-to-machine | `127.0.0.1` | **Security boundary.** The endpoint serves Google credentials in cleartext, so the host publish is what limits who can fetch them. Unset means loopback; widen it only for a Home Assistant that can neither see `./data` nor share this host's network namespace. A wildcard (`0.0.0.0`, `::`, `*`) is **refused** by both launchers and, for the direct `docker compose` path that runs neither, by the container's entrypoint; a non-loopback value makes them warn that the transport is plain http: trusted LAN or this host, for the seconds the handoff takes. |
| `GFMY_NOVNC_BIND` | 7900 | the browser, via the host's network stack | `127.0.0.1` | Where the viewer actually listens. Everything the launchers print about reachability is derived from **this** value, never from the printed one. |
| `GFMY_NOVNC_URL_HOST` | 7900 | printed text only | the noVNC bind | The address you are told to open. A wildcard bind is never printed as a URL: `login.sh` substitutes the first detected address, `login.cmd` (which does not auto-detect) falls back to `127.0.0.1` and asks you to pass `--ip`. |

Home Assistant cannot derive the noVNC address for you: that link is opened by
**your browser**, which usually runs on a different machine than the Docker
host, and no machine on the LAN can know where you are clicking from. The
config flow therefore only renders a clickable noVNC link when the host you
entered is a non-loopback IP address; otherwise it shows the guidance above.

> **Does the loopback token endpoint reach your Home Assistant?**
> Yes when HA shares the host's network namespace (Home Assistant OS, HA Core,
> or a container started with `network_mode: host`), because then HA's
> `127.0.0.1` *is* the host loopback. A HA container in a bridge network has its **own**
> loopback and cannot reach it; use the shared-network route below or the file
> handoff instead. Check the mode without guessing the container name:
> ```bash
> docker ps --format '{{.Names}}' | while read n; do \
>   printf '%-28s %s\n' "$n" "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$n")"; done
> ```

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
  bash login.sh
  ```
  Invoking it as `bash login.sh` (rather than `./login.sh`) is deliberate: HACS
  installs the integration from a ZIP without restoring the execute bit, so
  `./login.sh` can give `Permission denied`. Running it through bash needs no
  execute bit and works after every update.
- **Windows (Docker Desktop):** double-click `login.cmd`, or from a terminal:
  ```bat
  cd config\custom_components\googlefindmy\docker-login
  login.cmd
  ```

Then follow [First run (Google login)](#first-run-google-login-required) from
step 3.

The launcher runs a single command:
`docker compose -f docker-compose.yml run --build --service-ports --rm googlefindmy-login`.
`run` builds (if needed) **and** starts a fresh one-shot container in the
foreground with your terminal attached (so the CLI can ask you for the account
e-mail when it cannot read it out of the Chrome session); `--rm` removes it on
exit, so repeated logins never stack containers;
and `--service-ports` publishes the ports declared by the selected compose
files, which in this default case is only **noVNC on 7900**.
On Linux/QNAP the launcher also exports your `GFMY_HOST_UID`/`GFMY_HOST_GID` so
the finished `secrets.json` is handed back to you (see
[Using `secrets.json`](#using-secretsjson-in-home-assistant)).

On an interactive terminal the launcher **asks which handoff you want** before it
starts anything: `A` (the file in `./data`, the default a bare Enter picks), `B`
(also publish the token endpoint on 7901) or `C` (also print the bundle in this
terminal). Pass `--track a|b|c`, or set `GFMY_ONECLICK`/`GFMY_CLEARTEXT` yourself,
and the question is skipped; a non-interactive run (CI, a pipe, `< /dev/null`)
behaves exactly as it always did. Track A runs in every case — B and C are
additions on top of it, never replacements.

Only if you ask for the one-click handoff (track B, or `GFMY_ONECLICK=1`) does
the launcher add a second compose file, `docker-compose.oneclick.yml`, which
publishes the token endpoint on port 7901 — on `127.0.0.1` unless you name
another address of this host. Without that opt-in **no 7901 port is published at
all**, so a host that already uses port 7901 cannot stop the login container from
starting (see
[One-click handoff](#one-click-handoff-optional-no-manual-copy)).

### Manual alternative (`docker compose`)

If you prefer to run it by hand instead of the launcher:

```bash
cd config/custom_components/googlefindmy/docker-login
mkdir -p data
docker compose run --build --service-ports --rm googlefindmy-login
```

This publishes noVNC only; the one-click token port stays closed unless you add
the overlay file shown under
[One-click handoff](#one-click-handoff-optional-no-manual-copy).

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

2. Start the container **in the foreground**, with your terminal attached —
   `bash login.sh` / `login.cmd`, or
   `docker compose run --build --service-ports --rm googlefindmy-login`.
   Keep that terminal open for the whole run: it is where the instructions and
   the tracker list appear, and the CLI asks there for your account e-mail if it
   cannot read it out of the Chrome session.

3. Wait for the instruction block in the terminal. It names the noVNC URL and
   the password to use:
   ```
   [AuthFlow] Action required to sign in to Google:
   [AuthFlow]   1. Open http://localhost:7900 in your browser (password: secret).
   ```
   On the **loopback default** that is **http://localhost:7900** with password
   `secret`. On a **LAN bind** it is the `https://<address>:7900` URL shown, with
   the **per-run password** the container printed (`(password: …)`); accept the
   one-time self-signed-certificate warning.

4. Open that URL. You now see a live view of the desktop inside the container.
   **Chrome opens by itself** there within a few seconds, on the Google sign-in
   page — there is nothing to confirm in the terminal first.

5. In that noVNC window, log into your Google account as usual (2FA works the
   same as in any browser).

6. When the terminal prints `[AuthFlow] Retrieved Account Token successfully.`
   the CLI continues and lists your Find My Device / Find Hub trackers. Your
   tokens are now cached to `data/secrets.json` on the host.

   Chrome may close and reopen once or twice along the way. That is expected:
   the sign-in and the encryption-key retrieval are separate browser sessions.
   Shorter flickers on top of those two are possible as well: when a browser
   cannot be started the usual way, the driver retries with a different strategy
   (the terminal then logs `Trying headless mode...`). The owner-key step needs
   no browser at all.

## After the login (the menu, `q`, and the wrap-up)

The tracker list is not the end of the run. The CLI stays in a small loop and
asks:

```
Type the number of a tracker to locate it, 'r' to register a new tracker, or 'q' to quit:
```

Pick a number to locate that tracker, `r` to register a new one, or `q` to
finish. `q` prints `Goodbye!` and ends the CLI. **Quitting is the normal way to
end the run** — it is what lets the container do its wrap-up, in this order:

1. **The handoff runs** (only if you asked for one, and only after a successful
   login): the one-click endpoint and/or the clear-text block, described under
   [One-click handoff](#one-click-handoff-optional-no-manual-copy). These run
   *before* the container tears itself down, so an endpoint that has to outlive
   the CLI still gets its turn.
2. **`data/` goes back to you**: the bundle is written by the container's own
   user, and on exit ownership is handed back to the host user that started the
   launcher, keeping owner-only `0600` on the file and `0700` on the directory.
   You can read and copy it; no other local account can.
3. **The container stops**: the supervisor (X server, VNC, noVNC) is shut down
   and the run exits with the CLI's status.

Steps 2 and 3 also run when you stop the container with Ctrl-C or `docker stop`
instead of `q`. What you lose by not quitting is step 1 in the middle: a handoff
cut short that way leaves `data/secrets.json` on disk unless it had already been
consumed. That is the safe direction — no login has to be repeated — but the
bundle is then still sitting there for you to import or delete.

### Which handoff did you choose, and what does it cost?

| | Track A — file | Track B — one-click endpoint | Track C — clear-text block |
|---|---|---|---|
| How to ask for it | nothing (always on) | `GFMY_ONECLICK=1`, or pick it in the launcher menu | `GFMY_CLEARTEXT=1` |
| What it hands over | `data/secrets.json` on disk | the bundle over `http://<bind>:7901`, guarded by a pairing code | the bundle printed in the launcher's terminal |
| What happens to the file | **stays** until you delete it | **deleted** when Home Assistant acks, and also when the token TTL expires; **kept** after a pairing-code lockout | **deleted** right after it is printed |
| Where it goes in Home Assistant | the integration finds it by itself (see below), or you import the file by hand | enter the address and the pairing code in the *container login* step | paste into the *secrets.json* field |
| Good for | Home Assistant that shares this filesystem | Home Assistant on another machine or in another container | no shared filesystem and no port |

The two switches are independent, and with both set the clear-text block only
prints when `secrets.json` is still there — which in practice means the lockout
case. Then Track C *replaces* the file handoff rather than adding to it, because
it deletes the file after printing. The security note for that combination is in
the [clear-text section](#terminal-clear-text-copy-fallback-gfmy_cleartext1);
the one for a widened Track B bind is in the
[one-click section](#container-login-over-a-loopback-endpoint-gfmy_oneclick1).

### Track A needs no copying on a shared filesystem

When Home Assistant can see this directory, you do **not** have to move the file
or paste anything. The integration watches `docker-login/data/secrets.json` as a
built-in default path — not an option you have to set — and its config flow
offers the bundle it found, with "import it" preselected. Copying by hand is
only needed when Home Assistant cannot see this directory (a different machine,
or a container without this bind mount).

### Two different secrets, don't mix them up

- The **noVNC password** gets you into the desktop *during* the login, on port
  7900. On the loopback default it is the fixed `secret`; on a LAN bind the
  container mints a fresh one per run and prints it.
- The **pairing code** is only for Track B, on port 7901, and only exists
  *after* the login: it is minted when the one-click endpoint starts and
  authenticates Home Assistant's single fetch.

Different ports, different purposes, different lifetimes. The pairing code never
opens the viewer, and the noVNC password never fetches the bundle.

## Running on QNAP / Container Station

On a QNAP NAS running Home Assistant as a container, this wrapper is the
intended login path (there is no Supervisor Add-on store for HA Container):

- **SSH:** enable SSH on the NAS, `cd` into
  `.../config/custom_components/googlefindmy/docker-login`, then run `bash login.sh`
  (or `docker compose run --build --service-ports --rm googlefindmy-login`).
  noVNC is bound to loopback, so open it via an
  SSH tunnel from your workstation:
  ```bash
  ssh -L 7900:127.0.0.1:7900 <nas-ip>
  ```
  then browse to `http://localhost:7900`. Only on a trusted LAN, bind to a
  concrete NAS address — `bash login.sh --ip <nas-ip>` (or the interactive
  chooser) — and open the `https://<nas-ip>:7900` URL it prints with the per-run
  password (see [noVNC access & security](#novnc-access--security)). Prefer this
  concrete bind over the `GFMY_NOVNC_BIND=0.0.0.0` wildcard.
- **Container Station:** the interactive first login needs a terminal attached to
  its STDIN, which only the `docker compose run` path (the **SSH** option above)
  provides. Importing `docker-compose.yml` as a Container Station *application*
  starts it with `docker compose up` semantics, which does **not** forward a
  terminal — so the `Enter your Google account email:` prompt can never be
  answered and the login stalls there (see the compose file's own note and
  [Troubleshooting](#troubleshooting)). Run the **login** via SSH; you may use
  Container Station afterwards for the normal, already-authenticated runs, which
  never ask for input.

Because HA and the login container run on the same box here, the produced
`data/secrets.json` is on the same host you import it from.

## Normal (already authenticated) runs

```bash
bash login.sh       # or: docker compose run --service-ports --rm googlefindmy-login
```
If `data/secrets.json` already holds valid tokens, the browser step is skipped
and the CLI lists devices directly. You can ignore the noVNC link.

## Forcing a fresh login

```bash
# Option A: wipe and start over
echo '{}' > data/secrets.json

# Option B: keep the file but force re-authentication
GFMY_ARGS="--reauth" bash login.sh
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
host's loopback** (`127.0.0.1:7901`). The file is then deleted on whichever of
two equally normal outcomes comes first: Home Assistant confirms the bundle, or
the 300 s TTL of the endpoint elapses. Both delete the same file, so both are a
correct ending.

Which one you see is mostly a matter of timing, and the TTL branch is common by
design: Home Assistant only confirms **after** the config entry has been set up
end to end (credential validation, coordinator refresh, FCM registration,
platform setup), and on a slow or busy instance that takes longer than the TTL.
The TTL is deliberately the fallback guarantee: the secret disappears from the
container even if Home Assistant never gets around to confirming (aborted setup,
restart, network hiccup). A TTL delete is therefore not an error and needs no
action from you; the bundle already lives in the config entry at that point.

The endpoint is strictly single-use: once the bundle has been handed out, every
further request is refused, and repeated wrong pairing codes lock it out. A
lockout closes the endpoint but **keeps** `secrets.json`, so you never have to
repeat the Google login: fall back to the file handoff (Track A) or the
clear-text output (Track C), or simply run the container again for a fresh code.
Those two fallbacks are alternatives, not cumulative: Track C is ephemeral by
contract and deletes the file right after printing it, so if `GFMY_CLEARTEXT=1`
is set as well, the clear-text output replaces the file handoff.

> **Where the boundary actually sits.** Under Docker's default *bridge* network
> the published port is DNAT'd onto the container's `eth0`, not onto the
> container's loopback, so the server inside the container binds `0.0.0.0` (all
> container interfaces) on purpose — otherwise the published port would be
> unreachable. Who can reach it is decided entirely by the **host-side publish**
> `${GFMY_ONECLICK_BIND:-127.0.0.1}:7901:7901` in `docker-compose.oneclick.yml`:
> loopback unless you name another address of this host.
>
> **A widened bind is clear text.** There is no TLS on 7901 (Home Assistant's
> client speaks plain http here), so a non-loopback publish puts the bundle on
> the wire readable by anyone on that segment, protected only by the one-time
> pairing code, the 300-second expiry, the single successful fetch and the
> lockout after five wrong codes. Use it inside your own LAN or on this host, for
> the seconds the handoff takes, never across an untrusted network. A wildcard is
> refused outright.
>
> **`network_mode: host` is NOT supported for this service.** In host networking
> there is no bridge and no publish indirection, so the `0.0.0.0` bind would be
> LAN-visible on every interface with no address left to choose. Keep the default
> bridge network; widen the publish deliberately, or use the SSH tunnel below.

```bash
GFMY_ONECLICK=1 bash login.sh
```

On Windows, run the two lines `set GFMY_ONECLICK=1` and `login.cmd`.

**The 7901 publish is opt-in.** Compose cannot leave a `ports:` entry out
conditionally, so the token-port publish lives in a separate overlay file,
`docker-compose.oneclick.yml`, which the launcher adds **only** when
`GFMY_ONECLICK=1`. Every other run (file handoff, `GFMY_CLEARTEXT=1`) starts
with no 7901 publish at all and therefore also starts on a host where port 7901
is already taken. By hand, the one-click start is:

```bash
docker compose -f docker-compose.yml -f docker-compose.oneclick.yml \
  run --build --service-ports --rm googlefindmy-login
```

The overlay switches `GFMY_ONECLICK` on by default, so that command needs no
extra environment variable. Note that passing any `-f` disables Compose's
implicit auto-load of a `docker-compose.override.yml`; if you keep such a file,
list it explicitly as a further `-f` (the launchers do this for you). The
overlay publishes on `${GFMY_ONECLICK_BIND:-127.0.0.1}:7901:7901`, so a by-hand
run without that variable is loopback-only, exactly like the launchers.

The container prints a **pairing code** (generated at runtime — there is no
default) **in the terminal you started the launcher from**, right after the
Google sign-in completes; it is not shown in the noVNC viewer, which only ever
displays Chrome's own window. In Home Assistant, choose the *Container login*
auth method and enter port `7901`, that pairing code, and **the address the
launcher printed** in its `token endpoint published on … port 7901` line: that is
`127.0.0.1` by default, or whatever you chose. To reach it from another machine
you have two ways. Either keep the loopback default and tunnel:

```bash
ssh -L 7901:127.0.0.1:7901 <docker-host>
```

or publish it on a LAN address of this host, which the launcher offers when you
pick track B and which you can also state up front:

```bash
GFMY_ONECLICK_BIND=192.168.1.21 GFMY_ONECLICK=1 bash login.sh
```

The second way carries the tokens **unencrypted** over that LAN for the few
seconds of the handoff, so use it on a network you trust, and never on an
untrusted one. A wildcard bind is refused.

> **If Home Assistant itself runs in a bridged Docker container** (the *HA
> Container* install method), `127.0.0.1` inside Home Assistant points at the
> **HA container**, not at the Docker host — so the host-published
> `127.0.0.1:7901` is unreachable and the SSH tunnel only helps if its local end
> is opened inside HA's own network namespace. Three supported routes:
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
>    GFMY_ONECLICK=1 docker compose run --use-aliases --build --service-ports --rm googlefindmy-login
>    ```
>
>    This route needs **no** `docker-compose.oneclick.yml`: Home Assistant talks
>    to the container directly over the shared network, so no host port has to be
>    published at all. Leaving the overlay out of this command also keeps
>    Compose's automatic pickup of the `docker-compose.override.yml` you just
>    created (an explicit `-f` would switch that off).
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
> 3. **Publish 7901 on a LAN address of this host.** Pick track B in the launcher
>    menu and accept or type the address, or state it up front with
>    `GFMY_ONECLICK_BIND=192.168.1.21 GFMY_ONECLICK=1 bash login.sh`, then enter
>    that same address in Home Assistant. This is the route for a Home Assistant
>    on **another machine**, and the only one of the three that puts the bundle on
>    the wire: it is **unencrypted**, so keep it inside a LAN you trust and only
>    for the seconds the handoff takes. A wildcard bind is refused.
>
> For **HA OS, HA Core, or host-networked HA on the same machine**, the plain
> `127.0.0.1:7901` above is correct and needs none of this.

### Terminal clear-text copy fallback (`GFMY_CLEARTEXT=1`)

If you cannot share a filesystem *and* cannot open a port (or you simply prefer
copy/paste), this switch prints the full `secrets.json` at the end of the login
**in the terminal you started the launcher from** (equivalently: in
`docker logs` for that run). Select, copy, and paste it straight into Home
Assistant's *secrets.json* field. No port is opened.

> The block is printed on the entrypoint's stdout, which is a different sink
> from the noVNC viewer: that viewer shows the container's X display, and a
> terminal opened inside that desktop is a separate shell that never sees this
> output. Copy the block where the launcher runs.

```bash
GFMY_CLEARTEXT=1 bash login.sh
```

The block is framed by `BEGIN secrets.json` / `END secrets.json` markers so it is
easy to select. The file is **ephemeral**: it is deleted immediately after it is
displayed, so nothing lingers on disk. This switch is independent of
`GFMY_ONECLICK` and of the plain file handoff.

Combined with `GFMY_ONECLICK=1`, the clear-text block runs *after* the one-click
endpoint has returned, and only if `secrets.json` is still there. Home Assistant
acknowledging the handoff, and the token TTL expiring, both delete the file, so
in those cases there is deliberately nothing left to print. The combination
therefore matters in exactly the case it was meant for: the endpoint's attempt
lockout, where the file is kept on purpose so that the file and clear-text
tracks still work.

> **Worth knowing before you combine the two.** The lockout is what someone
> *else* on the machine triggers by guessing the pairing code five times. In
> that situation this switch is what puts the full bundle into the launcher's
> terminal output, which also means into `docker logs` for that run: it is only
> as private as shell access to that host and access to the Docker daemon are.
> `GFMY_NOVNC_BIND` does **not** limit it, that setting only governs the noVNC
> viewer on port 7900. On a shared host, or wherever the container logs are
> collected, prefer the file handoff and leave `GFMY_CLEARTEXT` unset.

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

**If you started it with the launcher** (`login.sh` / `login.cmd`) or with
`docker compose run --rm`, there is nothing to stop: that is a one-shot
container which removes itself when the run ends. Quit the CLI with `q` (see
[After the login](#after-the-login-the-menu-q-and-the-wrap-up)) and it is gone.
Ctrl-C ends it too and still hands `data/` back, but it cuts a handoff short if
one is waiting, so prefer `q`.

**Only if you started it with `docker compose up`** does a container stay behind
for `docker compose down` to remove:

```bash
docker compose down
```

Either way, `data/secrets.json` survives (it is a host-mounted file, not inside
the container) unless a handoff track consumed it.

## Keeping in sync with the integration

Because the integration code is bind-mounted read-only at run time (it is **not**
baked into the image), the container always runs the integration version that is
currently installed. After a HACS update just start the login container again;
there is no separate image to rebuild for code changes.

## Troubleshooting

- **Asked for your e-mail but you can't type:** you probably ran
  `docker compose up` (which does not attach your terminal's stdin). Use
  `bash login.sh` / `docker compose run --service-ports --rm googlefindmy-login`,
  which runs in the foreground with stdin attached.
- **noVNC shows an empty desktop:** give it a few seconds — Chrome is started
  right after the display comes up, and the terminal prints
  `[AuthFlow] Installing ChromeDriver...` just before it appears. You are not
  expected to confirm anything in the terminal to make it start.
- **`Permission denied` running `./login.sh`:** start it through bash instead —
  `bash login.sh`. HACS installs the integration from a ZIP and does not restore
  the execute bit, so `./login.sh` can fail even though the script is marked
  executable in the repository. `bash login.sh` needs no execute bit and survives
  every HACS update; that is why the commands above use this form.
- **noVNC page won't load:** give the container a few seconds; check
  `docker compose logs` for `[entrypoint] Display ready.`
- **`port is already allocated` on 7900 or 7901:** another process on the Docker
  host holds that port. Port 7901 is only requested when you opt into the
  one-click handoff, so plain logins, the file handoff, and `GFMY_CLEARTEXT=1`
  are unaffected by a busy 7901; just start without `GFMY_ONECLICK=1`. For a
  busy 7900, stop the conflicting process (a leftover login container:
  `docker compose ps` / `docker compose down`).
- **noVNC password:** on the **loopback default** it is `secret` (a fixed
  default of the base image, safe only because loopback is not network-reachable).
  On a **LAN bind** the container mints a **per-run random password** instead and
  serves the viewer over self-signed **HTTPS**; read that password from the
  container output (`(password: …)` in the noVNC-available line and the
  `[AuthFlow]` banner), not from this guide.
- **`SessionNotCreatedException` / chromedriver version mismatch:** set
  `GOOGLEFINDMY_CHROME_VERSION` to the container's Chrome major version
  (uncomment the line in `docker-compose.yml`). The integration's
  `chrome_driver.py` normally auto-detects this.
- **Permission error writing `secrets.json`:** make sure `./data` exists (the
  launcher runs `mkdir -p data`). The container chowns it to itself for the run,
  so a host `chmod` is not required.
- **Can't read `secrets.json` as your user after a manual run:** you ran it by
  hand without exporting `GFMY_HOST_UID`/`GFMY_HOST_GID`, so the file stayed
  container-owned. Re-run via `bash login.sh`, or `sudo chown "$(id -u):$(id -g)"
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
