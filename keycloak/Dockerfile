# Docker Hardened Image (dhi.io/keycloak). Requires `docker login dhi.io`
# before building. The 26.x tag line follows the DHI catalog; pin a
# specific 26.x.y (or digest) once the build is verified.
FROM dhi.io/keycloak:26

COPY --chown=65532:65532 providers/keycloak-2fa-email-authenticator.jar \
     /opt/keycloak/providers/keycloak-2fa-email-authenticator.jar

COPY --chown=65532:65532 import/ /opt/keycloak/data/import/

# DHI image already runs as nonroot (uid 65532); no `keycloak` user exists.
USER 65532
RUN /opt/keycloak/bin/kc.sh build
