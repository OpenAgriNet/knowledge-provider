FROM dhi.io/keycloak:26.8.2

COPY --chown=65532:65532 import/ /opt/keycloak/data/import/
RUN ["/opt/keycloak/bin/kc.sh", "build"]
