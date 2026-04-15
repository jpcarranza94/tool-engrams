# Open Source Release Checklist

Things to do before sharing this repo publicly as a proper open source project.

## Must-do (legal / correctness)

- [x] **Add LICENSE file** — MIT license added.
- [x] **Remove hardcoded personal paths from tests** — Replaced all `/home/user/` with generic paths.
- [x] **Remove company-specific references** — Replaced `mycli`, `deploy@`, `production` with generic examples (`psql -h replica`, `ssh deploy@`, etc.)
- [x] **Fix author name in pyproject.toml**
- [x] **Make seed memories opt-in** — `install.sh` no longer auto-seeds; `engram seed` is available but optional.
- [ ] **Add author email/URL to pyproject.toml** — So people know how to reach you.

## Should-do (quality / trust)

- [ ] **Add CONTRIBUTING.md** — Short guide: how to run tests, how to submit issues, code style expectations. Doesn't need to be long — 20 lines is fine for a project this size.

- [ ] **Add GitHub issue templates** — Bug report and feature request templates. Helps contributors file useful issues instead of vague ones.

- [ ] **Pin minimum Claude Code version** — README says `>= 2.1.59` but install.sh doesn't check. Add a version check or at least a clearer error when hooks fail on older versions.

- [ ] **Add `py.typed` marker** — If anyone uses this as a library, the marker tells type checkers that type hints are available. Just an empty file at `toolengrams/py.typed`.

## Nice-to-have (polish)

- [ ] **Add a changelog** — Even a simple `CHANGELOG.md` with a v0.1.0 entry describing the initial release. Helps people understand what changed between versions.

- [ ] **GitHub release / tag** — Tag the current state as `v0.1.0` so people can pin to a known version.

- [ ] **CI with GitHub Actions** — Run `pytest` on push. Simple workflow, builds trust that the tests actually pass. Free for public repos.

- [ ] **Badge in README** — Tests passing badge, Python version badge, license badge. Small thing but signals "this project is maintained."

- [ ] **Expand .gitignore** — Current one is good but missing: `.eggs/`, `*.egg`, `*.log`, `.coverage`, `htmlcov/`, `.idea/`, `.vscode/`. Not urgent since nothing sensitive, but tidier.

- [ ] **Review install.sh for portability** — Currently macOS-specific (launchd, `open` command). Document that the consolidation schedule only works on macOS, or add Linux cron support.

- [ ] **Clean up docs/design-v8.md** — Still has `mycli`/`deploy@` references. Internal design doc but visible in the public repo.
