#!/bin/bash
# cvbets daily loop wrapper for cron — activates the project venv.
cd /Users/sntvnce/cvbets || exit 1
exec .venv/bin/python daily.py