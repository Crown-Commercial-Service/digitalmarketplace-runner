# -*- coding: utf-8 -*-


import os
import signal
import subprocess
import threading

from .utils import PROCESS_TERMINATED, PROCESS_NOEXIST


class DMProcess:
    def __init__(self, app, log_queue, inject_environment={}):
        self.thread = None

        self.app = app
        self.log_queue = log_queue
        self.inject_environment = inject_environment

        self.run(init=True)

    def _get_command(self, init=False, remake=False):
        if self.app['name'].endswith('-fe-build'):
            command = ['make frontend-build']

        else:
            command = ['make run-app']

            if (init and self.app['run_all']) or remake:
                command = ['make run-all']

        if self.app['nix']:
            # TODO: Git fails to find cacerts on mac when run through nix-shell. Remove env hack when that is fixed.
            command = ['nix-shell', '--pure', '--run', 'GIT_SSL_NO_VERIFY="true" {}'.format(' '.join(command)), '.']

        return tuple([command, {} if self.app['nix'] else {'shell': True}])

    def _get_clean_env(self):
        env = os.environ.copy()

        if 'VIRTUAL_ENV' in env:
            del env['VIRTUAL_ENV']

        env['PYTHONUNBUFFERED'] = '1'

        for key, value in self.inject_environment.items():
            env[key] = value

        return env

    def log(self, log_entry):
        self.log_queue.put({'name': self.app['name'], 'log': log_entry.strip('\n')})

    def _run_in_thread(self, run_cmd, popen_args):
        app_instance = subprocess.Popen(run_cmd, cwd=self.app['repo_path'], env=self._get_clean_env(),
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
