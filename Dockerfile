# The execution image for `sandbox.kind: docker`.
#
# The harness itself runs on the host. This image is only ever entered by a
# tool command or an acceptance command, so it needs the runtimes those
# commands use and nothing else -- no harness source, no credentials, no
# network configuration. It is entered with the run workspace bind-mounted at
# /workspace and nothing else visible.
#
#   docker build -t yatra-harness-sandbox .
#   uv run harness run <task> --config configs/sandboxed.yaml ...
FROM python:3.12-slim

# git is needed because acceptance commands routinely shell out to it, and a
# workspace is a git repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The workshop's spreadsheet task needs openpyxl. Installed here rather than
# left to the agent, because the container has no network at runtime.
RUN pip install --no-cache-dir --root-user-action=ignore openpyxl==3.1.5

# The container runs as the host's uid (passed by --user at run time) so files
# written into the mounted workspace stay owned by the operator. That uid has
# no passwd entry, so give the tools a writable HOME that exists.
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    GIT_CONFIG_NOSYSTEM=1

WORKDIR /workspace

# No ENTRYPOINT on purpose: the harness supplies the whole command, and an
# entrypoint would silently rewrite what the policy engine approved.
CMD ["python", "--version"]
