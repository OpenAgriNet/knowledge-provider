# TODO: this is a third-party (Red Hat/Quay) image, not a Docker-curated
# common image — DHI's catalog likely has no hardened equivalent for it.
# Needs its own decision (stay on quay.io, or find/build an alternative)
# rather than the dhi.io swap the other Dockerfiles are getting.
FROM quay.io/keycloak/keycloak:26.1.4

COPY providers/keycloak-2fa-email-authenticator.jar \
     /opt/keycloak/providers/keycloak-2fa-email-authenticator.jar

COPY import/ /opt/keycloak/data/import/

USER keycloak
RUN /opt/keycloak/bin/kc.sh build
