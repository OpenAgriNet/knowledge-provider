---
status: accepted
---

# Read UI API and auth config at container start, not at build

Context: Every `VITE_*` value was compiled into the UI bundle by `ui/Dockerfile.prod`, so each environment needed its own image build to point at a different API path or Keycloak realm. Defaults had also drifted: the Dockerfile, compose and `scripts/export-prod-images.sh` disagreed on the API path, and compose built with `VITE_BASE=/docs-pipeline/` while the image only serves `/knowledge-provider/`.

Decision:
- `index.html` loads `config.js` before the app bundle. It sets `window.__ENV__`.
- `ui/src/runtimeEnv.js` `getEnv(name)` returns `window.__ENV__[name]` when set and rendered, else the build-time `VITE_{name}`. `config.js` and `auth/keycloak.js` read through it.
- The nginx image renders `config.js` from container env at start (`ui/docker/runtime-config.sh`, run as `/docker-entrypoint.d/40-runtime-config.sh`). A `config.js` mounted into the image is kept as is.
- nginx serves `config.js` with `Cache-Control: no-store`, so a changed value reaches users on their next load.
- Runtime: `API_BASE`, `AUTH_ENABLED`, `KEYCLOAK_*`, `DOCUMENT_VALIDITY_ENABLED`. Build-time: `VITE_BASE` only.

Consequences:
- One UI image is promoted across environments; changing the API path or Keycloak realm is a restart, not a rebuild.
- `npm run dev` is unchanged: `ui/public/config.js` is an empty `window.__ENV__`, so dev keeps reading `ui/.env`.
- Values in `config.js` are public, as they were in the bundle. It must never carry secrets.
- Images built by `scripts/export-prod-images.sh` no longer carry the production Keycloak defaults; each deployment sets them as container env.
- Serving the UI under a path other than `/knowledge-provider/` still needs a rebuild.
