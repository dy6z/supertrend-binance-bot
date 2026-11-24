#!/bin/bash
# Binance Auto Executor Loop (30-sec interval)

SCRIPT_DIR="/home/dy6z/.hermes/scripts"

while true; do
    /usr/bin/python3 "${SCRIPT_DIR}/binance_auto_executor.py"
    
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -ne 0 ]; then
        LOG_PREFIX="[$(date +'%Y-%m-%d %H:%M:%S %Z')]"
        echo "${LOG_PREFIX} ❌ Executor failed (exit $EXIT_CODE)"
    fi
    
    sleep 30
done
