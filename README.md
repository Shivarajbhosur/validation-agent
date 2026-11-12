# Validation Agent

Validation Agent is a Chainlit application for orchestrating baseline validation. It combines LLM-assisted analysis with local source repositories, generated program output, comparison reports, Azure DevOps, and a local Chroma knowledge base.

The application is Python-based, but some workflow steps call project-specific PowerShell cmdlets and a .NET application. The platform distinction below is important before attempting a full validation run.

The application supports both direct workflow commands and natural-language questions about a baseline, validation error, comparison result, or suggested fix.

## Platform Support

| Capability | macOS | Windows |
| --- | --- | --- |
| Install Python dependencies | Yes | Yes |
| Start the Chainlit chat UI | Yes | Yes |
| Configuration and import smoke checks | Yes | Yes |
| Knowledge-base scripts and local file/Git helpers | Yes, when the referenced files and repositories are available | Yes |
| Azure DevOps and LLM integrations | Yes, with network access and credentials | Yes, with network access and credentials |
| `copy`, `load`, and `unload` workflow steps | Only if PowerShell 7 (`pwsh`) and the required project cmdlets are installed and usable on macOS | Supported environment |
| `run` step (`dotnet run`) | Yes in principle, if the target .NET repository builds on macOS | Supported environment |
| `compare` step (`compare-x`) | Not supported by the repository's default Windows paths and Beyond Compare setup | Required for the supplied `CompareX.ps1` and Beyond Compare integration |
| Complete baseline validation workflow | Not supported as configured | Supported, with all external repositories, cmdlets, and tools installed |

macOS is therefore suitable for UI development, prompt/orchestration work, configuration checks, and integrations that do not invoke the Windows validation toolchain. Use Windows for an end-to-end baseline run.

## Requirements

- **Python 3.13.x**. This is the supported and tested Python version for the project.
- Python versions newer than 3.13, including Python 3.14+, currently have compatibility issues and are not supported.
- Git.
- An LLM provider API key and model configuration. See `prompts/model_registry.yaml`.
- Optional: Azure DevOps access and a personal access token for repository, work item, or wiki features.
- For workflow execution:
	- PowerShell 7 (`pwsh`) and the team's PowerShellScriptLibrary for PowerShell-backed steps.
	- The target SSAB.OX repository and its .NET SDK/runtime for the `run` step.
	- Beyond Compare 5 console tools for the supplied `Docs/CompareX.ps1` comparison/report path.

Do not commit `.env`, API keys, PATs, reports, sessions, or local knowledge-base files. These paths are ignored by Git.

## Installation

### macOS (zsh)

