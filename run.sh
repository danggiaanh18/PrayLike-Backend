#!/bin/bash

cd /home/prayer/amen-api
source .venv/bin/activate
exec uvicorn main:app --host 0.0.0.0 --port 8080 --reload