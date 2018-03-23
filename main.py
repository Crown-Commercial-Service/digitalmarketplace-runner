#!/usr/bin/env python

import argparse
import os
import re
import subprocess
import sys
import yaml
from dmrunner.runner import DMRunner, RUNNER_COMMANDS

"""
TODO:
* Proper logging
* Implement --rebuild to run secondary processes for frontend-build:watch on frontend apps
* Big ol' refactor
* nix-shell can't clone repos at the moment

Run functional tests at end of setup

Get rid of self._apps (should be part of DMProcess)

Refactor DMService/DMProcess overlap
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rebuild', '-r', action='store_true',
                        help='Do a rebuild for all apps (equivalent to using `make run-all`). This will take '
                             'longer on initial startup but will help to ensure everything is in the best state.')
    parser.add_argument('--config-path', '-c', type=str, default='config/config.yml',
                        help='Path to your configuration file, which will be created from the example config if it does'
                             'not already exist (default: config/config.yml).')
    parser.add_argument('command', type=str, default='run', choices=RUNNER_COMMANDS,
                        help="'config': Creates a local copy of configuration for you to edit.\n"
                             " 'setup': Performs setup and checks that your environment meets DM requirements.\n"
                             "   'run': Run the Digital Marketplace (default)")

    args = parser.parse_args()

    aws_access_key_id = subprocess.check_output('aws configure get aws_access_key_id'.split(), universal_newlines=True)
    aws_secret_access_key = subprocess.check_output('aws configure get aws_secret_access_key'.split(),
                                                    universal_newlines=True)

    path_to_credentials = os.getenv('DM_CREDENTIALS_REPO')
    if not path_to_credentials:
        print('You must define the environment variable DM_CREDENTIALS_REPO.')
        sys.exit(1)

    all_creds = yaml.safe_load(subprocess.check_output(f'{path_to_credentials}/sops-wrapper '
                                                       f'-d {path_to_credentials}/vars/preview.yaml'.split(),
                                                       universal_newlines=True))

    mandrill_key = all_creds['shared_tokens']['mandrill_key']
    notify_key = all_creds['notify_api_key']

    os.environ['AWS_ACCESS_KEY_ID'] = aws_access_key_id
    os.environ['AWS_SECRET_ACCESS_KEY'] = aws_secret_access_key
    os.environ['DM_MANDRILL_API_KEY'] = mandrill_key
    os.environ['DM_NOTIFY_API_KEY'] = notify_key

    runner = DMRunner(command=args.command.lower(), rebuild=args.rebuild, config_path=args.config_path)
    runner.run()


if __name__ == '__main__':
    main()
