#!/bin/bash
set -e

CREATE_USER_CMD="create user "
CREATE_USER_CMD+=$DMRUNNER_USER
CREATE_USER_CMD+=" superuser;"

psql -d postgresql://postgres@localhost:5432 -c "$CREATE_USER_CMD"
psql -d postgresql://localhost:5432 -c 'create database digitalmarketplace;'
psql -d postgresql://localhost:5432/digitalmarketplace -c 'create database digitalmarketplace_test;'
psql -d postgresql://localhost:5432/digitalmarketplace -f /docker-entrypoint-initdb.d/data/*.sql
