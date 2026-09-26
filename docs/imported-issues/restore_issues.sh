#!/usr/bin/env bash
# Recreates the imported open issues on a target repository.
# Requires: gh CLI authenticated with permission to create issues on TARGET_REPO.
# Usage:   TARGET_REPO=ka0923s-a11y/HTDT ./restore_issues.sh
# Note:    GitHub renumbers issues; a mapping is written to issue-map.csv
#          (original_number,new_url) next to this script.
set -euo pipefail
TARGET_REPO="${TARGET_REPO:?set TARGET_REPO=owner/repo}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
MAP="$SRC_DIR/issue-map.csv"
echo "original_number,new_url" > "$MAP"

for f in "$SRC_DIR"/issues/issue-*.md; do
  base="$(basename "$f")"
  num="${base#issue-}"; num="${num%.md}"; num=$((10#$num))
  title="$(head -1 "$f")"; title="${title#\# }"
  url="$(gh issue create --repo "$TARGET_REPO" --title "$title" --body-file "$f")"
  echo "$num,$url" >> "$MAP"
  echo "created $url (was #$num)"
done
echo "Done - mapping written to $MAP"
