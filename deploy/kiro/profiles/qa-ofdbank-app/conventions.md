# Conventions: ofdbank-app, Python/behave harness (`integrationtests/`)

Scope of this configuration: the Python harness that runs behave + Appium against the Flutter app.
The Flutter/Dart app (`lib/`, `test/`, `android/`, `ios/`) is out of scope. Agents must not touch it
and must not run `flutter` or `dart`.

## Priority rules

1. Write only to the harness code (`integrationtests/features/environment.py`,
   `integrationtests/features/steps/**`, `integrationtests/helpers/**`, `integrationtests/modules/**`,
   `integrationtests/parallel.py`, `integrationtests/data/logging.json`) and `integrationtests/tests/**`
   (unit tests).
2. New tests go in `integrationtests/tests/`, never under `integrationtests/features/`. behave loads
   everything in `features/`, so a test module there would be picked up (or break) behave runs.
3. Run tests from the repository root, with no `cd`:
   `python3 -m unittest discover -s integrationtests/tests -t integrationtests`.
   Run one module with `python3 -m unittest integrationtests/tests/test_<name>.py`.
4. Only the standard library is guaranteed. `pytest`, `behave`, `appium`, `selenium`, `ipdb`,
   `sauce_storage_api` and the `sauron` submodule (empty checkout) are not available. `requests` may
   not be either. Tests must stub these in `sys.modules` before importing harness code.
5. Tests never reach Appium, SauceLabs, Sauron, the backend API, or any network. Never start a real
   `behave` subprocess and never call `parallel.worker()` with a real command.
6. Never read, print, or copy values from `integrationtests/behave.ini` (it holds `api_key` and
   passwords) or `integrationtests/appium_drivers/credentials.py` (hardcoded SauceLabs key). Both are
   protected. Use fake values in tests.
7. Do not add dependencies, lint rules, or CI changes. `.gitlab-ci.yml` and `requirements.txt` are
   protected.
8. Commit messages follow the agent prefix from the shared conventions on the work branch. The official
   branch uses the repo's own format, `TPZSO-NNNN - mensagem` in Portuguese (set in `agents-team.yml`).

## Code

Layout. The harness root is `integrationtests/`. `environment.py` holds the behave hooks and sits in
`features/` because behave requires it there. Shared helpers are in `helpers/`
(`user.py`, `generator.py`, `strings.py`, `constants.py`). `parallel.py` is a standalone CLI that
splits `.feature` files across workers and shells out to `behave`.

Imports. Modules import each other as top-level names relative to `integrationtests/`
(`from helpers.user import User`, `from appium_drivers.sauce_physical import sauce_uploader`).
`helpers/` and `integrationtests/` have no `__init__.py`. Keep that style in production code.

Naming and typing. snake_case functions, PascalCase classes. Type hints are used on the functions of
`parallel.py` and `helpers/strings.py` and omitted in `helpers/generator.py`. Add hints on new
functions.

Docstrings. Google style (`Args:`, `Returns:`), written in Portuguese, as in `parallel.py` and
`helpers/strings.py`. Keep new docstrings in Portuguese.

Logging and output. `environment.setup_logger()` reads `data/logging.json` through
`helpers.constants`. `parallel.py` reports with `print`. Do not introduce a new logging mechanism.

Import-time and cwd pitfalls that affect testing:
- `helpers/constants.py` builds `LOG_FILE_DIR` and `LOGGER_CONFIG` from `os.getcwd()` when imported.
  From the repo root these point to the root, not to `integrationtests/`. Do not call
  `setup_logger()` against the real files. Patch `helpers.constants` or use a temp directory.
- `parallel.prepare_tests()` reads `behave.ini` from the current directory with `ConfigParser`.
  Patch `ConfigParser` or `chdir` to a temp directory that holds a fake ini.
- `parallel.py` imports `appium_drivers.sauce_physical` at module level, and `environment.py`
  imports `appium_drivers.local`, `behave`, `ipdb` and `sauron.test_uploader`. Stub them first.
- `environment.py` is not in an importable package. Load it with `importlib.util.spec_from_file_location`
  after the stubs are in place.
- `helpers/user.NewUser` calls the backend with `requests.put` in its constructor. Patch
  `helpers.user.put`. `User` itself is safe to build with fake `backend_url` and `api_key`.
- Random helpers in `helpers/generator.py` use `random` and `time`. Seed or patch them for
  deterministic assertions.

## Tests

Layout. `integrationtests/tests/__init__.py` must exist (unittest discovery with `-t integrationtests`
needs a package). It should put the `integrationtests/` directory on `sys.path` (idempotently), so
`import helpers...` and `import parallel` work both under `discover` and when a single file is run.
One test module per production module: `test_parallel.py`, `test_environment.py`,
`test_helpers_user.py`, and so on.

Style. `unittest.TestCase` classes named `Test<Subject>`, methods named `test_<behavior>`. Use
`unittest.mock` (`patch`, `MagicMock`) for all external collaborators. Put shared stubs for the
missing third-party modules in one helper module inside `tests/` instead of repeating them.

Stubs. Insert fakes with `patch.dict(sys.modules, {...})` inside `setUp` or a context manager, and
remove any harness module imported under the stubs (`sys.modules.pop`) so a later test does not see
a stale import.

Mocking policy. Mock at the process and network boundary: `parallel.Popen`, `parallel.sauce_uploader`,
`helpers.user.put`, and the `context.driver` / `context.sauron` objects handed to the hooks. Do not
mock the function under test. Pure functions such as `split_list`, `get_features`, `get_command`,
`get_behave_summary` and `clear_accents` need no mocks, except `listdir` or a temp directory for
`get_features`.

Running. Whole suite: `python3 -m unittest discover -s integrationtests/tests -t integrationtests`.
Single module: `python3 -m unittest integrationtests/tests/test_parallel.py`. Single test, by dotted name (illustrative
names): `python3 -m unittest integrationtests.tests.test_parallel.TestSplitList.test_remainder`.

## BDD

behave drives the real feature files, but they are not part of this configuration. Do not edit
`features/*.feature`, `features/steps/**`, `modules/page_objects/**`, or `behave.ini`, and do not
run `behave` or `parallel.py` (no behave, no device, no network). `environment.py` is the only file
in `features/` that agents may change, and it is tested through stubs, not by running behave.

## Anti-patterns

| ❌ Don't | ✅ Do |
|---|---|
| Put test files in `integrationtests/features/` | Put them in `integrationtests/tests/` |
| Add `pytest` fixtures or `import pytest` | Use `unittest.TestCase` and `unittest.mock` |
| `import behave` / `appium` at the top of a test | Stub in `sys.modules` before importing the harness module |
| Run `behave`, `parallel.py` main, or `Popen` for real | Patch `parallel.Popen` and assert on the command string |
| Call `setup_logger()` against the real `data/logging.json` | Patch `helpers.constants` or use a temp dir |
| Instantiate `NewUser` directly | Patch `helpers.user.put` and return a fake response with the expected JSON |
| Paste a value from `behave.ini` or `credentials.py` into a test | Use obvious fakes such as `"fake-key"` |
| `cd integrationtests && python3 -m unittest` | Run from the repo root with the commands above |
| Loosen or skip a failing test to get green | Report the failure as a finding |
