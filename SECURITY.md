# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest `v1.7.x` release | Yes |
| Older `1.7` releases | Fixes land in the next `1.7` release, not as backports |
| Anything older | No |

This is a Home Assistant custom integration installed through HACS. "Supported"
means: a confirmed security issue is fixed on the `1.7` development branch and
ships with the next release from it. There is no long-term support branch.

## Reporting a vulnerability

Use **GitHub private vulnerability reporting**: open the
[Security tab](../../security) of this repository and choose *Report a
vulnerability*. That creates a private draft advisory visible only to you and
the maintainers. It is enabled on this repository; no email address, no
external form, no account anywhere else.

If the button is not there, you are looking at a fork or mirror where the
setting is off. In that case open a normal issue **without** exploit details and
say that you have something to report privately.

What helps, in rough order of usefulness:

- the integration version (Settings → Devices & Services → Google Find My
  Device, or `manifest.json`),
- what an attacker would have to already possess (network reach, a Home
  Assistant login, filesystem access, a stolen link),
- the concrete path from that starting point to the impact,
- a log excerpt or a reproduction, with tokens removed.

Expect a first response within about a week. This is a spare-time project; that
is a realistic figure, not a service level.

## Security finding or hardening suggestion?

Both are welcome, but they are different things and mixing them costs everyone
time. Please say which one you are filing.

- A **security finding** names an attacker, what that attacker already has, and
  what they gain. "An unauthenticated request to *X* returns *Y*" is a finding.
  Findings belong in private vulnerability reporting.
- A **hardening suggestion** improves a property without a reachable attack
  path: a header, a shorter secret lifetime, a narrower dependency set, a
  clearer log line. These are useful and we act on them. They belong in a normal
  public issue, one issue per suggestion, so each can be discussed, fixed and
  closed on its own.

The distinction matters here for a specific reason. Parts of this project run
**outside** Home Assistant: the browser-based credential extraction is a manual,
user-initiated command-line step on the user's own machine, not something the
Home Assistant runtime ever executes. A property of that step is not a property
of a running Home Assistant instance, and rating it as if it were produces
severity numbers that nothing in the code supports. If you are unsure which side
of that line your finding falls on, say so in the report and we will sort it out
together.

## Scope

In scope: this integration's code, its Home Assistant HTTP views, what it
stores, what it logs, and its dependency manifest.

Out of scope: Google's services and their protocols; Home Assistant Core's own
trust model (anyone with administrator or filesystem access can read the
credentials of *every* integration, including this one — that is Home
Assistant's boundary, not ours); and findings that require access you would only
have after already compromising the host.
