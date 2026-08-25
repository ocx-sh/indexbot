"""Pipeline generation — `indexbot ci`.

The workflows an index runs are not that index's business. They are the bot's
governance model expressed as YAML: which trigger the privileged half may use,
what it is allowed to check out, which pathspec selects a package root, what
happens to a fork PR whose checks failed. Copying them between repositories is
how a deployment ends up running a two-year-old version of a security argument
it never read.

So they are generated from `.github/index-policy.json`, and `--check` is the
gate that keeps a hand-edit from surviving. The pattern is
`@ocx-sh/catalog`'s C-007, including its hard-won rule: on GitLab, render the
*included* job file and never the root `.gitlab-ci.yml`, so an operator keeps
somewhere of their own to put everything else.
"""
