#!/usr/bin/env python3

import ast
import atexit
import colored
import configparser
import datetime
import errno
import itertools
import json
import multiprocessing
import os
import pexpect
import prettytable
import psutil
import re
import readline
from reconfigure.parsers import NginxParser
import requests
import shutil
import signal
import subprocess
import sys
import time
import threading
from urllib.parse import urljoin
import yaml

from .process import DMProcess
from .utils import get_app_name, PROCESS_TERMINATED, PROCESS_NOEXIST

TERMINAL_CARRIAGE_RETURN = '\r'
TERMINAL_ESCAPE_CLEAR_LINE = '\033[K'


class DMRunner:
    INPUT_STRING = 'Enter command (or H for help): '
    MFA_CODE_STRING = 'Enter MFA code for credentials decryption (or A to abort): '

    NGINX_CONFIG_FILE = '/usr/local/etc/nginx/nginx.conf'
    CURR_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    REPO_DIR = os.path.dirname(CURR_DIR)
    CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.dmrunner')
    LOGGING_DIR = os.path.join(CURR_DIR, 'logs')

    COMPILED_API_REPO_PATTERN = re.compile(r'^digitalmarketplace-(?:.+)?api$')
    COMPILED_FRONTEND_REPO_PATTERN = re.compile(r'^digitalmarketplace-.*-frontend$')
    CREDENTIALS_REPO_PATTERNS = re.compile(r'^digitalmarketplace-(?:aws|credentials)$')
    DM_ENV_LOGGING_PATTERN = re.compile(r'^DM_[A-Z_]+=.*$')

    DM_REPO_PATTERNS = [COMPILED_API_REPO_PATTERN, COMPILED_FRONTEND_REPO_PATTERN]
    DM_DOWNLOAD_REPO_PATTERNS = [CREDENTIALS_REPO_PATTERNS, COMPILED_API_REPO_PATTERN, COMPILED_FRONTEND_REPO_PATTERN]

    CREDENTIALS_TO_INJECT = {
        'DM_NOTIFY_API_KEY': lambda x: x['notify_api_key'],
        'SECRET_KEY': lambda x: x['shared_tokens']['password_key'],
        'SHARED_EMAIL_KEY': lambda x: x['shared_tokens']['shared_email_key'],
        'DM_MANDRILL_API_KEY': lambda x: x['shared_tokens']['mandrill_key'],
        'DM_CLARIFICATION_QUESTION_EMAIL': lambda x: x['supplier_frontend']['clarification_question_email'],
        'DM_FOLLOW_UP_EMAIL_TO': lambda x: x['supplier_frontend']['follow_up_email_to'],
        'DM_MAILCHIMP_USERNAME': lambda x: x['supplier_frontend']['mailchimp_username'],
        'DM_MAILCHIMP_API_KEY': lambda x: x['supplier_frontend']['mailchimp_api_key'],
        'DM_MAILCHIMP_OPEN_FRAMEWORK_NOTIFICATION_MAILING_LIST_ID':
            lambda x: x['supplier_frontend']['mailchimp_open_framework_notification_mailing_list_id'],
    }

    HELP_SYNTAX = """
 h /     help - Display this help file.\n
 s /   status - Check status for your apps.
 b /   branch - Check which branches your apps are running against.
 f /   filter - Start showing logs only from specified apps*\n
 c /    creds - Load keys and tokens from digitalmarketplace-credentials (will restart all apps).\n
 r /  restart - Restart any apps that have gone down (using `make run-app`).
rm /   remake - Restart any apps that have gone down (using `make run-all`).
fe / frontend - Run `make frontend-build` against specified apps*\n
 k /     kill - Kill specified apps*
 q /     quit - Terminate all running apps and quit back to your shell.

            * - Specify apps as a space-separator partial match on the name, e.g. 'buy search' to match the
                buyer-frontend and the search-api. If no match string is supplied, all apps will match."""

    DEFAULT_CONFIG_STYLES = {
        'api': {'fg': 'blue', 'attr': 'bold'},
        'search-api': {'fg': 'cyan', 'attr': 'bold'},
        'admin-frontend': {'fg': 'green', 'attr': 'bold'},
        'brief-responses-frontend': {'fg': 'yellow', 'attr': 'bold'},
        'briefs-frontend': {'fg': 'red', 'attr': 'bold'},
        'buyer-frontend': {'fg': 'magenta', 'attr': 'bold'},
        'supplier-frontend': {'fg': 'white', 'attr': 'bold'},
        'user-frontend': {'fg': 'dark_orange_3b', 'attr': 'bold'},
    }

    def __init__(self, download=False, run_all=False, rebuild=False, nix=False):
        self.download = download
        self.run_all = run_all
        self.rebuild = rebuild
        self.nix = nix

        try:
            os.makedirs(DMRunner.LOGGING_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        try:
            os.makedirs(DMRunner.CONFIG_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        self._load_config()

        curr_signal = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        self.manager = multiprocessing.Manager()
        self.log_queue = self.manager.Queue()
        self.apps = self.manager.dict()

        signal.signal(signal.SIGINT, curr_signal)  # Probably a race condition?

        self._decrypting_credentials = False
        self._credentials = {}
        self._processes = {}
        self._repositories = self._get_repository_directories()
        self._populate_multiprocessing_components()

        self._shutdown = False
        self._awaiting_input = False
        self._suppress_log_printing = False
        self._filter_logs = []

        atexit.register(self.cmd_kill_apps, silent_fail=True)

        self._environment = {}

        self.log_processor_shutdown = threading.Event()
        self.log_thread = threading.Thread(target=self._process_logs, daemon=True, name='Thread-Logging')
        self.log_thread.start()

        readline.parse_and_bind('tab: complete')
        readline.set_completer(self._app_name_completer)
        readline.set_completer_delims(' ')

    @property
    def _app_name_width(self):
        if not self._repositories:
            return 20

        return max(len(get_app_name(r)) for r in itertools.chain.from_iterable(self._repositories))

    def _load_config(self):
        config_file_path = os.path.join(DMRunner.CONFIG_DIR, 'config')

        self.config = configparser.ConfigParser(converters={'dict': lambda x: ast.literal_eval(x)})
        self.config['styles'] = DMRunner.DEFAULT_CONFIG_STYLES
        self.config.read(config_file_path)

        with open(config_file_path, 'w') as config_file:
            self.config.write(config_file)

    def _app_name_completer(text, state):
        options = [name for name in self.apps.keys() if text in name]
        if state < len(options):
            return options[state]
        else:
            return None

    def _populate_multiprocessing_components(self):
        for repo in itertools.chain.from_iterable(self._repositories):
            app_name = get_app_name(repo)

            app = self.manager.dict()
            app['name'] = app_name
            app['process'] = PROCESS_NOEXIST
            app['repo_path'] = repo
            app['run_all'] = self.run_all
            app['rebuild'] = self.rebuild
            app['nix'] = self.nix

            self.apps[app_name] = app

    def _get_repository_directories(self):
        """Automagically locates digitalmarketplace frontend and api repositories.
        :return: Two tuples (api_repository, ...), (frontend_repository, ...). Each repository tuple should come up fully
        (/_status endpoint resolves) before the next set will be launched."""
        all_repos = []

        for matcher in DMRunner.DM_REPO_PATTERNS:
            matched_repos = []

            directories = filter(lambda x: os.path.isdir(x),
                                 map(lambda x: os.path.join(os.path.realpath('..'), x),
                                     os.listdir('..')))
            for directory in directories:

                if matcher.match(os.path.basename(directory)):
                    matched_repos.append(directory)

            if matched_repos:
                all_repos.append(matched_repos)

        return tuple(tuple(repos) for repos in all_repos)

    def _get_status_endpoint_prefix(self, port):
        try:
            with open(DMRunner.NGINX_CONFIG_FILE) as infile:
                parser = NginxParser()
                nginx_config = parser.parse(infile.read())

        except FileNotFoundError:
            try:
                with open(os.path.join(DMRunner.REPO_DIR, 'digitalmarketplace-functional-tests',
                                       'nginx', 'nginx.conf')) as infile:
                    parser = NginxParser()
                    nginx_config = parser.parse(infile.read())

            except FileNotFoundError:
                return None

        location = list(filter(lambda loc: loc.children[0].value.endswith(str(port)),
                               nginx_config['http']['server'].get_all('location')))
        if location:
            return location[0].parameter

        return '/'

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
                try:
                    # Find ports bound by the above processes and check /_status endpoints.
                    parent = psutil.Process(app['process'])

                    child = parent.children()[0].children()[0] if app['nix'] else parent.children()[0]

                    valid_conns = list(filter(lambda x: x.status == 'LISTEN', child.connections()))[0]

                    base_url = 'http://{}:{}'.format(*valid_conns.laddr)
                    prefix = self._get_status_endpoint_prefix(valid_conns.laddr[1])
                    if prefix is None:
                        return 'unknown', {'message': 'Nginx configuration not detected'}

                    status_endpoint = urljoin(base_url, os.path.join(prefix, '_status'))

                    # self.print_out('Checking status for {} at {}'.format(app['name'], status_endpoint))
                    res = requests.get(status_endpoint)
                    data = json.loads(res.text)

                    return data['status'], data

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

                except (ProcessLookupError, psutil.NoSuchProcess) as e:
                    error_msg = 'Process has gone away'
                    break

            checked = True

        return status, {'message': error_msg}

    def _ensure_repos_up(self, repos, quiet=False):
        down_apps = set()

        for repo in repos:
            app_name = get_app_name(repo)
            if not quiet:
                self.print_out('Checking {} ...'.format(app_name))

            self._suppress_log_printing = quiet
            result, data = self._check_app_status(self.apps[app_name], loop=True)
            self._suppress_log_printing = False

            if not data or 'status' not in data or data['status'] != 'ok':
                self.print_out('Error running {} - {}'.format(app_name, data['message']))

                down_apps.add(app_name)

        return down_apps

    def _process_logs(self):
        while not self.log_processor_shutdown.is_set():
            log_job = self.log_queue.get()
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

            self.log_queue.task_done()

    def _find_matching_apps(self, selectors):
        if not selectors:
            found_apps = self.apps.keys()
        else:
            found_apps = []
            for selector in selectors:
                found_app = None
                for app_name, app_process in self.apps.items():
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

        self.print_out('Locating Digital Marketplace repositories...')
        while res is None or res.links.get('next', {}).get('url', None):
            page += 1
            res = requests.get('https://api.github.com/orgs/alphagov/repos?per_page=100&page={}'.format(page))
            if res.status_code != 200:
                print(res)
                print(res.text)

            repos = json.loads(res.text)
            for repo in repos:
                for pattern in DMRunner.DM_DOWNLOAD_REPO_PATTERNS:
                    if pattern.match(repo['name']):
                        app_name = get_app_name(repo['name'])
                        self.print_out('Found {} '.format(app_name))
                        matching_repos[app_name] = {'url': repo['html_url']}

        for repo_name, repo_details in matching_repos.items():
            self.print_out('Cloning {} ...'.format(repo_name))
            retcode = subprocess.call(['git', 'clone', repo_details['url']], cwd=DMRunner.REPO_DIR)
            if retcode != 0:
                self.print_out('Problem cloning {} - errcode {}'.format(repo_name, retcode))

        self.print_out('Done')

    def _stylize(self, text, **styles):
        style_string = ''.join(getattr(colored, key)(val) for key, val in styles.items())
        return colored.stylize(text, style_string)

    def _prompt(self):
        if self._decrypting_credentials:
            return DMRunner.MFA_CODE_STRING

        return DMRunner.INPUT_STRING

    def print_out(self, msgs, app_name='manager'):
        if self._awaiting_input:
            # We've printed a prompt - let's overwrite it.
            sys.stdout.write('{}{}'.format(TERMINAL_CARRIAGE_RETURN, TERMINAL_ESCAPE_CLEAR_LINE))

        for msg in msgs.split('\n'):
            if DMRunner.DM_ENV_LOGGING_PATTERN.match(msg):
                continue

            datetime_prefixed_log_pattern = r'^\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}\s{}\s'.format(app_name)

            if re.match(datetime_prefixed_log_pattern, msg):
                msg = re.sub(datetime_prefixed_log_pattern, '', msg)

            timestamp = datetime.datetime.now().strftime('%H:%M:%S')
            padded_app_name = r'{{:>{}s}}'.format(self._app_name_width).format(app_name)
            colored_app_name = re.sub(app_name,
                                      self._stylize(app_name, **self.config['styles'].getdict(app_name, fallback={})),
                                      padded_app_name)
            log_prefix = '{} {}'.format(timestamp, colored_app_name)

            terminal_width = shutil.get_terminal_size().columns - (len(timestamp) + self._app_name_width + 4)
            msgs = [msg[x:x + terminal_width] for x in range(0, len(msg), terminal_width)]

            for key in self.config['styles'].keys():
                msgs = [re.sub(r'\s{}\s'.format(key), ' {} '.format(
                    self._stylize(key, **self.config['styles'].getdict(key, fallback={}))), msg) for msg in msgs]

            for msg in msgs:
                msg = re.sub(r'(WARN(?:ING)?)', self._stylize(r'\1', fg='yellow'), msg)
                msg = re.sub(r'(ERROR)', self._stylize(r'\1', fg='yellow'), msg)
                print('{} | {}'.format(log_prefix, msg), flush=True)
                log_prefix = '{} {}'.format(timestamp, ' ' * len(padded_app_name))

        if self._awaiting_input and not self._shutdown:
            # We cleared the prompt before dispalying the log line; we should show the prompt (and any input) again.
            sys.stdout.write('{}{}'.format(self._prompt(), readline.get_line_buffer()))
            sys.stdout.flush()

    def run_single_repository(self, app):
        # We are here if the script is booting up. If run_all was supplied, we should run-all for the initial run.
        self._processes[app['name']] = DMProcess(app, self.log_queue, inject_environment=self._environment)

        if app['rebuild']:
            self.print_out('Running {}-build...'.format(self._stylize(app['name'], attr='bold')))
            # self._processes['{}-build'.format(app['name'])] = DMProcess(app, log_queue)

    def run(self):
        try:
            if self.download:
                self._download_repos()
                return

            down_apps = set()

            for repos in self._repositories:
                for repo in repos:
                    app_name = get_app_name(repo)
                    self.run_single_repository(app=self.apps[app_name])

                down_apps.update(self._ensure_repos_up(repos))

            if not down_apps:
                self.print_out('All apps up and running: {}  '.format(' '.join(self.apps.keys())))
            else:
                self.print_out('There were some problems bringing up the full DM app suite.')

            self.cmd_apps_status()

        except KeyboardInterrupt:
            self._shutdown = True

        self.process_input()


    def cmd_switch_logs(self, selectors=''):
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

        for app_name, app in self.apps.items():
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

        for app_name, app in self.apps.items():
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

    def cmd_load_credentials(self):
        self._decrypting_credentials = True
        decrypted_credentials = {}

        def sops_env():
            sops_env = os.environ.copy()
            sops_env['AWS_PROFILE'] = 'sops'
            return sops_env

        creds_dir = os.path.join(DMRunner.REPO_DIR, 'digitalmarketplace-credentials')

        def spawn_creds_process():
            return pexpect.spawn(f'{creds_dir}/sops-wrapper -d vars/preview.yaml', cwd=creds_dir, env=sops_env(),
                                 encoding='utf-8')

        creds_process = spawn_creds_process()

        while not creds_process.terminated:
            idx = creds_process.expect(['Enter MFA code:',
                                        'MultiFactorAuthentication failed with invalid MFA one time pass code.',
                                        'Member must have length less than or equal to 6',
                                        'Invalid length for parameter TokenCode',
                                        pexpect.EOF])

            if idx == 0:
                mfa_code = input(DMRunner.MFA_CODE_STRING).lower()
                if mfa_code == 'a' or mfa_code == 'abort' or mfa_code == 'q' or mfa_code == 'quit':
                    creds_process.terminate()
                    break

                creds_process.sendline(mfa_code)

            elif idx >= 1 and idx <= 3:
                self.print_out('Could not decrypt credentials using supplied MFA token. Please try again.')
                creds_process = spawn_creds_process()

            else:
                try:
                    output = creds_process.before
                    decrypted_credentials = yaml.load(output)

                except yaml.YAMLError as e:
                    self.print_out('Could not decode credentials: {}\n\n{}'.format(e, output))

                break

        # We want to grab some preview credentials to use for our local setup, e.g. to send emails with, so functional
        # tests run on development machines. Grab these here.

        self._decrypting_credentials = False

        if decrypted_credentials:
            self._credentials = decrypted_credentials

            for key, getter in DMRunner.CREDENTIALS_TO_INJECT.items():
                self._environment.update({key: getter(decrypted_credentials)})

            self.cmd_kill_apps()
            self.cmd_restart_down_apps()

    def cmd_restart_down_apps(self, selectors='', remake=False):
        matched_apps = self._find_matching_apps(selectors)
        recovered_apps = set()
        failed_apps = set()

        for repos in self._repositories:
            for repo in repos:
                app_name = get_app_name(repo)
                app = self.apps[app_name]

                if app_name not in matched_apps:
                    continue

                try:
                    p = psutil.Process(app['process'])
                    assert p.cwd() == app['repo_path']

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

        if recovered_apps and len(recovered_apps) < len(self.apps.keys()):
            self.print_out('These apps are back up and running: {}  '.format(' '.join(recovered_apps)))

        if not failed_apps and len(recovered_apps) == len(self.apps.keys()):
            self.print_out('All apps up and running: {}  '.format(' '.join(recovered_apps)))

    def cmd_kill_apps(self, selectors='', silent_fail=False):
        procs = []

        for app_name in self._find_matching_apps(selectors):
            try:
                p = psutil.Process(self.apps[app_name]['process'])
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
                app_build = self.apps[app_name].copy()
                app_build['name'] = app_build_name

                if app_build_name not in self.config['styles'].keys():
                    self.config['styles'][app_build_name] = self.config['styles'].get(app_name)

                # Ephemeral process to run the frontend-build. Not tracked.
                DMProcess(app_build, self.log_queue, inject_environment=self._environment)

            self.print_out('Starting frontend-build on {} '.format(app_name))

    def process_input(self):
        """Takes input from user and performs associated actions (e.g. switching log views, restarting apps, shutting
        down)"""
        while True:
            try:
                if self._shutdown:
                    self.print_out('Shutting down...')
                    self.log_processor_shutdown.set()
                    self.cmd_kill_apps()
                    return

                self._awaiting_input = True
                command = input(self._prompt()).lower().strip()
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

                elif verb == 'c' or verb == 'creds' or verb == 'credentials':
                    self.cmd_load_credentials()

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
