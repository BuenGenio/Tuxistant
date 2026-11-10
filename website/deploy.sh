#!/bin/sh
# Publishes the site to the gh-pages branch, which GitHub Pages serves. Only when the owner asks.
# SITE_URL is where it lives: change it here if a custom domain is connected, and add a CNAME file.
set -eu
cd "$(dirname "$0")"
remote=$(git remote get-url origin)
revision=$(git rev-parse --short HEAD)
SITE_URL=${SITE_URL:-https://buengenio.github.io/Tuxistant} python3 build.py
cd dist
git init -q -b gh-pages
git add -A
git commit -q -m "Publish the site from $revision

Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>"
git push -q -f "$remote" gh-pages
rm -rf .git
echo "published $revision"
