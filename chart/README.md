# claudia Helm chart

Skeleton — `templates/` is empty. Fill in deployment.yaml, service.yaml,
httproute.yaml (or ingress.yaml), pvc.yaml as part of v1 build step 1.

See `../docs/design.md` "Helm chart values" section for the canonical
values reference.

## Install (once templates are filled)

```bash
helm install claudia oci://ghcr.io/lmacka/charts/claudia \
  --version 0.1.0 \
  --values my-values.yaml \
  --namespace claudia \
  --create-namespace
```

`my-values.yaml` minimum:

```yaml
mode: adult
displayName: "Liam"
dob: "1985-02-14"
ingress:
  host: claudia.example.com
  tls:
    secretName: claudia-tls
```

Required secrets in the namespace before install:

- `anthropic-api-key` with key `api-key`
- `claudia-auth` with key `password`
- `google-oauth` with keys `client-id`, `client-secret` (only if
  `googleOAuthSecretRef.enabled: true`)

## Schema enforcement

`values.schema.json` enforces the kid-mode safety floor non-disableably:
`kid.safety.haiku_classifier`, `kid.safety.write_tools_disabled`,
`kid.safety.no_anthropomorphism`, and `kid.encryption.enabled` all have
`"const": true` constraints when `mode=kid`. Helm rejects the install
if any are flipped.

This is intentional. See `../docs/design.md` Premise 3 and the
"Reviewer concerns" section item C16 for the reasoning.
