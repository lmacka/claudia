# claudia

Self-hosted, single-tenant AI companion. One Helm release per user.
**Adult mode** for adults wanting a thinking-partner sounding board.
**Kid mode** for parents to deploy a confidential frontal-cortex
prosthetic for their kid.

Forked from [robo-therapist](https://github.com/lmacka/robo-therapist) on
2026-04-26. Same bones, distributable shape, multi-persona.

## Status

**v0.1.0 — pre-alpha.** Skeleton repo. App lifted from robo-therapist;
Helm chart skeleton in place but `chart/templates/` is empty. Adult-mode
and kid-mode prompts not yet differentiated. CI not yet wired.

See [`docs/design.md`](docs/design.md) for the full v1 plan and
[`docs/wireframe/`](docs/wireframe/) for the locked UX framework
(open `docs/wireframe/index.html` in a browser).

## Build target

"Another autistic dad with a homelab" — k8s, kubectl, SOPS or
sealed-secrets, NFS or Longhorn-backed PV. Not docker-compose, not
one-click VPS. The Helm chart is the shipping unit:

```bash
helm install claudia oci://ghcr.io/lmacka/charts/claudia \
  --version <ver> \
  --values my-values.yaml \
  --namespace claudia \
  --create-namespace
```

(Once `chart/templates/` is filled and CI publishes the chart.)

## Why this exists

The original robo-therapist works. It's been through 8 iterations,
51 tests, weeks of real use. But it was built single-user-Liam:
two-repo gitops split, NFS-on-Synology assumed, no setup wizard, no
library UI, profile.md curated by hand. Someone else can't `helm install`
and start chatting.

claudia is the re-roll for everyone else who could benefit from the
auditor + spine + handover-PDF loop, including Liam's own son Jasper —
who needs a different persona (frontal-cortex prosthetic, not
sounding-board), a different safety floor (active classifier + no
anthropomorphism), and a different confidentiality model (encrypted with
his passphrase, parent break-glass envelope) than what works for adults.

## License

MIT (TBC). Personal/family use is the build target; commercial use not
considered.
