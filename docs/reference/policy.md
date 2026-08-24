# Deployment policy

`.github/index-policy.json`, at the root of the *index* repository — not of
this package. One index format, many copies: the public index serves bytes
from `ghcr.io`, a corporate copy from its own Harbor, Artifactory or ECR.
Each index states its own.

```json
{ "registry_hosts": ["ghcr.io"] }
```

That is the whole grammar. Anything else is a hard error: malformed JSON, a
non-object document, an unknown key, a missing or non-array `registry_hosts`,
an empty array, or an entry that is not a bare lowercase host.

## Why the host shape is strict

The allowlist is matched against a URL's parsed `hostname`, which is always
lowercased and never carries a port. So `https://harbor.corp`, `Harbor.Corp`
and `harbor.corp:5000` would each parse happily as policy entries and then
match nothing at all — an allowlist that silently admits no one. Rejecting
them at parse time is the difference between a loud error and an index where
every download fails for a reason nobody can see.

A registry on a non-standard port is allowlisted by its bare host:
`harbor.corp` admits `oci://harbor.corp:5000/team/tool`.

## Why it is a committed file

**Never an environment variable, a repository variable or an Actions
variable.** `repository` is the pointer every client follows to fetch bytes,
so widening the allowlist is a supply-chain trust decision. "Extend only via
reviewed pull request" *is* the control — and a settings-page value can be
changed by anyone with settings access, silently, with no diff and no
reviewer.

Keeping it under `.github/**` also puts it on the same surface branch
protection and CODEOWNERS already guard, beside the other governance data the
bot reads.

## Two failures, both early and loud

**No policy file — fail closed.** An index copy that never stated a policy
says so, rather than inheriting the public index's hosts by accident.

**A host no adapter can serve.** The bot fetches from the hosts it has a
registry client wired for, and nothing else. Allowlisting
`harbor.corp.internal` today would produce roots that pass every validation
check and then cannot be fetched — strictly worse than the honest refusal it
replaces. The error names the missing piece: implement the client, add its
host, dispatch it, in the same change.

## The PR-head question

The unprivileged validation job runs against pull-request-head content by
design — it checks the PR's own claims, and holds no credential. It therefore
also loads the PR's own `.github/index-policy.json`. That is not a
self-authorization hole:

- The policy path is outside every package root's refresh scope, so a pull
  request touching it is classified human-lane and can never auto-merge.
  Merging a widened policy requires a human, which is precisely the control.
- The no-adapter guard closes it independently: a PR-head policy naming a host
  with no client fails the run outright.

`announce` is the one flow that deliberately does not read a local policy —
its publisher runs outside any index checkout, so it reads the target index's
committed policy at the base ref over the API instead.
