# Single sign-on (OpenID Connect)

Reviewbot supports sign-in with any OpenID Connect provider (Okta, Microsoft Entra ID, Keycloak, Google
Workspace, Auth0 and others) and with GitLab. This is in addition to passwords, invites, and Sign in with
GitHub. Identity providers can also decide who gets in and with which role, by mapping groups to roles.

All settings are environment variables, so a misconfigured identity provider cannot be changed from the dashboard
or lock everyone out.

## Generic OIDC provider

1. Register an application (a "web app" with a client secret) at your identity provider:
   - Redirect URI: `https://<your-host>/api/v1/auth/oidc/sso/callback`
   - Scopes: `openid email profile`, plus whatever your provider needs to include group names in the ID token or
     userinfo response (see below).
2. Configure Reviewbot:

   | Variable | Example | Description |
   |---|---|---|
   | `REVIEWBOT_OIDC_ISSUER` | `https://acme.okta.com` | Issuer URL. Reviewbot reads `<issuer>/.well-known/openid-configuration`. |
   | `REVIEWBOT_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | | From step 1. |
   | `REVIEWBOT_OIDC_NAME` | `Okta` | Button label: "Sign in with Okta". |
   | `REVIEWBOT_OIDC_SCOPES` | `openid email profile groups` | Requested scopes. |
   | `REVIEWBOT_OIDC_GROUPS_CLAIM` | `groups` | Claim that lists the user's groups (ID token or userinfo). |
   | `REVIEWBOT_OIDC_ROLE_MAPPING` | `admin=reviewbot-admins;reviewer=engineering;viewer=*` | Optional; see below. |
   | `REVIEWBOT_OIDC_SYNC_ROLES` | `true` | Update roles from groups at every sign-in. |
   | `REVIEWBOT_OIDC_TRUST_EMAIL` | `false` | Treat the `email` claim as verified when the provider omits `email_verified` (Entra ID). |

3. Restart the API. The login page now shows **Sign in with Okta**.

### Who can sign in

With **no role mapping**, single sign-on only signs in people who already have access. It matches, in order:

1. a linked identity (same provider and `sub`);
2. an invite (the invite link offers "Continue with Okta");
3. an existing account with the same **verified** email address. The identity is then linked automatically.

New people need an invite, or self-signup (`REVIEWBOT_ALLOW_SIGNUP` with `REVIEWBOT_SIGNUP_EMAIL_DOMAINS`).

With a **role mapping**, the identity provider decides:

```
REVIEWBOT_OIDC_ROLE_MAPPING="admin=reviewbot-admins;reviewer=engineering,qa;viewer=*"
```

- Each rule is `role=group,group`. The first role, by priority admin > reviewer > viewer, that matches one of
  the user's groups wins. `*` matches everyone.
- People with a verified email who match a rule get an account automatically.
- People who match no rule cannot sign in, even with an existing account. Removing someone from the IdP groups
  removes their access at their next sign-in.
- With `REVIEWBOT_OIDC_SYNC_ROLES=true`, roles follow the groups at every sign-in. Reviewbot never demotes the
  last active admin.

### Provider notes

| Provider | Issuer | Groups |
|---|---|---|
| Okta | `https://<org>.okta.com` (or a custom authorization server) | Add a `groups` claim to the ID token (filter, e.g. "starts with reviewbot-"). Scope `openid email profile groups`. |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` | Enable the `groups` optional claim. Entra sends group **object IDs**; use them in the mapping. Set `REVIEWBOT_OIDC_TRUST_EMAIL=true` (Entra omits `email_verified`). |
| Keycloak | `https://<host>/realms/<realm>` | Add a "Group Membership" mapper to the client (claim `groups`, without full path). |
| Google Workspace | `https://accounts.google.com` | No group claims. Use invites, or a domain-limited self-signup. |

## Sign in with GitLab

GitLab is an OpenID Connect provider too:

1. In GitLab, create an application. Use **User Settings → Applications**, or **Admin → Applications** on
   self-managed GitLab.
   - Redirect URI: `https://<your-host>/api/v1/auth/oidc/gitlab/callback`
   - Scopes: `openid`, `email`, `profile`
   - Confidential: yes
2. Set `REVIEWBOT_GITLAB_OAUTH_CLIENT_ID` and `REVIEWBOT_GITLAB_OAUTH_CLIENT_SECRET`. The issuer defaults to the
   GitLab instance connected under Integrations (or `https://gitlab.com`). Override it with
   `REVIEWBOT_GITLAB_OAUTH_URL`.
3. Optional: `REVIEWBOT_GITLAB_ROLE_MAPPING="admin=acme/platform;reviewer=acme"`, using GitLab group full paths.

## Enforcing single sign-on

`REVIEWBOT_PASSWORD_LOGIN` controls password sign-in:

| Value | Effect |
|---|---|
| `all` (default) | Everyone can sign in with a password. |
| `admins` | Only admins can use a password, as a break-glass path if the IdP is down. Everyone else must use SSO, and invites are accepted through SSO. |
| `none` | No password sign-in at all. Make sure an admin can sign in through SSO first. |

The first-run `/setup` page and `manage.py createadmin` always work, so you can recover an instance.

## Security

- Authorization code flow with PKCE (S256), `state`, and `nonce`.
- ID tokens are verified against the provider's JWKS. Only asymmetric algorithms are accepted (RS*, PS*, ES*),
  and Reviewbot checks `iss`, `aud`, `exp`, `iat`, and `nonce`. Rotated signing keys are picked up automatically.
- Userinfo claims are merged only when they describe the same `sub`, and the verified ID token wins on conflicts.
- Accounts are matched by email only when the provider says the email is verified, or with `TRUST_EMAIL`.
- Every sign-in, failed sign-in, identity link or unlink, and role change is in the audit log.
- People can link or unlink providers under **Account → Single sign-on**. Reviewbot refuses to unlink the last way
  to sign in.
