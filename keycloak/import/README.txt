Keycloak import / tenant access
==============================

This directory (import/) is copied into /opt/keycloak/data/import/ and every
.json file here is auto-loaded by `--import-realm` on container boot as a
FULL realm export. Only full RealmRepresentation exports belong here.

../partial-imports/partial-tenant-groups-and-roles.json
  Partial import of product roles + group tree for multi-state access:
    /global/super-admin
    /states/{STATE}/{contributor|reviewer}
  NOT a full realm export — uses the partial-import schema (top-level
  "ifResourceExists"), which crashes `--import-realm` if placed in this
  directory. Apply manually on an EXISTING realm (e.g. bharat-vistaar):
    Admin Console → Realm settings → Partial import → Skip existing

docs-pipeline-realm.json
  Local-dev realm export with legacy roles and a passkey browser flow.
  Configure the console client and enrollment mail using
  docs/KEYCLOAK_PASSWORDLESS.md. Existing realms need an explicit flow update.

Bootstrap (idempotent groups + roles + groups claim mapper)
----------------------------------------------------------
  python scripts/keycloak_bootstrap_tenant_groups.py \
    --base-url https://dev-auth-vistaar.da.gov.in/auth \
    --realm bharat-vistaar \
    --admin-password "$KEYCLOAK_ADMIN_PASSWORD"

App behaviour
-------------
SSO JWT carries groups; API maps them to instances + roles.
The Users screen shows Viewer for users with read-only access to every
provider. New assignments use the realm role global_viewer and group
/global/global-viewer.

Existing realms can leave members in /global/bh-viewer during rollout. The
API reads that group as global_viewer and includes the older is_bh_viewer
field in /auth/me for callers that still need it. To move a member, save
their Viewer access again in the Users screen. This joins the new global
group, removes the previous global group and keeps provider-scoped group
memberships. Ask the user to sign out and back in if their session still
has the previous group claim.
