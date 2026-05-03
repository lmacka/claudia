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

See `../docs/design.md` "Helm chart values" section for the canonical
values reference.

## Install (adult mode, v0.8+)

```bash
helm install claudia oci://ghcr.io/lmacka/charts/claudia \
  --version 0.8.0 \
  --values examples/adult-values.yaml \
  --namespace claudia \
  --create-namespace
```

Minimum values:

```yaml
mode: adult
displayName: "Liam"
dob: "1985-02-14"
ingress:
  host: claudia.example.com
  tls:
    secretName: claudia-tls
```

**No Secrets need to exist before install.** v0.8 captures all credentials
in the in-app `/setup` wizard the first time you open the URL:

- Step 1: Anthropic API key (live-validated)
- Step 2: Pick a password (or sign in with Google)
- Step 3: Profile + model + custom instructions
- Step 4: Library import + auto-draft profile
- Step 5: Theme + therapist alias → finish

Anything you set lands in SQLite at `/data/claudia.db` on the PVC.

### Optional pre-bootstrap (SOPS / Vault)

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

## Install (kid mode)

Kid mode is operator-managed (parent ssh + Helm); the kid never sees the
setup wizard. Pre-create both Secrets:

```bash
kubectl create secret generic anthropic-api-key \
  --from-literal=api-key=sk-ant-...
kubectl create secret generic claudia-jasper-auth \
  --from-literal=password='your-parent-admin-password'
```

Use `examples/kid-values.yaml` (which references both secrets). The chart
schema requires `basicAuth` when `mode: kid` — the parent admin password
is non-optional in kid mode.

## Schema enforcement

`values.schema.json` enforces two kid-mode safety floor settings
non-disableably (`"const": true` when `mode=kid`):

- `kid.safety.haiku_classifier` — pre-turn safety classifier call.
- `kid.safety.no_anthropomorphism` — kid prompt forbids streaks, romance,
  exclusivity, anti-parent secrecy.

Gmail and Calendar tools are gated at the tool registry rather than the
chart layer — kid mode physically cannot register them regardless of any
value. See `app/main.py:_google_enabled`. In adult mode they are
opt-in via `adult.integrations.google.enabled` (default false).

Helm rejects the install if any are flipped. See `../docs/design.md`
Premise 3 and "Reviewer concerns" item C16.

`kid.encryption.enabled` is `default: true` (NOT const) for v1 dev mode
and is currently ignored by the app. v1.5 re-pins it to `const: true`
via schema migration. See top-of-file warning.
