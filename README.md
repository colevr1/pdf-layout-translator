# PDF Layout Translator

Translate text-based PDFs with an LLM while preserving the original page geometry, images, vector graphics, alignment, and inline bold or italic emphasis.

The repository contains native plugins for Codex and Claude Code, a portable [Agent Skill](https://agentskills.io/), and a local [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server. Both plugins use the same skill and Python translation engine. The active model performs the translation; no separate translation API or API key is required.

## Features

- Preserves page size, images, and vector artwork.
- Replaces text inside the source text regions instead of rebuilding the document from scratch.
- Preserves inline bold and italic phrases across grammatical reordering.
- Recovers synthetic PDF bold and italic styling encoded through stroke and shear operations.
- Validates formatting markers before rendering.
- Detects left-aligned and centered text using PDF geometry and neighboring text flow.
- Fits longer translations into the available region and reports aggressive scaling or fitting failures.
- Keeps the source PDF unchanged.

## Limitations

- The source must contain selectable text. Scanned or image-only PDFs require OCR, which is not included.
- Very complex typography, unusual writing directions, forms, annotations, or heavily fragmented PDFs may need manual review.
- Fonts are rendered with an available sans-serif substitute; exact proprietary font matching is not guaranteed.
- Layout preservation is heuristic because PDF files store positioned drawing operations rather than semantic paragraphs.

Always review the translated PDF before publishing or relying on it for legal, medical, safety-critical, or regulatory use.

## Requirements

- [Codex Desktop or Codex CLI](https://developers.openai.com/), or [Claude Code](https://code.claude.com/docs/en/overview), with plugin support.
- [uv](https://docs.astral.sh/uv/) on `PATH`.
- Python 3.11 or newer. `uv` installs and manages the compatible runtime and locked dependencies automatically.
- Internet access during the first run so `uv` can download Python packages.
- An LLM capable of calling MCP tools. The included skill supplies the required orchestration instructions.

Install `uv` with one of the official methods:

```bash
# macOS with Homebrew
brew install uv

# Windows with WinGet
winget install --id=astral-sh.uv -e

# macOS or Linux installer
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Install in Codex Desktop

Add this Git repository as a marketplace, then install the plugin:

```bash
codex plugin marketplace add colevr1/pdf-layout-translator --ref main
codex plugin add pdf-layout-translator@pdf-layout-tools
```

Fully restart Codex Desktop after installation, then create a new task so the new skill and MCP server are loaded.

Attach a text-based PDF and ask:

```text
/translate this PDF into French
```

Natural language works too:

```text
Translate the attached PDF into German and preserve its layout.
```

By default, the translated file is written beside the source as `<original-name>-<language>.pdf`. The caller may supply another output path. The source is never overwritten.

### Verify the installation

```bash
codex plugin list
```

The list should contain `pdf-layout-translator@pdf-layout-tools` with status `installed, enabled`.

### Update

```bash
codex plugin marketplace upgrade pdf-layout-tools
codex plugin add pdf-layout-translator@pdf-layout-tools
```

Restart Codex Desktop and begin a new task after upgrading.

### Remove

```bash
codex plugin remove pdf-layout-translator@pdf-layout-tools
codex plugin marketplace remove pdf-layout-tools
```

## Install in Claude Code

Add the GitHub repository as a Claude Code marketplace, then install the plugin:

```bash
claude plugin marketplace add colevr1/pdf-layout-translator
claude plugin install pdf-layout-translator@pdf-layout-tools
```

Start a new Claude Code session. If you install from an active interactive session, run `/reload-plugins` when Claude Code requests it. Claude may ask you to approve the local MCP server the first time it starts.

Invoke the namespaced skill and attach or reference a text-based PDF:

```text
/pdf-layout-translator:translate this PDF into French
```

Natural language can also trigger the skill:

```text
Translate the attached PDF into German and preserve its layout.
```

### Verify the Claude Code installation

```bash
claude plugin list
```

### Update in Claude Code

```bash
claude plugin marketplace update pdf-layout-tools
claude plugin update pdf-layout-translator@pdf-layout-tools
```

Run `/reload-plugins` or start a new session after upgrading.

### Remove from Claude Code

```bash
claude plugin uninstall pdf-layout-translator@pdf-layout-tools
claude plugin marketplace remove pdf-layout-tools
```

## Install in another MCP-compatible LLM client

Clients that implement neither Codex nor Claude Code plugins can run the MCP server directly.

1. Clone and prepare the repository:

   ```bash
   git clone https://github.com/colevr1/pdf-layout-translator.git
   cd pdf-layout-translator
   uv sync --project plugins/pdf-layout-translator --locked
   ```

2. Add a stdio MCP server to the client, replacing `/absolute/path` with the cloned repository path:

   ```json
   {
     "mcpServers": {
       "pdf-layout-translator": {
         "command": "uv",
         "args": [
           "run",
           "--project",
           "/absolute/path/pdf-layout-translator/plugins/pdf-layout-translator",
           "--locked",
           "/absolute/path/pdf-layout-translator/plugins/pdf-layout-translator/server.py"
         ]
       }
     }
   }
   ```

3. Give the model the workflow in [`skills/translate/SKILL.md`](plugins/pdf-layout-translator/skills/translate/SKILL.md), or install that directory using the client's skill mechanism.

The client must support local stdio MCP servers and allow the model to call tools. Configuration filenames and UI steps vary by client.

### Codex agent-install prompt

The following prompt can be given to a coding agent with shell access:

```text
Install https://github.com/colevr1/pdf-layout-translator as a Codex marketplace.
Ensure `uv` is installed and available on PATH. Run:
  codex plugin marketplace add colevr1/pdf-layout-translator --ref main
  codex plugin add pdf-layout-translator@pdf-layout-tools
Verify it with `codex plugin list`, then tell me to restart Codex Desktop and open a new task.
Do not modify the plugin files or replace the locked dependencies.
```

### Claude Code agent-install prompt

```text
Install https://github.com/colevr1/pdf-layout-translator as a Claude Code marketplace.
Ensure `uv` is installed and available on PATH. Run:
  claude plugin marketplace add colevr1/pdf-layout-translator
  claude plugin install pdf-layout-translator@pdf-layout-tools
Verify it with `claude plugin list`, then tell me to start a new session or run /reload-plugins.
Do not modify the plugin files or replace the locked dependencies.
```

## Troubleshooting

### Codex says the cached skill path moved

Restart Codex Desktop completely and create a new task. A running app can retain the previous versioned plugin catalog after an upgrade.

### Claude Code does not show the skill or MCP tools

Run `/reload-plugins` or start a new Claude Code session. Then use `claude plugin list` to confirm that `pdf-layout-translator@pdf-layout-tools` is enabled. Run `claude --debug` to inspect MCP startup errors.

### The MCP server does not start

Confirm that `uv` is available to GUI applications, not only to an interactive shell:

```bash
uv --version
codex plugin list
```

If `uv` was installed after Codex Desktop started, restart the app so it receives the updated `PATH`.

### The PDF has no selectable text

The document is probably scanned or image-only. Run OCR in another application first, then retry with the OCR-enabled PDF.

### The output reports fitting warnings

Longer translations may require substantial scaling inside a fixed source rectangle. Review every warned page and shorten or revise only the affected translation if necessary.

## How it works

The model and MCP server divide responsibilities:

1. `prepare_translation` extracts positioned text segments and records their geometry and style.
2. `get_translation_batch` returns source segments to the active model.
3. The model translates each segment and preserves semantic `[[B]]` and `[[I]]` marker pairs.
4. `submit_translations` validates IDs and formatting markers.
5. `render_translated_pdf` removes only the original text, inserts translated HTML text, and retains images and vector drawings.

The server exposes these MCP tools:

| Tool | Purpose |
| --- | --- |
| `prepare_translation` | Validate the input and create a translation job. |
| `get_translation_batch` | Return the next untranslated segments. |
| `submit_translations` | Validate and store translated segments. |
| `render_translated_pdf` | Produce the final PDF and report fitting warnings. |

## Privacy and security

- PDF processing and rendering run locally.
- Extracted text is sent only through the active LLM session used to translate it.
- The plugin has no authentication secrets and does not call a third-party translation service.
- Review the repository and lockfile before installing, as with any executable plugin.
- Each host uses its own plugin-root resolution: Codex resolves a working directory relative to its plugin root, while Claude Code uses `${CLAUDE_PLUGIN_ROOT}`. No absolute developer-machine path is embedded.

## Development

```bash
git clone https://github.com/colevr1/pdf-layout-translator.git
cd pdf-layout-translator
uv sync --project plugins/pdf-layout-translator --locked
uv run --project plugins/pdf-layout-translator --locked python plugins/pdf-layout-translator/test_server.py
uvx ruff check plugins/pdf-layout-translator
claude plugin validate .
```

The test suite checks segment extraction, inline formatting markers, alignment inference, PDF rendering, vector preservation, and the MCP handshake.

When publishing an update:

1. Change `version` in both plugin manifests and `plugins/pdf-layout-translator/pyproject.toml`.
2. Run the tests and both plugin validators.
3. Commit, tag the release, and push.
4. Users refresh their marketplace and update or reinstall the plugin in their client.

## Repository layout

```text
.
├── .agents/plugins/marketplace.json
├── .claude-plugin/marketplace.json
├── .github/workflows/ci.yml
└── plugins/pdf-layout-translator/
    ├── .claude-plugin/plugin.json
    ├── .codex-plugin/plugin.json
    ├── .mcp.json               # Claude Code MCP paths
    ├── skills/translate/SKILL.md
    ├── pyproject.toml
    ├── server.py
    ├── test_server.py
    └── uv.lock
```

## License

[MIT](LICENSE)
