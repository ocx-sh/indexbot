# Golden dispatch objects

Real OCI image indices, byte-exact, one per file, stored at
`sha256/<hex>.json` where `<hex>` is the sha256 of the file's own bytes --
the same CAS convention as `p/<ns>/<pkg>/o/sha256/<hex>.json` in a real
index tree (`adr_oci_index_only_dispatch.md` D1).

Both consumers read `expected_platforms.json`, not each other's code, for
"which descriptors count as platform-selection candidates": ocx's
`dispatch_conformance.rs` (WP-A6b) and the bot's `test_serializer_golden.py`.
That file also records where every byte in this directory came from.

## Never hand-edit

Editing a fixture directly changes its sha256, which breaks the CAS
invariant the filename encodes. Regenerate via a fresh capture (or, for
`attestation_descriptor`, by re-deriving from the same source manifests
named below) and update `expected_platforms.json` in the same commit.

## Vectors

| File (`sha256/<hex>`) | What it is | Provenance |
|---|---|---|
| `22af3b60…65a7cc3` | single-platform image index (linux/amd64 only) | The `linux/amd64` descriptor is real, copied from `ghcr.io/michael-herwig/ocx-e2e-hello:1.0.2` (see below). Wrapped alone in a fresh index with `artifactType` set and no `annotations` -- the exact shape `merge_platform_into_index` produces on its "starting fresh" branch (`Err(ClientError::ManifestNotFound(_))` -- no existing manifest/index at the target tag, in `crates/ocx_lib/src/oci/client.rs`) for a first, single-platform push: an empty index seeded with `artifactType`, then the one platform-bearing entry pushed. Not the `oci::Manifest::Image(_)` "wrap an existing bare manifest" branch of the same function -- that branch yields *two* descriptors (a platform-less wrapped entry plus the new platform-bearing one). |
| `bce4d35f…9194f8c` | multi-platform image index: `linux/amd64`, `linux/arm64`, `darwin/arm64` | Synthesized -- no registry ever served this exact document; it is assembled from real descriptors captured from two different registries. `linux/amd64` and `linux/arm64` descriptors: real, from the same `ocx-e2e-hello:1.0.2` capture (ghcr.io). `darwin/arm64` descriptor: real, copied from `docker.io/docker/buildx-bin:latest` (registry-1.docker.io, index digest `sha256:917570d8d0ae91ae49251f84f848a6801eedd114554c56a4fdf7ec88cac48eeb` at capture time), a genuine multi-OS buildx-produced index -- digest/size/mediaType are the registry's own for that platform. |
| `2f1b78d3…d436f69d614` | index carrying an attestation descriptor (`platform: {"os":"unknown","architecture":"unknown"}`) **and** a descriptor with **no `platform` key at all**, plus `annotations` and `artifactType` | Load-bearing for ocx defect N-2 (`oci/index.rs:388`, `None => oci::Platform::any()` lets a platform-less descriptor satisfy every requirement) and N-15 (`Platform::from_image_index`'s `?` aborting `ocx index list --platforms`). The `linux/arm64` leaf and its `unknown/unknown` attestation-manifest sibling are both real, copied from `docker.io/moby/buildkit:latest` (index digest `sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec` at capture time), a real `docker buildx build --provenance=true` output. The third descriptor is that same repository's real `linux/ppc64le` leaf (digest/size/mediaType unchanged) with its `platform` key **removed** -- no public registry response was found carrying a platform-less descriptor in the same fetch as an attestation one, so this one field removal is a deliberate construction, not a captured byte sequence; every value that remains is real. `annotations`/`artifactType` are the real values from the `ocx-e2e-hello:1.0.2` capture, reused here so this vector also exercises ADR R4 (stored verbatim, never rendered; `artifactType` is never inspected by the admission gate per OQ2). |
| `50e02438…3926ccb5ee1` | index carrying `annotations` and `artifactType`, fully real and byte-unmodified | `GET https://ghcr.io/v2/michael-herwig/ocx-e2e-hello/manifests/1.0.2` with `Accept: application/vnd.oci.image.index.v1+json`. Response `Docker-Content-Digest: sha256:50e02438d1d8e4968ad9a663d29185638931b2771e7e4f68cc9923926ccb5ee1` matches this file's own filename -- not just parsed and re-verified, the literal response body. Captured 2026-07-25. |

## Real captures this directory draws from

- `ghcr.io/michael-herwig/ocx-e2e-hello` tags `1.0.1`/`1.0`/`1`/`1.0.2`/`latest`
  all resolve to the same real, ocx-published two-platform index
  (`linux/amd64` + `linux/arm64`); `1.0.2`'s response is vector
  `50e02438…3926ccb5ee1` verbatim.
- `docker.io/docker/buildx-bin:latest` -- real multi-OS buildx index (source
  of the `darwin/arm64` descriptor). Index digest at capture time:
  `sha256:917570d8d0ae91ae49251f84f848a6801eedd114554c56a4fdf7ec88cac48eeb`.
  `latest` is a rolling tag -- this digest is the reproducibility anchor once
  the tag moves on; re-derive against `docker.io/docker/buildx-bin@<digest>`,
  not `:latest`.
- `docker.io/moby/buildkit:latest` -- real buildx index with provenance
  attestations (source of the `linux/arm64` leaf, its `unknown/unknown`
  attestation sibling, and the `linux/ppc64le` leaf reused platform-less).
  Index digest at capture time:
  `sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec`.
  Same rolling-tag caveat -- re-derive against
  `docker.io/moby/buildkit@<digest>`, not `:latest`.

## `expected_platforms.json`

One entry per vector: the digest, the file path, and the exact
`(platform, digest)` list a correct `select_best`/`_catalog_platforms`
implementation derives -- i.e. every descriptor whose `platform` key parses
into a platform ocx can represent (`oci::Platform::try_from`, per
`oci/index.rs:373-386`: present, and every field recognized -- an absent
`platform` key or one that fails to parse, such as `{"os":"unknown",
"architecture":"unknown"}`, is skipped, not just the literal
`(os, architecture) == ("unknown", "unknown")` pair). For the attestation
vector this is a single entry (`linux/arm64`); neither the attestation
descriptor nor the platform-less one appears. This rule is pinned only for
the specific `(os, architecture)` pairs the committed vectors exercise
(`unknown/unknown` and platform-absent) -- it does not by itself prove
behavior for other unrepresentable pairs (e.g. `linux/unknown`,
`unknown/amd64`); a vector for those is a scope call left to the owner.
