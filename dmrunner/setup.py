"""
Contains all the logic and process involved in validating that a user has a suitable local environment for running
the Digital Marketplace (eg git, Docker, checked-out code, etc)
"""
from distutils.version import LooseVersion
import docker
import errno
import glob
import gzip
import itertools
import multiprocessing
import os
import psutil
import requests
import subprocess
import threading
import time
from typing import Callable, Dict
import webbrowser

from dmrunner.utils import (
    APP_COMMAND_RESTART,
    EXITCODE_BAD_SERVICES,
    EXITCODE_DOCKER_NOT_AVAILABLE,
    EXITCODE_GIT_AUTH_FAILED,
    EXITCODE_GIT_NOT_AVAILABLE,
    EXITCODE_BOOTSTRAP_FAILED,
    EXITCODE_SETUP_ABORT,
    EXITCODE_NODE_NOT_IN_PATH,
    EXITCODE_NODE_VERSION_NOT_SUITABLE,
    EXITCODE_CONFIG_NO_EXIST,
    group_by_key,
    get_app_info,
    nologger,
    red,
    yellow,
    green,
    bold,
    load_config,
    save_config
)
from dmrunner.process import DMServices, DMProcess, background_services, blank_context

MINIMUM_DOCKER_VERSION = '17.09'
SPECIFIC_NODE_VERSION = 'v6.12.2'  # TODO: This should be pulled from the docker base image really.
# TODO: Check Yarn version once we've converted to Yarn completely


def _setup_config_modifications(logger, config, config_path):
    exitcode, interim_config = load_config(config_path)

    if not exitcode:
        default_code_directory = os.path.realpath(interim_config['code']['directory'])
        logger('If you are an existing developer, enter the directory where your current Digital Marketplace code is '
               'checked out.')
        logger('If you do not have code currently checked out, enter the directory you would like '
               'code to be downloaded to.')
        logger('[default: {}]:'.format(yellow(default_code_directory)), end='')
        requested_code_directory = os.path.realpath(input(' ').strip() or default_code_directory)
        os.makedirs(requested_code_directory, exist_ok=True)

        logger('Code directory set to ' + yellow(requested_code_directory))
        interim_config['code']['directory'] = requested_code_directory

        save_config(interim_config, config_path)

    # Patch the runner config with our new/modified configuration.
    config.update(interim_config)

    return 0


def _setup_logging_directory(config):
    # Create raw log directory used to persistently store any application logs.
    try:
        os.makedirs(os.path.join(os.path.realpath('.'), config['logging']['directory']))

    except OSError as e:
        if e.errno != errno.EEXIST:
            return e.errno

    return 0

