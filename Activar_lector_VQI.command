#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .vqi-env
.vqi-env/bin/python -m pip install --upgrade 'yt-dlp[default]'
.vqi-env/bin/yt-dlp --version
echo "Lector instalado. Pulsa Consultar datos técnicos de nuevo en la ficha VQI."
