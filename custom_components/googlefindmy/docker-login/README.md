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

> **`network_mode: host` is NOT supported for this service.** In host networking
> there is no bridge and no publish indirection, so the `GFMY_NOVNC_BIND`
> restriction collapses: the viewer is reachable on every interface, and on the
> loopback default that means the fixed password `secret` over plain HTTP on the
> whole LAN. Keep the default bridge network; bind deliberately, or use the SSH
> tunnel below.

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

### The two address roles

Where the noVNC viewer **binds** and what you are **told to open** are two
different questions, so they have separate settings. Never collapse them into
one value.

| Setting | Port | Consumer | Default | Why |
|---|---|---|---|---|
| `GFMY_NOVNC_BIND` | 7900 | the browser, via the host's network stack | `127.0.0.1` | Where the viewer actually listens. Everything the launchers print about reachability is derived from **this** value, never from the printed one. |
| `GFMY_NOVNC_URL_HOST` | 7900 | printed text only | the noVNC bind | The address you are told to open. A wildcard bind is never printed as a URL: `login.sh` substitutes the first detected address, `login.cmd` (which does not auto-detect) falls back to `127.0.0.1` and asks you to pass `--ip`. |

Home Assistant cannot derive the noVNC address for you: that link is opened by
**your browser**, which usually runs on a different machine than the Docker
host, and no machine on the LAN can know where you are clicking from.

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
  The release ZIP and a git checkout give `login.cmd` Windows (CRLF) line
  ends. If you fetch the single file some other way (the *Raw* button on
  GitHub, for example), it arrives with LF ends, and `cmd.exe` can then fail
  with `The system cannot find the batch label specified - <label>`; take it
  from the ZIP instead.

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
starts anything: `A` (the file in `./data`, the default a bare Enter picks) or
`B` (print the bundle in this terminal instead). Pass `--track a|b`, or set
`GFMY_CLEARTEXT` yourself, and the question is skipped. The two are
**alternatives, not layers**: the login always writes `data/secrets.json`, but
track B prints it and the container then deletes it, so B gives up the
watched-file import rather than adding to it (see
[Which handoff did you choose](#which-handoff-did-you-choose-and-what-does-it-cost)).

**The two launchers differ here, and it matters for automation.** `login.sh`
gates the question on a real TTY (`[ -t 0 ]`), so a non-interactive run (CI, a
pipe, `< /dev/null`) behaves exactly as it always did. `login.cmd` **cannot**:
batch has no `[ -t 0 ]` equivalent, so it always asks. At EOF (no redirect, or an
exhausted one) `set /p` leaves the answer untouched and the run continues on the
A default, which is the historical behaviour. But a redirect that still **has
content** is a real cost, not a no-op: the prompt **consumes its first line**.
That line never reaches the container's account-e-mail question, which can then
hit EOF and abort the login, and a line starting with `b` silently selects that
track. **Windows automation must therefore name the track explicitly** —
`login.cmd --track a` (or a preset `GFMY_CLEARTEXT`), which skips the question —
rather than answering it through redirected stdin. `login.cmd` carries the same warning in the comment above the prompt.

Neither track publishes a port of its own, so a host with a busy port can never
stop the login container from starting.

### Manual alternative (`docker compose`)

If you prefer to run it by hand instead of the launcher:

```bash
cd config/custom_components/googlefindmy/docker-login
mkdir -p data
docker compose run --build --service-ports --rm googlefindmy-login
```

This publishes noVNC only; no other port is opened.

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

## Language and keyboard

Several pieces of software show up on your screen during a login, and each
takes its language from a different place. None of them is pinned to a
language by this container.

| What you see | Where its language comes from | How to change it |
| --- | --- | --- |
| The noVNC viewer (toolbar, panels) | Your **own browser's** language, which noVNC reads from it directly | Change your browser's language |
| Chrome inside the viewer, and the Google sign-in page it loads | `GFMY_LOCALE`, which the launchers fill in from your own locale; empty on a bare `docker compose run`, which reads no locale of its own | `bash login.sh --lang fr` or `GFMY_LOCALE=fr bash login.sh` |
| The `[login]` lines and the help of `login.sh` and `login.cmd` | The same `GFMY_LOCALE`: German for a `de` tag (`de`, `de-DE`, `de_DE.UTF-8`), English for everything else. `login.cmd` writes its German without umlauts (`ae`, `oe`, `ue`, `ss`), so it reads correctly on any Windows console | `bash login.sh --lang de`, `login.cmd --lang de` |
| The `[entrypoint]` / `[AuthFlow]` lines in your terminal | English, like the rest of the project | — |

`--lang <tag>` is the short name for `GFMY_LOCALE` on both launchers: `bash
login.sh --lang de` is the same as `GFMY_LOCALE=de bash login.sh`, `login.cmd
--lang de` the same as `set GFMY_LOCALE=de` before `login.cmd`, and it wins
over a `GFMY_LOCALE` you inherited. A value that starts with `-` is taken as
the next option and refused. An empty one differs between the two:
`login.sh --lang=` means "no preference", like `GFMY_LOCALE=`, while
`login.cmd` refuses it, because `cmd.exe` cannot hold an empty variable; use
`--lang C` there. Because the launcher's own lines and Chrome read
the same value, the terminal and the sign-in page always speak the same
language. A bare `docker compose run` has no launcher and no flag; there,
`GFMY_LOCALE=de` is the way.

The noVNC viewer is the exception, deliberately: it follows your **browser**,
not `GFMY_LOCALE`. noVNC's own German translation is also incomplete, so with a
German browser you will still meet English text inside the viewer; the
*Clipboard* panel, for example, shows a German heading above an English hint.
That mix comes from noVNC, not from this container, and the clipboard box works
the same in either language.

The launchers fill `GFMY_LOCALE` in from your own environment: `login.sh` reads
your shell locale (`LC_ALL` / `LC_MESSAGES` / `LANG`) and `login.cmd` asks
Windows for its culture name, so the sign-in page usually arrives in the
language you read without you setting anything. Both accept a locale in either
spelling (`de-DE` or `de_DE.UTF-8`).

Set it yourself to override that — `GFMY_LOCALE=en` to stay in English — or
hand the choice back to Chrome, which in this image means English. There are two
spellings for handing it back, and which one you have depends on your shell:

| Shell | Opt out with | Why |
| --- | --- | --- |
| `bash` (`login.sh`) | `GFMY_LOCALE= bash login.sh` **or** `GFMY_LOCALE=C bash login.sh` | An empty value is still a value here, so the launcher leaves it alone; `C` it passes on, and the container reads that as no preference |
| `cmd.exe` (`login.cmd`) | `set GFMY_LOCALE=C` | `set GFMY_LOCALE=` *deletes* the variable on Windows, and the launcher then asks Windows for your culture instead |
| `docker compose` directly | `GFMY_LOCALE=C` or nothing at all | This path reads no locale of its own, so it is already empty unless you fill it in |

`GFMY_LOCALE=C` is the one that works everywhere, `docker compose` included:
`C` is the POSIX locale for "no localisation", and the container reads it as a
stated absence of a preference rather than as a broken value — so it is dropped
in silence, without the warning a typo earns.

Any OTHER value that is not a language tag is ignored with a warning rather than
passed on: a login must not fail over the language of its own error messages. Two
kinds of value are dropped in silence instead, and deliberately so, because both are an
answer rather than a mistake: an empty one, and the POSIX locale above. Spell the
latter with a capital `C` — `c` and `posix` end up at the same English page, but
by way of the warning, which reads as if your setting had been wrong. What is read is the
part before the first `.` or `@`, with `_` read as `-` — together that is what
turns `de_DE.UTF-8` into `de-DE` — so whatever follows those characters is
dropped without a warning, and only the remainder has to look like a language
tag. To see which tag actually reached Chrome, add `--debug` to `GFMY_ARGS`
(`GFMY_ARGS=--debug bash login.sh`, or `set GFMY_ARGS=--debug` before
`login.cmd`): the login then logs the language it applied, and, if your value
was shortened, what it was shortened from. Note that `--debug` makes the whole
run verbose in the launcher terminal and in `docker logs`, your account address
included, so it is a diagnostic setting rather than an everyday one.

### Typing "@" and other AltGr characters

A VNC viewer does not forward your keyboard; it forwards *characters* plus the
modifier keys you are holding. On a German, French or Spanish layout the "@" of
your e-mail address is made with AltGr, and the base image's VNC server used to
receive that stray AltGr *around* the character and produce nothing at all —
which makes the login impossible, since every account name contains an "@".

The container now tells its VNC server to resolve keys through XKEYBOARD and to
drop the level-3 modifiers together with the right Alt key (`ISO_Level3_Shift`,
`Mode_switch` and `Alt_R`, in `~/.x11vncrc`, written by `entrypoint.sh`), so the
character arrives on its own. `Alt_R` is on the list because on some hosts AltGr
arrives as the right Alt held down next to the finished "@", and Chrome then
reads Alt+@ as a shortcut. The price is the right Alt key itself: inside the
container it does nothing, which no login needs. It is on by default;
`GFMY_KEYBOARD_FIX=0` restores the base image's behaviour, right Alt key included.

Two fallbacks, in the order worth trying:

1. **Paste instead of type.** Open the panel on the left edge of the noVNC
   window, type or paste the text into the *Clipboard* box, then press `Ctrl+V`
   in the browser field. This path never touches the keyboard translation and
   therefore always works.
2. **Give the container your layout.** `GFMY_KEYBOARD_LAYOUT=de bash login.sh`
   (the value is passed to `setxkbmap`, so `de -variant nodeadkeys` works too).
   Only needed for the rarer cases the default cannot reach, such as dead keys.
   If the layout cannot be applied, the container says so on startup, and where
   `setxkbmap` gave a reason it repeats that reason, so a rejected name is
   distinguishable from a display that was not up yet. Either way the login
   continues on the default layout rather than aborting.

## Cancelling a login

The login has two sign-ins, and each opens its own Chrome window. Sign-in 1 of
2 gets the account token; sign-in 2 of 2 gets the key from the Google unlock page
and is skipped when `secrets.json` already holds that key. A login can end early
in four ways that the flow itself names, and the line it prints says which one it
was. (`Ctrl+C` is a fifth way out. It ends with the same status, but it is the
one cancellation that still prints a Python `KeyboardInterrupt` traceback instead
of an `[AuthFlow]` line. A `--reauth` run can also stop with that status before
either window opens, at the question after its overwrite warning; see
[Forcing a fresh login](#forcing-a-fresh-login).)

A window you close is opened again first, at most twice per sign-in (see
[Browser closed by accident](#browser-closed-by-accident)). The two "closed"
lines below therefore appear only on the third close.

Closing the sign-in window 1 of 2 a third time, after it was opened again twice:

```
[AuthFlow] Login cancelled: the browser window was closed before Google issued
an account token. Nothing was saved; start the login again to retry.
```

Walking away from sign-in 1 of 2 and letting its five-minute wait expire, with
the window still open:

```
[AuthFlow] No login completed within 5 minutes, so no account token was
received. Nothing was saved; start the login again when you are ready.
```

Closing the sign-in window 2 of 2 a third time, after it was opened again twice:

```
[AuthFlow] The sign-in window 2 of 2 was closed before Google sent the key. The
result of sign-in 1 of 2 is kept; run the login again to finish sign-in 2 of 2.
```

Pressing cancel on the Google unlock page during sign-in 2 of 2, which is not
opened again because the page ended the step on purpose:

```
[AuthFlow] Sign-in 2 of 2 was cancelled on the Google page before it sent the
key. The result of sign-in 1 of 2 is kept; run the login again to finish
sign-in 2 of 2.
```

These are cancellations, not failures. The exit status is `130` ("cancelled by
the user") on every one of them, which is deliberately distinct from the `1`/`2`
the launcher uses for its own failures, so a script around this can tell "you
stopped" from "it broke". Running out of time in sign-in 2 of 2 is not one of
them: it ends with status `1` as a failed key step, and the window is not opened
again.

A cancelled sign-in 1 of 2 stores nothing. A cancelled sign-in 2 of 2 keeps what
sign-in 1 of 2 stored, so `secrets.json` then holds the tokens without the key.
Home Assistant does not offer such a file, and the container names it on its way
out, with the path inside the container in brackets:

```
[entrypoint] The sign-in stopped before it finished. It left an incomplete credential file:
[entrypoint]   data/secrets.json next to the launcher (/data/secrets.json inside the container).
[entrypoint] Home Assistant does not offer this file. Run the login again to complete it, or delete the file.
```

Either way, re-running the login is the whole recovery procedure. After a
cancelled sign-in 2 of 2 the re-run finds the stored tokens and opens only the
second window.

Cancelling a **re-authentication** costs nothing either. `--reauth` (passed
through `GFMY_ARGS`, see [Forcing a fresh login](#forcing-a-fresh-login)) clears
the cached tokens before it starts, because an empty cache is what makes the CLI
open the login at all. If sign-in 1 of 2 then ends without a token (you closed
its window a third time, the wait expired, you pressed `Ctrl+C`, the container
was stopped), the cleared tokens are put back and the run says so:

```
Login did not complete; restored 3 cached token(s).
```

(The number is whatever was cached; a used cache holds more than three keys.)

A `--reauth` cancelled in sign-in 1 of 2 therefore leaves you signed in exactly
as you were. A `--reauth` cancelled in sign-in 2 of 2 puts nothing back: sign-in
1 of 2 has already stored the new sign-in, and running the login again (without
`--reauth`) finishes the second step. Three cases cannot be undone, and each says
so instead of pretending otherwise:

* **The login already stored a new token** and was then cut short in the moment
  between saving it and returning — a `Ctrl+C`, a broken pipe or a stop from
  outside in that narrow window. The cleared tokens are then *not* put back, because they belong to the
  sign-in the new one replaces and a mix of the two would send the next run back
  to the old account:

  ```
  A new oauth_token was stored before the run ended, so the previous tokens were
  not put back (they belong to the old sign-in). Run the login again (without
  --reauth) to finish signing in with the new one.
  ```

* **`secrets.json` turned unreadable** in the meantime. The restore refuses to
  overwrite the file (it would throw away whatever else the file still holds)
  and tells you to run `--reauth` again.

* **Writing the restored file failed** (a full or read-only disk). The run says
  so and leaves the cleared state on disk rather than replacing the reason it
  ended with a disk error; `--reauth` again is the recovery here too.

Stopping the container from outside is a different ending. `docker stop` sends
SIGTERM, and the run then ends with exit status `143` (128 + 15, the status a
shell reports for a process that SIGTERM ended) in whatever step it was. A
stop while sign-in 2 of 2 reads the account vault or waits for the push
registration first saves what the run had read by then and says so:

```
The login was stopped (SIGTERM) during sign-in 2 of 2; whatever it had read by then was saved.
```

A stop before the key was read can leave the incomplete file shown further
up, and the same notice then names it. Running the login again is the recovery
here too.

Under `--reauth`, a stop during sign-in 1 of 2 first puts the cleared tokens
back, exactly as a cancelled sign-in 1 of 2 does (the restore line, or one of
the three exceptions, shown above), and then ends with exit status `143` and
this line:

```
The login was stopped (SIGTERM) during sign-in 1 of 2.
```

In the usual case you are then signed in exactly as before the run.

## Browser closed by accident

Closing a sign-in window before it finished does not end the login at once. The
terminal names the window and opens it again, at most twice per sign-in.

Closing the sign-in window 1 of 2 the first time:

```
[AuthFlow] The sign-in window 1 of 2 was closed before it finished; opening it
again (1 of 2). Press Ctrl+C here to cancel.
```

Closing the sign-in window 2 of 2 the first time, also while it reads the
account vault:

```
[AuthFlow] The sign-in window 2 of 2 was closed before it finished; opening it
again (1 of 2). Press Ctrl+C here to cancel.
```

The second close says `(2 of 2)`. The third one ends the login with the line
quoted under [Cancelling a login](#cancelling-a-login) and exit status `130`.
Only a window you closed is opened again. A crashed browser, a wait that ran out
and a cancel on the Google unlock page end the run as before.

The program closes each window itself once its sign-in is done, and says so
first, so a window that vanishes is not taken for a failure. Leave it to the
program at that point. When sign-in 1 of 2 is done:

```
[AuthFlow] Sign-in 1 of 2 finished; this window closes now. Sign-in 2 of 2
opens shortly unless the key is already stored.
```

When sign-in 2 of 2 is done:

```
[AuthFlow] Sign-in 2 of 2 finished; this window closes now.
```

When `secrets.json` already holds the key, sign-in 2 of 2 only reads the account
vault for the owner-key versions (see
[Owner-key versions from the account vault](#owner-key-versions-from-the-account-vault)).
Its window is opened again the same way. Closing it a third time does not fail
the login, because the bundle is complete without the versions; the run says why
they are missing:

```
The sign-in window 2 of 2 was closed before the account vault was read.

Note: owner-key versions were not saved (cause=window_closed, entries=0).
This is non-fatal: the generated secrets.json is ready to use.
```

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
   login): the clear-text block, described under
   [Terminal clear-text copy fallback](#track-b-terminal-clear-text-copy-gfmy_cleartext1).
   It runs *before* the container tears itself down.
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

| | Track A — file | Track B — clear-text block |
|---|---|---|
| How to ask for it | nothing (always on) | `GFMY_CLEARTEXT=1`, or pick it in the launcher menu |
| What it hands over | `data/secrets.json` on disk | the bundle printed in the launcher's terminal |
| What happens to the file | **stays** until you delete it | **deleted** right after it is printed |
| Where it goes in Home Assistant | the integration finds it by itself (see below), or you import the file by hand | paste into the *secrets.json* field |
| Good for | Home Assistant that shares this filesystem | no shared filesystem |

Track B *replaces* the file handoff rather than adding to it, because it deletes
the file after printing. Its security note is in the
[clear-text section](#track-b-terminal-clear-text-copy-gfmy_cleartext1).

A third track once existed: it published a second port on the Docker host and
Home Assistant fetched the bundle from it, authenticated by a code the container
printed. It was removed in PR #1218 (2026-07), because that transport was
unencrypted HTTP: safe only while the publish could not leave the Docker host,
and every way of securing it beyond that host cost the user more steps than
pasting the bundle. Only the noVNC viewer is published now.

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
- The **credential bundle** is what the login produces: `data/secrets.json`,
  or the block Track B prints. It is what Home Assistant needs, and it is not a
  password you type anywhere during the login.

Different purposes, different lifetimes. The noVNC password opens the viewer and
nothing else; the bundle is the result you carry over to Home Assistant.

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
If `data/secrets.json` already holds valid tokens and the owner-key version
field (see below), the browser step is skipped and the CLI lists devices
directly. You can ignore the noVNC link.

A `secrets.json` written before that field existed does not have it yet. Then
each run signs in to Google once more (no unlock page) to read the key vault,
and waits up to 5 minutes for that sign-in, until one run has written the
field. Open the noVNC link for it. Without a terminal (Container Station
application) the CLI skips this step silently and lists devices as before.

### Owner-key versions from the account vault

Some trackers are encrypted with a newer owner-key version than the one Google's Spot service hands out. Home Assistant can still decrypt them when `secrets.json` carries the owner keys of every version, read from the account's key vault during the login run (field `owner_key_versions`).

A login run writes this field when both conditions hold:

- The vault was read in the same browser session. A full login (Google sign-in plus the unlock page) reads it together with the shared key; the unlock page waits up to 5 minutes for your PIN or pattern, then the run ends with a timeout. If the shared key is already cached but the field is missing, a normal run signs in to Google once more, without the unlock page, and reads only the vault.
- A self-test passes: the vault entry of the version Spot serves right now must decrypt to the owner key Spot returns. If Spot's version is not in the vault, or the entry does not match, the field is not written and the run prints `Note: owner-key versions were not saved (cause=..., entries=...)`. The `secrets.json` stays usable either way.

When a tracker needs a version that neither Spot nor the field can supply, Home Assistant raises a Repairs issue for that tracker instead of asking you to re-authenticate the whole account. Its text names one of two remedies:

> A tracker needs owner key version {required_version}, but Google serves version {current_version} and the secrets bundle has no usable key for this version. Versions in the bundle: {bundle_versions}.
>
> Run the login tool again (Docker login helper or `main.py`) and import the new `secrets.json`. If the tool still keeps a `secrets.json` from an earlier login, delete that file first or start the tool with `--reauth` (Docker login helper: `GFMY_ARGS=--reauth`); otherwise it reuses the file and does not read the key vault again. Other trackers are not affected.

> A tracker needs owner key version {required_version}, but Google serves version {current_version} and the key vault of this Google account did not hold this version when the login tool last read it. Versions in the bundle: {bundle_versions}.
>
> Google may add this version to the vault later; only a login run after that can fetch it. To try again, run the login tool (Docker login helper or `main.py`) and import the new `secrets.json`. If the tool still keeps a `secrets.json` from an earlier login, delete that file first or start the tool with `--reauth` (Docker login helper: `GFMY_ARGS=--reauth`); otherwise it reuses the file and does not read the key vault again. Until then the tracker's locations stay unavailable, unless Google starts serving version {required_version}. This issue disappears once the tracker can be decrypted again. Other trackers are not affected.

To see what the vault and Spot report, run the read-only diagnosis. It prints domain, version and epoch of each vault entry, their count, a cause class and the versions Spot serves, never key material, and it never writes the owner-key version field. Like any login run, it signs in first and may refresh the login tokens in `secrets.json`:

```bash
GFMY_ARGS="--diagnose-owner-key-versions" bash login.sh
```

## Forcing a fresh login

```bash
# Option A: wipe and start over
echo '{}' > data/secrets.json

# Option B: keep the file but force re-authentication
GFMY_ARGS="--reauth" bash login.sh
```

There is only one `data/secrets.json`. If it still holds a bundle (for example
from a first run for another account that Home Assistant has not imported yet),
a `--reauth` run warns before it changes anything; the account is masked:

```
The credential file (data/secrets.json next to the launcher) still holds a bundle for u***@example.com.
If Home Assistant has not imported it yet, this run replaces it: import it first, or copy the file.
```

At a terminal it then asks you to press Enter to go on. `Ctrl+C` (or the end of
input) stops the run with exit status `130` and this line, and the file stays as
it was:

```
Stopped. Nothing was changed.
```

Without a terminal (input not attached) the warning is printed and the run goes
on, unless `GOOGLEFINDMY_ASSUME_INTERACTIVE=1` says someone is there (an IDE
console that passes input through a pipe); then it asks as well. A file only knows that it holds a bundle, not whether Home Assistant has
imported it already, so the warning also appears when the import has happened
but the file could not be removed (a read-only mount).

`GFMY_ARGS` is forwarded to `main.py`. Useful values: `--reauth` (force login),
`--debug` (verbose bootstrap/FCM logging), `--entry <id>` (select one config
entry when the cache holds several).

## Terminal handoff (optional, no manual copy of the file)

By default the login writes `data/secrets.json` and you import that file into
Home Assistant. One optional switch prints the bundle instead, for the case
where Home Assistant cannot see this directory. It is off unless you opt in;
when you do opt in, it **replaces** the file handoff, because the file is
deleted right after it is printed.

### Track B: terminal clear-text copy (`GFMY_CLEARTEXT=1`)

If you cannot share a filesystem (or you simply prefer copy/paste), this switch
prints the full `secrets.json` at the end of the login
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
displayed, so nothing lingers on disk.

> **Worth knowing before you use it.** This switch puts the full bundle into the
> launcher's terminal output, which also means into `docker logs` for that run:
> it is only as private as shell access to that host and access to the Docker
> daemon are. `GFMY_NOVNC_BIND` does **not** limit it, that setting only governs
> the noVNC viewer on port 7900. On a shared host, or wherever the container logs
> are collected, prefer the file handoff and leave `GFMY_CLEARTEXT` unset.

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
  `docker compose logs` for `[entrypoint] Display ready.` If it shows
  `[entrypoint] ERROR: supervisord exited` instead, the lines above it name
  the cause.
- **A character will not type ("@", "\\", "|", "€"):** those are AltGr
  characters, which a VNC viewer handles differently from the rest. Paste them
  instead — the panel on the left edge of the noVNC window has a *Clipboard*
  box, and `Ctrl+V` puts its contents into the browser field. See
  [Typing "@" and other AltGr characters](#typing--and-other-altgr-characters)
  for the setting behind it.
- **Everything appears in the wrong language:** the viewer follows your own
  browser, Chrome follows `GFMY_LOCALE` (taken from your shell locale unless you
  set it), and so do the launcher's own `[login]` lines.
  `bash login.sh --lang en` (or `login.cmd --lang en`) keeps both in English. See
  [Language and keyboard](#language-and-keyboard).
- **The run ended with exit status 130:** that is not an error, it is a
  cancellation. The `[AuthFlow]` line above it names which sign-in ended and
  why: a window closed a third time, a wait that ran out, or a cancel on the
  Google unlock page. A `Ctrl+C` gets the same status but ends in a
  `KeyboardInterrupt` traceback rather than an `[AuthFlow]` line. A `--reauth`
  run that ends with `Stopped. Nothing was changed.` was stopped at the question
  after its overwrite warning, before any window opened, and left `secrets.json`
  as it was (see [Forcing a fresh login](#forcing-a-fresh-login)). Start the login
  again; after a cancelled sign-in 2 of 2 it opens only the second window. The
  messages are quoted in full under [Cancelling a login](#cancelling-a-login).
- **The run ended with exit status 143:** the container was stopped from
  outside, usually by `docker stop`, which sends SIGTERM. It is neither a
  cancellation (`130`) nor a failed step (`1`). A stop at the tracker menu,
  after the login finished, leaves a complete `secrets.json`; a stop before
  that needs another run. What a stop during sign-in 2 of 2 keeps is described
  under [Cancelling a login](#cancelling-a-login).
- **A `TimeoutException` traceback instead of an `[AuthFlow]` line:** that is
  the opposite case, and the exit status is *not* `130`. Chrome or ChromeDriver
  stopped answering while the login was still being waited for, so the run
  reports a driver failure rather than claiming you cancelled anything. The
  wait may not even have started to run out. Retry the login; if it keeps
  happening, the traceback is what to attach to a bug report.
- **`port is already allocated` on 7900:** another process on the Docker host
  holds the noVNC port. Stop the conflicting process (a leftover login
  container: `docker compose ps` / `docker compose down`).
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
