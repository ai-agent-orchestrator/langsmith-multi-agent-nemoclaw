# Third-party plugins

The `plugins/` directory redistributes plugins developed by Anthropic and the
authors credited in each plugin's manifest and source files. This repository is
an independent curated collection, not an official Anthropic marketplace.

- Requested source: https://github.com/ai-agent-orchestrator/claude-plugins-official
- Original project: https://github.com/anthropics/claude-plugins-official
- Imported commit: `0412be76f50ab4f4f8abff3753efa9269fbbca5b`
- Imported selection: the 14 plugin directories listed in `plugins.lock.json`.
- Each plugin retains its upstream `LICENSE`, copyright notices, authorship,
  documentation, and any nested license files. The bundled plugin licenses are
  Apache License 2.0; consult the files inside each plugin for their full terms.

## Redistribution modification

`plugins/claude-md-management/.claude-plugin/plugin.json` has a single metadata
adaptation: `name` is `project-md-management` and `description` includes a notice
of this rename. Claude Code 2.1.311 rejects the original `claude-` prefix in a
third-party marketplace. The directory keeps its upstream name, and the plugin's
commands, skills, and behavior are unchanged. Install the bundled version as
`project-md-management@claude-10-multi-agent`.

The other 245 imported files are byte-for-byte copies of the pinned commit.
`plugins.lock.json` records every bundled SHA-256 digest and the original digest
of the modified manifest. It also records upstream Git executable modes, which
are retained in this repository. Run `python scripts/verify_plugins.py` to check
the bundled files after cloning or extracting the repository ZIP.

The root marketplace manifest, integrity checker, lock file, and download and
installation instructions were added for this collection. Files in `examples/`
are the separate illustrative implementations that were already in this
repository; their presence does not make them upstream plugin source.
