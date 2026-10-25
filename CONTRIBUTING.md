# Contributing

## One branch per change

`main` only receives pull requests. Start every feature or fix from an
up-to-date `main`, on its own branch named after the kind of change:

```bash
git switch main && git pull
git switch -c fix/short-description     # feat/…, fix/…, docs/…, ci/…, chore/…
```

Keep a branch to one feature or fix, so it can be reviewed, tested and
reverted on its own.

## Before pushing

Run what the CI runs, in an environment with `requirements_test.txt`
installed:

```bash
script/check
pre-commit install --hook-type pre-commit --hook-type commit-msg   # once
```

`script/check` runs ruff, ruff format, strict mypy (also on the experimental
build), the tests with coverage, and hassfest when `HA_CORE` points to a
checkout of home-assistant/core. The CI also runs the tests on the oldest
supported Home Assistant (`requirements_test_min.txt`, Python 3.13).

## Commit messages and pull request titles

Use [Conventional Commits](https://www.conventionalcommits.org/):
`feat: …`, `fix(scope): …`, `docs: …`, `ci: …`, `chore: …`, `test: …`,
`refactor: …`, and `!` or a `BREAKING CHANGE:` footer for a breaking change.
The `commit-msg` hook checks commits, and a CI check checks pull request titles.

Pull requests are **squash-merged**: the title becomes the single commit on
`main`. [release-please](https://github.com/googleapis/release-please) reads
those commits to choose the next version and write `CHANGELOG.md`:

| Type | Changelog section | Version |
|---|---|---|
| `feat` | Added | minor |
| `fix` | Fixed | patch |
| `perf` | Changed | patch |
| `docs` | Documentation | — |
| `ci`, `build` | Project | — |
| `chore`, `test`, `refactor`, `style` | not listed | — |
| `!` / `BREAKING CHANGE` | ⚠ Breaking changes | minor while below 1.0 |

## Releases

release-please keeps a *chore(main): release X.Y.Z* pull request up to date.
Its checks wait for a maintainer's approval (*Approve workflows to run*)
because a bot opened it. Merging it creates the tag and a draft release; the
*Release* workflow then reruns the tests and validation on the merged commit,
attaches `xeno_saber_ble-<version>.zip` with a build provenance attestation,
and only then publishes the release. If a step fails, the release stays a
draft that nobody sees: fix the problem on a branch, then publish or delete
the draft by hand.

## Experimental features

Anything that writes firmware or board settings, or is otherwise not yet
verified on a real saber, goes in `experimental/<feature>/`, never in
`custom_components/`: see *Experimental features* in the README.
