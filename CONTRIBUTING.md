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

## Releases

Main commits use conventional commit titles. Semantic Release automatically updates the package version and changelog, creates the matching tag and GitHub Release, and dispatches the PyPI publication workflow, following Quarry's release pipeline.

Publication validates that exact tag through CI, builds the bundled UI and Python distributions, and rejects a package/tag mismatch before uploading. Manual dispatch remains available for an exact tag or a one-off forced version bump. Before the first publication, configure the `ablation-workbench` PyPI trusted publisher for `yiminspace/ablation`, `release.yml`, and the GitHub `pypi` environment. A local build does not publish anything.

Installed GUIs check PyPI in the background and show the upgrade command and release notes. They never install an update automatically. Source/editable installs skip checks; `ABLATION_UPDATE_CHECK=0` disables them. Restart the GUI after upgrading. Package upgrades do not install or synchronize personal skills. Release verification details are in [the release checklist](docs/release-review.md).
