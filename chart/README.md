# claudia Helm chart

Single-tenant, single-user-per-deploy companion app. One Helm release per
user; the user customises it via the in-app `/setup` wizard on first visit.

## Install

```bash
helm install claudia oci://ghcr.io/lmacka/charts/claudia \
  --version 0.9.0 \
  --values examples/adult-values.yaml \
  --namespace claudia \
  --create-namespace
```

Minimum values:

```yaml
displayName: "Liam"
dob: "1985-02-14"
ingress:
  host: claudia.example.com
  tls:
    secretName: claudia-tls
```

**No Secrets need to exist before install.** All credentials are captured
in the in-app `/setup` wizard the first time you open the URL:

- Step 1: Anthropic API key (live-validated)
- Step 2: Pick a password (or sign in with Google)
- Step 3: Profile + model + custom instructions
- Step 4: Library import + auto-draft profile
- Step 5: Theme + therapist alias → finish

Anything you set lands in SQLite at `/data/claudia.db` on the PVC.

## Optional pre-bootstrap (SOPS / Vault)

If you want to manage credentials externally instead of via the wizard,
add either or both of these blocks:

```yaml
anthropicSecretRef:
  name: anthropic-api-key
  key: api-key
basicAuth:
  passwordSecretRef:
    name: claudia-auth
    key: password
```

Env-mounted Secret values **always win** over `/settings` edits — the UI
fields render read-only with a tooltip ("set by Helm Secret — edit chart
to change"). To rotate, edit the Secret and bounce the pod, or remove the
ref from the chart and use the wizard.

## Google integrations (optional)

Gmail + Calendar tools are opt-in via `integrations.google.enabled`
(default `false`). When enabled, OAuth credentials can come from a Helm
Secret or be entered in `/settings`:

```yaml
integrations:
  google:
    enabled: true
    secretRef:
      name: google-oauth
      clientIdKey: client-id
      clientSecretKey: client-secret
```

The redirect URI is computed automatically as
`https://{ingress.host}/oauth/callback`. Add that URI to your Google Cloud
Console OAuth client's authorized redirect URIs.
