# -*- coding: utf-8 -*-


import ansicolor
from contextlib import contextmanager
import getpass
import os
import psycopg2
import re
import requests
import pexpect
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

from .utils import PROCESS_TERMINATED, PROCESS_NOEXIST, EXITCODE_NOT_ANTICIPATED_EXECUTION


class DMExecutable:
    def _get_clean_env(self):
        env = os.environ.copy()

        if 'VIRTUAL_ENV' in env:
            del env['VIRTUAL_ENV']

        env['PYTHONUNBUFFERED'] = '1'
        env['DMRUNNER_USER'] = getpass.getuser()

        return env

    def _log(self, log_entry, log_name, attach=None):
        self._logger(log_entry.strip('\r\n').strip('\n'), log_name, attach)



class DMServices(DMExecutable):
    def __init__(self, logger, docker_compose_filepath, docker_arg='up', log_name='services'):
        self._logger = logger
        self._docker_compose_filepath = docker_compose_filepath
        self._docker_arg = docker_arg
        self._log_name = log_name

        self._service_process = None
        self._thread_process = None
        self._thread_healthcheck: Optional[threading.Thread] = None
        self._process_alive = threading.Event()
        self._logs_finished = threading.Event()

        self.run()

    @staticmethod
    def _get_docker_compose_command(docker_compose_filepath, docker_arg):
        return ['docker-compose', '-f', docker_compose_filepath, docker_arg]

    @classmethod
    def build_services(cls, docker_compose_filepath):
        return subprocess.call(cls._get_docker_compose_command(docker_compose_filepath, 'build'))

    @staticmethod
    def services_healthcheck(shutdown_event, check_once=False):
        """Attempts to validate that required background services (NGINX, Elasticsearch, Postgres) are all
        operational. It takes some shortcuts in doing so, but should be effective in most cases."""
        healthcheck_result = {
            'nginx': False,
            'elasticsearch': False,
            'postgres': False
        }

        try:
            while not all(healthcheck_result.values()) and (not shutdown_event.is_set() or check_once):
                # Try to connect to port 80 - assume that a successful connection means nginx is listening on port 80.
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.connect(('localhost', 80))
                    healthcheck_result['nginx'] = True

                except ConnectionError:
                    healthcheck_result['nginx'] = False

                finally:
                    s.close()

                try:
                    # Check ES cluster health - assume that a 200 response means ES is fine.
                    cluster_endpoint = requests.get('http://localhost:9200/_cluster/health')
                    healthcheck_result['elasticsearch'] = cluster_endpoint.status_code == 200

                except (requests.exceptions.ConnectionError, AttributeError) as e:
                    healthcheck_result['elasticsearch'] = False

                # Connect to Postgres with default parameters - assume a successful connection means postgres is up.
                try:
                    psycopg2.connect(dbname='digitalmarketplace', user=getpass.getuser(), host='localhost').close()
                    healthcheck_result['postgres'] = True

                except psycopg2.OperationalError:
                    healthcheck_result['postgres'] = False

                if all(healthcheck_result.values()):
                    break

                if check_once:
                    break

                time.sleep(1)

        except KeyboardInterrupt as e:
            print(sys.exc_info())
            sys.exit(EXITCODE_NOT_ANTICIPATED_EXECUTION)

        return all(healthcheck_result.values()), healthcheck_result


    def _run_in_thread(self):
        self._service_process = subprocess.Popen(self._get_docker_compose_command(self._docker_compose_filepath,
                                                                                  self._docker_arg),
                                                 env=self._get_clean_env(),
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 universal_newlines=True, bufsize=1, start_new_session=True)

        self._process_alive.set()

        try:
            while True:
                log_entry = self._service_process.stdout.readline()
                clean_log_entry = ansicolor.strip_escapes(log_entry)

                try:
                    if clean_log_entry.index('|') >= 0:
                        service_name = clean_log_entry[:clean_log_entry.index('|')].strip()
                        log_entry = re.sub(r'^[^|]+\s+\|\s+', '', clean_log_entry)

                    else:
                        service_name = self._log_name

                except ValueError:
                    service_name = self._log_name

                self._log(log_entry, log_name=service_name)

                if self._service_process.poll() is not None:
                    log_entries = self._service_process.stdout.read().split('\n')
                    for log_entry in log_entries:
                        self._log(log_entry, log_name=service_name)
                    break

        except (pexpect.exceptions.EOF, IOError):
            pass

        except Exception as e:  # E.g. SIGINT from Ctrl+C on main thread; bail out
            self._log('{}: {}'.format(type(e), str(e)), log_name=self._log_name)

        self._logs_finished.set()

    def blocking_healthcheck(self, shutdown_event):
        self._thread_healthcheck = threading.Thread(target=self.services_healthcheck, args=(shutdown_event, ),
                                                    name='Thread-Services-HC')
        self._thread_healthcheck.start()

        self._log('Running services healthcheck...', log_name=self._log_name)

        try:
            self._thread_healthcheck.join()

        except KeyboardInterrupt:
            # We will sent an interrupt, but if this is received before docker-compose has reached a certain point,
            # the containers /may not/ be shutdown.
            self._service_process.send_signal(signal.SIGINT)
            raise

        else:
            self._log(log_entry='Services are up.', log_name=self._log_name)

    def run(self):
        self._thread_process = threading.Thread(target=self._run_in_thread, name='Thread-Services')
        self._thread_process.start()

    def wait(self, interrupt=False):
        self._process_alive.wait()

        if interrupt:
            self._service_process.send_signal(signal.SIGINT)

        returncode = self._service_process.wait()

        self._logs_finished.wait()

        return returncode


