# Open Source Release Checklist

Things to do before sharing this repo publicly as a proper open source project.

## Must-do (legal / correctness)

- [ ] **Add LICENSE file** — pyproject.toml declares MIT but there's no actual LICENSE file. Without it, the code is effectively all-rights-reserved despite the declaration. Copy the standard MIT text and add your name + year.

- [ ] **Remove hardcoded personal paths from tests** — `/home/user/projects/myapp` appears in:
  - `tests/test_pretool.py` (7 occurrences in test payloads)
  - `tests/test_extract.py` (1 occurrence)
  - `tests/test_formation.py` (1 occurrence)
  - `toolengrams/commands/pretool.py` (1 occurrence in docstring example)
  - Replace with `/home/user/projects/...` or `/tmp/test/...`

- [ ] **Add author email to pyproject.toml** — Currently just `{ name = "Juan Pablo Carranza Hurtado" }`. Add email or GitHub URL so people know how to reach you.

## Should-do (quality / trust)

- [ ] **Add CONTRIBUTING.md** — Short guide: how to run tests, how to submit issues, code style expectations. Doesn't need to be long — 20 lines is fine for a project this size.

- [ ] **Add GitHub issue templates** — Bug report and feature request templates. Helps contributors file useful issues instead of vague ones.

- [ ] **Pin minimum Claude Code version** — README says `>= 2.1.59` but install.sh doesn't check. Add a version check or at least a clearer error when hooks fail on older versions.

- [ ] **Review seed memories for generality** — `engram seed` inserts example memories. Make sure they're useful for any user, not just your personal workflow (check for ergeon-specific or project-specific references).

- [ ] **Add `py.typed` marker** — If anyone uses this as a library, the marker tells type checkers that type hints are available. Just an empty file at `toolengrams/py.typed`.

## Nice-to-have (polish)

- [ ] **Add a changelog** — Even a simple `CHANGELOG.md` with a v0.1.0 entry describing the initial release. Helps people understand what changed between versions.

- [ ] **GitHub release / tag** — Tag the current state as `v0.1.0` so people can pin to a known version.

- [ ] **CI with GitHub Actions** — Run `pytest` on push. Simple workflow, builds trust that the tests actually pass. Free for public repos.

- [ ] **Badge in README** — Tests passing badge, Python version badge, license badge. Small thing but signals "this project is maintained."

- [ ] **Expand .gitignore** — Current one is good but missing: `.eggs/`, `*.egg`, `*.log`, `.coverage`, `htmlcov/`, `.idea/`, `.vscode/`. Not urgent since nothing sensitive, but tidier.

- [ ] **Review install.sh for portability** — Currently macOS-specific (launchd, `open` command). Document that the consolidation schedule only works on macOS, or add Linux cron support.
