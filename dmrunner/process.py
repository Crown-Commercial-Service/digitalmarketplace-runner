# -*- coding: utf-8 -*-


import ansicolor
import getpass
import os
import re
import signal
import subprocess
import threading

from .utils import PROCESS_TERMINATED, PROCESS_NOEXIST


class DMService:
    def __init__(self, service, config, log_queue):
        self.thread = None

        self.service = service
        self.config = config
        self.log_queue = log_queue

        self.run()

    def log(self, log_entry):
        self.log_queue.put({'name': self.service['name'], 'log': log_entry.strip('\n')})

    def _get_service_env(self):
        env = os.environ.copy()

        env['PYTHONUNBUFFERED'] = '1'
        env['DMRUNNER_USER'] = getpass.getuser()

        return env

    def _run_in_thread(self):
        if self.service['config']['type'] == 'docker-compose':
            command = ['docker-compose', '-f', os.path.realpath(self.config['compose']), 'up',
                       self.service['name']]

            service_instance = subprocess.Popen(command, env=self._get_service_env(),
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                universal_newlines=True, bufsize=1, start_new_session=True)

            self.service['process'] = service_instance.pid

            try:
                while True:
                    log_entry = service_instance.stdout.readline()
                    log_entry = re.sub(r'^[^|]+\s+\|\s+', '', ansicolor.strip_escapes(log_entry))  # TODO: dedupe

                    self.log(log_entry)

                    if service_instance.poll() is not None:
                        log_entries = service_instance.stdout.read().split('\n')
                        for log_entry in log_entries:
                            self.log(log_entry)
                        break

            except Exception as e:  # E.g. SIGINT from Ctrl+C on main thread; bail out
                self.log(e)

            self.service['process'] = PROCESS_TERMINATED

        else:
            print(f'ERROR: Unrecognised service type in manifest file:\n\n{service_config}')

    def run(self):
        self.service['process'] = PROCESS_NOEXIST

        curr_signal = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        self.thread = threading.Thread(target=self._run_in_thread, name='Thread-{}'.format(self.service['name']))
        self.thread.start()

        signal.signal(signal.SIGINT, curr_signal)  # Probably a race condition?


class DMProcess:
    def __init__(self, app, config, log_queue):
        self.thread = None

        self.app = app
        self.config = config
        self.log_queue = log_queue

        self.run(init=True)

    def _get_command(self, init=False, remake=False):
        if self.app['config']['type'] == 'docker':
            command = ['docker-compose', '-f', os.path.realpath(self.config['compose']),
                       'up', self.app['name']]

        return tuple([command, {'shell': True}])

    def _get_clean_env(self):
        env = os.environ.copy()

        if 'VIRTUAL_ENV' in env:
            del env['VIRTUAL_ENV']

        env['PYTHONUNBUFFERED'] = '1'

        repo_env_name = 'DM_{}_DEVELOPMENT_REPO'.format(self.app['name'].upper().replace('-', '_'))
        env[repo_env_name] = os.path.join(os.path.realpath(self.config['checkout_directory']),
                                          self.app['config']['dirname'])

        return env

    def log(self, log_entry):
        self.log_queue.put({'name': self.app['name'], 'log': log_entry.strip('\n')})

    def _run_in_thread(self, run_cmd, popen_args):
        dirpath = os.path.join(os.path.realpath(self.config['checkout_directory']), self.app['config']['dirname'])

        app_instance = subprocess.Popen(' '.join(run_cmd), cwd=dirpath, env=self._get_clean_env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                        bufsize=1, start_new_session=True, **popen_args)

        self.app['process'] = app_instance.pid

        try:
            while True:
                log_entry = app_instance.stdout.readline()

                self.log(log_entry)

                if app_instance.poll() is not None:
                    log_entries = app_instance.stdout.read().split('\n')
                    for log_entry in log_entries:
                        self.log(log_entry)
                    break

        except Exception as e:  # E.g. SIGINT from Ctrl+C on main thread; bail out
            self.log(e)

        if self.app['name'].endswith('-fe-build'):
            self.log('Build complete for {} '.format(self.app['name']))

        self.app['process'] = PROCESS_TERMINATED

    def run(self, init=False, remake=False):
        self.app['process'] = PROCESS_NOEXIST

        curr_signal = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        self.thread = threading.Thread(target=self._run_in_thread, args=self._get_command(init, remake),
                                       name='Thread-{}'.format(self.app['name']))
        self.thread.start()

        signal.signal(signal.SIGINT, curr_signal)  # Probably a race condition?
