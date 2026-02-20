#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "========================================================"
echo "  auto-dev-agent"
echo "========================================================"

if [[ ! -f ".env" ]]; then
  echo ""
  echo -e "${YELLOW}[setup] .env not found. Creating from .env.example...${NC}"
  cp .env.example .env
  echo -e "${RED}[setup] Open .env and set GEMINI_API_KEY first.${NC}"
  exit 1
fi

# shellcheck disable=SC1091
source .env
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo -e "${RED}[error] GEMINI_API_KEY is not set in .env${NC}"
  exit 1
fi

echo -e "${GREEN}[ok] .env loaded${NC}"

if [[ $# -lt 1 ]]; then
  echo -e "${RED}[error] brief file path is required${NC}"
  echo "usage: ./run.sh brief.txt [--iterations N] [--interval SEC]"
  exit 1
fi

BRIEF_PATH="$1"
shift

SANITIZED_ARGS=()
for arg in "$@"; do
  # Ignore stray single dash tokens accidentally inserted by copy/paste.
  if [[ "$arg" == "-" ]]; then
    continue
  fi

  # Accept common typo forms.
  if [[ "$arg" == "-iterations" ]]; then
    SANITIZED_ARGS+=("--iterations")
    continue
  fi
  if [[ "$arg" == "-interval" ]]; then
    SANITIZED_ARGS+=("--interval")
    continue
  fi

  SANITIZED_ARGS+=("$arg")
done

if [[ ! -f "$BRIEF_PATH" ]]; then
  echo -e "${RED}[error] brief file not found: $BRIEF_PATH${NC}"
  exit 1
fi

echo -e "${GREEN}[ok] brief: $BRIEF_PATH${NC}"

PYTHON_CMD=""
if command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
else
  echo -e "${RED}[error] python not found${NC}"
  exit 1
fi

if ! "$PYTHON_CMD" -c "import sys; print(sys.version)" >/dev/null 2>&1; then
  echo -e "${RED}[error] python command is not executable: ${PYTHON_CMD}${NC}"
  exit 1
fi

echo "[setup] checking dependencies..."
"$PYTHON_CMD" -c "import google.generativeai" >/dev/null 2>&1 || {
  echo -e "${YELLOW}[setup] installing requirements...${NC}"
  "$PYTHON_CMD" -m pip install -r requirements.txt -q
}

if command -v gh >/dev/null 2>&1; then
  echo -e "${GREEN}[ok] GitHub CLI detected${NC}"
else
  echo -e "${YELLOW}[info] GitHub CLI not found (optional)${NC}"
  echo "  https://cli.github.com/"
fi

echo ""
echo -e "${GREEN}[run] starting...${NC}"
echo ""

"$PYTHON_CMD" run.py --brief "$BRIEF_PATH" "${SANITIZED_ARGS[@]}"
