# Glama builds every listed server in a sandbox and withholds the ones whose
# build fails — an AI-inferred Dockerfile could not be produced for this repo,
# so the listing read "This server cannot be installed". Checking one in takes
# the guesswork out of it.
#
# Nothing here is needed to USE khwan-mcp: `uvx khwan-mcp` remains the install,
# and the server talks stdio to a client on the same machine.
FROM python:3.13-slim

WORKDIR /app

# pyproject reads README.md for the package description, so the build needs it
# present — copying only src/ fails at metadata time, not at import time.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

# stdio: the client speaks JSON-RPC over the container's stdin/stdout. No port.
# KHWAN_API_KEY is deliberately NOT baked in — the server starts, and lists its
# tools, without one; it is read on the first call that actually reaches Khwan.
ENTRYPOINT ["khwan-mcp"]
