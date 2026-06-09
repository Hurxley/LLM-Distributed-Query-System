#!/bin/bash
# Switch between normal and skewed dataset for adaptive demo
set -e

DATA_DIR="$(dirname "$0")/../data"

if [ "$1" = "skewed" ]; then
    echo "Switching to skewed dataset..."
    cp "$DATA_DIR/salary_skewed.db" "$DATA_DIR/salary.db"
    echo "Done. Restart worker_c to apply changes."
elif [ "$1" = "normal" ]; then
    echo "Regenerating normal dataset..."
    cd "$(dirname "$0")/.."
    python3 scripts/gen_data.py
    echo "Done. Restart worker_c to apply changes."
else
    echo "Usage: $0 [normal|skewed]"
    echo "  normal  - Restore the standard dataset"
    echo "  skewed  - Switch to the skewed (uneven) dataset for adaptive demo"
fi
