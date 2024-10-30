#!/bin/bash
echo "Atring up the http server"

flask --app app run --host 0.0.0.0 --port 8080