@contextmanager
def background_services(logger, docker_compose_filepath):
    docker_services = DMServices(logger=logger, docker_compose_filepath=docker_compose_filepath)
    docker_services.blocking_healthcheck(threading.Event())
    yield
    docker_services.wait(interrupt=True)


@contextmanager
def blank_context():
    yield


class DMProcess(DMExecutable):
    def __init__(self, app, logger, app_command):
        self._thread = None

        self._app = app
        self._logger = logger
        self._app_command = app_command

        self._app_instance = None

        self.run(app_command=self._app_command)

    def _get_command(self, app_command):
        return self._app['commands'][app_command] if app_command in self._app['commands'] else app_command

    def _run_in_thread(self, app_command):
        self._app_instance = pexpect.spawn(self._get_command(app_command),
                                           cwd=self._app['repo_path'],
                                           env=self._get_clean_env(),
                                           timeout=1)

        self._app['process'] = self._app_instance.pid

        try:
            while not self._app_instance.eof():
                try:
                    # pexpect's pseudo-tty adds Windows-style line endings even on unix systems, so need to remove \r\n.
                    log_entry = self._app_instance.readline().decode('utf-8').strip('\r\n')
                    self._log(log_entry, log_name=self._app['name'])

                except pexpect.exceptions.TIMEOUT:
                    if not self._app.get('attached'):
                        try:
                            self._app_instance.expect('(Pdb)', timeout=0)

                            log_entries = self._app_instance.before.decode('utf-8').split('\r\n')
                            for log_entry in log_entries:
                                self._log(log_entry, log_name=self._app['name'])

                                self._app['attached'] = True
                                self._log('Attaching to {} ...'.format(self._app['name']),
                                          log_name = self._app['name'], attach=True)

                        except pexpect.exceptions.TIMEOUT:
                            continue

        except BaseException as e:  # E.g. SIGINT from Ctrl+C on main thread; bail out
            self._log(repr(e), log_name=self._app['name'])

        if self._app['name'].endswith('-fe-build'):
            self._log('Build complete for {} '.format(self._app['name']), log_name=self._app['name'])

        self._app['process'] = PROCESS_TERMINATED

    def run(self, app_command):
        self._app['process'] = PROCESS_NOEXIST

        self._thread = threading.Thread(target=self._run_in_thread, args=(app_command, ),
                                        name='Thread-{}'.format(self._app['name']))
        self._thread.start()

    def process_input(self, user_input):
        self._app_instance.sendline(user_input)

        if user_input.lower().strip() in ['q', 'quit', 'c', 'con', 'cont', 'continue']:
            self._app['attached'] = False
            self._log('Detaching from {} ...'.format(self._app['name']), log_name='manager', attach=True)

    def wait(self):
        try:
            self._thread.join()

        except KeyboardInterrupt:
            self._app_instance.kill(signal.SIGINT)
            self._app_instance.close()
            raise

        self._app_instance.close()
        return self._app_instance.exitstatus
