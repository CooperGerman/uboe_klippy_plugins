#!/bin/bash
###############################################################################
##
## 88        88 88
## 88        88 88
## 88        88 88
## 88        88 88,dPPYba,   ,adPPYba,   ,adPPYba,
## 88        88 88P'    "8a a8"     "8a a8P_____88
## 88        88 88       d8 8b       d8 8PP"""""""
## Y8a.    .a8P 88b,   ,a8" "8a,   ,a8" "8b,   ,aa
##  `"Y8888Y"'  `"8Ybbd8"'   `"YbbdP"'   `"Ybbd8"'
##
###############################################################################
## © Copyright 2023 Uboe S.A.S
## File:        install.sh
## Author(s):   Y.L.P.
## Description: Installation script to symlink Python files to Klipper extras
###############################################################################

set -e

KLIPPY_DIR="${HOME}/klipper/klippy"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Create the extras directory if it doesn't exist
mkdir -p "${KLIPPY_DIR}/extras"

echo "Linking .py files from ${SCRIPT_DIR} to ${KLIPPY_DIR}/extras"

# Find and symlink all .py files in the script directory (not subdirectories)
for f in "${SCRIPT_DIR}"/*.py ; do
	if [ -f "$f" ]; then
		base=$(basename "$f")
		rm -f "${KLIPPY_DIR}/extras/${base}"
		ln -sf "${SCRIPT_DIR}/${base}" "${KLIPPY_DIR}/extras/${base}"
		echo "Linked: ${base}"
	fi
done

echo "Installation complete!"
