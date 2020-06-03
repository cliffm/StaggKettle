#!/bin/bash

if ((`pgrep -fc stagg_kettle.py` >= 1)); then
    echo "Process already running"
    exit 1
fi

source /home/cliffm/stagg_kettle/bin/activate
/home/cliffm/src/StaggKettle/stagg_kettle.py > /dev/null

