# Conventions: integration-tests (Analytics Dashboard)

Python 3.11, behave, Selenium, page objects. Gherkin is pt-BR, code is English.
Scope: the behave harness in `analyticsbdd/`.

## Priority rules

1. `FORGE_GUIDELINES.md` (repo root) is the authority for steps, page objects, and Gherkin.
   Read it before editing. Where `CLAUDE.md` disagrees (for example it shows `AnaliseIncidentes(context)`),
   follow `FORGE_GUIDELINES.md`.
2. Write only in `analyticsbdd/features/steps/**` and `analyticsbdd/pages/**`.
   `analyticsbdd/pages/base_page.py` is protected by `FORGE_GUIDELINES.md`: it holds only methods used by
   several screens. Add to it only when the card or a review comment asks for it, and prefer a dedicated
   page object (e.g. `pages/toast.py`, `pages/tela_inicial.py`) for screen- or widget-specific logic.
3. Do not edit: `analyticsbdd/features/environment.py`, `analyticsbdd/webdrivers/**`,
   `analyticsbdd/modules/webdrivers.py`, `behave.ini`, `.gitlab-ci.yml`, `ruff.toml`, `mypy.ini`,
   `requirements*.txt`. Edit `.feature` files only when the card is about scenarios.
4. Behave does not run on this machine (it needs a browser and a live environment). Do not try to run it.
   The only local checks are ruff and mypy through the commands in `agents-team.yml`.
5. The CI gate is ruff 0.11.12 (`check` and `format --check`) and mypy 1.15.0, applied only to the `.py`
   files changed in the MR. Every file you touch must pass all three, including old code in that file.
6. Page objects take only `driver`: `def __init__(self, driver: WebDriver)`, instantiated as
   `Page(context.driver)`. Never take `context` or business data in a page object.
7. Steps assert, page objects do not. No `assert` in `analyticsbdd/pages/`.
8. A refactor must keep behavior identical. Moving logic out of a step into a page object or helper must not
   change locators, waits, or the text of assertion messages unless a review comment asks for it.
9. Never weaken a check: no `# noqa`, `# type: ignore`, new per-file ignores, or changes to `ruff.toml`
   or `mypy.ini` to get a green result.
10. Do not copy values from `behave.ini`, `.env`, or any credential into code, logs, or artifacts.

## Code

- Layout: steps in `analyticsbdd/features/steps/<domain>.py` (cross-domain steps in `steps.py`), one page
  object module per screen in `analyticsbdd/pages/<screen>.py`, shared utilities in `analyticsbdd/helpers/`,
  wait helpers in `analyticsbdd/modules/webdrivers.py`.
- Where extracted logic goes: logic tied to one screen goes into that screen's page object. Logic shared by
  several screens goes into `BasePage` (see rule 2) or a helper in `analyticsbdd/helpers/`.
  Pure data manipulation with no Selenium goes into `helpers/` (for example `helpers/utils.py`).
- Formatting is enforced by `ruff format`: single quotes, 79-column lines, 4-space indent, trailing-comma
  handling per `ruff.toml`. Run the format check before finishing, do not hand-wrap lines.
- Lint rules selected in `ruff.toml`: E, W, F, I, UP, B (E501 ignored). Imports are sorted with
  `analyticsbdd` as first-party. Use modern syntax (`list[dict]`, not `typing.List`).
- Typing: mypy runs with `follow_imports = skip`, `no_strict_optional`, `warn_unused_ignores`, and
  `warn_redundant_casts`. Annotate new functions (`-> None`, parameter types). Step functions are annotated
  as `def step_name(context: Context) -> None` with `from behave.runner import Context`.
- Docstrings: the code base mixes Google style and `:return:` style, written in Portuguese. New docstrings
  use Google style (`Args:` / `Returns:`), Portuguese or English.
- Error handling: catch specific Selenium exceptions (`TimeoutException`, ...). No bare `except Exception`.
  Let `ValueError` and `IndexError` from parsing surface.
- Waits and clicks: use helpers from `analyticsbdd.modules.webdrivers` (`wait_element`,
  `wait_loading_disappear`, ...) and `analyticsbdd.helpers.browser_util` (`find_element_by_css_selector`,
  `find_elements_by_css_selector`). Do not add `time.sleep` or raw polling lambdas.
- Selectors: `data-testid`, then role/ARIA, then semantic id, then scoped CSS. Tag name only with `# TODO`.
- Delete code that becomes unused after a move (methods, imports, steps), but only code you made unused.

## Tests

There is no unit-test suite. The "tests" are the `.feature` files plus step definitions.

- Quality checks for a changed file, in this order, each with `{target}` set to the changed files:
  ruff `check` (`commands.test`), ruff `format --check` (`commands.lint`), mypy (`commands.typecheck`).
- `commands.test_single` is the same ruff check on one file.
- Every Gherkin phrase that a step decorator matches must still match after a refactor. When you rename,
  merge, or remove a step, search the `.feature` files for the phrase first. A step not used by any
  `.feature` file should not exist, but only remove it if a review comment asks for it.
- Because behave cannot run here, behavior changes that need a real browser to confirm are not verifiable
  locally. Report them to the human instead of claiming they work.

## BDD

`FORGE_GUIDELINES.md` is the authority for features, steps, and page objects. This file only adds what it
does not cover:

- `analyticsbdd/pageobjects/` and `BasePageObject` are described in `FORGE_GUIDELINES.md`, but that
  directory does not exist in this checkout. New page objects go in `analyticsbdd/pages/` and extend
  `BasePage`.
- The driver-only constructor migration (AD-12207) is partial. Some page objects already take `driver`
  (for example `HistoricoAtividades` in `pages/historico_atividades.py`), others still take `context`.
  Do not convert untouched page objects unless a review comment asks for it.
- `CLAUDE.md` suggests `pylint analyticsbdd/`. CI does not run pylint and it is not installed. Ignore it.
- Steps that carry data between steps use `context.<domain>_<thing>`. Do not set or overwrite
  hook-owned attributes (`context.driver`, `context.browser`, `context.base_url`, `context.timezone`).

## Anti-patterns

| ❌ | ✅ |
|----|----|
| Page object `__init__(self, context)` | `__init__(self, driver: WebDriver)` |
| `assert` inside `analyticsbdd/pages/` | Return data or a bool, assert in the step |
| Long data-processing loop inside a step body | Move it to the page object or a helper, keep the assert in the step |
| `# noqa` or `# type: ignore` to silence the gate | Fix the code |
| Editing `ruff.toml` / `mypy.ini` to pass the gate | Fix the code |
| Reformatting whole files that are not in the review comments | Touch only what the review asks for |
| Moving a method out of `base_page.py` and leaving callers pointing at the old location | Update every caller in `pages/` and `features/steps/` |
| Deleting a step because it looks unused | Search the `.feature` files first |
| `time.sleep(...)` or a polling lambda | Helpers from `modules/webdrivers.py` |
| `except Exception:` | `except TimeoutException:` (or the specific exception) |
| Running `behave` locally | Rely on ruff and mypy, flag unverified runtime behavior to the human |
