#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
exec /usr/bin/env node download-waterlooworks.mjs
