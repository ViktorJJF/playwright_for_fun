#!/bin/sh
conda activate fun
python -m flask --app main run -p $PORT --debug