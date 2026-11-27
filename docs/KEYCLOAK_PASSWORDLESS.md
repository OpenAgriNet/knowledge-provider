# Console sign-in

The console collects the email address and the one-time code. The Python API starts a private Keycloak authentication session and submits both steps. Keycloak sends the code using the realm's SMTP settings. Python exchanges the authorization code with PKCE and uses the confidential client for session refresh and logout.

The browser calls `POST /auth/email/send`, `POST /auth/email/verify` and the console's session endpoints. The send response carries a short-lived encrypted challenge. The API uses it to resume the private Keycloak session. PostgreSQL stores one digest per address for the send cooldown. Keycloak owns the code, its lifetime and attempt limit.

## Configure Keycloak

1. Install the reviewed Email OTP Authenticator v26.7.2 build for Keycloak 26.8.2. In the self-hosted Compose stack set `KEYCLOAK_EMAIL_OTP_JAR` to the absolute path to that file. Keep the Keycloak listener on the private network. [Plugin release](https://github.com/mesutpiskin/keycloak-2fa-email-authenticator/releases/tag/v26.7.2)
2. Configure and test the realm SMTP settings. Enable login with email and require unique email addresses. Preserve the existing Keycloak account IDs and provider groups during migration.
3. Bind a browser flow to the console client through **Authentication Flow Overrides**. Set **Identity Provider Redirector** and an email subflow to **Alternative**. Leave the redirector’s default provider blank. In the email subflow, set **Username Form** and **Email OTP (No Enrollment)** to **Required**. Set delivery to Keycloak SMTP and simulation to off. Use six digits, a five-minute lifetime and a three-attempt limit.
4. Keep the console client confidential. Enable Standard Flow with PKCE S256. Keep Direct Access Grants and Implicit Flow disabled. Register the exact redirect URI configured in `KEYCLOAK_CONSOLE_REDIRECT_URI`. The API consumes this redirect internally. The local fixture registers `http://127.0.0.1/knowledge-provider/auth/sso-callback`.
5. Set `KEYCLOAK_CLIENT_ID` and `KEYCLOAK_CLIENT_SECRET` on the API. Set `KEYCLOAK_ISSUER` to the issuer in Keycloak tokens. When the API uses a different address, set `KEYCLOAK_BACKCHANNEL_URL` and `KEYCLOAK_JWKS_URL` to their private URLs. The browser build needs only `VITE_AUTH_ENABLED=true` and the usual API base. The gateway blocks Keycloak paths. The exact Google callback goes to Python. [Keycloak reverse proxy guidance](https://www.keycloak.org/server/reverseproxy)

## Keep Google sign-in

Keep the existing Google identity provider with alias `google`, its OAuth client, and the users' Keycloak identity links. Set `KEYCLOAK_GOOGLE_CONSOLE_URL` to the console login URL, for example `https://knowledge.example.org/knowledge-provider/login`. This enables **Continue with Google**. Keycloak builds the Google callback from its frontend hostname and realm path. Set its frontend hostname to the console origin plus `/auth`, use that issuer on the API, and keep the API's backchannel and JWKS URLs private.

Register `https://knowledge.example.org/auth/realms/<realm>/broker/google/endpoint` in the existing Google OAuth client. If the previous callback used a different host, add this URL before switching the console. Route this exact path to the Python endpoint `/auth/google/callback` using the [gateway example](../deploy/nginx-docs-pipeline.snippet.conf). Python validates the callback against the browser's short-lived sign-in state and exchanges the resulting PKCE code. [Keycloak Google identity provider](https://www.keycloak.org/docs/26.8.1/server_admin/#_google)

A user with an existing Google link signs in to the same Keycloak account. For an unlinked account, use the console's email code and have an administrator verify and link the account. The console does not auto-link users by email.

## Disable password prompts

In the realm, turn off **Forgot password** and disable the **Update Password** required action. Apply these settings directly to an existing realm. The import templates apply when a realm is created. Remove a queued `UPDATE_PASSWORD` action from an affected legacy account and preserve other required actions. Updating an existing user through the console removes that action. Keep password forms out of the console's browser flow and the Google broker flow.

## Verify

Open the console's login page and enter the email address. Submit an incorrect code and then the correct code in the same form. Check that the account has the expected provider groups and the session survives a refresh. Sign out and confirm the refresh token has been revoked. With Google enabled, sign in with an existing linked account and confirm the user ID and provider groups. Check that the browser returns from Google to Python and that Keycloak has no published port.

The local fixture sends mail to Mailpit. Production rollout still needs a test through deployment SMTP. A disposable Keycloak realm verified the Google exchange using a signed test identity. Verify the production OAuth client with a real linked account after registering the callback.
