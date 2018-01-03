import colored
import itertools
import os
import ruamel.yaml
from typing import Tuple, Dict

APP_COMMAND_RESTART = 'run'
APP_COMMAND_REBUILD = 'rebuild'
APP_COMMAND_FRONTEND = 'frontend'

EXITCODE_DOCKER_NOT_AVAILABLE = 1
EXITCODE_BAD_SERVICES = 2
EXITCODE_DOCKER_BUILD_FAILED = 3
EXITCODE_GIT_NOT_AVAILABLE = 4
EXITCODE_GIT_AUTH_FAILED = 5
EXITCODE_NO_POSTGRES_DATA = 6
EXITCODE_NOT_ANTICIPATED_EXECUTION = 7
EXITCODE_NODE_NOT_IN_PATH = 8
EXITCODE_NODE_VERSION_NOT_SUITABLE = 9
EXITCODE_BOOTSTRAP_FAILED = 10
EXITCODE_SETUP_ABORT = 11
EXITCODE_CONFIG_NO_EXIST = 12

PROCESS_NOEXIST = -1
PROCESS_TERMINATED = -2

RUNNER_COMMAND_SETUP = 'setup'
RUNNER_COMMAND_RUN = 'run'
RUNNER_COMMANDS = [RUNNER_COMMAND_SETUP, RUNNER_COMMAND_RUN]

EXAMPLE_CONFIG_PATH = os.path.join(os.path.realpath('.'), 'config', 'example-config.yml')


def bold(text):
    return colored.stylize(text, colored.attr('bold'))


def red(text):
    return bold(colored.stylize(text, colored.fg('light_red')))


def yellow(text):
    return bold(colored.stylize(text, colored.fg('yellow')))


def green(text):
    return colored.stylize(text, colored.fg('green'))


def group_by_key(dictionary, key, include_missing=False):
    """Returns a nested list of app names which config wants us to run to bring up the Digital Marketplace locally.
    Each sublist must come up completely before the next list will be executed. This allows APIs to come up before
    frontends, preventing errors that might otherwise occur due to services not being available."""
    items = filter(lambda x: key in x[1], dictionary.items())

    # Group by run-order and then return only the names of the apps in the groups.
    grouped_items = [[y[0] for y in x[1]] for x in itertools.groupby(items, lambda x: x[1][key])]

    if include_missing:
        grouped_items.append([x[0] for x in sorted(dictionary.items(), key=lambda x: x[0]) if key not in x[1]])

    return grouped_items


def get_app_info(repo_name, config, settings, container):
    """THIS NEEDS TO GO. BAD MOJO."""

    container['name'] = settings['repositories'][repo_name]['name']
    container['commands'] = settings['repositories'][repo_name].get('commands', {}).copy()
    container['repo_path'] = os.path.join(os.path.realpath('.'), config['code']['directory'], repo_name)
    container['repo_name'] = repo_name
    container['attached'] = False
    container['process'] = PROCESS_NOEXIST

    return container


def nologger(*args, **kwargs):
    """Logging data/search api calls during the heavy load of indexing incurs a significant (~100%) performance
    penalty, so use this as the logger to ignore them."""
    return


def load_config(config_path, must_exist=False) -> Tuple[int, Dict]:
    exitcode = 0
    interim_config = {}

    try:
        with open(config_path, 'rt') as config_file:
            interim_config = ruamel.yaml.round_trip_load(config_file.read())

    except OSError:
        if must_exist:
            exitcode = EXITCODE_CONFIG_NO_EXIST

        try:
            with open(EXAMPLE_CONFIG_PATH, 'r') as example_config_file:
                example_config = example_config_file.read()
                example_config = example_config.split('# ' + ('-' * 118))[1]
                interim_config = ruamel.yaml.round_trip_load(example_config)

        except OSError as e:
            exitcode = exitcode or e.errno

    return exitcode, interim_config


def save_config(config, config_path) -> None:
    with open(config_path, 'wt') as config_file:
        config_file.write(ruamel.yaml.round_trip_dump(config))
