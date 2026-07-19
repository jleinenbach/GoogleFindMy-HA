# GoogleFindMy-HA — containerised one-time Google login helper.
#
# This is a Docker wrapper around the *integration's own* standalone CLI
# (custom_components/googlefindmy/main.py), NOT the upstream GoogleFindMyTools
# CLI. It bundles Chrome + a virtual display + noVNC so the one-time Google
# login can be driven from a browser tab, and it writes a secrets.json that is
# byte-for-byte the file the integration consumes.
#
# Attribution: the noVNC/entrypoint container design is adapted from
# sincze/GoogleFindMyTools @ docker-novnc-auth
# (https://github.com/sincze/GoogleFindMyTools/tree/docker-novnc-auth); the
# original CLI is leonboe1/GoogleFindMyTools. This wrapper turns that design
# onto the integration's *own* main.py so the produced secrets.json matches the
# installed integration byte-for-byte. See README.md → Credits.
#
# Build model: M2 (runtime mount). The integration code is NOT baked into the
# image — it is bind-mounted read-only at run time from its real import path
# (see docker-compose.yml). The image therefore stays version-independent and
# always runs whatever integration version is installed. Only the declared
# runtime dependencies are baked in.
#
# Build context is the integration package dir (custom_components/googlefindmy),
# see docker-compose.yml `build.context: ..`.
FROM selenium/standalone-chrome:latest

USER root
WORKDIR /app

# --- Python dependencies -------------------------------------------------------
# The integration's declared runtime deps are sufficient for the standalone CLI.
# Home Assistant is deliberately NOT installed: main.py installs lightweight
# homeassistant.* stubs when the package is absent (find_spec("homeassistant")
# is None), so the login/bootstrap flow runs without a full HA install.
#
# requirements.txt lives inside the integration package (the build context), so
# it is COPYed at build time. The integration *code* is NOT copied (M2): it is
# mounted at run time, which keeps the image independent of the code version.
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# secrets.json is written next to main.py under Auth/ (the upstream layout the
# integration mirrors). The dir is provided at run time by a writable bind mount
# (docker-compose.yml → ./data). We still create it here as a fallback mount
# point and hand /app to the runtime user.
RUN mkdir -p /app/custom_components/googlefindmy/Auth \
 && chown -R seluser:seluser /app \
 && chown -R seluser:seluser /home/seluser

COPY docker-login/entrypoint.sh /opt/bin/gfm-entrypoint.sh
RUN chmod +x /opt/bin/gfm-entrypoint.sh

USER seluser
ENTRYPOINT ["/opt/bin/gfm-entrypoint.sh"]