```bash
git clone <repository-url>
cd Validation-Agent
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `python3.13` is not installed, install Python 3.13 from python.org or Homebrew. Do not substitute a newer Python version. Keep the environment activated for all commands below.

### Windows (PowerShell)

```powershell
git clone <repository-url>
Set-Location Validation-Agent
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks script activation, use the organization's approved execution-policy process or invoke the environment's Python directly, for example `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.

## Configuration

Before the first application run, create or update `.env` with local values. Do not overwrite an existing file containing API keys or other credentials. Use `.env.example` as a reference for the available settings. The required LLM settings are:

```dotenv
API_URL=https://<llm-gateway-url>
API_KEY=<llm-api-key>
DEFAULT_MODEL=<model-id>
```

`DEFAULT_MODEL` is optional and defaults to the model configured in `prompts/model_registry.yaml`. If EnsoAI failover is enabled, also set `ENSO_AI_API_URL` and `ENSO_AI_API_KEY`. Verify the required settings are present before starting Chainlit; this check reports only missing variable names and never prints key values:

```bash
python -c "from dotenv import dotenv_values; required=('API_URL','API_KEY'); missing=[key for key in required if not dotenv_values('.env').get(key)]; print('Missing required .env variables: ' + ', '.join(missing) if missing else 'Required LLM settings are present.'); raise SystemExit(1 if missing else 0)"
```

For Azure DevOps features, configure:

```dotenv
AZURE_DEVOPS_ORG=https://dev.azure.com/<organization>
AZURE_DEVOPS_PROJECT=<project>
AZURE_DEVOPS_REPO=<repository>
AZURE_DEVOPS_PAT=<token>
```

For a full Windows validation run, also set paths to the baseline source, working directory, PowerShell library, comparison script, and target repositories:

```dotenv
WORKSPACE_PATH=C:/Clients/SSAB.OX/baseline
SOURCE_PATH=G:/Clients/SSAB-OX/Validation
PS_LIBRARY_PATH=C:/Users/<user>/git/PowerShellScriptLibrary
COMPARE_SCRIPT_PATH=C:/Clients/SSAB.OX/baseline/CompareX.ps1
SSAB_OX_REPO_PATH=C:/Users/<user>/git/SSAB.OX
GIT_REPO_PATH=C:/Users/<user>/git/SSAB.OX
GIT_MAIN_BRANCH=develop
```

Use absolute paths appropriate to the host. The defaults in `config.py` are Windows examples and should not be copied unchanged to macOS. Optional `COBOL_SOURCE_PATH`, `EXPERLIB_REPO_PATH`, `AZURE_DEVOPS_WIKI_ID`, and report-delivery settings can remain empty when those features are not needed. `GIT_MAIN_BRANCH` defaults to `develop`.

## External Windows Toolchain

The following workflow steps execute `pwsh` through Python subprocesses:

- `copy`: `New-Sysout`, `New-Env`, and `Copy-Input`
- `load`: `load-BaselineData`
- `unload`: `unload-BaselineData`
- `compare`: `compare-x`

The first three use the command catalogs under `prompts/skills/*/scripts/_powershell.yaml`. The compare implementation runs `pwsh -NoProfile -NonInteractive` and expects the `compare-x` cmdlet from the configured PowerShell library. `Docs/CompareX.ps1` additionally expects Beyond Compare 5's `BComp.com` at its configured Windows path.

PowerShell 7 can be installed on macOS, but that alone does not provide the team's custom cmdlets, Windows-mounted source/baseline drives, or Beyond Compare setup. Treat the full comparison workflow as Windows-only unless those dependencies have been deliberately ported and verified.

## Running the Application

From an activated virtual environment:

```bash
chainlit run app.py -w
```

Open the URL printed by Chainlit, usually `http://localhost:8000`. On Windows, the equivalent command can be run in an activated PowerShell environment:

```powershell
chainlit run app.py -w
```

The application creates session and log directories on startup. The local Chroma database under `data/knowledge_db` is used when present; ingestion scripts can populate or refresh local knowledge data.

## Testing and Smoke Checks

There is currently no dedicated `tests/` suite or test runner configuration in this repository. Run these checks after installation:

```bash
# Verify the interpreter and installed dependencies
python --version
python -m pip check

# Verify configuration imports and required directory initialization
python -c "import config; print(config.BASE_DIR)"

# Exercise the knowledge-base smoke test
python scripts/test_kb.py

# Start the UI and verify the Chainlit page loads in a browser
chainlit run app.py -w
```

On Windows, run the same Python commands in the activated PowerShell environment. For a full validation test, confirm `pwsh --version`, verify that the configured PowerShell library exposes the required cmdlets, build the target .NET repository, and execute a non-production baseline before relying on generated reports.

When adding behavior, place focused tests in a new `tests/` directory and keep external calls behind test doubles or fixtures. Do not use production paths, credentials, or customer data in tests.

## Useful Scripts

- `scripts/ingest_cobol.py` - ingest COBOL source into the knowledge base.
- `scripts/ingest_codebase.py` - ingest a codebase into the knowledge base.
- `scripts/ingest_prs.py` - ingest Azure DevOps pull requests.
- `scripts/probe_wiki.py` - check Azure DevOps wiki access.
- `scripts/test_kb.py` - exercise the local knowledge base.

## Project Areas

- `app.py` - Chainlit entry point and chat handlers.
- `agents/` - analysis, chat, validation, and reporting agents.
- `prompts/` - prompt templates, skills, and model registry.
- `prompts/skills/` - workflow skills and their local scripts/catalogs.
- `services/` - LLM, knowledge-base, source, wiki, and ingestion services.
- `workflows/` - session lifecycle management.
- `tools/` - workflow tools and auto-apply helpers.
- `models/` - Pydantic data models (session, fix suggestion).
- `utils/` - logging and progress helpers.
- `poster/` - poster generation assets.
- `languages/` - language-specific helpers.
- `scripts/` - ingestion and smoke-test scripts.
- `public/` - Chainlit UI assets.
- `Docs/` - PowerShell comparison script and design notes.

## Troubleshooting

- **`pwsh: command not found`**: install PowerShell 7 or run the workflow on the supported Windows environment.
- **PowerShell cmdlet not found**: check `PS_LIBRARY_PATH`, module installation, and the current PowerShell profile/module path.
- **`dotnet` execution fails**: install the SDK required by the target repository and verify `SSAB_OX_REPO_PATH`/`SSAB_OX_PROJECT_PATH`.
- **Compare reports fail**: verify the baseline folder layout, `COMPARE_COMMAND`, `COMPARE_SCRIPT_PATH`, and Beyond Compare's `BComp.com` path.
- **Azure DevOps calls fail**: check the organization/project/repository values and PAT scopes without logging the token.

## Contributing

Keep changes focused, add or update tests for behavior changes, and document new environment variables or external dependencies in `.env.example` and this README. Never commit secrets or customer data.