# rozi-finder agent guide

## Mission

A rozi picker for the files and directories around the focused pane: find a path, then type it
into the pane, `cd` to it, open it, or copy it. The repository is a rozi extension: a manifest and
one Python command.

## Commands

- Unit tests: `python3 -m unittest discover tests -p 'test_*.py'`
- Manifest validation: `rozi extensions check .`
- Whitespace check: `git diff --check`

## Workflow rules

- Use only public interfaces: `git`, `zoxide query`, and rozi's documented CLI (`api describe`, `pick`,
  `list-panes`, `send-text`, `send-keys`, `split`, `notify`) and extension environment.
- Never type a command into a pane unless `list-panes` shows a supported shell, or no foreground
  program, in it. Open a new pane instead.
- Everything typed into a pane goes through `shlex.quote`. A path never composes a command line
  any other way.
- A picker snapshot holds 512 rows, so anything past the limit must stay reachable by searching:
  by answering `query` replies on a rozi with `pick-query-events`, and through the `search` action
  on one without.
- Rows sent for search text are ranked best match first: rozi shows them in that order.
- A repository met while walking a plain directory is listed with `git ls-files`, never walked,
  so ignored files stay out.
- Keep the command standard-library Python 3.10 with no third-party dependencies.
- Keep `README.md` in sync with behavior, keys, settings, and `min_rozi`.
- Bump `version` in `extension.toml` with every change users install: patch for a fix, minor for a
  feature or setting.
- Recapture `docs/screenshot.webp` from `docs/screenshot-scene.sh` after a visible change.
- Update this guide when durable repository conventions change.

## Commits

- Use Conventional Commits and include a DCO `Signed-off-by` trailer.
- Do not commit local configuration, credentials, runtime data, or `__pycache__`.
