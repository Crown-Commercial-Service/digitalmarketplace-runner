#!/usr/bin/env python3

import ansiwrap
import atexit
import colored
import datetime
import itertools
import json
import multiprocessing
import os
import prettytable
import psutil
import re
import readline
import requests
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import threading
from typing import List, Optional, Tuple, Dict
import yaml

from .process import DMProcess, DMServices
from .setup import setup_and_check_requirements
from .utils import (
    RUNNER_COMMAND_RUN,
    RUNNER_COMMANDS,
    PROCESS_TERMINATED,
    PROCESS_NOEXIST,
    APP_COMMAND_RESTART,
    APP_COMMAND_REBUILD,
    APP_COMMAND_FRONTEND,
    group_by_key,
    get_app_info,
    yellow
)

TERMINAL_CARRIAGE_RETURN = '\r'
TERMINAL_ESCAPE_CLEAR_LINE = '\033[K'

SETTINGS_PATH = os.path.join(os.path.realpath('.'), 'config', 'settings.yml')
STATUS_OK = 'OK'
STATUS_DOWN = 'DOWN'
STATUS_ATTACHED = 'ATTACHED'


class DMRunner:
    INPUT_STRING = 'Enter command (or H for help):'

    HELP_SYNTAX = """
 h /     help - Display this help file.
 s /   status - Check status for your apps.
 b /   branch - Check which branches your apps are running against.
 r /  restart - Restart any apps that have gone down (using `make run-app`).
rb /  rebuild - Rebuild and restart any failed or down apps using `make run-all`.
 f /   filter - Start showing logs only from specified apps*
fe / frontend - Run `make frontend-build` against specified apps*
 k /     kill - Kill specified apps*
 q /     quit - Terminate all running services and apps and drop back to your shell.

            * - Specify apps as a space-separator partial match on the name, e.g. 'buy search' to match the
                buyer-frontend and the search-api. If no match string is supplied, all apps will match."""

    def __init__(self, command: str, rebuild: bool, config_path: str, nix: bool = False,
                 settings_path: str = SETTINGS_PATH):
        self._command = command
        self._rebuild: bool = rebuild
        self._nix: bool = nix  # Not currently supported
        self._config_path: str = config_path
        self._settings_path: str = settings_path

        assert command in RUNNER_COMMANDS

        # Some state flags/vars used by eg the UI/event loop
        self._primary_attached_app: Dict = None
        self._shutdown: threading.Event = threading.Event()
        self._awaiting_input: bool = False
        self._suppress_log_printing: bool = False
        self._filter_logs: list = []
        self._use_docker_services: bool = False
        self._processes: dict = {}
        self._dmservices = None
        self._main_log_name = 'manager'
        self.config = {}

        # Temporarily ignore SIGINT while setting up multiprocessing components.
        # START
        curr_signal = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        self._manager = multiprocessing.Manager()
        self._apps = self._manager.dict()

        signal.signal(signal.SIGINT, curr_signal)  # Probably a race condition?
        # END

        with open(self._settings_path) as settings_file:
            self.settings: dict = yaml.safe_load(settings_file.read())

        self._main_log_name = 'setup'
        # Handles initialization of external state required to run this correctly (repos, docker images, config, etc).
        exitcode, self._use_docker_services, self.config = setup_and_check_requirements(
                logger=self.logger, config=self.config, config_path=self._config_path, settings=self.settings,
                only_check_services=self._command == RUNNER_COMMAND_RUN
        )

        if exitcode or self._command != RUNNER_COMMAND_RUN:
            self.shutdown()
            sys.exit(exitcode)

        self._main_log_name = 'manager'

        self._populate_multiprocessing_components()

        # Setup tab completion for app names.
        readline.parse_and_bind('tab: complete')
        readline.set_completer(self._app_name_completer)
        readline.set_completer_delims(' ')

    @property
    def _app_repositories(self) -> List[List[str]]:
        """Returns a nested list of repository names grouped by the order they should be started."""
        return group_by_key(self.settings['repositories'], 'run-order')


    @property
    def _app_name_width(self) -> int:
        """Dynamically determines the width of the longest app name so that tables/logging output can be rendered
        in a consistent format, regardless of which app any given entry comes from (i.e. this is used to pad text with
        spaces)."""
        try:
            if not self._app_repositories:
                return 20

        except AttributeError:
            return 20

        return max(len(self._get_app_name(r)) for r in itertools.chain.from_iterable(self._app_repositories))

    @property
    def _prompt_string(self) -> str:
        """Returns the text that should be used to form a prompt for user input. Changes when attached to an
        application (PDB prompt)."""
        prompt = DMRunner.INPUT_STRING

        if self._attached_app:
            prompt = self._get_cleaned_wrapped_and_styled_text('(Pdb) ', self._attached_app['name'])[0]

        return prompt

    @property
    def _attached_app(self) -> Optional[Dict]:
        if self._primary_attached_app:
            if self._primary_attached_app['attached'] is True:
                return self._primary_attached_app

        for app in self._apps.values():
            if app['attached']:
                self._primary_attached_app = app
                return app

        return None

    def _get_input_and_pipe_to_target(self) -> None:
        """Receives input from the user. Directs it to the appropriate channel, whether that be the DMRunner command
        interpreter or an application's stdin (for using interactive debuggers)."""
        try:
            while not self._shutdown.is_set():
                print('{}{}{}'.format(TERMINAL_CARRIAGE_RETURN, TERMINAL_ESCAPE_CLEAR_LINE, self._prompt_string),
                      flush=True, end='')

                self._awaiting_input = True
                user_input = input(' ')
                self._awaiting_input = False

                if self._attached_app:
                    self._processes[self._attached_app['name']].process_input(user_input)

                else:
                    self.process_input(user_input)

        except KeyboardInterrupt:
            pass

    def _get_app_name(self, repository: str) -> str:
        return self.settings['repositories'][repository]['name']

    def _app_name_completer(self, text: str, state: int) -> Optional[str]:
        """Used by readline to provide tab completion of app names."""
        options = [name for name in self._apps.keys() if text in name]
        if state < len(options):
            return options[state]
        else:
            return None

    def _populate_multiprocessing_components(self) -> None:
        """Populates the `self._apps` multiprocessing dictionary, which is read and written to by subprocesses managing
        the running applications."""
        for repository_name in itertools.chain.from_iterable(self._app_repositories):
            app_name = self.settings['repositories'][repository_name]['name']

            app = self._manager.dict()
            app['name'] = app_name
            app['process'] = PROCESS_NOEXIST
            app['repo_path'] = os.path.join(os.path.realpath(self.config['code']['directory']), repository_name)
            app['repo_name'] = repository_name
            app['attached'] = False
            app['commands'] = self.settings['repositories'][repository_name]['commands'].copy()

            self._apps[app_name] = get_app_info(repository_name, self.config, self.settings, self._manager.dict())

    def _check_app_status(self, app, loop=False):
        checked = False
        status = 'down'
        error_msg = 'Error not specified.'

        while loop or not checked:
            if app['process'] == PROCESS_NOEXIST:
                time.sleep(0.5)
                continue

            elif app['process'] == PROCESS_TERMINATED:
                error_msg = 'Process has gone away'
                break

            elif self._attached_app and self._attached_app['name'] == app['name']:
                status = STATUS_ATTACHED

            else:
                try:
                    status_endpoint = 'http://{server}:{port}{endpoint}'.format(
                            server=self.settings['server'],
                            port=self.settings['repositories'][app['repo_name']]['healthcheck']['port'],
                            endpoint=self.settings['repositories'][app['repo_name']]['healthcheck']['endpoint'],
                    )

                    # self.print_out('Checking status for {} at {}'.format(app['name'], status_endpoint))
                    res = requests.get(status_endpoint)
                    data = json.loads(res.text)

                    return data['status'], data

                except requests.exceptions.ConnectionError:
                    time.sleep(0.5)

                except json.decoder.JSONDecodeError as e:
                    status = 'unknown'
                    error_msg = 'Invalid data retrieved from /_status endpoint'
                    break

            checked = True

        return status, {'message': error_msg}

    def _ensure_apps_up(self, repository_names, quiet=False):
        down_apps = set()

        for repository_name in repository_names:
            app_name = self._get_app_name(repository_name)

            if self._attached_app and self._attached_app['name'] == app_name:
                continue

            if not quiet:
                self.print_out('Checking {} ...'.format(app_name))

            self._suppress_log_printing = quiet
            result, data = self._check_app_status(self._apps[app_name], loop=True)
            self._suppress_log_printing = False

            if not data or 'status' not in data or data['status'] != 'ok':
                self.print_out('Error running {} - {}'.format(app_name, data['message']))

                down_apps.add(app_name)

        time.sleep(0.1)  # TODO: remove dirty hack.

        return down_apps

    def logger(self, log_entry, log_name=None, log_attach=None, end=os.linesep):
        if self._suppress_log_printing:
            return

        if not log_name or log_attach is not None:
            log_name = self._main_log_name

        if self._filter_logs and log_name and log_name not in self._filter_logs:
            return

        if self.config.get('logging', {}).get('save-to-disk', False):
            for f in ['combined.log', '{}.log'.format(log_name)]:
                filepath = os.path.join(os.path.realpath('.'), self.config['logging']['directory'], f)
                with open(filepath, 'a') as log_file:
                    log_file.write('{}{}'.format(repr(log_entry), end))

        self.print_out(log_entry, app_name=log_name, end=end)

    def _find_matching_apps(self, selectors: Optional[List] = None) -> Tuple[str]:
        if not selectors:
            found_apps = self._apps.keys()
        else:
            found_apps = []
            for selector in selectors:
                found_app = None
                for app_name, app_process in self._apps.items():
                    if selector in app_name and app_name not in found_apps:
                        found_app = app_name if not found_app or len(app_name) < len(found_app) else found_app

                if found_app:
                    found_apps.append(found_app)
                elif selectors:
                    self.print_out('Unable to find an app matching "{}".'.format(selector))

        return tuple(found_apps)

    def _start_services(self):
        docker_compose_filepath = os.path.join(os.path.realpath('.'), self.settings['docker-compose-filepath'])
        self._dmservices = DMServices(logger=self.logger, docker_compose_filepath=docker_compose_filepath)
        self._dmservices.blocking_healthcheck(self._shutdown)


    def _stylize(self, text, **styles):
        style_string = ''.join(getattr(colored, key)(val) for key, val in styles.items())
        return colored.stylize(text, style_string)

    def _get_cleaned_wrapped_and_styled_text(self, text, app_name):
        def pad_name(name):
            return r'{{:>{}s}}'.format(self._app_name_width).format(name)

        if type(text) != str:
            text = repr(text)

        cleaned_lines = []
        wrapped_lines = []
        styled_lines = []
        timestamp = datetime.datetime.now().strftime('%H:%M:%S')
        padded_app_name = pad_name(app_name)
        log_styling = self.config.get('styling', {}).get('logs', {})
        colored_app_name = re.sub(app_name,
                                  self._stylize(app_name,
                                                **log_styling.get(app_name, {})),
                                  padded_app_name)

        for line in text.split('\n'):
            datetime_prefixed_log_pattern = (
                r'^(?:\) )?\d{{4}}-\d{{2}}-\d{{2}}[\sT]\d{{2}}:\d{{2}}:\d{{2}}(?:,\d{{3}})?\s{}\s'.format(app_name)
            )

            if re.match(datetime_prefixed_log_pattern, line):
                line = re.sub(datetime_prefixed_log_pattern, '', line)

                # TODO: Bit of a hack - any log that starts with a datetime from a given app is assumed to be 'breaking
                # TODO: out' of PDB, if the user is currently attached.
                if self._attached_app and self._attached_app['name'] == app_name:
                    self._attached_app['attached'] = False
                    cleaned_lines.append((pad_name(self._main_log_name), 'Detaching from {} ...'.format(app_name)))

            cleaned_lines.append((colored_app_name, line))

        for log_name, line in cleaned_lines:
            # We sort colorize keys by length to ensure partial matches do not override longer matches (eg 'api'
            # being highlighted rather than 'search-api').
            for key in sorted(log_styling.keys(), key=lambda x: len(x), reverse=True):
                line = re.sub(r'([\s-]){}\s'.format(key), '\\1{} '.format(
                        self._stylize(key, **log_styling.get(key, {}))), line)

            line = re.sub(r'(WARN(?:ING)?|ERROR)', self._stylize(r'\1', fg='yellow'), line)
            line = re.sub(r' "((?:API (?:request )?)?GET|PUT|POST|DELETE)',
                          ' "{}'.format(self._stylize(r'\1', fg='white')), line)

            styled_lines.append((log_name, line))

        for log_name, line in styled_lines:
            terminal_width = shutil.get_terminal_size().columns - (len(timestamp) + self._app_name_width + 4)

            try:
                lines = ansiwrap.ansi_terminate_lines(
                        ansiwrap.wrap(line,
                                      width=terminal_width,
                                      subsequent_indent=' ' * self.config.get('logging', {}).get('wrap-line-indent', 0),
                                      drop_whitespace=False)
                )

            except ValueError:  # HACK: Problem decoding some ansi codes from Docker, so just wrap them ignorantly.
                lines = textwrap.wrap(line,
                                      width=terminal_width,
                                      subsequent_indent=' ' * self.config.get('logging', {}).get('wrap-line-indent', 0),
                                      drop_whitespace=False)

            log_prefix = '{} {}'.format(timestamp, log_name)
            lines = ['{} | {}'.format(log_prefix, line) for line in lines]

            wrapped_lines.extend(lines)

        return wrapped_lines

    def print_out(self, text, app_name=None, end=os.linesep):
        if not app_name:
            app_name = self._main_log_name

        if self._awaiting_input or self._attached_app:
            # We've printed a prompt - let's overwrite it.
            sys.stdout.write('{}{}'.format(TERMINAL_CARRIAGE_RETURN, TERMINAL_ESCAPE_CLEAR_LINE))

        lines = self._get_cleaned_wrapped_and_styled_text(text, app_name)
        for i, line in enumerate(lines, start=1):
            # This should be the ONLY direct call to print - everything else should go through this `print_out`.
            print('{}{}'.format(TERMINAL_CARRIAGE_RETURN, line), flush=True, end=end if i == len(lines) else os.linesep)

        # We cleared the prompt before displaying the log line; we should show the prompt (and any input) again.
        if not self._shutdown.is_set() and (self._attached_app or self._awaiting_input):
            sys.stdout.write('{} {}'.format(self._prompt_string, readline.get_line_buffer()))
            sys.stdout.flush()

    def run(self):
        # Make sure everything gets cleaned up properly, even in the event of a crash.
        atexit.register(self.shutdown)

        try:
            if self._use_docker_services:
                self._start_services()

            if not self._shutdown.is_set():
                down_apps = set()

                app_command = APP_COMMAND_REBUILD if self._rebuild else APP_COMMAND_RESTART

                for repositories in self._app_repositories:
                    for repository in repositories:
                        app_name = self._get_app_name(repository)
                        self._processes[app_name] = DMProcess(self._apps[app_name],
                                                              logger=self.logger,
                                                              app_command=app_command)

                    down_apps.update(self._ensure_apps_up(repositories))

                if not down_apps:
                    self.print_out('All apps up and running: {}  '.format(' '.join(self._apps.keys())))
                else:
                    self.print_out('There were some problems bringing up the full DM app suite.')

                self.cmd_apps_status()

        except KeyboardInterrupt:
            self.shutdown()

        else:
            self._get_input_and_pipe_to_target()
            self.shutdown()


    def cmd_switch_logs(self, selectors: list):
        if not selectors:
            self._filter_logs = []
            self.print_out('New logs coming in from all apps will be interleaved together.\n\n')

        else:
            self._filter_logs = self._find_matching_apps(selectors)
            self.print_out('Incoming logs will only be shown for these apps: {} '.format(' '.join(self._filter_logs)))

    def cmd_apps_status(self):
        status_table = prettytable.PrettyTable()
        status_table.field_names = ['APP', 'PPID', 'STATUS', 'LOGGING', 'DETAILS']
        status_table.align['APP'] = 'r'
        status_table.align['PPID'] = 'r'
        status_table.align['STATUS'] = 'l'
        status_table.align['LOGGING'] = 'l'
        status_table.align['DETAILS'] = 'l'

        self._suppress_log_printing = True

        for app_name, app in self._apps.items():
            status, data = self._check_app_status(app)

            ppid = str(app['process']) if app['process'] > 0 else 'N/A'
            status = status.upper()
            logging = 'visible' if not self._filter_logs or app_name in self._filter_logs else 'hidden'
            notes = data.get('message', data) if status != 'OK' else ''

            styling = self.config['styling']
            status_style = styling['status'][status] if status in styling['status'].keys() else {}
            logging_style = styling['filter'][logging] if logging in styling['filter'].keys() else {}

            status = self._stylize(status, **status_style)
            logging = self._stylize(logging, **logging_style)

            status_table.add_row([app_name, ppid, status, logging, notes])

        # A dirty hack to 'ensure' (in most realistic cases) that all logs from the requests have been processed.
        time.sleep(0.25)

        self._suppress_log_printing = False

        self.print_out(status_table.get_string())

    def cmd_apps_branches(self):
        branches_table = prettytable.PrettyTable()
        branches_table.field_names = ['APP', 'BRANCH', 'LAST COMMIT']
        branches_table.align['APP'] = 'r'
        branches_table.align['BRANCH'] = 'l'
        branches_table.align['LAST COMMIT'] = 'r'

        for app_name, app in self._apps.items():
            try:
                branch_name = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                                      cwd=app['repo_path'], universal_newlines=True).strip()
            except:
                branch_name = "unknown"

            try:
                last_commit = subprocess.check_output(['git', 'log', '-1', '--format=%cd', '--date=local'],
                                                      cwd=app['repo_path'], universal_newlines=True).strip()
                last_commit_datetime = datetime.datetime.strptime(last_commit, '%c')
                last_commit_days_old = max(0, (datetime.datetime.utcnow() - last_commit_datetime).days)
                age = ('{} days ago'.format(last_commit_days_old)
                       if last_commit_days_old != 1 else
                       '{}  day ago'.format(last_commit_days_old))
            except:
                age = "unknown"

            branches_table.add_row([app_name, branch_name, age])

        self.print_out(branches_table.get_string())

    def cmd_restart_down_apps(self, selectors: list, rebuild: bool = False) -> None:
        matched_apps = self._find_matching_apps(selectors)
        recovered_apps = set()
        failed_apps = set()

        for repos in self._app_repositories:
            for repo in repos:
                need_restart = False
                app_name = self._get_app_name(repo)
                app = self._apps[app_name]

                if app_name not in matched_apps:
                    continue

                try:
                    p = psutil.Process(app['process'])
                    assert p.cwd() == app['repo_path']

                    if rebuild and selectors:
                        self.cmd_kill_apps(selectors, silent_fail=True)
                        need_restart = True

                except (ProcessLookupError, psutil.NoSuchProcess, KeyError, AssertionError, ValueError):
                    need_restart = True

                if need_restart:
                    try:
                        self.print_out('{} the {} ...'.format('Rebuilding' if rebuild else 'Restarting', app_name))
                        self._processes[app_name].run(APP_COMMAND_REBUILD if rebuild else APP_COMMAND_RESTART)
                        recovered_apps.add(app_name)

                    except Exception as e:
                        self.print_out('Could not re{} {}: {} ...'.format('build' if rebuild else 'start', app_name, e))

            failed_apps.update(self._ensure_apps_up(filter(lambda x: self._get_app_name(x) in recovered_apps, repos)))

        recovered_apps -= failed_apps

        if failed_apps:
            self.print_out('These apps could not be recovered: {} '.format(' '.join(failed_apps)))

            if not rebuild:
                self.print_out(yellow('Try rebuilding the app(s) to refresh cached assets using `rebuild`.'))

        if recovered_apps and len(recovered_apps) < len(self._apps.keys()):
            self.print_out('These apps are back up and running: {}  '.format(' '.join(recovered_apps)))

        if not failed_apps and len(recovered_apps) == len(self._apps.keys()):
            self.print_out('All apps up and running: {}  '.format(' '.join(recovered_apps)))

    def cmd_kill_apps(self, selectors: Optional[List] = None, silent_fail: bool=False) -> None:
        procs = []

        for app_name in self._find_matching_apps(selectors):
            try:
                if self._apps[app_name]['process'] in (PROCESS_TERMINATED, PROCESS_NOEXIST):
                    continue

                p = psutil.Process(self._apps[app_name]['process'])
                procs.append(p)

                children = []
                for child in p.children(recursive=True):
                    children.append(child)
                    procs.append(child)

                for child in children:
                    child.kill()

                p.kill()

                self.print_out('Taken {} down.'.format(app_name))

            except (ProcessLookupError, psutil.NoSuchProcess, KeyError, ValueError):
                if not silent_fail:
                    self.print_out('No process found for {} - already down?'.format(app_name))

        for proc in procs:
            proc.wait()

    def cmd_kill_services(self) -> None:
        if not self._dmservices:
            return

        healthcheck_result, service_results = self._dmservices.services_healthcheck(self._shutdown, check_once=True)

        if self._use_docker_services and healthcheck_result is True:
            self.print_out('Stopping background services...')
            self._dmservices.wait(interrupt=True)
            self.print_out('Background services have stopped.')

    def cmd_frontend_build(self, selectors: Optional[List] = None) -> None:
        for app_name in self._find_matching_apps(selectors):
            if app_name.endswith('-frontend'):
                app_build_name = app_name.replace('frontend', 'fe-build')
                app_build = self._apps[app_name].copy()
                app_build['name'] = app_build_name

                colorize = self.config['styling']['logs']
                if app_name in colorize.keys() and app_build_name not in colorize.keys():
                    colorize[app_build_name] = colorize[app_name]

                # Ephemeral process to run the frontend-build. Not tracked.
                DMProcess(app_build, logger=self.logger, app_command=APP_COMMAND_FRONTEND)

            self.print_out('Starting frontend-build on {} '.format(app_name))

    def shutdown(self):
        # Ignore further sigints so that everything wraps up properly.
        # There's a small chance this makes it a real PITA to kill the app though...
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        shutdown_set = self._shutdown.is_set()
        self._shutdown.set()

        if not shutdown_set:
            self.print_out('Wrapping up ...')

        self.cmd_kill_apps([])
        self.cmd_kill_services()

        if not shutdown_set:
            self.print_out('Goodbye!')

    def process_input(self, user_input):
        """Takes input from user and performs associated actions (e.g. switching log views, restarting apps, shutting
        down)"""
        try:
            words: List[str] = user_input.split(' ')
            verb: str = words[0]

            if verb == 'h' or verb == 'help':
                print(DMRunner.HELP_SYNTAX, flush=True)
                print('')

            elif verb == 's' or verb == 'status':
                self.cmd_apps_status()

            elif verb == 'b' or verb == 'branch' or verb == 'branches':
                self.cmd_apps_branches()

            elif verb == 'r' or verb == 'restart':
                self.cmd_restart_down_apps(words[1:])

            elif verb == 'rb' or verb == 'rebuild':
                self.cmd_restart_down_apps(words[1:], rebuild=True)

            elif verb == 'k' or verb == 'kill':
                self.cmd_kill_apps(words[1:])

            elif verb == 'q' or verb == 'quit':
                self.shutdown()

            elif verb == 'f' or verb == 'filter':
                self.cmd_switch_logs(words[1:])

            elif verb == 'fe' or verb == 'frontend':
                self.cmd_frontend_build(words[1:])

            else:
                self.print_out('')

        except Exception as e:
            self.print_out('Exception handling user input.')
            self.print_out(e)
