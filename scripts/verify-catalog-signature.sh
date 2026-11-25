#!/usr/bin/env bash
# Kind: script
# bash-floor: 4
#
# verify-catalog-signature.sh -- verify a served index catalog's signature with cosign.
#
# Usage: scripts/verify-catalog-signature.sh CATALOG BUNDLE
#
#   CATALOG  path of the downloaded c/index.json
#   BUNDLE   path of the downloaded c/index.json.sigstore.json
#
# Environment:
#   SIGNER_IDENTITY  required; the certificate identity the signer must carry
#   SIGNER_ISSUER    required; the OIDC issuer of that identity
#   PREDICATE_TYPE   attestation type (default https://ocx.sh/attestation/index-catalog/v1)
#   COSIGN           cosign command line, split on spaces (default `cosign`), e.g.
#                    `ocx package exec ocx.sh/sigstore/cosign:3.1.1 -- cosign`
#
# Exit codes:
#   0   cosign verified the catalog against the bundle and the identity
#   2   usage error: wrong argument count, missing file, or SIGNER_IDENTITY / SIGNER_ISSUER unset
#   1+  cosign refused (its own non-zero code, passed through)
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: verify-catalog-signature.sh CATALOG BUNDLE" >&2
  exit 2
fi
catalog=$1
bundle=$2
for f in "$catalog" "$bundle"; do
  [[ -f "$f" ]] || {
    echo "verify-catalog-signature.sh: $f is not a file" >&2
    exit 2
  }
done
for v in SIGNER_IDENTITY SIGNER_ISSUER; do
  [[ -n "${!v:-}" ]] || {
    echo "verify-catalog-signature.sh: $v is required" >&2
    exit 2
  }
done

read -r -a cosign <<<"${COSIGN:-cosign}"

exec "${cosign[@]}" verify-blob-attestation \
  --bundle "$bundle" \
  --type "${PREDICATE_TYPE:-https://ocx.sh/attestation/index-catalog/v1}" \
  --certificate-identity "$SIGNER_IDENTITY" \
  --certificate-oidc-issuer "$SIGNER_ISSUER" \
  "$catalog"
