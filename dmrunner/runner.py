#!/usr/bin/env python3

import atexit
import colored
import datetime
import errno
import itertools
from http.client import RemoteDisconnected
import json
import multiprocessing
import os
import prettytable
import psutil
import re
import readline
import requests
from requests.exceptions import ConnectionError
import shutil
import signal
import subprocess
import sys
import time
import threading
from urllib.parse import urljoin
import yaml

from .process import DMProcess, DMService
from .utils import PROCESS_TERMINATED, PROCESS_NOEXIST

TERMINAL_CARRIAGE_RETURN = '\r'
TERMINAL_ESCAPE_CLEAR_LINE = '\033[K'


class DMRunner:
    INPUT_STRING = 'Enter command (or H for help): '

    CURR_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    LOGGING_DIR = os.path.join(CURR_DIR, 'logs')

    HELP_SYNTAX = """
 h /     help - Display this help file.
 s /   status - Check status for your apps.
 b /   branch - Check which branches your apps are running against.
 r /  restart - Restart any apps that have gone down (using `make run-app`).
rm /   remake - Restart any apps that have gone down (using `make run-all`).
 f /   filter - Start showing logs only from specified apps*
fe / frontend - Run `make frontend-build` against specified apps*
 k /     kill - Kill specified apps*
 q /     quit - Terminate all running apps and quit back to your shell.

            * - Specify apps as a space-separator partial match on the name, e.g. 'buy search' to match the
                buyer-frontend and the search-api. If no match string is supplied, all apps will match."""

    def __init__(self, manifest_path: str, command: str, download: bool=False):
        self._manifest_path: str = os.path.realpath(manifest_path)
        self._command: str = command
        self._download: bool = download

        with open(self._manifest_path) as manifest:
            self.manifest = yaml.safe_load(manifest)

        self._checkout_directory = os.path.realpath(self.manifest['config']['checkout_directory'])

        try:
            os.makedirs(self._checkout_directory)
            os.makedirs(DMRunner.LOGGING_DIR)

        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        curr_signal = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        self._manager: multiprocessing.Manager = multiprocessing.Manager()
        self._log_queue: multiprocessing.Queue = self._manager.Queue()
        self._services: dict = self._manager.dict()
        self._apps: dict = self._manager.dict()

        signal.signal(signal.SIGINT, curr_signal)  # Probably a race condition?

        self._processes: dict = {}
        self._populate_multiprocessing_components()

        self._shutdown = False
        self._awaiting_input = False
        self._suppress_log_printing = False
        self._filter_logs = []

        self._log_processor_shutdown = threading.Event()
        self._log_thread = threading.Thread(target=self._process_logs, name='Thread-Logging')
        self._log_thread.setDaemon(True)
        self._log_thread.start()

        readline.parse_and_bind('tab: complete')
        readline.set_completer(self._app_name_completer)
        readline.set_completer_delims(' ')

    @property
    def _app_name_width(self):
        return max(len(name) for name in itertools.chain(self.manifest['services'], self.manifest['apps']))

    def _app_name_completer(self, text, state):
        options = [name for name in self.manifest['apps'].keys() if text in name]
        if state < len(options):
            return options[state]
        else:
            return None

    def _populate_multiprocessing_components(self):
        for name in itertools.chain(self.manifest['services']):
            service = self._manager.dict()
            service['name'] = name
            service['process'] = PROCESS_NOEXIST
            service['config'] = self.manifest['services'][name]

            self._services[name] = service

        for name in itertools.chain(self.manifest['apps']):
            app = self._manager.dict()
            app['name'] = name
            app['process'] = PROCESS_NOEXIST
            app['dirpath'] = os.path.join(self._checkout_directory,
                                          os.path.basename(self.manifest['apps'][name]['git']))
            app['config'] = self.manifest['apps'][name]

            self._apps[name] = app

    def _sorted_and_grouped_app_config(self):
        """Groups entries in self.manifest together in a nested list of lists, where each sublist contains all
        entries in the manifest with the same order, and where the sublists are in ascending order (ie lowest first).

        Example return: [[{'api': config}, {'search-api': config}], [{'buyer-fe': config}]]
        """
        return [{app_name: app_config for app_name, app_config in app_group} for _, app_group in
                itertools.groupby(self.manifest['apps'].items(), lambda d: d[1]['order'])]

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

            else:
                print('checking...')
                try:
                    status_endpoint = app['config']['healthcheck']

                    self.print_out('Checking status for {} at {}'.format(app['name'], status_endpoint))
                    try:
                        res = requests.get(status_endpoint)
                        data = json.loads(res.text)

                        return data['status'], data

                    except (RemoteDisconnected, ConnectionError):
                        time.sleep(0.5)
                        continue

                except json.decoder.JSONDecodeError as e:
                    status = 'unknown'
                    error_msg = 'Invalid data retrieved from /_status endpoint'
                    break

                except ValueError as e:
                    error_msg = 'Process does not exist or has crashed'
                    break

                except IndexError as e:
                    error_msg = 'Process launched but not yet bound to port'
                    time.sleep(0.5)

            checked = True

        return status, {'message': error_msg}

    def _ensure_repos_up(self, repos, quiet=False):
        down_apps = set()

        for app_name, app_config in repos.items():
            if not quiet:
                self.print_out('Checking {} ...'.format(app_name))

            self._suppress_log_printing = quiet
            result, data = self._check_app_status(self._apps[app_name], loop=True)
            self._suppress_log_printing = False

            if not data or 'status' not in data or data['status'] != 'ok':
                self.print_out('Error running {} - {}'.format(app_name, data['message']))

                down_apps.add(app_name)

        return down_apps

    def _process_logs(self):
        while not self._log_processor_shutdown.is_set():
            log_job = self._log_queue.get()
            log_entry = log_job['log']
            log_name = log_job.get('name', 'manager')

            if self._suppress_log_printing:
                continue

            if self._filter_logs and log_name and log_name not in self._filter_logs:
                continue

            self.print_out(log_entry, app_name=log_name)

            for f in ['combined.log', '{}.log'.format(log_name)]:
                with open(os.path.join(DMRunner.LOGGING_DIR, f), 'a') as outfile:
                    outfile.write('{}\n'.format(log_entry))

            self._log_queue.task_done()

    def _find_matching_apps(self, selectors):
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
                elif selectors != '':
                    self.print_out('Unable to find an app matching "{}".'.format(selector))

        return tuple(found_apps)

    def _download_repos(self):
        matching_repos = {}
        res = None
        page = 1

        retcode = subprocess.call(['ssh', '-T', 'git@github.com'])

        if retcode != 1:
            self.print_out('Unable to continue - authentication with Github failed.')
        else:
            self.print_out('Authentication to Github succeeded.')

        for app_name, app_config in self.manifest['apps'].items():
            self.print_out('Cloning {} ...'.format(app_name))
            retcode = subprocess.call(['git', 'clone', app_config['git']], cwd=self._checkout_directory)
            if retcode != 0:
                self.print_out('Problem cloning {} - errcode {}'.format(app_name, retcode))

        self.print_out('Done')

    def _stylize(self, text, **styles):
        style_string = ''.join(getattr(colored, key)(val) for key, val in styles.items())
        return colored.stylize(text, style_string)

    def print_out(self, msgs, app_name='manager'):
        if self._awaiting_input:
            # We've printed a prompt - let's overwrite it.
            sys.stdout.write('{}{}'.format(TERMINAL_CARRIAGE_RETURN, TERMINAL_ESCAPE_CLEAR_LINE))

        for msg in msgs.split('\n'):
            datetime_prefixed_log_pattern = r'^\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}\s{}\s'.format(app_name)

            if re.match(datetime_prefixed_log_pattern, msg):
                msg = re.sub(datetime_prefixed_log_pattern, '', msg)

            timestamp = datetime.datetime.now().strftime('%H:%M:%S')
            padded_app_name = r'{{:>{}s}}'.format(self._app_name_width).format(app_name)
            colored_app_name = re.sub(app_name,
                                      self._stylize(app_name,
                                                    **self.manifest['apps'].get(app_name, {}).get('styles', {})),
                                      padded_app_name)
            log_prefix = '{} {}'.format(timestamp, colored_app_name)

            terminal_width = shutil.get_terminal_size().columns - (len(timestamp) + self._app_name_width + 4)
            msgs = [msg[x:x + terminal_width] for x in range(0, len(msg), terminal_width)]

            for app_name in self.manifest['apps'].keys():
                msgs = [re.sub(r'\s{}\s'.format(app_name), ' {} '.format(
                    self._stylize(app_name,
                                  **self.manifest['apps'].get(app_name, {}).get('styles', {}))), msg) for msg in msgs]

            for msg in msgs:
                msg = re.sub(r'(WARN(?:ING)?)', self._stylize(r'\1', fg='yellow'), msg)
                msg = re.sub(r'(ERROR)', self._stylize(r'\1', fg='yellow'), msg)
                print('{} | {}'.format(log_prefix, msg), flush=True)
                log_prefix = '{} {}'.format(timestamp, ' ' * len(padded_app_name))

        if self._awaiting_input and not self._shutdown:
            # We cleared the prompt before dispalying the log line; we should show the prompt (and any input) again.
            sys.stdout.write('{}{}'.format(DMRunner.INPUT_STRING, readline.get_line_buffer()))
            sys.stdout.flush()

    def run_single_repository(self, app):
        # We are here if the script is booting up. If run_all was supplied, we should run-all for the initial run.
        self._processes[app['name']] = DMProcess(app=app, config=self.manifest['config'], log_queue=self._log_queue)

    def run_services(self, manifest_services):
        for service_name, service_config in filter(lambda x: x[1].get('type') != 'local', manifest_services.items()):
            self._processes[service_name] = DMService(service=self._services[service_name],
                                                      config=self.manifest['config'].copy(),
                                                      log_queue=self._log_queue)

    def run(self):
        atexit.register(self.cmd_kill_apps, silent_fail=True)

        try:
            if self._download:
                self._download_repos()
                return

            self.run_services(self.manifest['services'])

            down_apps = set()

            for app_group in self._sorted_and_grouped_app_config():
                for app_name, app_config in app_group.items():
                    self.run_single_repository(app=self._apps[app_name])

                down_apps.update(self._ensure_repos_up(app_group))

            if not down_apps:
                self.print_out('All apps up and running: {}  '.format(' '.join(self._apps.keys())))
            else:
                self.print_out('There were some problems bringing up the full DM app suite.')

            self.cmd_apps_status()

        except KeyboardInterrupt:
            self._shutdown = True

        self.process_input()


    def cmd_switch_logs(self, selectors):
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
            log_status = 'visible' if not self._filter_logs or app_name in self._filter_logs else 'hidden'
            notes = data.get('message', data) if status != 'OK' else ''

            status_style = {'fg': 'green'} if status == 'OK' else {'fg': 'red'} if status == 'DOWN' else {}
            log_status_style = {'fg': 'green'} if log_status == 'visible' else {'fg': 'red'}

            status = self._stylize(status, **status_style)
            log_status = self._stylize(log_status, **log_status_style)

            status_table.add_row([app_name, ppid, status, log_status, notes])

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
                                                      cwd=app['dirpath'], universal_newlines=True).strip()
            except:
                branch_name = "unknown"

            try:
                last_commit = subprocess.check_output(['git', 'log', '-1', '--format=%cd', '--date=local'],
                                                      cwd=app['dirpath'], universal_newlines=True).strip()
                last_commit_datetime = datetime.datetime.strptime(last_commit, '%c')
                last_commit_days_old = max(0, (datetime.datetime.utcnow() - last_commit_datetime).days)
                age = ('{} days ago'.format(last_commit_days_old)
                       if last_commit_days_old != 1 else
                       '{}  day ago'.format(last_commit_days_old))
            except:
                age = "unknown"

            branches_table.add_row([app_name, branch_name, age])

        self.print_out(branches_table.get_string())

    def cmd_restart_down_apps(self, selectors, remake=False):
        matched_apps = self._find_matching_apps(selectors)
        recovered_apps = set()
        failed_apps = set()

        for app_group in self._sorted_and_grouped_app_config():
            for app_name, app_config in app_group.items():
                app_name = get_app_name(repo)
                app = self._apps[app_name]

                if app_name not in matched_apps:
                    continue

                try:
                    p = psutil.Process(app['process'])
                    assert p.cwd() == app['dirpath']

                except (ProcessLookupError, psutil.NoSuchProcess, KeyError, AssertionError, ValueError):
                    self.print_out('The {} is DOWN. Restarting ...'.format(app_name))
                    try:
                        self._processes[app_name].run(remake=remake)
                        recovered_apps.add(app_name)
                    except:
                        self.print_out('Could not re-run {} ...'.format(app_name))

            failed_apps.update(self._ensure_repos_up(filter(lambda x: get_app_name(x) in recovered_apps, repos)))

        recovered_apps -= failed_apps

        if failed_apps:
            self.print_out('These apps could not be recovered: {} '.format(' '.join(failed_apps)))
            if not remake:
                self.print_out('Try `remake` to launch using `make run-all`')

        if recovered_apps and len(recovered_apps) < len(self._apps.keys()):
            self.print_out('These apps are back up and running: {}  '.format(' '.join(recovered_apps)))

        if not failed_apps and len(recovered_apps) == len(self._apps.keys()):
            self.print_out('All apps up and running: {}  '.format(' '.join(recovered_apps)))

    def cmd_kill_apps(self, selectors='', silent_fail=False):
        procs = []

        for app_name in self._find_matching_apps(selectors):
            try:
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

    def cmd_frontend_build(self, selectors=''):
        for app_name in self._find_matching_apps(selectors):
            if app_name.endswith('-frontend'):
                app_build_name = app_name.replace('frontend', 'fe-build')
                app_build = self._apps[app_name].copy()
                app_build['name'] = app_build_name

                if app_build_name not in self.manifest['styles'].keys():
                    self.manifest['styles'][app_build_name] = self.manifest['styles'].get(app_name)

                # Ephemeral process to run the frontend-build. Not tracked.
                DMProcess(app=app_build, config=self.manifest['config'], log_queue=self._log_queue)

            self.print_out('Starting frontend-build on {} '.format(app_name))

    def process_input(self):
        """Takes input from user and performs associated actions (e.g. switching log views, restarting apps, shutting
        down)"""
        while True:
            try:
                if self._shutdown:
                    self.print_out('Shutting down...')
                    self._log_processor_shutdown.set()
                    self.cmd_kill_apps()
                    return

                self._awaiting_input = True
                command = input(DMRunner.INPUT_STRING).lower().strip()
                self._awaiting_input = False

                words = command.split(' ')
                verb = words[0]

                if verb == 'h' or verb == 'help':
                    print(DMRunner.HELP_SYNTAX, flush=True)
                    print('')

                elif verb == 's' or verb == 'status':
                    self.cmd_apps_status()

                elif verb == 'b' or verb == 'branch' or verb == 'branches':
                    self.cmd_apps_branches()

                elif verb == 'r' or verb == 'restart':
                    self.cmd_restart_down_apps(words[1:])

                elif verb == 'rm' or verb == 'remake':
                    self.cmd_restart_down_apps(words[1:], remake=True)

                elif verb == 'k' or verb == 'kill':
                    self.cmd_kill_apps(words[1:])

                elif verb == 'q' or verb == 'quit':
                    self._shutdown = True

                elif verb == 'f' or verb == 'filter':
                    self.cmd_switch_logs(words[1:])

                elif verb == 'fe' or verb == 'frontend':
                    self.cmd_frontend_build(words[1:])

                else:
                    self.print_out('')

            except KeyboardInterrupt:
                self._shutdown = True

            except Exception as e:
                self.print_out('Exception handling command.')
                self.print_out(e)
