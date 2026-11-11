#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
xelatex -interaction=nonstopmode -halt-on-error -file-line-error -output-directory=build main.tex
bibtex build/main
xelatex -interaction=nonstopmode -halt-on-error -file-line-error -output-directory=build main.tex
xelatex -interaction=nonstopmode -halt-on-error -file-line-error -output-directory=build main.tex
