#!/bin/bash
for _ in 1 2 3 4 5 6; do
    DATE=$(curl -sIk --max-time 10 https://www.redhat.com 2>/dev/null | grep -i "^date:" | cut -d' ' -f2- | tr -d '\r')
    if [ -n "$DATE" ]; then
        date -s "$DATE"
        exit 0
    fi
    sleep 10
done
