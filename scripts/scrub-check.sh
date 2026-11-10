#!/usr/bin/env bash
# Fail if secrets or home-directory paths leak into tracked files.
#
# Patterns in this file are generic (keys, tokens, /home/<name>/, private IPv4).
# Machine, profile, and person names stay out of the published script.
# Add those locally in scripts/scrub-local.txt (gitignored): one extended
# regex per line. Blank lines and lines starting with # are ignored.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Token names are split so this file does not contain a match of its own pattern.
PAT='(/home/[a-z]+/|192\.168\.|API_SERVER_'"KEY"'|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16}|xox[bp]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,}|hf_[A-Za-z0-9]{20,})'

files() {
  git ls-files -co --exclude-standard
}

hits=$(files | xargs -r grep -nIE "$PAT" 2>/dev/null || true)

local_file="$ROOT/scripts/scrub-local.txt"
if [[ -f "$local_file" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# ]] && continue
    extra=$(files | xargs -r grep -nIE -- "${line}" 2>/dev/null || true)
    if [[ -n "${extra}" ]]; then
      hits="${hits}"$'\n'"${extra}"
    fi
  done < "$local_file"
fi

hits=$(printf '%s\n' "$hits" | sed '/^$/d')
if [[ -n "$hits" ]]; then
  echo "SCRUB FAILED — secrets or blocked strings in tracked files:"
  echo "$hits" | head -40
  exit 1
fi
echo "scrub-check OK ($(files | wc -l) files clean)"
