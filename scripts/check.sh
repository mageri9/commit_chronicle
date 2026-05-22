#!/usr/bin/env bash

echo "=================================="
echo " GitHub Profiler Validation Check "
echo "=================================="

echo ""
echo "[1/4] Ruff lint..."
ruff check .
if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Ruff lint failed"
    exit 1
fi

echo ""
echo "[2/4] Ruff format..."
ruff format --check .
if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Formatting issues found"
    echo "Run: bash scripts/fix.sh"
    exit 1
fi

echo ""
echo "[3/4] MyPy..."
mypy github_profiler
if [ $? -ne 0 ]; then
    echo ""
    echo "❌ MyPy failed"
    exit 1
fi

echo ""
echo "[4/4] Pytest..."
pytest
if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Tests failed"
    exit 1
fi

echo ""
echo "✅ All checks passed"