"""
Configuration settings for Validation Agent.
Loads environment variables from .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# ============================================
# PATH CONFIGURATION
# ============================================

# Base directory (project root)
BASE_DIR = Path(__file__).parent

# Workspace path (where baselines are copied TO - destination)
WORKSPACE_PATH = Path(os.getenv("WORKSPACE_PATH", "C:/Clients/SSAB.OX/baseline"))

# Source path (where baselines are copied FROM - G drive)
SOURCE_PATH = Path(os.getenv("SOURCE_PATH", "G:/Clients/SSAB-OX/Validation"))

# PowerShell library path
PS_LIBRARY_PATH = Path(os.getenv("PS_LIBRARY_PATH", ""))

# Compare script path
COMPARE_SCRIPT_PATH = os.getenv("COMPARE_SCRIPT_PATH", r"C:\Clients\SSAB.OX\baseline\CompareX.ps1")

# Sessions path (for tracking progress)
SESSIONS_PATH = Path(os.getenv("SESSIONS_PATH", "./data/sessions"))

# Logs path
LOGS_PATH = Path(os.getenv("LOGS_PATH", "./data/logs"))

# Codebase repo path (each dev sets their own local checkout in .env)
SSAB_OX_REPO_PATH = os.getenv("SSAB_OX_REPO_PATH", "")

# ExperLib runtime library repo path (optional).
# When set, the AnalysisAgent will scan the analyzed C# code + error text
# for ExperLib type names (DateType, SqlQuery, Clock, etc.) and inject the
# matching .cs source into the LLM prompt so the AI understands the actual
# runtime it's working against — instead of guessing at the API surface.
# Leave empty to disable the feature entirely (zero overhead).
EXPERLIB_REPO_PATH = os.getenv("EXPERLIB_REPO_PATH", "")

# COBOL source path
COBOL_SOURCE_PATH = os.getenv("COBOL_SOURCE_PATH", "")

# Compare Command
COMPARE_COMMAND = os.getenv("COMPARE_COMMAND", 'compare-x -BaselineName "{baseline_name}" -PromptOnFail $false -GenerateReportOn All')

# ============================================
# XRF UPDATE (Auto Apply hook)
# ============================================
# When Auto Apply modifies a .cs file directly (bypassing Visual Studio +
# ArcEdit), the paired .xrf cross-reference file is left stale. This script
# regenerates the .xrf line mappings from the git diff.
# Empty path disables the feature — Auto Apply then behaves as before.
XRF_UPDATE_SCRIPT_PATH = os.getenv(
    "XRF_UPDATE_SCRIPT_PATH",
    str(BASE_DIR / "scripts" / "Update-XrefFromCsEdit.ps1"),
)
XRF_UPDATE_POWERSHELL_EXE = os.getenv("XRF_UPDATE_POWERSHELL_EXE", "pwsh")
XRF_UPDATE_TIMEOUT_SECONDS = int(os.getenv("XRF_UPDATE_TIMEOUT_SECONDS", "60"))
XRF_UPDATE_BASE_REF = os.getenv("XRF_UPDATE_BASE_REF", "HEAD")

# ============================================
# AZURE DEVOPS CONFIGURATION
# ============================================

AZURE_DEVOPS_ORG = os.getenv("AZURE_DEVOPS_ORG", "")
AZURE_DEVOPS_PROJECT = os.getenv("AZURE_DEVOPS_PROJECT", "")
AZURE_DEVOPS_REPO = os.getenv("AZURE_DEVOPS_REPO", "")
AZURE_DEVOPS_PAT = os.getenv("AZURE_DEVOPS_PAT", "")

# --- Wiki-help feature (project-generic) ---
# Leave AZURE_DEVOPS_WIKI_ID empty to auto-detect the project's default wiki
# via GET /wikis. Set to "DISABLED" to turn the feature off completely (zero
# overhead — chat behaves exactly as before).
AZURE_DEVOPS_WIKI_ID = os.getenv("AZURE_DEVOPS_WIKI_ID", "")
# In-memory TOC/page cache TTL. 30 min = fresh enough for wiki edits, cheap
# for repeat questions. Set 0 to disable caching (not recommended).
WIKI_CACHE_TTL_SECONDS = int(os.getenv("WIKI_CACHE_TTL_SECONDS", "1800"))
# Assistant persona label. Empty = auto-derived from AZURE_DEVOPS_PROJECT.
WIKI_ASSISTANT_NAME = os.getenv("WIKI_ASSISTANT_NAME", "")

# Local Git Repo
GIT_REPO_PATH = Path(os.getenv("GIT_REPO_PATH", ""))
GIT_MAIN_BRANCH = os.getenv("GIT_MAIN_BRANCH", "develop")


# ============================================
# ENSO AI CONFIGURATION (Week 6+)
# ============================================

ENSO_AI_API_URL = os.getenv("ENSO_AI_API_URL", "")
ENSO_AI_API_KEY = os.getenv("ENSO_AI_API_KEY", "")


# ============================================
# LOGGING
# ============================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


# ============================================
# HELPER FUNCTIONS
# ============================================

def get_baseline_group(baseline_name: str) -> str:
    """
    Extract group (SG01, SG02, etc.) from baseline name.
    
    Logic: First 2 digits after "P" = Group number
    
    Example:
        P01001-HOTO311P → "01" → SG01
        P02003-HPPL043P → "02" → SG02
    """
	# Extract digits at position 1-2 (after "P")
    group_number = baseline_name[1:3]
    return f"SG{group_number}"


def get_source_path(baseline_name: str) -> Path:
    """
    Get source path for a baseline (G drive).
    
    Example: 
        Input: "P02003-HPPL043P"
        Output: G:/Clients/SSAB-OX/Validation/SG02/P02003-HPPL043P
    """
    group = get_baseline_group(baseline_name)
    return SOURCE_PATH / group / baseline_name


def get_baseline_path(baseline_name: str) -> Path:
    """
    Get destination path for a baseline (C drive).
    
    Example: 
        Input: "P02003-HPPL043P"
        Output: C:/Clients/SSAB.OX/baseline/SG02/P02003-HPPL043P
    """
    group = get_baseline_group(baseline_name)
    return WORKSPACE_PATH / group / baseline_name


def get_report_path(baseline_name: str) -> Path:
    """
    Get report folder path for a baseline.
    
    Example:
        Input: "P02003-HPPL043P"
        Output: C:/Clients/SSAB.OX/baseline/SG02/P02003-HPPL043P/REPORT
    """
    return get_baseline_path(baseline_name) / "REPORT"


# ============================================
# CREATE DIRECTORIES IF NOT EXIST
# ============================================

def init_directories():
    """Create required directories if they don't exist."""
    SESSIONS_PATH.mkdir(parents=True, exist_ok=True)
    LOGS_PATH.mkdir(parents=True, exist_ok=True)