def _setup_check_git_available(logger):
    logger(bold('Verifying Git is available ...'))

    try:
        subprocess.check_call(['git', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger(green('* Git is available. Obviously.'))

    except Exception:  # noqa
        logger(red('* You do not appear to have Git installed and/or in your path. Please install it.'))
        return EXITCODE_GIT_NOT_AVAILABLE

    return 0


def _setup_check_docker_available(logger):
    logger(bold('Verifying Docker is available ...'))

    try:
        docker_client = docker.from_env()

    except requests.exceptions.ConnectionError:
        logger(red('* You do not appear to have Docker installed and/or running. Please install Docker and '
                   'ensure it is running in the background.'))
        return EXITCODE_DOCKER_NOT_AVAILABLE

    except docker.errors.APIError as e:
        logger(red('* An error occurred connecting to the Docker API. Please make sure it has finished starting up and '
                   'is running properly: {}'.format(e)))
        return EXITCODE_DOCKER_NOT_AVAILABLE

    try:
        v = docker_client.version()['Version']
        assert LooseVersion(v) >= LooseVersion(MINIMUM_DOCKER_VERSION)

    except AssertionError:
        logger(yellow('* WARNING - You are running Docker version {}. If you are on macOS, you need '
                      'Docker for Mac version {} or higher.'.format(v, MINIMUM_DOCKER_VERSION)))

    else:
        logger(green('* Docker is available and a suitable version appears to be installed ({}).'.format(v)))

    return 0


def _setup_check_node_version(logger):
    exitcode = 0
    logger(bold('Checking Node version ...'))

    try:
        node_version = subprocess.check_output(['node', '-v'], universal_newlines=True).strip()

    except Exception:
        logger(red('* Unable to verify Node version. Please check that you have Node installed and in your path.'))
        exitcode = EXITCODE_NODE_NOT_IN_PATH

    else:
        try:
            assert LooseVersion(node_version) == LooseVersion(SPECIFIC_NODE_VERSION)
            logger(green('* You are using a suitable version of Node ({}).'.format(node_version)))

        except AssertionError:
            logger(red('* You have Node {} installed; you should use {}'.format(node_version, SPECIFIC_NODE_VERSION)))
            exitcode = EXITCODE_NODE_VERSION_NOT_SUITABLE

    return exitcode


def _setup_download_repos(logger, config, settings):
    exitcode = 0
    logger(bold('Checking authentication with GitHub ...'))

    try:

        retcode = subprocess.call(['ssh', '-T', 'git@github.com'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if retcode != 1:
            logger(red(* 'Authentication failed - check that your local SSH keys have been uploaded to GitHub.'))
            return EXITCODE_GIT_AUTH_FAILED

        else:
            logger(green('* Authentication to Github succeeded.'))

        code_directory = os.path.realpath(os.path.join('.', config['code']['directory']))

        logger(bold(f'Ensuring you have local copies of Digital Marketplace code in {code_directory} ...'))

        os.makedirs(code_directory, exist_ok=True)

        nested_repositories = group_by_key(settings['repositories'], 'run-order', include_missing=True)
        for repo_name in itertools.chain.from_iterable(nested_repositories):
            repo_path = os.path.join(code_directory, repo_name)

            if os.path.isdir(repo_path):
                continue

            logger(green('* Downloading') + ' ' + settings['repositories'][repo_name].get('name', repo_name) + ' ')
            process = subprocess.run(['git', 'clone', os.path.join(settings['base-git-url'], repo_name)],
                                     cwd=code_directory,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

            if process.returncode != 0:
                logger(red(process.stdout))
                return process.returncode

        if not exitcode:
            logger(green('* Your Digital Marketplace code is all present and accounted for.'))

    except KeyboardInterrupt:
        exitcode = EXITCODE_SETUP_ABORT

    return exitcode


def _setup_check_background_services(logger):
    use_docker_services = False

    logger(bold('Checking for existing background services...'))
    healthcheck_passed, healthcheck_results = DMServices.services_healthcheck(threading.Event(), check_once=True)
    first_result = next(iter(healthcheck_results.values()))  # Used to ensure all results are identical

    if not healthcheck_passed and not all(map(lambda x: x is first_result, healthcheck_results.values())):
        services_up = [x[0].title() for x in list(filter(lambda y: y[1] is True, healthcheck_results.items()))]
        services_down = [x.title() for x in set(healthcheck_results.keys()) - set([x.lower() for x in services_up])]
        logger(red('* You have some services running locally (Up: {}. Down: {}).'.format(', '.join(services_up),
                                                                                         ', '.join(services_down))))
        logger(red('* You can either manage all background services yourself or allow DMRunner to manage them for you '
                   '- but not a bit of both.'))
        return EXITCODE_BAD_SERVICES, False

    elif healthcheck_passed:
        logger(green('* Discovered full suite of existing background services.'))

    else:
        logger(green('* None found. Background services will be managed for you.'))
        use_docker_services = True

    return 0, use_docker_services


def _setup_check_postgres_data_if_required(logger, settings, use_docker_services):
    exitcode = 0
    logger(bold('Checking that you have data available to populate your Postgres database.'))

    if use_docker_services:
        data_path = os.path.join(os.path.realpath('.'), settings['sql-data-path'])
        os.makedirs(data_path, exist_ok=True)

        def data_available():
            return glob.glob(os.path.join(data_path, '*.sql')) or glob.glob(os.path.join(data_path, '*.sql.gz'))

        while not data_available():
            logger(red('* No data is available.') + ' When you press ENTER, a link will be opened for you. Please '
                                                  'download the file to `{data_path}` then press ENTER '
                                                  'again.'.format(data_path=data_path), end='')
            input(' ')
            webbrowser.open(settings['data-dump-url'])
            logger('* ')
            logger('* Press ENTER, after saving the file to `{data_path}`, to continue, or type anything to '
                   'abort.'.format(data_path=data_path), end='')
            user_input = input(' ').strip()
            if user_input:
                raise KeyboardInterrupt

        if not exitcode:
            gzip_sql_files = glob.glob(os.path.join(data_path, '*.sql.gz'))
            for gzip_sql_file in gzip_sql_files:
                target_sql_file = gzip_sql_file[:-3]  # Remove '.gz' suffix

                if not os.path.isfile(target_sql_file):
                    logger('* Extracting {} ...'.format(gzip_sql_file))

                    try:
                        with open(target_sql_file, 'wb') as outfile, gzip.open(gzip_sql_file, 'rb') as infile:
                            outfile.write(infile.read())

                    except KeyboardInterrupt:
                        os.remove(target_sql_file)
                        exitcode = EXITCODE_SETUP_ABORT

                    else:
                        os.remove(gzip_sql_file)
                        logger('* Extracted.')

        if not exitcode:
            logger(green('* You have data available to populate your Postgres database.'))

    return exitcode


def _setup_bootstrap_repositories(logger: Callable, config: dict, settings: dict):
    exitcode = 0
    logger(bold('Bootstrapping repositories ...'))

    try:
        nested_repositories = group_by_key(settings['repositories'], 'run-order', include_missing=True)
        for repo_name in itertools.chain.from_iterable(nested_repositories):
            if 'bootstrap' in settings['repositories'][repo_name]:
                app_info = get_app_info(repo_name, config, settings, {})

                logger(green('* Starting bootstrap of') + ' ' + app_info['name'] + ' ...', log_name='setup')

                bootstrap_command = settings['repositories'][repo_name]['bootstrap']
                exitcode = DMProcess(app=app_info, logger=logger, app_command=bootstrap_command).wait()

                if exitcode:
                    logger(red('* Bootstrap failed for ') + app_info['name'] + red(' with exit code {}').format(exitcode))
                    exitcode = EXITCODE_BOOTSTRAP_FAILED
                    break

                else:
                    logger(green('* Bootstrap completed for') + ' ' + app_info['name'] + ' ', log_name='setup')

    except KeyboardInterrupt:
        exitcode = EXITCODE_SETUP_ABORT

    return exitcode


def _setup_indices(logger: Callable, config: dict, settings: dict):
    exitcode = 0
    manager = multiprocessing.Manager()

    logger(bold('Bootstrapping search indices ...'))

    dependencies = []
    for dependency in settings['index']['dependencies']:
        dependency_app_info = get_app_info(dependency, config, settings, manager.dict())
        dependencies.append((DMProcess(app=dependency_app_info,
                                       logger=nologger,
                                       app_command=APP_COMMAND_RESTART),
                             dependency_app_info))

    time.sleep(10)

    for index in settings['index']['indices']:
        index_name = index['keyword']['index']

        app_info = get_app_info(settings['index']['repository'], config, settings, manager.dict())
        try:
            assert requests.get(settings['index']['test'].format(index=index_name)).status_code == 200

        except Exception:
            index_command = '{command} {keyword} {positional}'.format(
                command=settings['index']['command'],
                keyword=' '.join(['--{k}={v}'.format(k=k, v=v) for k, v in index['keyword'].items()]),
                positional=' '.join(index['positional']),
            )

            logger("* Creating index '{}' ...".format(index_name))

            exitcode = DMProcess(app=app_info, logger=logger, app_command=index_command).wait()
            if exitcode:
                logger(red("* Something went wrong when creating the '{}' index: exitcode "
                           "{}".format(index_name, exitcode)))
                exitcode = EXITCODE_BOOTSTRAP_FAILED
                break

            else:
                logger(green("* Index '{}' created successfully.".format(index_name)))

        else:
            logger(green("* Index '{}' already exists.".format(index_name)))

    for dependency, dependency_app_info in dependencies:
        try:
            p = psutil.Process(dependency_app_info['process'])

            for child in p.children(recursive=True):
                child.kill()

            p.kill()

        except Exception as e:
            logger(str(e))
            exitcode = EXITCODE_BOOTSTRAP_FAILED

    return exitcode


def setup_and_check_requirements(logger: Callable, config: dict, config_path: str, settings: Dict,
                                 only_check_services: bool = False):
    """This runs some basic checks to ensure that the User has everything required for DMRunner to function
    correctly, eg their own config file, Docker (and possibly Nix in the future), docker images, checked-out code.
    """
    exitcode = 0
    use_docker_services = False

    if only_check_services:
        exitcode, interim_config = load_config(config_path, must_exist=True)
        config.update(interim_config)
        if not exitcode:
            exitcode, use_docker_services = _setup_check_background_services(logger)

    else:
        logger(bold('Starting setup ...'))

        try:
            exitcode = _setup_config_modifications(logger, config, config_path)
            exitcode = exitcode or _setup_logging_directory(config)
            exitcode = exitcode or _setup_check_git_available(logger)
            exitcode = exitcode or _setup_check_docker_available(logger)
            exitcode = exitcode or _setup_check_node_version(logger)
            exitcode = exitcode or _setup_download_repos(logger, config, settings)

            exitcode, use_docker_services = _setup_check_background_services(logger) \
                if not exitcode else \
                (exitcode, False)
            exitcode = exitcode or _setup_check_postgres_data_if_required(logger, settings,
                                                                          use_docker_services)

            with (background_services(logger, docker_compose_filepath=settings['docker-compose-filepath'])
                    if use_docker_services and not exitcode else blank_context()):
                exitcode = exitcode or _setup_bootstrap_repositories(logger, config, settings)
                exitcode = exitcode or _setup_indices(logger, config, settings)

        except BaseException:
            exitcode = EXITCODE_SETUP_ABORT

    if exitcode:
        if only_check_services:
            if exitcode == EXITCODE_CONFIG_NO_EXIST:
                logger(red('Configuration file not found. Run `make setup` to generate.'))

            else:
                logger(red('Startup failed with exitcode: {}'.format(exitcode)))

        else:
            logger(red('Aborting setup ...'))

    elif not only_check_services:
        logger(bold('Setup completed successfully.'))

    return exitcode, use_docker_services, config
