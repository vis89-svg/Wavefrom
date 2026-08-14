#!/bin/sh
set -e

export HOME="${HOME:-/root}"
mkdir -p "$HOME/.config/opencode"
mkdir -p /workspace

PROVIDER_LINE=""
MODEL_LINE=""
[ -n "$DEFAULT_PROVIDER" ] && PROVIDER_LINE="\"provider\": \"$DEFAULT_PROVIDER\","
[ -n "$DEFAULT_MODEL" ] && MODEL_LINE="\"model\": \"$DEFAULT_MODEL\","

if [ -n "$DEFAULT_PROVIDER" ] || [ -n "$DEFAULT_MODEL" ]; then
  {
    echo "{"
    echo "  $PROVIDER_LINE"
    echo "  $MODEL_LINE"
    echo "  \"server\": { \"hostname\": \"0.0.0.0\", \"port\": ${PORT:-4096} }"
    echo "}"
  } > "$HOME/.config/opencode/opencode.json"
fi

git config --global user.name "${GIT_USER_NAME:-opencode}"
git config --global user.email "${GIT_USER_EMAIL:-opencode@localhost}"

if [ -n "$GITHUB_TOKEN" ]; then
  git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f'
fi

cd /workspace

if [ -n "$REPO_URL" ]; then
  REPO_DIR="$(basename "${REPO_URL%.git}")"
  if [ ! -d "$REPO_DIR" ]; then
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  cd "$REPO_DIR"
fi

echo "Starting opencode server on port ${PORT:-4096}"
exec opencode serve --hostname 0.0.0.0 --port "${PORT:-4096}"