# Initialize on import
init_directories()


# ============================================
# TEST CONFIG
# ============================================

if __name__ == "__main__":
    print("=== Configuration ===")
    print(f"BASE_DIR: {BASE_DIR}")
    print(f"SOURCE_PATH: {SOURCE_PATH}")
    print(f"WORKSPACE_PATH: {WORKSPACE_PATH}")
    print(f"PS_LIBRARY_PATH: {PS_LIBRARY_PATH}")
    print(f"SESSIONS_PATH: {SESSIONS_PATH}")
    print(f"LOGS_PATH: {LOGS_PATH}")
    print(f"LOG_LEVEL: {LOG_LEVEL}")
    
    print("\n=== Test Paths ===")
    print(f"P02003-HPPL043P SOURCE: {get_source_path('P02003-HPPL043P')}")
    print(f"P02003-HPPL043P DEST:   {get_baseline_path('P02003-HPPL043P')}")
    print(f"P01001-HOTO311P SOURCE: {get_source_path('P01001-HOTO311P')}")
    print(f"P01001-HOTO311P DEST:   {get_baseline_path('P01001-HOTO311P')}")
    print(f"\n=== Azure DevOps ===")
    print(f"AZURE_DEVOPS_ORG: {AZURE_DEVOPS_ORG}")
    print(f"AZURE_DEVOPS_REPO: {AZURE_DEVOPS_REPO}")
    print(f"GIT_REPO_PATH: {GIT_REPO_PATH}")
    print(f"GIT_MAIN_BRANCH: {GIT_MAIN_BRANCH}")