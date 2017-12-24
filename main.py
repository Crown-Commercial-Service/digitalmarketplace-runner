#!/usr/bin/env python

import argparse
from dmrunner.runner import DMRunner

"""
TODO:
* nginx bootstrapping
* Proper logging
* Implement --rebuild to run secondary processes for frontend-build:watch on frontend apps
* Big ol' refactor
* nix-shell can't clone repos at the moment
* better config management via configparser
 * eg for colours, filters
* make this command-line accessible from anywhere via eg 'dmrunner' command
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', '-m', help='Specify the location of the manifest file to use for detecting and'
                                                 'running services and applications (default: manifest.yml).')
    parser.add_argument('--command', '-c', help='Override the command used from the manifest for running apps '
                                                '(default: run).')
    parser.add_argument('--checkout-directory', default='./code', help='The directory in which to checkout required '
                                                                       'code from source control (default=./code).')
    parser.add_argument('--download', action='store_true', help='Download main digitalmarketplace repositories.')

    args = parser.parse_args()

    runner = DMRunner(manifest=args.manifest, command=args.command, checkout_dir=args.checkout_directory,
                      download=args.download)
    runner.run()


if __name__ == '__main__':
    main()
