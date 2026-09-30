# Releases

The source under `src/agent_msg/` is canonical. The committed
`plugins/agent-msg/` directory is generated so GitHub marketplace installs do
not need pip, build tools, or a running server. Python 3.10+ is still required.
Build tools below are development dependencies only.

1. Update `src/agent_msg/__init__.py` and `pyproject.toml` to the same version.
   Update versioned download examples in the README. Keep the root `LICENSE`
   and `src/agent_msg/LICENSE` identical.
2. Run `python -m unittest discover -s test -p 'test_*.py' -v`. Normal tests
   are offline; native messaging tests require explicit opt-in and authorized
   same-workdir test sessions. Record live test coverage separately.
3. Run `python -m agent_msg plugin export /tmp/agent-msg-release-plugin` with
   the current package on Python's import path and a new destination directory.
   Replace only the generated `plugins/agent-msg/` tree with this output.
4. Validate the root marketplace and plugin with
   `claude plugin validate .` and `claude plugin validate plugins/agent-msg`.
   Check that both marketplace entries point to the generated plugin. The root
   marketplace is `agent-msg`; the legacy checkout marketplace is `agent-msg-local`.
5. Install `build` in a development environment and run
   `python -m build --outdir dist/release`. Use a clean output directory.
   Zip the generated plugin under one top-level `agent-msg/` directory as
   `agent-msg-plugin-VERSION.zip`, excluding Python caches.
6. Install the wheel into a fresh virtual environment with
   `python -m pip install --no-index --no-deps /absolute/path/to/package.whl`.
   Outside the checkout, verify `agent-msg --version`, `plugin export`, and
   the exported launcher's `--version`. Verify the wheel and ZIP contain
   the license, skill, static UI, and plugin assets, with no journals or test results.
7. Test marketplace add/install for both harnesses using disposable configuration
   directories (`CLAUDE_CONFIG_DIR` and `CODEX_HOME`). Run the installed bundled
   launcher from outside the checkout. Do not alter normal agent configuration.
   `python test/install_smoke.py --marketplace /absolute/path/to/checkout`
   automates this check; pass `nsssayom/agent-msg` for the public GitHub source.
8. Generate `SHA256SUMS` for the wheel, sdist, and plugin ZIP. Review the staged
   files, commit, and push. Create a version tag at that exact commit and a
   GitHub release attaching those four files. Mark experimental releases as
   prereleases. Do not move published tags or silently replace release assets.
9. Download the public release assets, verify their checksums, and test a fresh
   install from the public URL. Marketplace publication and official-directory
   review are separate; follow [SUBMISSION.md](SUBMISSION.md).

The root marketplace can be pinned to a tag in Codex with
`codex plugin marketplace add nsssayom/agent-msg --ref v0.1.1`.
Users installing from the default branch follow the marketplace's current files.
No PyPI release has been made; do not document `pip install agent-msg` as our
distribution until ownership of that package name and publication are confirmed.
