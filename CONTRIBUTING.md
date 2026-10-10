# Contributing

Use Python 3.11 or newer. Install the source into an isolated environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Keep inference and agent tests offline: use local HTTP fixtures and child-process stubs. Do not check in captured company logs, credentials, personal filesystem paths, or model outputs containing private data. Synthetic demo results must remain visibly labelled.

CLI and GUI share the experiment engine. Changes to experiment formats need a migration or explicit version rejection, and a regression test using an older fixture. Comments bind to the reviewed content; explanation-only reviews must not silently create candidates or replays.

Use conventional commit titles (`feat:`, `fix:`, `docs:`, `test:`, `chore:`) and describe observable behavior and validation in pull requests. Update `CHANGELOG.md` for user-visible changes.

## Release review

The source and development-preview distributions are published through GitHub Releases. PyPI publication remains disabled by default. Before registry publication, confirm package ownership and configure the trusted publisher. Build and inspect the wheel and sdist, run tests from the wheel in a clean virtual environment, and review the bundled public skill and synthetic fixtures.

The version-preparation workflow runs manually and derives a version and changelog from conventional commits. It saves a reviewable patch and distribution artifacts, with commit, tag, push, and release creation disabled. The equivalent local command, with `python-semantic-release==10.5.3` installed in a development environment, is:

```sh
semantic-release version --no-commit --no-tag --no-push --no-vcs-release --skip-build
```

After reviewing and adopting the prepared version, create its matching tag. The separate publication workflow only runs manually for that exact tag, requires the repository variable `ABLATION_PUBLIC_RELEASE=enabled`, and uses a `public-release` environment. Configure required reviewers and the package registry's trusted-publisher identity before enabling it. A passing local build does not publish anything. Skill installation or synchronization is not performed by package upgrades.

Source and GitHub preview builds contain `RELEASE_PUBLISHED = False`, disabling registry checks and upgrades even when installed from a wheel. Only the approved registry-publication workflow changes that distribution marker while building the PyPI package. This prevents a provisional package name from resolving to an unrelated registry owner.
