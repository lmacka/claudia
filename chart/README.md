# claudia Helm chart

> ## ⚠ v1 DEV MODE — no at-rest encryption
>
> v1 ships kid mode WITHOUT at-rest encryption. Sessions are stored as
> plaintext JSONL on the PVC. Anyone with `kubectl exec` access to the
> pod can read kid session content. **Do NOT promise privacy on v1
> deploys.** Encryption restoration is Step 11 in `../docs/build-plan-v1.md`
> and will land before any deploy that needs to make confidentiality
> claims to a kid.
>
> The rest of the kid-mode safety floor (Haiku safety classifier, write
> tools disabled, no-anthropomorphism prompt, crisis-aware help routing)
> IS enforced and IS non-disableable.

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

`values.schema.json` enforces three kid-mode safety floor settings
non-disableably (`"const": true` when `mode=kid`):

- `kid.safety.haiku_classifier` — pre-turn safety classifier call.
- `kid.safety.write_tools_disabled` — Gmail send + calendar create blocked.
- `kid.safety.no_anthropomorphism` — kid prompt forbids streaks, romance,
  exclusivity, anti-parent secrecy.

Helm rejects the install if any are flipped. See `../docs/design.md`
Premise 3 and "Reviewer concerns" item C16.

`kid.encryption.enabled` is `default: true` (NOT const) for v1 dev mode
and is currently ignored by the app. v1.5 re-pins it to `const: true`
via schema migration. See top-of-file warning.
